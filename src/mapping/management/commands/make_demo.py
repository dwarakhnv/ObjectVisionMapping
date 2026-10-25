"""Build the "demo_plaza" environment and two synthetic camera videos with known ground truth.

    python manage.py make_demo [--seconds 20] [--fps 15]

A flat plaza with painted reference markers is rendered from two fixed
cameras with the same pinhole model the app uses. Real photographic cutouts
(people and a bus, segmented from the sample image that ships with
Ultralytics) move along known paths, scaled by perspective, so the detector
sees real objects. Outputs:

  data/sources/demo_plaza_north.mp4, demo_plaza_east.mp4   the camera videos
  data/sources/demo_plaza_truth.json                       where everything really was
  data/environments/demo_plaza/environment.json            environment, cameras calibrated
"""
import json
import math
import shutil
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings
from django.core.management.base import BaseCommand

from environments.environment import Calibration, Camera, Environment
from environments.geometry import CameraModel

PX_PER_M = 20
TEX_X = (-40.0, 50.0)
TEX_Y = (-10.0, 110.0)
W, H = 1280, 720

REFERENCE_POINTS = [
    ("P1", "Marker NW near", (-8.0, 10.0)), ("P2", "Marker NE near", (8.0, 10.0)),
    ("P3", "Marker W mid", (-10.0, 22.0)), ("P4", "Marker E mid", (10.0, 22.0)),
    ("P5", "Marker centre", (0.0, 18.0)), ("P6", "Marker NW far", (-6.0, 34.0)),
    ("P7", "Marker NE far", (8.0, 36.0)), ("P8", "Marker E far", (16.0, 30.0)),
]

CAMERAS = [
    # id, name, ground (x, y), height m, aim at (x, y), hfov
    ("north", "Plaza north-facing", (0.0, -2.0), 6.0, (0.0, 22.0), 70.0),
    ("east", "Plaza east side", (26.0, 44.0), 7.5, (2.0, 20.0), 62.0),
]


def _paths(seconds):
    """Ground-truth tracks: (key, class, height m, cutout, [(t, x, y)])."""
    return [
        ("adult_1", "person", 1.78, "person_a", [(0.0, -14.0, 14.0), (16.0, 12.0, 14.0)]),
        ("adult_2", "person", 1.70, "person_b", [(2.0, 4.0, 42.0), (20.0, 1.5, 9.0)]),
        ("child_1", "person", 1.20, "person_b", [(4.0, 12.0, 26.0), (18.0, -8.0, 24.0)]),
        ("bus_1", "bus", 3.2, "bus", [(0.0, -4.5, 85.0), (11.0, -4.5, 30.0), (seconds, -4.5, 30.0)]),
    ]


def _at(path, t):
    if t < path[0][0] or t > path[-1][0]:
        return None
    for (t0, x0, y0), (t1, x1, y1) in zip(path, path[1:]):
        if t0 <= t <= t1:
            a = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return x0 + a * (x1 - x0), y0 + a * (y1 - y0)
    return None


def _texture():
    w = int((TEX_X[1] - TEX_X[0]) * PX_PER_M)
    h = int((TEX_Y[1] - TEX_Y[0]) * PX_PER_M)
    rng = np.random.RandomState(7)
    tex = (rng.normal(92, 7, (h, w, 1)).clip(60, 125)).astype(np.uint8).repeat(3, axis=2)
    tex = cv2.GaussianBlur(tex, (3, 3), 0)

    def px(x, y):
        return int((x - TEX_X[0]) * PX_PER_M), int((TEX_Y[1] - y) * PX_PER_M)

    # Paving grid every 5 m, a road with lane lines, a crossing.
    for gx in np.arange(-40, 51, 5):
        cv2.line(tex, px(gx, TEX_Y[0]), px(gx, TEX_Y[1]), (112, 112, 112), 2)
    for gy in np.arange(-10, 111, 5):
        cv2.line(tex, px(TEX_X[0], gy), px(TEX_X[1], gy), (112, 112, 112), 2)
    cv2.rectangle(tex, px(-8.5, 110), px(-0.5, 28), (60, 60, 62), -1)
    for y in np.arange(30, 110, 6):
        cv2.line(tex, px(-4.5, y), px(-4.5, y + 3), (235, 235, 235), 4)
    cv2.line(tex, px(-8.5, 110), px(-8.5, 28), (210, 210, 210), 3)
    cv2.line(tex, px(-0.5, 110), px(-0.5, 28), (210, 210, 210), 3)
    for x in np.arange(-8, -0.5, 1.2):
        cv2.rectangle(tex, px(x, 28.0), px(x + 0.6, 26.0), (230, 230, 230), -1)
    # Reference markers: white disc, black ring, label.
    for pid, _, (x, y) in REFERENCE_POINTS:
        c = px(x, y)
        cv2.circle(tex, c, int(0.45 * PX_PER_M), (245, 245, 245), -1)
        cv2.circle(tex, c, int(0.45 * PX_PER_M), (20, 20, 20), 2)
        cv2.circle(tex, c, 3, (0, 0, 220), -1)
        cv2.putText(tex, pid, (c[0] + 12, c[1] - 12), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (250, 250, 250), 3, cv2.LINE_AA)
    return tex


def _background(cam, tex):
    """Ground rendered from `cam` by inverse mapping every pixel to the texture."""
    us, vs = np.meshgrid(np.arange(W) + 0.5, np.arange(H) + 0.5)
    d_cam = np.stack([(us - cam.K[0, 2]) / cam.K[0, 0], (vs - cam.K[1, 2]) / cam.K[1, 1], np.ones_like(us)], -1)
    d = d_cam @ cam.R  # camera -> world: R^T applied to row vectors
    t = np.where(d[..., 2] < -1e-6, -cam.C[2] / np.minimum(d[..., 2], -1e-6), np.inf)
    gx = cam.C[0] + t * d[..., 0]
    gy = cam.C[1] + t * d[..., 1]
    map_x = ((gx - TEX_X[0]) * PX_PER_M).astype(np.float32)
    map_y = ((TEX_Y[1] - gy) * PX_PER_M).astype(np.float32)
    ground = np.isfinite(t) & (t < 400)
    map_x[~ground] = -1
    map_y[~ground] = -1
    img = cv2.remap(tex, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(95, 140, 110))
    # Sky gradient above the horizon, light haze with distance.
    sky = np.linspace(235, 190, H)[:, None, None] * np.array([1.0, 0.93, 0.82])[None, None, :]
    img = np.where(ground[..., None], img, sky[::-1].astype(np.uint8).repeat(W, axis=1))
    haze = np.clip((np.where(ground, t, 400) - 40) / 260, 0, 0.6)[..., None]
    img = (img * (1 - haze) + np.array([200, 200, 205]) * haze).astype(np.uint8)
    return img


def _cutouts():
    """Person and bus cutouts (BGRA) from Ultralytics' bundled sample image."""
    import ultralytics

    from vision import feeds

    img = cv2.imread(str(Path(ultralytics.__file__).parent / "assets" / "bus.jpg"))
    dets = feeds.detector().detect(img, labels=["person", "bus"], min_score=0.6)
    out = {}
    people = sorted([d for d in dets if d.label == "person" and d.box[0] > 5 and d.box[2] < img.shape[1] - 5],
                    key=lambda d: d.box[0])
    buses = [d for d in dets if d.label == "bus"]
    picks = {"person_a": people[0], "person_b": people[1], "bus": buses[0]}
    for key, d in picks.items():
        mask = np.zeros(img.shape[:2], np.uint8)
        cv2.fillPoly(mask, [np.asarray(d.polygon, np.int32)], 255)
        x1, y1, x2, y2 = (int(round(v)) for v in d.box)
        crop = img[y1:y2, x1:x2]
        if key == "bus":
            # People stood in front of the bus in the photo. Fill the bus outline and paint over them,
            # so the cutout is just a bus (no extra people that aren't in the ground truth).
            occluders = np.zeros(img.shape[:2], np.uint8)
            for p in dets:
                if p.label == "person":
                    cv2.fillPoly(occluders, [np.asarray(p.polygon, np.int32)], 255)
            hull = cv2.convexHull(np.asarray(d.polygon, np.int32))
            mask = np.zeros(img.shape[:2], np.uint8)
            cv2.fillPoly(mask, [hull], 255)
            occluders = cv2.dilate(occluders, np.ones((9, 9), np.uint8)) & mask
            crop = cv2.inpaint(np.ascontiguousarray(crop), occluders[y1:y2, x1:x2], 7, cv2.INPAINT_TELEA)
        alpha = cv2.GaussianBlur(mask[y1:y2, x1:x2], (3, 3), 0)
        out[key] = np.dstack([crop, alpha])
    return out


def _paste(frame, cut, cx, bottom, height_px):
    h0, w0 = cut.shape[:2]
    scale = height_px / h0
    w, h = max(2, int(round(w0 * scale))), max(2, int(round(height_px)))
    if h > 4 * H:
        return
    small = cv2.resize(cut, (w, h), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    x0, y0 = int(round(cx - w / 2)), int(round(bottom - h))
    fx0, fy0, fx1, fy1 = max(0, x0), max(0, y0), min(W, x0 + w), min(H, y0 + h)
    if fx1 <= fx0 or fy1 <= fy0:
        return
    patch = small[fy0 - y0:fy1 - y0, fx0 - x0:fx1 - x0]
    a = patch[..., 3:4].astype(np.float32) / 255.0
    region = frame[fy0:fy1, fx0:fx1].astype(np.float32)
    frame[fy0:fy1, fx0:fx1] = (patch[..., :3] * a + region * (1 - a)).astype(np.uint8)


def _aim(ground, height, target):
    dx, dy = target[0] - ground[0], target[1] - ground[1]
    heading = math.degrees(math.atan2(dx, dy)) % 360
    tilt = math.degrees(math.atan2(height, math.hypot(dx, dy)))
    return heading, tilt


class Command(BaseCommand):
    help = "Create the demo_plaza environment and synthetic camera videos with ground truth."

    def add_arguments(self, parser):
        parser.add_argument("--seconds", type=float, default=20.0)
        parser.add_argument("--fps", type=float, default=15.0)

    def handle(self, *args, seconds, fps, **kw):
        sources = Path(settings.SOURCES_DIR)
        sources.mkdir(parents=True, exist_ok=True)
        tex = _texture()
        cuts = _cutouts()
        paths = _paths(seconds)
        truth = {"fps": fps, "seconds": seconds, "objects": [{"key": k, "class": c, "height_m": h} for k, c, h, _, _ in paths],
                 "frames": []}
        cams = []
        for cid, name, ground, height, target, hfov in CAMERAS:
            heading, tilt = _aim(ground, height, target)
            cams.append((cid, name, ground, height, heading, tilt, hfov,
                         CameraModel([ground[0], ground[1], height], heading, tilt, 0.0, hfov, W, H)))
        n = int(seconds * fps)
        for cid, name, ground, height, heading, tilt, hfov, cam in cams:
            bg = _background(cam, tex)
            path = sources / ("demo_plaza_%s.mp4" % cid)
            out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (W, H))
            for i in range(n):
                t = i / fps
                frame = bg.copy()
                placed = []
                for key, cls, h, cut, pts in paths:
                    p = _at(pts, t)
                    if p is None:
                        continue
                    foot = np.array([p[0], p[1], 0.0])
                    pix, front = cam.project(np.array([foot, foot + [0, 0, h]]))
                    if not front.all():
                        continue
                    depth = float((foot - cam.C) @ cam.R[2])
                    placed.append((depth, cuts[cut], pix[0][0], pix[0][1], pix[0][1] - pix[1][1]))
                for _, cut, u, v, hp in sorted(placed, key=lambda x: -x[0]):
                    _paste(frame, cut, u, v, hp)
                out.write(frame)
            out.release()
            self.stdout.write("wrote %s" % path)
        for i in range(n):
            t = i / fps
            truth["frames"].append({"t": round(t, 4), "objects": {k: [round(v, 3) for v in _at(pts, t)]
                                                                   for k, _, _, _, pts in paths if _at(pts, t)}})
        with open(sources / "demo_plaza_truth.json", "w", encoding="utf-8", newline="\n") as fh:
            json.dump(truth, fh)

        # The environment: reference points, and cameras calibrated from where the markers appear.
        rng = np.random.RandomState(3)
        env = Environment.from_dict({
            "name": "Demo plaza", "slug": "demo_plaza", "extent": 60,
            "description": "Synthetic plaza with painted markers P1-P8 and two cameras (see make_demo).",
            "coordinates": {"kind": "local", "units": "m", "meters_per_unit": 1.0, "ground": 0},
            "reference_points": [{"id": pid, "name": nm, "position": {"x": x, "y": y, "z": 0}} for pid, nm, (x, y) in REFERENCE_POINTS],
            "cameras": [],
        })
        for cid, name, ground, height, heading, tilt, hfov, cam in cams:
            # Start from a deliberately rough guess, as someone would enter it on site ...
            d = {"id": cid, "name": name, "position": {"x": ground[0] + 1.5, "y": ground[1] - 1.0, "z": 0},
                 "height": height + 1.0, "heading": heading + 6, "tilt": tilt - 4, "roll": 0, "hfov": hfov,
                 "resolution": [W, H], "source": {"kind": "file", "uri": "demo_plaza_%s.mp4" % cid}}
            env.cameras.append(Camera.from_dict(d, env.coordinates))
            # ... then calibrate from the markers' pixels (with half-pixel click noise).
            pts = np.array([[x, y, 0.0] for _, _, (x, y) in REFERENCE_POINTS])
            pix, front = cam.project(pts)
            corr = []
            for (pid, _, _), (u, v), f in zip(REFERENCE_POINTS, pix, front):
                if f and 5 < u < W - 5 and 5 < v < H - 5:
                    u, v = u + rng.uniform(-0.5, 0.5), v + rng.uniform(-0.5, 0.5)
                    corr.append({"point_id": pid, "u": u / W, "v": v / H})
            env.camera(cid).calibration = Calibration.from_dict({"resolution": [W, H], "points": corr})
            report = env.solve_camera(cid)
            c = env.camera(cid)
            self.stdout.write("%s: %d points, %.2f px; heading %.2f (true %.2f) tilt %.2f (true %.2f) height %.2f (true %.2f)" % (
                cid, len(corr), report["rmse_px"], c.heading, heading, c.tilt, tilt, c.height, height))
        env.save(settings.ENVIRONMENTS_DIR)
        # A still image to try on the Live page.
        import ultralytics
        shutil.copy(Path(ultralytics.__file__).parent / "assets" / "bus.jpg", sources / "sample_bus.jpg")
        self.stdout.write(self.style.SUCCESS("demo_plaza ready"))
