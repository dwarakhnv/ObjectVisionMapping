"""Saves video around activity: from 15 s before movement until 15 s after it stops.

While detection runs for an environment camera (an app is connected or the
Detect page is open), a Recorder keeps the last PRE_SECONDS of video in memory.

What counts as movement (ActivityCheck, independent of the environment's tracking):
an object of the chosen classes that is seen steadily (not a flicker) and travels
at least MIN_MOVE_M on the ground within MOVE_WINDOW_S. A false detection stuck
on one spot (a bush seen as a person) never moves, so it never records, and a
parked car never records. Once something has moved that far it counts again
the moment it walks / drives on after a stop, and a stop shorter than
POST_SECONDS doesn't end the clip, so stop-and-go is one recording.

Movement starts a clip with the buffered video, recording continues while it
goes on, and stops POST_SECONDS after the last movement. Long activity is saved
as consecutive SEGMENT_SECONDS clips. Video is encoded in its own thread, so
detection never waits for it and clips never freeze.

Files, per environment, in data/recordings/<env>/:
    <start>_<camera>.webm   the video (VP8 WebM: plays in any browser)
    <start>_<camera>.jpg    a thumbnail from the moment movement started
    <start>_<camera>.json   when, which camera, what was seen, and a timeline of detections
"""
import collections
import itertools
import json
import math
import queue
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings

FPS = 10
PRE_SECONDS = 15
POST_SECONDS = 15
SEGMENT_SECONDS = 60
JPEG_QUALITY = 80
TIMELINE_HZ = 2          # detection snapshots stored per second, for drawing boxes during playback
MAX_BACKLOG_S = 60       # video waiting to be encoded; beyond this, frames are skipped (the last one repeats)
NAME_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?_[a-z0-9_]+$")

# Movement that counts
MIN_MOVE_M = 2.0         # travelled on the ground ...
MOVE_WINDOW_S = 10.0     # ... within this long (so slow, stop-and-go movement still adds up)
RECENT_S = 2.0           # after that it counts while it keeps moving: this far ...
RECENT_MOVE_M = 0.5      # ... in this long (slower than a slow walk is standing)
LINK_GAP_S = 2.0         # a sighting may be missing this long (flicker, brief occlusion) and still continue
MIN_HITS = 3             # steady: seen in at least this many results ...
MIN_HIT_RATIO = 0.5      # ... and in at least half the results since first seen
MAX_SPEED = {"person": 3.0}  # m/s, to connect sightings between results; others DEFAULT_MAX_SPEED
DEFAULT_MAX_SPEED = 20.0
LINK_SLACK_M = 1.5       # plus this much for position jitter


def recordings_dir(env=None):
    root = Path(settings.DATA_DIR) / "recordings"
    return root / env if env else root


def ground_xy(obj, map_key):
    """An object's position on the ground in metres, or None."""
    if obj.get("world"):
        return float(obj["world"][0]), float(obj["world"][1])
    where = obj.get(map_key)
    if not where:
        return None
    if map_key == "geo":  # metres east / north; fine for distances within one place
        return where["lon"] * 111320.0 * math.cos(math.radians(where["lat"])), where["lat"] * 110540.0
    return float(where["x"]), float(where["y"])


class ActivityCheck:
    """Finds objects that are really moving, without the environment's tracker.

    Each result's sightings are connected to the nearest sighting of the same kind
    from the previous results, within how far it could have gone. A connected run
    is steady once seen in MIN_HITS results and in MIN_HIT_RATIO of them; it becomes
    a mover once it travels MIN_MOVE_M within MOVE_WINDOW_S, and from then on counts
    whenever it moves RECENT_MOVE_M within RECENT_S. Positions are averaged over 3
    results to ignore jitter.
    """

    def __init__(self, groups=None):
        self.groups = groups or {}
        self.runs = []
        self.results = 0
        self._ids = itertools.count(1)

    def update(self, now, objects, classes, map_key):
        """Feed one result. Returns {class: set of run ids} moving now (empty: nothing is)."""
        self.results += 1
        sightings = []
        for o in objects:
            if classes and o["class"] not in classes:
                continue
            p = ground_xy(o, map_key)
            if p is not None:
                sightings.append((o["class"], p))
        self.runs = [r for r in self.runs if now - r["last"] <= LINK_GAP_S]
        group = lambda c: self.groups.get(c, c)  # noqa: E731  car / truck flips stay one run
        pairs = []
        for si, (cls, p) in enumerate(sightings):
            for ri, r in enumerate(self.runs):
                if group(cls) != group(r["class"]):
                    continue
                d = math.hypot(p[0] - r["points"][-1][1], p[1] - r["points"][-1][2])
                if d <= LINK_SLACK_M + MAX_SPEED.get(r["class"], DEFAULT_MAX_SPEED) * min(now - r["last"], 0.5):
                    pairs.append((d, si, ri))
        used_s, used_r = set(), set()
        for d, si, ri in sorted(pairs):  # closest first
            if si in used_s or ri in used_r:
                continue
            used_s.add(si)
            used_r.add(ri)
            r = self.runs[ri]
            r["points"].append((now,) + sightings[si][1])
            r["hits"] += 1
            r["last"] = now
        for si, (cls, p) in enumerate(sightings):
            if si not in used_s:
                self.runs.append({"id": next(self._ids), "class": cls, "points": [(now,) + p], "hits": 1,
                                  "first": self.results, "last": now})
        moving = {}
        for r in self.runs:
            r["points"] = [q for q in r["points"] if now - q[0] <= MOVE_WINDOW_S]
            steady = r["hits"] >= MIN_HITS and r["hits"] / (self.results - r["first"] + 1) >= MIN_HIT_RATIO
            if not steady or r["last"] != now:
                continue
            r["mover"] = r.get("mover") or _travelled(r["points"]) >= MIN_MOVE_M
            if r["mover"] and _travelled([q for q in r["points"] if now - q[0] <= RECENT_S]) >= RECENT_MOVE_M:
                moving.setdefault(r["class"], set()).add(r["id"])
        return moving


def _travelled(points):
    """How far the latest position is from the farthest earlier one (each averaged over 3 results)."""
    if len(points) < 3:
        return 0.0
    smooth = [(sum(q[1] for q in points[i - 2:i + 1]) / 3, sum(q[2] for q in points[i - 2:i + 1]) / 3)
              for i in range(2, len(points))]
    x, y = smooth[-1]
    return max(math.hypot(x - a, y - b) for a, b in smooth)


class Recorder:
    """Records one camera. Feed it detections with observe(); close() when detection stops."""

    def __init__(self, env, camera_id, camera_key, groups=None):
        from simulation import camera as shared_cameras

        self.env, self.camera_id = env, camera_id
        self._cams = shared_cameras
        self.cam = shared_cameras.acquire(camera_key)   # the reader detection already uses: no second connection
        self.activity = ActivityCheck(groups)
        self.buffer = collections.deque()                 # (time, jpeg) for the pre-roll
        self.clip = None                                  # the clip being recorded, or None
        self.last_activity = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._jobs = queue.Queue()                        # for the encoder: ("frame", clip, t, jpeg) / ("finish", clip)
        self._encoder = threading.Thread(target=self._encode, daemon=True, name="rec-enc-%s-%s" % (env, camera_id))
        self._encoder.start()
        self._thread = threading.Thread(target=self._run, daemon=True, name="rec-%s-%s" % (env, camera_id))
        self._thread.start()

    # ---------------------------------------------------------------- called by the detection worker
    def observe(self, objects, classes, map_key):
        """Detections for the newest frame. Starts / extends a clip when something moves."""
        now = time.time()
        moving = self.activity.update(now, objects, classes, map_key)
        with self._lock:
            if moving:
                self.last_activity = now
                if self.clip is None:
                    self._start_clip(now, pre_roll=True)
            if self.clip is not None:
                c = self.clip
                if now - c["last_timeline"] >= 1.0 / TIMELINE_HZ:
                    c["last_timeline"] = now
                    c["timeline"].append({"t": round(now - c["start"], 2), "objects": [_brief(o, map_key) for o in objects]})
                for cls, ids in moving.items():
                    c["seen"].setdefault(cls, set()).update(ids)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=5)
        with self._lock:
            self._finish_clip()
        self._jobs.put(None)
        self._encoder.join(timeout=MAX_BACKLOG_S)  # let the clip in progress be finished
        self._cams.release(self.cam)

    # ---------------------------------------------------------------- frames, at a steady FPS
    def _run(self):
        last_id, next_t = 0, time.time()
        frame = None
        while not self._stop.is_set():
            fid, f = self.cam.latest(last_id, timeout=1.0 / FPS)
            if f is not None:
                last_id, frame = fid, f
            now = time.time()
            if now < next_t:
                self._stop.wait(next_t - now)
                continue
            next_t += 1.0 / FPS
            if next_t < now - 1:  # fell far behind (e.g. machine busy): don't try to catch up
                next_t = now + 1.0 / FPS
            if frame is None:
                continue
            with self._lock:
                # Far behind encoding: skip this frame (the encoder repeats the previous one).
                if self.clip is None or self._jobs.qsize() < MAX_BACKLOG_S * FPS:
                    ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                    if ok and self.clip is not None:
                        self._jobs.put(("frame", self.clip, now, jpg.tobytes()))
                    elif ok:
                        self.buffer.append((now, jpg.tobytes()))
                        while self.buffer and now - self.buffer[0][0] > PRE_SECONDS:
                            self.buffer.popleft()
                if self.clip is not None:
                    if now - self.last_activity > POST_SECONDS:
                        self._finish_clip()
                    elif now - self.clip["start"] >= SEGMENT_SECONDS:
                        self._finish_clip()
                        self._start_clip(now, pre_roll=False)  # activity goes on: the next minute

    # ---------------------------------------------------------------- clips (called with the lock held)
    def _start_clip(self, now, pre_roll):
        folder = recordings_dir(self.env)
        folder.mkdir(parents=True, exist_ok=True)
        frames = list(self.buffer) if pre_roll else []
        start = frames[0][0] if frames else now
        stem = datetime.fromtimestamp(start).strftime("%Y%m%d-%H%M%S") + "_" + self.camera_id
        n = 1
        while (folder / (stem + ".webm")).exists():
            n += 1
            stem = datetime.fromtimestamp(start).strftime("%Y%m%d-%H%M%S") + "-%d_%s" % (n, self.camera_id)
        (folder / (stem + ".webm")).touch()  # claim the name now: the encoder may get to it later
        self.clip = {"stem": stem, "start": start, "writer": None, "written": 0, "size": None, "timeline": [],
                     "last_timeline": 0.0, "seen": {}, "trigger": now, "continued": not pre_roll, "thumb": False}
        for t, jpg in frames:  # the 15 s before movement, encoded in the background
            self._jobs.put(("frame", self.clip, t, jpg))
        self.buffer.clear()

    def _finish_clip(self):
        c, self.clip = self.clip, None
        if c is not None:
            self._jobs.put(("finish", c))

    # ---------------------------------------------------------------- the encoder thread
    def _encode(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                if job[0] == "frame":
                    img = cv2.imdecode(np.frombuffer(job[3], dtype=np.uint8), cv2.IMREAD_COLOR)
                    if img is not None:
                        self._write(job[1], img, job[2])
                else:
                    self._save(job[1])
            except Exception:  # one bad frame or full disk must not stop recording for good
                traceback.print_exc()

    def _write(self, c, frame, t):
        if c["writer"] is None:
            h, w = frame.shape[:2]
            c["size"] = (w, h)
            c["writer"] = cv2.VideoWriter(str(recordings_dir(self.env) / (c["stem"] + ".webm")),
                                          cv2.VideoWriter_fourcc(*"VP80"), FPS, (w, h))
        if (frame.shape[1], frame.shape[0]) != c["size"]:
            frame = cv2.resize(frame, c["size"])
        # Keep real time: repeat the frame if fewer than FPS frames arrived (slow camera, skipped frames).
        due = int((t - c["start"]) * FPS) + 1
        while c["written"] < due:
            c["writer"].write(frame)
            c["written"] += 1
        if not c["thumb"] and t >= c["trigger"]:
            cv2.imwrite(str(recordings_dir(self.env) / (c["stem"] + ".jpg")), cv2.resize(frame, (480, int(480 * frame.shape[0] / frame.shape[1]))))
            c["thumb"] = True

    def _save(self, c):
        if c["writer"] is not None:
            c["writer"].release()
        if not c["written"]:
            (recordings_dir(self.env) / (c["stem"] + ".webm")).unlink(missing_ok=True)
            return
        meta = {"environment": self.env, "camera": self.camera_id, "file": c["stem"] + ".webm",
                "thumbnail": c["stem"] + ".jpg" if c["thumb"] else None,
                "start": datetime.fromtimestamp(c["start"]).isoformat(timespec="seconds"),
                "duration_s": round(c["written"] / FPS, 1), "fps": FPS, "size": c["size"],
                "continued": c["continued"], "seen": {k: len(v) for k, v in c["seen"].items()},
                "timeline": c["timeline"]}
        with open(recordings_dir(self.env) / (c["stem"] + ".json"), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(meta, fh)


def _brief(o, map_key):
    return {"id": o["id"], "class": o["class"], "label": o["label"], "box": o["box"], "where": o.get(map_key)}


# ---------------------------------------------------------------- listing / deleting
def list_recordings(env):
    out = []
    folder = recordings_dir(env)
    for f in sorted(folder.glob("*.json"), reverse=True) if folder.exists() else []:
        try:
            with open(f, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        meta.pop("timeline", None)
        meta["id"] = f.stem
        meta["bytes"] = (folder / meta["file"]).stat().st_size if (folder / meta["file"]).exists() else 0
        out.append(meta)
    return out


def recording_path(env, rec_id, kind):
    """Path of a recording's video / thumbnail / metadata, or None (also guards against odd ids)."""
    if not NAME_RE.match(rec_id or "") or not re.match(r"^[a-z0-9_]+$", env or ""):
        return None
    p = recordings_dir(env) / (rec_id + {"video": ".webm", "thumb": ".jpg", "meta": ".json"}[kind])
    return p if p.exists() else None


def delete_recording(env, rec_id):
    if recording_path(env, rec_id, "meta") is None:
        raise FileNotFoundError(rec_id)
    for kind in ("video", "thumb", "meta"):
        p = recording_path(env, rec_id, kind)
        if p:
            p.unlink()
