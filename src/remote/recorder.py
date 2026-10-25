"""Saves video around activity: from 15 s before movement until 15 s after it stops.

While detection runs for an environment camera, a Recorder keeps the last
PRE_SECONDS of video in memory. When the detector reports movement (an object
of the chosen classes that is not stationary), it starts a clip with that
buffered video, keeps recording while movement continues, and stops
POST_SECONDS after the last movement. Long activity is saved as consecutive
SEGMENT_SECONDS clips, so each file stays short and quick to open.

Files, per environment, in data/recordings/<env>/:
    <start>_<camera>.webm   the video (VP8 WebM: plays in any browser)
    <start>_<camera>.jpg    a thumbnail from the moment movement started
    <start>_<camera>.json   when, which camera, what was seen, and a timeline of detections
"""
import collections
import json
import re
import threading
import time
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
NAME_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?_[a-z0-9_]+$")


def recordings_dir(env=None):
    root = Path(settings.DATA_DIR) / "recordings"
    return root / env if env else root


def is_activity(obj, classes):
    """Movement worth recording: a moving object of one of `classes` (all classes when empty)."""
    if classes and obj["class"] not in classes:
        return False
    return obj.get("direction") not in (None, "stationary", "unknown")


class Recorder:
    """Records one camera. Feed it detections with observe(); close() when detection stops."""

    def __init__(self, env, camera_id, camera_key):
        from simulation import camera as shared_cameras

        self.env, self.camera_id = env, camera_id
        self._cams = shared_cameras
        self.cam = shared_cameras.acquire(camera_key)   # the reader detection already uses: no second connection
        self.buffer = collections.deque()                 # (time, jpeg) for the pre-roll
        self.clip = None                                  # the clip being written, or None
        self.last_activity = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="rec-%s-%s" % (env, camera_id))
        self._thread.start()

    # ---------------------------------------------------------------- called by the detection worker
    def observe(self, objects, classes, map_key):
        """Detections for the newest frame. Starts / extends a clip when something moves."""
        now = time.time()
        active = [o for o in objects if is_activity(o, classes)]
        with self._lock:
            if active:
                self.last_activity = now
                if self.clip is None:
                    self._start_clip(now, pre_roll=True)
            if self.clip is not None:
                c = self.clip
                if now - c["last_timeline"] >= 1.0 / TIMELINE_HZ:
                    c["last_timeline"] = now
                    c["timeline"].append({"t": round(now - c["start"], 2), "objects": [_brief(o, map_key) for o in objects]})
                for o in objects:
                    if not classes or o["class"] in classes:
                        c["seen"].setdefault(o["class"], set()).add(o["id"])

    def close(self):
        self._stop.set()
        self._thread.join(timeout=5)
        with self._lock:
            self._finish_clip()
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
                if self.clip is not None:
                    self._write(frame, now)
                    quiet = now - self.last_activity
                    if quiet > POST_SECONDS:
                        self._finish_clip()
                    elif now - self.clip["start"] >= SEGMENT_SECONDS:
                        self._finish_clip()
                        self._start_clip(now, pre_roll=False)  # activity goes on: the next minute
                else:
                    ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                    if ok:
                        self.buffer.append((now, jpg.tobytes()))
                    while self.buffer and now - self.buffer[0][0] > PRE_SECONDS:
                        self.buffer.popleft()

    # ---------------------------------------------------------------- clips
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
        self.clip = {"stem": stem, "start": start, "writer": None, "written": 0, "size": None, "timeline": [],
                     "last_timeline": 0.0, "seen": {}, "trigger": now, "continued": not pre_roll, "thumb": False}
        for t, jpg in frames:  # the 15 s before movement
            img = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                self._write(img, t)
        self.buffer.clear()

    def _write(self, frame, t):
        c = self.clip
        if c["writer"] is None:
            h, w = frame.shape[:2]
            c["size"] = (w, h)
            c["writer"] = cv2.VideoWriter(str(recordings_dir(self.env) / (c["stem"] + ".webm")),
                                          cv2.VideoWriter_fourcc(*"VP80"), FPS, (w, h))
        if (frame.shape[1], frame.shape[0]) != c["size"]:
            frame = cv2.resize(frame, c["size"])
        # Keep real time: repeat the frame if the camera delivered fewer than FPS frames.
        due = int((t - c["start"]) * FPS) + 1
        while c["written"] < due:
            c["writer"].write(frame)
            c["written"] += 1
        if not c["thumb"] and t >= c["trigger"]:
            cv2.imwrite(str(recordings_dir(self.env) / (c["stem"] + ".jpg")), cv2.resize(frame, (480, int(480 * frame.shape[0] / frame.shape[1]))))
            c["thumb"] = True

    def _finish_clip(self):
        c, self.clip = self.clip, None
        if c is None:
            return
        if c["writer"] is not None:
            c["writer"].release()
        if not c["written"]:
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
