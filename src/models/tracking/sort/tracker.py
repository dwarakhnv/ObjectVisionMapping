"""Multi-object tracker (SORT with a ByteTrack-style second pass).

Each track keeps a constant-velocity Kalman filter over the box centre, area
and aspect ratio. Every frame, confident detections are matched to predicted
tracks by IoU (Hungarian algorithm), then low-confidence detections get a
second chance against the tracks still unmatched. A track keeps its id while
it's matched in neighbouring frames, and survives a few missed frames.
Any detection left over starts a new track, shown at once: a faint or fast
object is never hidden just because it hasn't been matched yet.

untracked() is the cheap alternative when tracking is off: every detection
becomes a one-frame track (no history, so no speed or motion heading).
"""
import itertools

import numpy as np
from scipy.optimize import linear_sum_assignment


def iou_matrix(a, b):
    """IoU between boxes a (N,4) and b (M,4) in x1,y1,x2,y2."""
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)))
    a = np.asarray(a, float)[:, None, :]
    b = np.asarray(b, float)[None, :, :]
    ix = np.clip(np.minimum(a[..., 2], b[..., 2]) - np.maximum(a[..., 0], b[..., 0]), 0, None)
    iy = np.clip(np.minimum(a[..., 3], b[..., 3]) - np.maximum(a[..., 1], b[..., 1]), 0, None)
    inter = ix * iy
    area_a = (a[..., 2] - a[..., 0]) * (a[..., 3] - a[..., 1])
    area_b = (b[..., 2] - b[..., 0]) * (b[..., 3] - b[..., 1])
    return inter / np.maximum(area_a + area_b - inter, 1e-9)


def _to_z(box):
    x1, y1, x2, y2 = box
    w, h = max(x2 - x1, 1e-3), max(y2 - y1, 1e-3)
    return np.array([x1 + w / 2, y1 + h / 2, w * h, w / h])


def _to_box(x):
    s, r = max(x[2], 1e-3), max(x[3], 1e-3)
    w = np.sqrt(s * r)
    h = s / w
    return np.array([x[0] - w / 2, x[1] - h / 2, x[0] + w / 2, x[1] + h / 2])


class KalmanBox:
    """State: cx, cy, area, aspect, vcx, vcy, varea (aspect assumed constant)."""

    def __init__(self, box):
        self.x = np.zeros(7)
        self.x[:4] = _to_z(box)
        self.F = np.eye(7)
        for i in range(3):
            self.F[i, i + 4] = 1.0
        self.H = np.zeros((4, 7))
        self.H[:4, :4] = np.eye(4)
        self.P = np.diag([10, 10, 10, 10, 1e4, 1e4, 1e4]).astype(float)
        self.Q = np.diag([1, 1, 1, 0.01, 0.01, 0.01, 1e-4]).astype(float)
        self.R = np.diag([1, 1, 10, 10]).astype(float)

    def predict(self):
        if self.x[2] + self.x[6] <= 0:
            self.x[6] = 0.0
        self.x = self.F @ self.x
        self.P = self.F @ self.P @ self.F.T + self.Q
        return _to_box(self.x)

    def update(self, box):
        z = _to_z(box)
        y = z - self.H @ self.x
        S = self.H @ self.P @ self.H.T + self.R
        K = self.P @ self.H.T @ np.linalg.inv(S)
        self.x = self.x + K @ y
        self.P = (np.eye(7) - K @ self.H) @ self.P

    def box(self):
        return _to_box(self.x)


class Track:
    def __init__(self, track_id, det, frame_index):
        self.id = track_id
        self.kf = KalmanBox(det.box)
        self.det = det
        self.hits = 1
        self.age = 0
        self.missed = 0
        self.first_frame = frame_index
        self.last_frame = frame_index
        self.class_votes = {det.class_key: det.score}
        self.history = [(frame_index, tuple(det.box))]  # recent observed boxes
        self.extra = {}  # per-track state other stages keep (world history, estimates ...)

    @property
    def class_key(self):
        return max(self.class_votes, key=self.class_votes.get)

    def predict(self):
        self.age += 1
        return self.kf.predict()

    def update(self, det, frame_index):
        self.kf.update(det.box)
        self.det = det
        self.hits += 1
        self.missed = 0
        self.last_frame = frame_index
        self.class_votes[det.class_key] = self.class_votes.get(det.class_key, 0.0) + det.score
        self.history.append((frame_index, tuple(det.box)))
        if len(self.history) > 90:
            self.history = self.history[-90:]

    def box(self):
        return self.kf.box()


class Tracker:
    def __init__(self, iou_threshold=0.25, high_score=0.5, max_missed=15, min_hits=1, groups=None):
        """groups: {class_key: group}; a track may only take detections of its own group
        (e.g. car/bus/truck share "vehicle", since detectors flip between them)."""
        self.groups = groups or {}
        self.iou_threshold = iou_threshold
        self.high_score = high_score
        self.max_missed = max_missed
        self.min_hits = min_hits
        self.tracks = []
        self.frame_index = 0
        self._ids = itertools.count(1)

    def _match(self, tracks, dets, predicted):
        if not tracks or not dets:
            return [], list(range(len(tracks))), list(range(len(dets)))
        iou = iou_matrix(predicted, [d.box for d in dets])
        # Only match like with like (vehicles may swap car/truck labels, so allow those).
        for i, t in enumerate(tracks):
            for j, d in enumerate(dets):
                if not self._compatible(t.class_key, d.class_key):
                    iou[i, j] = 0.0
        rows, cols = linear_sum_assignment(-iou)
        matches, used_t, used_d = [], set(), set()
        for r, c in zip(rows, cols):
            if iou[r, c] >= self.iou_threshold:
                matches.append((r, c))
                used_t.add(r)
                used_d.add(c)
        return (matches, [i for i in range(len(tracks)) if i not in used_t],
                [j for j in range(len(dets)) if j not in used_d])

    def update(self, detections):
        """Advance one frame. Returns the confirmed tracks seen this frame."""
        self.frame_index += 1
        predicted = [t.predict() for t in self.tracks]
        high = [d for d in detections if d.score >= self.high_score]
        low = [d for d in detections if d.score < self.high_score]

        matches, un_t, un_d = self._match(self.tracks, high, predicted)
        for ti, di in matches:
            self.tracks[ti].update(high[di], self.frame_index)
        # Second pass: leftover tracks vs low-confidence detections.
        rest = [self.tracks[i] for i in un_t]
        m2, un_t2, un_low = self._match(rest, low, [predicted[i] for i in un_t])
        for ti, di in m2:
            rest[ti].update(low[di], self.frame_index)
        for i in un_t2:
            rest[i].missed += 1
        for d in [high[i] for i in un_d] + [low[i] for i in un_low]:
            self.tracks.append(Track(next(self._ids), d, self.frame_index))
        self.tracks = [t for t in self.tracks if t.missed <= self.max_missed]
        return [t for t in self.tracks if t.missed == 0 and (t.hits >= self.min_hits or self.frame_index <= self.min_hits)]

    def _compatible(self, a, b):
        return a == b or (a in self.groups and self.groups.get(a) == self.groups.get(b))

    def reset(self):
        self.tracks = []
        self.frame_index = 0


def untracked(detections):
    """Each detection as its own one-frame track, ids 1..N in this frame only (tracking off)."""
    return [Track(i, d, 1) for i, d in enumerate(detections, 1)]
