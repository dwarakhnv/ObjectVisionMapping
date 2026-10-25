"""Combine objects seen by several calibrated cameras into one world picture.

Each camera's tracker gives per-camera ids. Objects of the same class group
whose ground positions are close (within a radius that grows with object
size and distance from the camera) are taken to be the same object. Every
fused object gets a global id that stays the same while any of its
per-camera tracks continues.
"""
import itertools
import math
import threading
import time

import numpy as np


class Fusion:
    def __init__(self, registry, base_radius=1.5, forget_s=5.0):
        self.registry = registry
        self.base_radius = base_radius
        self.forget_s = forget_s
        self._ids = itertools.count(1)
        self._global = {}     # (feed_id, track_id) -> (global id, last seen)
        self._lock = threading.Lock()

    def _group(self, key):
        c = self.registry.classes.get(key)
        return c.group if c else key

    def fuse(self, per_feed):
        """per_feed: {feed_id: [object dict with "world"]} -> list of fused objects."""
        now = time.monotonic()
        items = []
        for feed_id, objs in per_feed.items():
            for o in objs:
                if o.get("world"):
                    items.append((feed_id, o))
        # Most confident first, so they seed clusters.
        items.sort(key=lambda it: -it[1].get("score", 0))
        clusters = []
        for feed_id, o in items:
            w = np.asarray(o["world"][:2], float)
            radius = self.base_radius + 0.5 * max(o["dimensions"]["length"], o["dimensions"]["width"])
            radius += 0.03 * (o.get("distance_m") or 0)  # ground positions get less certain with distance
            best, best_d = None, None
            for c in clusters:
                if c["group"] != self._group(o["class"]) or feed_id in c["feeds"]:
                    continue  # one camera never sees the same object twice
                d = float(np.linalg.norm(c["xy"] - w))
                if d <= max(radius, c["radius"]) and (best is None or d < best_d):
                    best, best_d = c, d
            if best is None:
                clusters.append({"group": self._group(o["class"]), "members": [(feed_id, o)], "feeds": {feed_id},
                                 "xy": w, "radius": radius})
            else:
                best["members"].append((feed_id, o))
                best["feeds"].add(feed_id)
                ws = [1.0 / max(m.get("distance_m") or 1.0, 1.0) for _, m in best["members"]]
                best["xy"] = np.average([m["world"][:2] for _, m in best["members"]], axis=0, weights=ws)
                best["radius"] = max(best["radius"], radius)

        out = []
        with self._lock:
            used = set()
            for c in clusters:
                keys = [(f, m["id"]) for f, m in c["members"]]
                known = sorted(self._global[k][0] for k in keys if k in self._global and self._global[k][0] not in used)
                gid = known[0] if known else next(self._ids)
                used.add(gid)
                for k in keys:
                    self._global[k] = (gid, now)
                lead = min(c["members"], key=lambda fm: fm[1].get("distance_m") or math.inf)[1]  # nearest camera
                z = float(np.mean([m["world"][2] for _, m in c["members"]]))
                speeds = [m["speed_mps"] for _, m in c["members"] if m.get("speed_mps") is not None]
                out.append({
                    "id": gid, "class": lead["class"], "label": lead["label"], "subtype": lead.get("subtype"),
                    "world": [round(float(c["xy"][0]), 3), round(float(c["xy"][1]), 3), round(z, 3)],
                    "position": lead.get("position"), "heading": lead.get("heading"),
                    "speed_mps": round(float(np.mean(speeds)), 2) if speeds else None,
                    "dimensions": lead["dimensions"], "direction": lead.get("direction"),
                    "score": max(m.get("score", 0) for _, m in c["members"]),
                    "seen_by": [{"feed": f, "id": m["id"], "distance_m": m.get("distance_m")} for f, m in c["members"]],
                })
            self._global = {k: v for k, v in self._global.items() if now - v[1] < self.forget_s}
        out.sort(key=lambda o: o["id"])
        return out
