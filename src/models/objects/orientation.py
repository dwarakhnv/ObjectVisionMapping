"""Which way a vehicle points, from its shape on the ground.

A parked car never moves, so motion can't tell its heading. Its segmentation
outline can: the outline's bottom edge is where the car meets the road. Mapped
onto the ground, that edge runs

  * along the car's length when we see its side (the usual case for parked cars),
  * across its width when we see only its front or back,
  * as an "L" (side + front) when we see it at an angle; the long leg is the side.

The result is an *axis* (0-180 degrees): it says "east-west", not "east" or "west".
Motion, when there is real motion, picks the end that is the front.

Far away (each pixel covers a lot of ground) and at an angle, outlines are less
reliable. StreetGrid learns the scene's two main directions (along the road and
across it: driveways, parking bays) from the reliable cases and snaps the rest.
"""
import math

import cv2
import numpy as np

RASTER_WIDTH = 64  # outline is redrawn at most this wide to find its bottom edge


def bottom_edge(polygon, box):
    """Lowest outline pixel in each column of the box: where the object touches the ground (image pixels)."""
    if not polygon or len(polygon) < 6:
        return None
    x1, y1, x2, y2 = box
    bw, bh = max(x2 - x1, 1.0), max(y2 - y1, 1.0)
    scale = min(1.0, RASTER_WIDTH / bw)
    w, h = max(int(math.ceil(bw * scale)), 2), max(int(math.ceil(bh * scale)), 2)
    mask = np.zeros((h, w), np.uint8)
    pts = (np.asarray(polygon, float) - [x1, y1]) * scale
    cv2.fillPoly(mask, [np.round(pts).astype(np.int32)], 1)
    cols = np.flatnonzero(mask.any(axis=0))
    if len(cols) < 6:
        return None
    trim = max(1, len(cols) // 20)  # the outline's ends are ragged
    cols = cols[trim:-trim]
    rows = h - 1 - np.argmax(mask[::-1, cols], axis=0)  # lowest filled row per column
    return np.column_stack([x1 + (cols + 0.5) / scale, y1 + (rows + 1.0) / scale])


def footprint_axis(polygon, box, ground_fn, length_m, width_m):
    """(axis, clear) or None: compass axis (0-180 deg) of a vehicle's length.

    clear is True for a straight side or end edge (reliable), False for an "L" (less so).

    ground_fn maps image pixels (N x 2) to ground metres east / north (N x 2).
    length_m / width_m are the class's average size, used to tell a side view from an end view.
    """
    edge = bottom_edge(polygon, box)
    if edge is None:
        return None
    ground = np.asarray(ground_fn(edge), float)
    ground = ground[np.isfinite(ground).all(axis=1)]
    if len(ground) < 6:
        return None
    (_, _), (rw, rh), angle = cv2.minAreaRect(ground.astype(np.float32))
    long_side, short_side = max(rw, rh), min(rw, rh)
    if long_side < 0.8:
        return None  # too small to say anything
    a = math.radians(angle)
    ux, uy = (math.cos(a), math.sin(a)) if rw >= rh else (-math.sin(a), math.cos(a))
    edge_axis = math.degrees(math.atan2(ux, uy)) % 180.0  # compass bearing of the long side

    if short_side / long_side > 0.3:
        return edge_axis, False                 # an "L": side + one end; the long leg is the side
    if long_side >= 0.75 * length_m:
        return edge_axis, True                  # a straight edge as long as a car: its side
    if long_side <= 1.15 * width_m:
        return (edge_axis + 90.0) % 180, True   # a straight edge as short as a car is wide: its front or back
    return None                                 # in between: can't tell


def axis_distance(a, b):
    """Smallest difference between two axes (degrees, 0-90)."""
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def heading_distance(a, b):
    """Smallest difference between two headings (degrees, 0-180)."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


class AxisSmoother:
    """Running average of an axis (angles that wrap at 180): average the doubled angle."""

    def __init__(self, weight=0.2):
        self.weight = weight
        self.vec = None

    def add(self, axis):
        v = np.array([math.cos(math.radians(2 * axis)), math.sin(math.radians(2 * axis))])
        self.vec = v if self.vec is None else (1 - self.weight) * self.vec + self.weight * v
        return self.value

    @property
    def value(self):
        if self.vec is None or np.linalg.norm(self.vec) < 1e-6:
            return None
        return (math.degrees(math.atan2(self.vec[1], self.vec[0])) / 2.0) % 180.0


class StreetGrid:
    """The scene's two main directions (a road and the perpendicular), learned from reliable observations.

    Directions 90 degrees apart are the same grid, so angles are averaged at 4x (wrap at 90).
    """

    def __init__(self, min_weight=8.0, snap_deg=20.0):
        self.vec = np.zeros(2)
        self.total = 0.0
        self.min_weight = min_weight
        self.snap_deg = snap_deg

    def add(self, axis, weight=1.0):
        a = math.radians(4 * axis)
        self.vec += weight * np.array([math.cos(a), math.sin(a)])
        self.total += weight

    @property
    def value(self):
        """Grid direction (0-90 deg), once enough evidence agrees; else None."""
        if self.total < self.min_weight or np.linalg.norm(self.vec) < 0.5 * self.total:
            return None
        return (math.degrees(math.atan2(self.vec[1], self.vec[0])) / 4.0) % 90.0

    def snap(self, axis):
        """The grid line nearest to axis (0-180), if within snap_deg; else axis unchanged."""
        g = self.value
        if g is None:
            return axis
        best = min((g, g + 90.0), key=lambda line: axis_distance(axis, line))
        return best % 180.0 if axis_distance(axis, best) <= self.snap_deg else axis
