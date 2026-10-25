"""Score the full pipeline (detect -> track -> estimate -> fuse) on the demo videos against ground truth.

    python manage.py eval_demo [--imgsz 640] [--stride 1]

Reports, per camera and fused: how often each true object was found, the
class / child-vs-adult accuracy, world position error, direction accuracy
and how often track ids switched.
"""
import collections
import json
import math
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings
from django.core.management.base import BaseCommand

from environments.environment import Environment
from mapping.fusion import Fusion
from models.objects.estimate import ObjectEstimator
from models.tracking.sort.tracker import Tracker
from vision import feeds

MATCH_M = 3.0


def _truth_direction(truth, key, i, cam_c, fps):
    """True image-direction label from the ground-truth path (same rule as the estimator)."""
    a, b = truth["frames"][max(0, i - int(fps // 2))]["objects"].get(key), truth["frames"][i]["objects"].get(key)
    if a is None or b is None:
        return None
    v = np.subtract(b, a)
    if np.linalg.norm(v) < 0.2:
        return "stationary"
    los = np.subtract(b, cam_c[:2])
    los = los / np.linalg.norm(los)
    radial = float(v @ los)
    lateral = float(abs(v[0] * los[1] - v[1] * los[0]))
    return "radial" if abs(radial) > lateral else "lateral"


class Command(BaseCommand):
    help = "Evaluate detection, tracking, placement and fusion on the demo_plaza videos."

    def add_arguments(self, parser):
        parser.add_argument("--imgsz", type=int, default=None)
        parser.add_argument("--stride", type=int, default=1, help="process every Nth frame")

    def handle(self, *args, imgsz, stride, **kw):
        src = Path(settings.SOURCES_DIR)
        with open(src / "demo_plaza_truth.json", encoding="utf-8") as fh:
            truth = json.load(fh)
        fps = truth["fps"]
        env = Environment.load(settings.ENVIRONMENTS_DIR, "demo_plaza")
        reg = feeds.registry()
        det = feeds.detector()
        if imgsz:
            det.imgsz = imgsz
        self.stdout.write("inference size %d" % det.imgsz)
        classes = {o["key"]: o for o in truth["objects"]}
        per_frame = collections.defaultdict(dict)  # frame index -> feed -> objects
        summary = {}
        for cam in env.cameras:
            cap = cv2.VideoCapture(str(src / cam.source.uri))
            tracker, est = Tracker(groups=reg.groups()), ObjectEstimator(reg)
            stats = collections.Counter()
            errors, ids = [], collections.defaultdict(set)
            i = -1
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                i += 1
                if i % stride:
                    continue
                h, w = frame.shape[:2]
                model = cam.model(env.coordinates, w, h)
                ds = det.detect(frame, labels=reg.detector_labels(), min_score=0.3)
                for d in ds:
                    c = reg.for_label(d.label)
                    d.class_key = c.key if c else d.label
                objs = [est.estimate(t, i / fps, w, h, model, env.coordinates.ground_z(), cam.hfov, env.coordinates.from_world)
                        for t in tracker.update(ds)]
                per_frame[i][cam.id] = objs
                true_now = truth["frames"][i]["objects"]
                for key, xy in true_now.items():
                    pix, front = model.project(np.array([[xy[0], xy[1], 0.0]]))
                    u, v = pix[0]
                    if not (front[0] and 0 <= u < w and 0 <= v < h):
                        continue  # not in this camera's view
                    stats["visible"] += 1
                    cands = [o for o in objs if o["world"]]
                    best = min(cands, key=lambda o: math.dist(o["world"][:2], xy), default=None)
                    if best is None or math.dist(best["world"][:2], xy) > MATCH_M:
                        stats["missed"] += 1
                        stats["missed_" + key] += 1
                        continue
                    stats["found"] += 1
                    errors.append(math.dist(best["world"][:2], xy))
                    ids[key].add(best["id"])
                    want = classes[key]["class"]
                    stats["class_ok"] += best["class"] == want
                    if want == "person" and best.get("subtype"):
                        stats["sub_n"] += 1
                        stats["sub_ok"] += best["subtype"] == ("child" if classes[key]["height_m"] < 1.4 else "adult")
                    td = _truth_direction(truth, key, i, model.C, fps)
                    if td and best["direction"] != "unknown":
                        stats["dir_n"] += 1
                        got = "stationary" if best["direction"] == "stationary" else ("radial" if best["direction"] in ("approaching", "receding") else "lateral")
                        stats["dir_ok"] += got == td
            cap.release()
            switches = sum(max(0, len(v) - 1) for v in ids.values())
            summary[cam.id] = {
                "found %": round(100 * stats["found"] / max(stats["visible"], 1), 1),
                "class %": round(100 * stats["class_ok"] / max(stats["found"], 1), 1),
                "child/adult %": round(100 * stats["sub_ok"] / max(stats["sub_n"], 1), 1),
                "direction %": round(100 * stats["dir_ok"] / max(stats["dir_n"], 1), 1),
                "median error m": round(float(np.median(errors)), 2) if errors else None,
                "p90 error m": round(float(np.percentile(errors, 90)), 2) if errors else None,
                "id switches": switches,
                "missed by object": {k[7:]: v for k, v in stats.items() if k.startswith("missed_")},
            }
        # Fusion: does each true object appear once?
        fusion = Fusion(reg)
        fstats = collections.Counter()
        for i in sorted(per_frame):
            fused = fusion.fuse({"demo_plaza." + c: o for c, o in per_frame[i].items()})
            true_now = truth["frames"][i]["objects"]
            fstats["frames"] += 1
            fstats["true"] += len(true_now)
            fstats["fused"] += len(fused)
            pairs = sorted((math.dist(o["world"][:2], xy), key, o["id"]) for key, xy in true_now.items() for o in fused)
            used_t, used_o = set(), set()
            for dist, key, oid in pairs:
                if dist <= MATCH_M and key not in used_t and oid not in used_o:
                    used_t.add(key)
                    used_o.add(oid)
            fstats["found"] += len(used_t)
            # A fused object near a true object that already has its match is a duplicate.
            fstats["dupes"] += sum(1 for o in fused if o["id"] not in used_o
                                   and any(math.dist(o["world"][:2], xy) <= MATCH_M for xy in true_now.values()))
        summary["fused"] = {"found %": round(100 * fstats["found"] / max(fstats["true"], 1), 1),
                            "duplicates per frame": round(fstats["dupes"] / max(fstats["frames"], 1), 3),
                            "objects per frame (true / fused)": "%.2f / %.2f" % (fstats["true"] / fstats["frames"], fstats["fused"] / fstats["frames"])}
        self.stdout.write(json.dumps(summary, indent=2))
