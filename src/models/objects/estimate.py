"""Per-object estimates from a tracked box: size, distance, direction, world position.

Two situations:

* **Calibrated camera** (pose known): the bottom-centre of the box is where the
  object touches the ground. Its ray meets the ground plane, which gives the
  world position and distance; the top of the box then gives the object's
  real height, which separates e.g. children from adults.
* **Uncalibrated**: distance comes from the class's average height,
  ``distance = focal_px * real_height / box_height_px``. Size is the average.

Direction in the image compares how fast the box grows (moving towards or
away from the camera, ``d ln(h) / dt``) with how fast it moves sideways
(``dx / focal_px / dt``). Both are in radians-per-second-like units, so they
compare directly. With a calibrated camera the world velocity gives a compass
heading and a speed.
"""
import math

import numpy as np

from .orientation import AxisSmoother, StreetGrid, axis_distance, footprint_axis, heading_distance

WINDOW_S = 1.0         # how much history to fit motion over
MIN_SPAN_S = 0.25      # need at least this much history to call a direction
STILL_RATE = 0.04      # below this (1/s) the object counts as stationary
HEADING_WINDOW_S = 2.0 # world positions used to decide whether something really moves
# Real movement, not detection jitter: fast enough, and along a fairly straight path.
MIN_MOVING_SPEED = {"person": 0.5}   # m/s; everything else uses DEFAULT_MOVING_SPEED
DEFAULT_MOVING_SPEED = 1.0
MIN_STRAIGHTNESS = 0.7               # net distance / path length over the window
SNAP_DEG = 35.0                      # motion within this of the shape's axis is snapped onto it
CLEAR_SHAPE_MAX_M = 25.0             # outlines nearer than this (and clear) teach the street grid


def _slope(t, y):
    t = np.asarray(t, float)
    y = np.asarray(y, float)
    if len(t) < 2 or np.ptp(t) < 1e-6:
        return 0.0
    tc = t - t.mean()
    return float((tc * (y - y.mean())).sum() / (tc ** 2).sum())


class ObjectEstimator:
    def __init__(self, registry):
        self.registry = registry
        self.grid = StreetGrid()  # learned per camera: along the road / across it

    def estimate(self, track, now, frame_w, frame_h, camera=None, ground_z=0.0, hfov=60.0, world_to_position=None,
                 ground_fn=None):
        """A JSON-ready description of a tracked object.

        camera: geometry.CameraModel at this frame's resolution when the camera is
        calibrated (or at least placed); None otherwise. hfov is used without one.
        world_to_position converts world metres to environment coordinates.
        ground_fn: optional pixels (N x 2) -> ground metres (N x 2) in the camera's world
        frame, e.g. the reference-point homography; used for vehicle orientation.
        """
        cls = self.registry.classes.get(track.class_key)
        dims = dict(cls.dimensions) if cls else {"length": 1.0, "width": 1.0, "height": 1.0}
        x1, y1, x2, y2 = track.det.box
        w_px, h_px = max(x2 - x1, 1.0), max(y2 - y1, 1.0)
        focal = camera.K[0, 0] if camera is not None else (frame_w / 2.0) / math.tan(math.radians(hfov) / 2.0)
        ex = track.extra
        hist = ex.setdefault("hist", [])
        hist.append((now, (x1 + x2) / 2.0, y2, math.log(h_px)))
        while hist and now - hist[0][0] > WINDOW_S:
            hist.pop(0)

        obj = {
            "id": track.id, "class": track.class_key, "label": cls.label if cls else track.class_key,
            "score": round(float(track.det.score), 3), "detector_label": track.det.label,
            "box": [round(v, 1) for v in (x1, y1, x2, y2)], "polygon": track.det.polygon,
            "age_s": round(now - ex.setdefault("born", now), 1),
        }

        # --- placement and size
        touches_edge = y2 >= frame_h - 2 or y1 <= 1  # partly out of frame: height in pixels is unreliable
        world = None
        measured_h = None
        if camera is not None:
            gp = camera.ground_point((x1 + x2) / 2.0, y2, ground_z)
            if gp is not None:
                horiz = gp[:2] - camera.C[:2]
                dist = float(np.linalg.norm(horiz))
                if dist > 1e-6:
                    # The box bottom is the near edge; the object's centre is about half its depth further away.
                    depth = min(dims["width"], dims["length"]) / 2.0
                    gp = gp.copy()
                    gp[:2] += horiz / dist * depth
                world = gp
                if not touches_edge:
                    top = camera.ray((x1 + x2) / 2.0, y1)
                    flat = math.hypot(top[0], top[1])
                    if flat > 1e-6:
                        z_top = camera.C[2] + top[2] * dist / flat
                        measured_h = z_top - ground_z
        sub = cls.subtype_for(measured_h) if (cls and measured_h is not None and measured_h > 0) else None
        if sub is not None:
            obj["subtype"] = sub.key
            obj["label"] = sub.label
            if sub.dimensions:
                dims = dict(sub.dimensions)
        if measured_h is not None and measured_h > 0:
            # Smooth the measured height over the track's life.
            hs = ex.setdefault("heights", [])
            hs.append(measured_h)
            del hs[:-30]
            dims["height"] = float(np.median(hs))
            obj["size_source"] = "measured"
        else:
            obj["size_source"] = "class average"
        if world is not None:
            distance = float(np.linalg.norm(world - camera.C))
        else:
            distance = focal * dims["height"] / h_px if not touches_edge else None
        obj["distance_m"] = None if distance is None else round(distance, 1)
        if distance is not None and obj["size_source"] == "class average" and camera is None:
            # Apparent width at that distance (side view ~ length, end view ~ width).
            obj["apparent_width_m"] = round(w_px * distance / focal, 2)
        obj["dimensions"] = {k: round(v, 2) for k, v in dims.items()}

        # --- direction in the image
        span = hist[-1][0] - hist[0][0] if len(hist) > 1 else 0.0
        if span >= MIN_SPAN_S:
            ts = [h[0] for h in hist]
            lateral = _slope(ts, [h[1] for h in hist]) / focal   # rad/s sideways
            radial = _slope(ts, [h[3] for h in hist])              # 1/s growth (+ = closer)
            if max(abs(lateral), abs(radial)) < STILL_RATE:
                direction = "stationary"
            elif abs(lateral) > abs(radial):
                direction = "crossing_right" if lateral > 0 else "crossing_left"
            else:
                direction = "approaching" if radial > 0 else "receding"
            ex["direction"] = direction
            obj["motion"] = {"lateral": round(lateral, 3), "radial": round(radial, 3)}
        obj["direction"] = ex.get("direction", "unknown")

        # --- world position, speed and heading
        obj["world"] = None
        if world is not None:
            whist = ex.setdefault("world", [])
            whist.append((now, float(world[0]), float(world[1])))
            while whist and now - whist[0][0] > HEADING_WINDOW_S:
                whist.pop(0)
            obj["world"] = [round(float(v), 3) for v in world]
            if world_to_position is not None:
                obj["position"] = world_to_position(world)
            if ground_fn is None:
                ground_fn = lambda px: _ground_points(camera, px, ground_z)  # noqa: E731
            heading, source, moving = self._heading(track, obj, now, whist, dims, ground_fn, camera, world)
            obj["heading"] = None if heading is None else round(heading, 1)
            obj["heading_source"] = source
        return obj

    def _heading(self, track, obj, now, whist, dims, ground_fn, camera, world):
        """Heading (compass degrees) from real motion and/or the vehicle's shape. Also sets speed and direction.

        Returns (heading or None, "motion" | "shape" | None, moving?).
        """
        ex = track.extra
        # 1. Shape: the axis of a vehicle's footprint (people are round: no axis).
        if track.class_key != "person":
            found = footprint_axis(track.det.polygon, track.det.box, ground_fn, dims["length"], dims["width"])
            if found is not None:
                axis, clear = found
                reliable = clear and (obj.get("distance_m") or 1e9) <= CLEAR_SHAPE_MAX_M
                if reliable:
                    self.grid.add(axis, 0.2)  # many frames per car: small weight each
                    ex["shape_reliable"] = ex.get("shape_reliable", 0) + 1
                ex.setdefault("axis", AxisSmoother()).add(self.grid.snap(axis))
        smoother = ex.get("axis")
        axis = smoother.value if smoother else None

        # 2. Motion: only when it is clearly more than detection jitter.
        moving, motion_heading = False, None
        if len(whist) > 2 and whist[-1][0] - whist[0][0] >= MIN_SPAN_S:
            ts = [h[0] for h in whist]
            vx, vy = _slope(ts, [h[1] for h in whist]), _slope(ts, [h[2] for h in whist])
            speed = math.hypot(vx, vy)
            path = sum(math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(whist, whist[1:]))
            net = math.hypot(whist[-1][1] - whist[0][1], whist[-1][2] - whist[0][2])
            straight = net / path if path > 1e-6 else 0.0
            moving = speed >= MIN_MOVING_SPEED.get(track.class_key, DEFAULT_MOVING_SPEED) and straight >= MIN_STRAIGHTNESS
            obj["speed_mps"] = round(speed, 2) if moving else 0.0
            if moving:
                motion_heading = math.degrees(math.atan2(vx, vy)) % 360.0
                if track.class_key != "person":
                    self.grid.add(motion_heading % 180.0, 0.5)  # vehicles drive along roads
                los = world[:2] - camera.C[:2]
                los = los / max(np.linalg.norm(los), 1e-6)
                right = camera.R[0][:2] / max(np.linalg.norm(camera.R[0][:2]), 1e-6)
                along, across = vx * los[0] + vy * los[1], vx * right[0] + vy * right[1]
                if abs(along) >= abs(across):
                    obj["direction"] = "receding" if along > 0 else "approaching"
                else:
                    obj["direction"] = "crossing_right" if across > 0 else "crossing_left"
            else:
                obj["direction"] = "stationary"
            ex["direction"] = obj["direction"]

        # 3. Combine.
        previous = ex.get("heading")
        if motion_heading is not None:
            if axis is not None and axis_distance(motion_heading % 180.0, axis) < SNAP_DEG:
                # Moving along its own length (as vehicles do): the shape gives the exact line, motion the end.
                motion_heading = min((axis, axis + 180.0), key=lambda h: heading_distance(h, motion_heading))
            heading = motion_heading if previous is None else _blend(previous, motion_heading, 0.3)
            ex["heading"], ex["heading_source"] = heading, "motion"
        elif axis is not None:
            # Parked / stopped: the shape gives the line; keep whichever end we had (unknown at first).
            ends = (axis, (axis + 180.0) % 360.0)
            heading = min(ends, key=lambda h: heading_distance(h, previous)) if previous is not None else axis
            ex["heading"] = heading
            ex["heading_source"] = ex.get("heading_source") if ex.get("heading_source") == "motion" else "shape"
        # Confident: from real motion, or from a clear, near outline seen in at least a few frames.
        obj["heading_confident"] = ex.get("heading_source") == "motion" or ex.get("shape_reliable", 0) >= 3
        return ex.get("heading"), ex.get("heading_source"), moving


def _blend(a, b, weight):
    """Move heading a toward heading b by `weight`, the short way round."""
    d = (b - a + 180.0) % 360.0 - 180.0
    return (a + weight * d) % 360.0


def _ground_points(camera, pixels, ground_z):
    """Pixels -> ground metres with a CameraModel (rows that miss the ground become NaN)."""
    out = []
    for u, v in pixels:
        p = camera.ground_point(u, v, ground_z)
        out.append((p[0], p[1]) if p is not None else (np.nan, np.nan))
    return np.array(out, float)
