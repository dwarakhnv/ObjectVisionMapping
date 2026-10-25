"""Live feeds from network cameras (RTSP, e.g. a Tapo C100) and cameras plugged into this machine.

Credentials live in config/secrets.json (git-ignored), not in code:

    {"cameras": {"tapo_c100": {"name": "Tapo C100", "host": "192.168.5.101", "port": 554,
                               "username": "<camera account user>", "password": "<camera account password>",
                               "stream": "stream1", "v_flipped": false, "h_flipped": false}}}

``v_flipped`` / ``h_flipped`` flip the picture for cameras mounted upside down or
mirrored (both together = rotated 180 degrees). They apply to every frame, so the
stand-alone viewer and the website both see the corrected image.

Tapo notes:
  * RTSP only runs after you create a *Camera Account* in the Tapo app
    (camera > Settings > Advanced Settings > Camera Account). Those are the
    credentials RTSP wants, not your TP-Link / Tapo login.
  * stream1 = full quality (1080p on a C100), stream2 = low quality (360p).
  * Privacy mode turns the stream off.

Local cameras (USB, built-in, virtual such as OBS) are found automatically; their
key is "local_" + a hash of the device path, so it survives Windows renumbering devices.

Run it on its own to open a window (press q to quit), from src/:

    ..\\.venv\\Scripts\\python -m simulation.camera            # first camera in secrets.json
    ..\\.venv\\Scripts\\python -m simulation.camera tapo_c100  # a named one
"""
import atexit
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

# TCP is far more reliable than UDP for RTSP over Wi-Fi (no smeared / grey frames);
# the short timeout makes a dead camera fail in seconds instead of 30.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000")

import cv2  # noqa: E402  (must come after the FFmpeg options are set)

SECRETS_FILE = Path(__file__).resolve().parents[2] / "config" / "secrets.json"


class CameraError(Exception):
    pass


def load_camera_configs(path=SECRETS_FILE):
    """{name: settings} for every camera with a host in config/secrets.json."""
    if not Path(path).exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        cams = json.load(fh).get("cameras", {})
    return {k: v for k, v in cams.items() if isinstance(v, dict) and v.get("host")}


class RTSPCamera:
    """Reads an RTSP camera on a background thread and always offers the newest frame.

    OpenCV queues frames internally; if you read slower than the camera sends,
    the picture falls further and further behind. Reading continuously on a
    thread and keeping only the latest frame keeps the feed real time.
    """

    kind = "network"

    def __init__(self, host, username, password, stream="stream1", port=554, name=None, key=None,
                 v_flipped=False, h_flipped=False):
        self.host, self.port, self.stream = host, int(port), stream
        self.v_flipped, self.h_flipped = bool(v_flipped), bool(h_flipped)
        self.username, self.password = username, password
        self.name = name or "Camera at %s" % host
        self.key = key or host
        self._frame = None
        self._frame_id = 0
        self._cond = threading.Condition()
        self._thread = None
        self._stop = threading.Event()
        self._start_lock = threading.Lock()
        self.error = None
        self.fps = 0.0
        self.size = None
        self.viewers = 0
        self.last_viewer = time.monotonic()

    @classmethod
    def from_config(cls, key=None, path=SECRETS_FILE):
        configs = load_camera_configs(path)
        if not configs:
            raise CameraError("No cameras in %s (see the example at the top of camera.py)" % path)
        key = key or next(iter(configs))
        if key not in configs:
            raise CameraError("No camera '%s' in %s; found: %s" % (key, path, ", ".join(configs)))
        c = configs[key]
        return cls(c["host"], c.get("username", ""), c.get("password", ""), c.get("stream", "stream1"),
                   c.get("port", 554), c.get("name"), key,
                   v_flipped=c.get("v_flipped", False), h_flipped=c.get("h_flipped", False))

    @property
    def url(self):
        auth = "%s:%s@" % (quote(self.username, safe=""), quote(self.password, safe="")) if self.username else ""
        return "rtsp://%s%s:%d/%s" % (auth, self.host, self.port, self.stream)

    @property
    def safe_url(self):
        return "rtsp://%s:%d/%s" % (self.host, self.port, self.stream)

    def _open(self):
        # Real timeouts: a stalled stream must error out (and reconnect) rather than block forever.
        return cv2.VideoCapture(self.url, cv2.CAP_FFMPEG,
                                [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000])

    def identity(self):
        """What makes this camera this camera (a change means the reader must restart)."""
        return (self.url, self.name, self.v_flipped, self.h_flipped)

    # ---------------------------------------------------------------- diagnosis
    def check(self, timeout=3.0):
        """Find out why a camera won't open. Returns (ok, message). Makes at most one login attempt."""
        try:
            sock = socket.create_connection((self.host, self.port), timeout)
        except socket.timeout:
            return False, "No reply from %s. Is the camera on and on this network?" % self.host
        except ConnectionRefusedError:
            return False, ("%s is reachable but refuses RTSP on port %d. On a Tapo camera this means no "
                           "Camera Account is set up (Tapo app > camera > Settings > Advanced Settings > "
                           "Camera Account), or this IP is a different device." % (self.host, self.port))
        except OSError as exc:
            return False, "Can't reach %s: %s" % (self.host, exc)
        uri = self.safe_url
        try:
            first = _rtsp_describe(sock, uri)
            if first.startswith("RTSP/1.0 200"):
                return True, "Camera answered without needing a login"
            m = re.search(r'Digest realm="([^"]+)", nonce="([^"]+)"', first)
            if not m:
                return False, "Unexpected answer from the camera: " + first.split("\r\n")[0]
            realm, nonce = m.groups()
            h = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731
            response = h("%s:%s:%s" % (h("%s:%s:%s" % (self.username, realm, self.password)), nonce, h("DESCRIBE:" + uri)))
            auth = 'Digest username="%s", realm="%s", nonce="%s", uri="%s", response="%s"' % (
                self.username, realm, nonce, uri, response)
            second = _rtsp_describe(sock, uri, auth)
        finally:
            sock.close()
        status = second.split("\r\n")[0]
        if status.startswith("RTSP/1.0 200"):
            return True, "Login accepted; %s is ready" % self.stream
        if " 401" in status:
            return False, ("Username or password rejected. Use the Tapo *Camera Account* credentials "
                           "(not your TP-Link login). Several failures in a row can lock it for ~30 min.")
        if " 404" in status:
            return False, "Stream '%s' doesn't exist; Tapo cameras use stream1 or stream2" % self.stream
        return False, "Camera said: " + status

    # ---------------------------------------------------------------- live frames
    def orient(self, frame):
        """Apply the configured flips (cv2.flip codes: 0 = vertical, 1 = horizontal, -1 = both)."""
        if self.v_flipped and self.h_flipped:
            return cv2.flip(frame, -1)
        if self.v_flipped:
            return cv2.flip(frame, 0)
        if self.h_flipped:
            return cv2.flip(frame, 1)
        return frame

    def start(self):
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                if not self._stop.is_set():
                    return  # already running
                # It is shutting down (e.g. an idle stop just fired): wait for it, then start fresh.
                self._thread.join(timeout=10)
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="rtsp-" + self.key)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def latest(self, after=0, timeout=5.0):
        """(frame_id, frame) for the newest frame newer than `after` (waits up to `timeout`)."""
        with self._cond:
            self._cond.wait_for(lambda: self._frame_id > after or self._stop.is_set(), timeout=timeout)
            return self._frame_id, self._frame

    def _run(self):
        delay = 2.0
        while not self._stop.is_set():
            cap = self._open()
            if cap is None or not cap.isOpened():
                if cap is not None:
                    cap.release()
                ok, why = self.check()
                self.error = why if not ok else "Could not open the stream"
                if not ok and not why.startswith(("No reply", "Can't reach")):
                    break  # wrong login / RTSP off / no such stream: retrying won't help and can lock the account
                self._stop.wait(delay)
                delay = min(delay * 2, 60.0)  # back off: repeated bad logins can lock a Tapo account
                continue
            self.error, delay = None, 2.0
            last = time.monotonic()
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    self.error = "Stream dropped; reconnecting"
                    break
                frame = self.orient(frame)
                now = time.monotonic()
                dt, last = now - last, now
                if dt > 0:
                    self.fps = 1.0 / dt if not self.fps else 0.9 * self.fps + 0.1 / dt
                with self._cond:
                    self._frame = frame
                    self._frame_id += 1
                    self.size = (frame.shape[1], frame.shape[0])
                    self._cond.notify_all()
            cap.release()
        with self._cond:
            self._cond.notify_all()

    def status(self):
        return {"key": self.key, "name": self.name, "kind": self.kind, "url": self.safe_url, "running": self.is_running(),
                "error": self.error, "fps": round(self.fps, 1),
                "width": self.size[0] if self.size else None, "height": self.size[1] if self.size else None,
                "viewers": self.viewers, "v_flipped": self.v_flipped, "h_flipped": self.h_flipped}


class LocalCamera(RTSPCamera):
    """A camera plugged into this machine (USB, built-in, or virtual like OBS), read like an RTSP camera."""

    def __init__(self, key, name, path, index, backend, kind="usb", vid="", pid=""):
        super().__init__(host="local", username="", password="", name=name, key=key)
        self.path, self.index, self.backend, self.kind = path, index, backend, kind
        self.usb_id = "%s:%s" % (vid, pid) if vid else ""

    @property
    def url(self):
        return "device:%s" % (self.path or self.index)

    @property
    def safe_url(self):
        label = {"usb": "USB camera", "built-in": "Built-in camera", "virtual": "Virtual camera"}.get(self.kind, "Camera")
        return label + (" (%s)" % self.usb_id if self.usb_id else "")

    def _current_index(self):
        """Device indexes shift when cameras are plugged in or out; find this one by its path."""
        for info in _enumerate_local():
            if info["key"] == self.key:
                return info["index"]
        return None

    def _open(self):
        index = self._current_index()
        if index is None:
            return None
        cap = cv2.VideoCapture(index, self.backend)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))  # full frame rate at HD over USB
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        return cap

    def check(self, timeout=3.0):
        if self._current_index() is None:
            return False, "%s is no longer connected" % self.name
        cap = self._open()
        ok = cap is not None and cap.isOpened()
        if cap is not None:
            cap.release()
        if ok:
            return True, "Camera opens"
        return False, "No reply from %s: it is connected but busy (another app may be using it)" % self.name

    def identity(self):
        return (self.key, self.name)


_VIRTUAL_HINTS = ("virtual", "obs", "snap camera", "manycam", "xsplit", "droidcam", "nvidia broadcast")
_local_cache = {"at": 0.0, "items": []}


def _enumerate_local(max_age=10.0):
    """Cameras attached to this machine: [{key, name, index, backend, path, kind, vid, pid}] (cached briefly)."""
    if time.monotonic() - _local_cache["at"] < max_age:
        return _local_cache["items"]
    items, seen = [], set()
    try:
        from cv2_enumerate_cameras import enumerate_cameras
    except ImportError:
        enumerate_cameras = None
    backends = {"win32": [cv2.CAP_DSHOW, cv2.CAP_MSMF], "linux": [cv2.CAP_V4L2], "darwin": [cv2.CAP_AVFOUNDATION]}
    for backend in (backends.get(sys.platform, [cv2.CAP_ANY]) if enumerate_cameras else []):
        try:
            entries = enumerate_cameras(backend)
        except Exception:
            continue
        for e in entries:
            ident = (e.path or "").split("#{")[0].lower() or "name:" + (e.name or "").lower()
            if ident in seen:
                continue  # the same device listed again by another backend
            seen.add(ident)
            name = (e.name or "Camera %d" % e.index).strip()
            low = name.lower()
            if any(h in low for h in _VIRTUAL_HINTS):
                kind = "virtual"
            elif "integrated" in low or "built-in" in low or "facetime" in low:
                kind = "built-in"
            else:
                kind = "usb"
            items.append({"key": "local_" + hashlib.sha1(ident.encode()).hexdigest()[:10], "name": name,
                          "index": int(e.index), "backend": int(e.backend), "path": e.path or "", "kind": kind,
                          "vid": "%04X" % e.vid if e.vid is not None else "",
                          "pid": "%04X" % e.pid if e.pid is not None else ""})
    _local_cache.update(at=time.monotonic(), items=items)
    return items


def local_cameras():
    return {i["key"]: LocalCamera(i["key"], i["name"], i["path"], i["index"], i["backend"], i["kind"], i["vid"], i["pid"])
            for i in _enumerate_local()}


# ---------------------------------------------------------------- one shared reader per camera
# The website preview and the detector both read the same camera; sharing one
# RTSPCamera means one connection to it (Tapo cameras allow only a few).
IDLE_STOP_SECONDS = 15.0
_shared = {}
_shared_lock = threading.Lock()


def shared_cameras():
    """{key: camera} for every camera in config/secrets.json plus every camera plugged into this machine.

    secrets.json is re-read each time, so edits apply without a restart.
    """
    found = {key: RTSPCamera.from_config(key) for key in load_camera_configs()}
    found.update(local_cameras())
    with _shared_lock:
        for key in list(_shared):
            if key not in found and not _shared[key].is_running():
                _shared.pop(key)  # unplugged, or removed from secrets.json
        for key, fresh in found.items():
            cam = _shared.get(key)
            if cam is None or cam.identity() != fresh.identity():
                if cam is not None:
                    cam.stop()
                _shared[key] = fresh
        return dict(_shared)


def acquire(key):
    """Start (or join) the shared reader for a camera. Pair every acquire with release()."""
    cam = shared_cameras().get(key)
    if cam is None:
        raise CameraError("No camera '%s' (not in config/secrets.json and not plugged in)" % key)
    with _shared_lock:
        cam.viewers += 1
    cam.start()
    return cam


def release(cam):
    """Done with a camera; it stops IDLE_STOP_SECONDS after the last user leaves."""
    with _shared_lock:
        cam.viewers = max(0, cam.viewers - 1)
        cam.last_viewer = time.monotonic()

    def stop_if_idle():
        with _shared_lock:  # decide under the same lock acquire() takes, so a new viewer can't slip in
            idle = cam.viewers == 0 and time.monotonic() - cam.last_viewer >= IDLE_STOP_SECONDS - 0.5
            if idle:
                cam._stop.set()
        if idle:
            cam.stop()
    threading.Timer(IDLE_STOP_SECONDS, stop_if_idle).start()


@atexit.register
def _release_all():
    """Stop every camera reader at shutdown: an open DirectShow device can otherwise keep the
    process alive (the dev server would hang when it reloads after a code change)."""
    cams = list(_shared.values())
    for cam in cams:
        cam._stop.set()
    for cam in cams:
        if cam._thread is not None:
            cam._thread.join(timeout=3)


def _rtsp_describe(sock, uri, auth=None):
    lines = ["DESCRIBE %s RTSP/1.0" % uri, "CSeq: %d" % (2 if auth else 1), "Accept: application/sdp"]
    if auth:
        lines.append("Authorization: " + auth)
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    return sock.recv(4096).decode(errors="replace")


# ---------------------------------------------------------------- stand-alone viewer
def main(key=None):
    try:
        cam = RTSPCamera.from_config(key)
    except CameraError as exc:
        print(exc)
        return 1
    print("Checking %s (%s)..." % (cam.name, cam.safe_url))
    ok, message = cam.check()
    print(("OK: " if ok else "Problem: ") + message)
    if not ok:
        return 1
    cam.start()
    last, shown = 0, time.monotonic()
    try:
        while True:
            last, frame = cam.latest(last, timeout=10)
            if frame is None:
                print("No frames: %s" % (cam.error or "timed out"))
                break
            cv2.imshow("%s - press q to quit" % cam.name, frame)
            if time.monotonic() - shown > 5:
                shown = time.monotonic()
                print("%dx%d at %.1f fps" % (frame.shape[1], frame.shape[0], cam.fps))
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        cam.stop()
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
