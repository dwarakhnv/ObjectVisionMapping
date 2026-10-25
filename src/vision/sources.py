"""Where pictures come from: video files, still images, streams and local devices.

Files live in data/sources/. Stream credentials come from config/secrets.json:

    {"cameras": {"front_gate": {"username": "viewer", "password": "..."}}}

and fill ``{username}`` / ``{password}`` placeholders in the stream URL, so
they are never stored with an environment.
"""
import re
import time
from pathlib import Path
from urllib.parse import quote

import cv2
from django.conf import settings

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}$")


class SourceError(Exception):
    pass


def safe_name(name):
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(str(name)).name).strip("._") or "upload"
    return stem[:120]


def list_sources():
    out = []
    folder = Path(settings.SOURCES_DIR)
    if folder.exists():
        for f in sorted(folder.iterdir()):
            ext = f.suffix.lower()
            if f.is_file() and NAME_RE.match(f.name) and (ext in VIDEO_EXT or ext in IMAGE_EXT):
                out.append({"name": f.name, "kind": "image" if ext in IMAGE_EXT else "video", "bytes": f.stat().st_size})
    return out


def resolve_file(uri):
    """A source file path: a name in data/sources, or an absolute path that exists."""
    uri = str(uri or "").strip()
    if not uri:
        raise SourceError("No file chosen for this camera")
    p = Path(uri)
    if p.is_absolute():
        if not p.is_file():
            raise SourceError("File not found: %s" % uri)
        return p
    root = Path(settings.SOURCES_DIR).resolve()
    candidate = (root / uri).resolve()
    if root not in candidate.parents or not candidate.is_file():
        raise SourceError("File not found in data/sources: %s" % uri)
    return candidate


def stream_url(uri, secret_name):
    if not secret_name:
        return uri
    entry = (settings.SECRETS.get("cameras") or {}).get(secret_name)
    if entry is None:
        raise SourceError("No entry '%s' under \"cameras\" in config/secrets.json" % secret_name)
    values = {k: quote(str(v), safe="") for k, v in entry.items()}
    try:
        return uri.format(**values)
    except (KeyError, IndexError, ValueError) as exc:
        raise SourceError("Stream URL placeholder not found in secret '%s': %s" % (secret_name, exc))


def redact(url):
    return re.sub(r"//[^/@]+@", "//***@", url)


class FrameSource:
    """Uniform reader. ``read()`` -> (frame, timestamp_s) or (None, None) at the end.

    Files are played back in real time (frames are skipped if processing is
    slower) and loop. Timestamps are media time for files and wall time for
    live sources, so motion estimates stay right either way.
    """

    def __init__(self, kind, uri, secret="", loop=True):
        self.kind, self.loop = kind, loop
        self.still = None
        self.cap = None
        self.shared = None
        self.label = uri
        if kind == "image":
            path = resolve_file(uri)
            self.still = cv2.imread(str(path))
            if self.still is None:
                raise SourceError("Could not read image %s" % path.name)
            self.fps = 2.0
        elif kind == "file":
            path = resolve_file(uri)
            self.cap = cv2.VideoCapture(str(path))
            self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 25.0
            if not (1.0 <= self.fps <= 240.0):
                self.fps = 25.0
        elif kind == "url":
            url = stream_url(uri, secret)
            self.label = redact(url)
            self.cap = cv2.VideoCapture(url)
            self.fps = None
        elif kind == "shared":
            # A camera from config/secrets.json, via the reader the website preview also uses.
            from simulation import camera as shared_cameras
            try:
                self.shared = shared_cameras.acquire(uri)
            except shared_cameras.CameraError as exc:
                raise SourceError(str(exc))
            self.label = self.shared.name
            self.fps = None
            self._last_id = 0
        elif kind == "device":
            try:
                index = int(uri or 0)
            except ValueError:
                raise SourceError("Device must be a number (0 is the first camera)")
            self.cap = cv2.VideoCapture(index)
            self.fps = None
        else:
            raise SourceError("This camera has no source yet")
        if self.cap is not None and not self.cap.isOpened():
            raise SourceError("Could not open %s" % self.label)
        self.started = time.monotonic()
        self.frame_index = 0
        self.loops = 0

    def read(self):
        if self.shared is not None:
            for attempt in range(3):  # ~30 s in all
                frame_id, frame = self.shared.latest(self._last_id, timeout=10)
                if frame is not None and frame_id != self._last_id:
                    self._last_id = frame_id
                    return frame, time.monotonic() - self.started
                # No new frame for 10 s: let go of this camera handle and take the current one
                # (it may have been stopped or replaced), which also restarts its reader.
                from simulation import camera as shared_cameras
                key = self.shared.key
                shared_cameras.release(self.shared)
                try:
                    self.shared = shared_cameras.acquire(key)
                except shared_cameras.CameraError:
                    self.shared = None
                    return None, None
                self._last_id = 0
            return None, None
        if self.still is not None:
            t = time.monotonic() - self.started
            return self.still.copy(), t
        if self.kind == "file":
            # Real-time playback: skip ahead to the frame that should be showing now.
            target = int((time.monotonic() - self.started) * self.fps)
            while self.frame_index < target - 1:
                if not self.cap.grab():
                    return self._rewind()
                self.frame_index += 1
            ok, frame = self.cap.read()
            if not ok:
                return self._rewind()
            self.frame_index += 1
            return frame, self.loops * 1e6 + self.frame_index / self.fps
        ok, frame = self.cap.read()
        if not ok:
            return None, None
        return frame, time.monotonic() - self.started

    def _rewind(self):
        if not self.loop:
            return None, None
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        self.frame_index = 0
        self.loops += 1
        self.started = time.monotonic()
        ok, frame = self.cap.read()
        if not ok:
            return None, None
        self.frame_index = 1
        return frame, self.loops * 1e6 + 1 / self.fps

    def wait_time(self):
        """Seconds until the next frame is due (files and stills)."""
        if self.still is not None:
            return 1.0 / self.fps
        if self.kind == "file":
            due = self.started + self.frame_index / self.fps
            return max(0.0, due - time.monotonic())
        return 0.0

    def close(self):
        if self.shared is not None:
            from simulation import camera as shared_cameras
            shared_cameras.release(self.shared)
            self.shared = None
        if self.cap is not None:
            self.cap.release()
