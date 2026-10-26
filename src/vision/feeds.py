"""Live feeds: one worker thread per feed reads frames, detects, (optionally) tracks and estimates.

A feed is either an environment camera (``<env_slug>.<camera_id>``) or a file
in data/sources (``~<file name>``). Workers start when a page asks for a feed
and stop after OVM_FEED_IDLE_SECONDS without anyone asking.

Results are kept in memory: the latest analysis, plus the last few frames so
the browser can fetch exactly the frame an analysis belongs to (encoded to JPEG
only when asked for: a camera's live video comes from the camera itself, see
video_camera(), and API clients only need the objects).

Tracking (stable ids, speed, motion heading) is off unless the feed's
environment turns it on ("tracking": true); without it each result simply
lists everything detected in that frame.

Every published analysis bumps a global sequence number and wakes whoever waits
in wait_change(), so live subscribers get results the moment they exist.
"""
import collections
import threading
import time
import traceback
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings

from environments.environment import Environment
from environments.geometry import CameraModel
from simulation.environment import Environment as GeoEnvironment
from models.objects.estimate import ObjectEstimator
from models.objects.registry import ObjectRegistry
from models.segmentation import base as detectors
from models.tracking.sort.tracker import Tracker, untracked

from .sources import FrameSource, SourceError, list_sources

RING = 8                 # frames kept for the browser to fetch (raw, ~3 MB each at 720p)
GEO_ENV_DIR = Path(settings.DATA_DIR) / "environments"
JPEG_QUALITY = 80
# Lowest detector confidence reported, per class. People are the priority: distant or
# partly hidden people often score only 0.25-0.35, so they get a lower bar.
MIN_SCORE = {"person": 0.25, "default": 0.3}

_lock = threading.Lock()
_workers = {}
_detector = None
_detector_error = None
_registry = None
_changed = threading.Condition()
_seq = 0


def seq():
    """Goes up by one every time any feed publishes a result."""
    return _seq


def wait_change(after, timeout):
    """Block until some feed publishes after sequence number `after` (or timeout)."""
    with _changed:
        _changed.wait_for(lambda: _seq > after, timeout=timeout)
        return _seq


def registry():
    global _registry
    if _registry is None:
        path = Path(settings.OBJECT_CLASSES_FILE)
        if not path.exists():
            ObjectRegistry.write_default(path)
        _registry = ObjectRegistry.load(path)
    return _registry


def detector():
    """The shared detector (loaded once; inference is serialised inside it)."""
    global _detector, _detector_error
    with _lock:
        if _detector is None and _detector_error is None:
            try:
                from models.segmentation.yolo.detector import default_variant, pick_device
                device = pick_device(settings.OVM_DEVICE)
                name = settings.OVM_DETECTOR or default_variant(device)
                _detector = detectors.create(name, settings.MODELS_DIR, imgsz=settings.OVM_IMGSZ, device=device)
            except Exception as exc:  # report it on every feed rather than crash the server
                _detector_error = "%s: %s" % (type(exc).__name__, exc)
        if _detector is None:
            raise RuntimeError("Detector unavailable - " + _detector_error)
        return _detector


# ---------------------------------------------------------------- feed catalogue

def list_feeds():
    feeds = []
    for env in Environment.list(settings.ENVIRONMENTS_DIR):
        for cam in env.cameras:
            feeds.append({"id": "%s.%s" % (env.slug, cam.id), "name": cam.name, "group": env.name,
                          "environment": env.slug, "camera": cam.id, "source": cam.source.kind,
                          "calibrated": bool(cam.calibration.solved), "playable": cam.source.kind != "none"})
    for env in GeoEnvironment.list(GEO_ENV_DIR):
        for cam in env.cameras:
            feeds.append({"id": "@%s.%s" % (env.slug, cam.id), "name": cam.name, "group": env.name,
                          "environment": env.slug, "camera": cam.id, "source": "camera",
                          "calibrated": len(cam.reference_points) >= 4, "playable": bool(cam.source)})
    for s in list_sources():
        feeds.append({"id": "~" + s["name"], "name": s["name"], "group": "Files in data/sources",
                      "environment": None, "camera": None, "source": "image" if s["kind"] == "image" else "file",
                      "calibrated": False, "playable": True})
    for f in feeds:
        w = _workers.get(f["id"])
        f["running"] = bool(w and w.is_alive())
    return feeds


def load_geo_camera(feed_id):
    """(environment, camera) from the Environments page for an "@<env>.<camera>" feed id."""
    slug, _, cam_id = feed_id[1:].partition(".")
    try:
        env = GeoEnvironment.load(GEO_ENV_DIR, slug)
        return env, env.camera(cam_id)
    except (OSError, KeyError, ValueError):
        raise KeyError(feed_id)


def geo_camera_model(cam, width, height):
    """A CameraModel (local metres around the reference points) from the camera's estimated pose."""
    pose = cam.estimate_pose()
    model = CameraModel([pose["east"], pose["north"], pose["height_m"]], pose["heading"], pose["tilt"], pose["roll"], pose["hfov"],
                        cam.resolution[0], cam.resolution[1])
    if (width, height) != tuple(cam.resolution):
        model = model.scaled(width, height)
    return model, pose


def homography_ground_fn(cam, width, height):
    """Pixels (N x 2, at width x height) -> ground metres around cam's reference points, via its homography.

    Same frame as geo_camera_model(); points that don't land on the ground come back as NaN.
    """
    H = cam._homography
    sx, sy = cam.resolution[0] / width, cam.resolution[1] / height

    def fn(pixels):
        p = np.asarray(pixels, float) * [sx, sy]
        hom = np.column_stack([p, np.ones(len(p))]) @ H.T
        out = hom[:, :2] / hom[:, 2:3]
        out[hom[:, 2] <= 1e-12] = np.nan
        return out
    return fn


def resolve_feed(feed_id):
    """(source kwargs, environment or None, camera id or None) for a feed id."""
    if feed_id.startswith("@"):
        _, cam = load_geo_camera(feed_id)
        if not cam.source:
            raise KeyError(feed_id)
        return {"kind": "shared", "uri": cam.source, "secret": "", "loop": False}, None, None
    if feed_id.startswith("~"):
        name = feed_id[1:]
        ext = Path(name).suffix.lower()
        from .sources import IMAGE_EXT
        return {"kind": "image" if ext in IMAGE_EXT else "file", "uri": name, "secret": "", "loop": True}, None, None
    slug, _, cam_id = feed_id.partition(".")
    try:
        env = Environment.load(settings.ENVIRONMENTS_DIR, slug)
        cam = env.camera(cam_id)
    except (OSError, KeyError, ValueError):
        raise KeyError(feed_id)
    s = cam.source
    return {"kind": s.kind, "uri": s.uri, "secret": s.secret, "loop": s.loop}, env, cam_id


def video_camera(feed_id):
    """The shared camera key behind a feed, whose own live video the browser can play; None for files and stills."""
    try:
        src, _, _ = resolve_feed(feed_id)
    except KeyError:
        return None
    return src["uri"] if src["kind"] == "shared" else None


def get_worker(feed_id, start=True):
    with _lock:
        w = _workers.get(feed_id)
        if w is not None and (w.is_alive() or (w.ended and time.monotonic() - w.ended < 5.0)):
            # A worker that just failed is kept briefly so pages see its error instead of restarting it.
            w.touch()
            return w
        if not start:
            return None
        resolve_feed(feed_id)  # raises KeyError for unknown feeds
        w = FeedWorker(feed_id)
        _workers[feed_id] = w
        w.start()
        return w


def stop_worker(feed_id):
    with _lock:
        w = _workers.pop(feed_id, None)
    if w:
        w.stop()


# ---------------------------------------------------------------- worker

class FeedWorker(threading.Thread):
    def __init__(self, feed_id):
        super().__init__(daemon=True, name="feed-" + feed_id)
        self.feed_id = feed_id
        self.cond = threading.Condition()
        self.frames = collections.OrderedDict()   # frame_id -> frame (numpy)
        self._jpegs = {}                           # frame_id -> JPEG bytes, encoded on request
        self.latest = {"frame_id": 0, "objects": [], "status": "starting"}
        self.frame_id = 0
        self.last_access = time.monotonic()
        self._stop_event = threading.Event()
        self.env = None
        self.env_mtime = None
        self.camera_id = None
        self.fps_measured = 0.0
        self.ended = None

    def touch(self):
        self.last_access = time.monotonic()

    def stop(self):
        self._stop_event.set()

    def wait_for(self, after, timeout=1.5):
        """Block until a frame newer than `after` exists (or timeout); return the latest analysis."""
        self.touch()
        with self.cond:
            self.cond.wait_for(lambda: self.latest["frame_id"] > after or self.latest.get("status") == "error"
                               or self._stop_event.is_set(), timeout=timeout)
            return self.latest

    def jpeg(self, frame_id):
        """A recent frame as JPEG (None once it has dropped out of the ring)."""
        self.touch()
        with self.cond:
            data = self._jpegs.get(frame_id)
            frame = self.frames.get(frame_id)
        if data is None and frame is not None:
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            data = buf.tobytes() if ok else None
            with self.cond:
                if frame_id in self.frames:
                    self._jpegs[frame_id] = data
        return data

    def snapshot(self):
        """The newest raw frame as JPEG (no overlays), for calibration."""
        self.touch()
        with self.cond:
            if not self.frames:
                return None
            frame_id = next(reversed(self.frames))
        return self.jpeg(frame_id)

    def _publish(self, result, frame=None):
        global _seq
        with self.cond:
            if frame is not None:
                self.frames[result["frame_id"]] = frame
                while len(self.frames) > RING:
                    old, _ = self.frames.popitem(last=False)
                    self._jpegs.pop(old, None)
            self.latest = result
            self.cond.notify_all()
        with _changed:
            _seq += 1
            _changed.notify_all()

    def _error(self, message):
        self._publish({"frame_id": self.frame_id, "objects": [], "status": "error", "error": message})

    def _record(self, objects, map_key):
        """Save video around activity (remote/recorder.py) when recording is on for this environment."""
        from remote import recorder, store

        now = time.monotonic()
        if now - getattr(self, "_rec_checked", 0.0) > 5.0:  # settings can change while running
            self._rec_checked = now
            slug, _, cam_id = self.feed_id[1:].partition(".")
            self._rec_settings = store.recording(slug)
            rec = getattr(self, "_recorder", None)
            if self._rec_settings["enabled"] and rec is None:
                try:
                    _, cam = load_geo_camera(self.feed_id)
                    self._recorder = recorder.Recorder(slug, cam_id, cam.source, groups=registry().groups())
                except Exception:  # never let recording stop detection
                    traceback.print_exc()
                    self._recorder = None
            elif not self._rec_settings["enabled"] and rec is not None:
                rec.close()
                self._recorder = None
        rec = getattr(self, "_recorder", None)
        if rec is not None:
            rec.observe(objects, self._rec_settings["classes"], map_key)

    def _geo_camera(self, width, height):
        """(camera, CameraModel or None, cam_info) for an Environments-page camera, reloaded when its file changes."""
        slug = self.feed_id[1:].partition(".")[0]
        try:
            mtime = (GEO_ENV_DIR / (slug + ".json")).stat().st_mtime
        except OSError:
            mtime = None
        key = (mtime, width, height)
        if getattr(self, "_geo_key", None) != key:
            self._geo_key = key
            try:
                env, cam = load_geo_camera(self.feed_id)
            except KeyError:
                self._geo = (None, None, None)
                self._tracking = False
                return self._geo
            self._tracking = env.tracking
            info = {"id": cam.id, "name": cam.name, "calibrated": False, "rmse_px": None}
            model = None
            if len(cam.reference_points) >= 4:
                try:
                    model, pose = geo_camera_model(cam, width, height)
                    info.update(calibrated=True, rmse_px=pose["error_px"], accuracy_m=cam.accuracy())
                except ValueError as exc:
                    info["error"] = str(exc)
            self._geo = (cam if info["calibrated"] else None, model, info)
        return self._geo

    def _reload_env(self, env_slug):
        path = Path(settings.ENVIRONMENTS_DIR) / env_slug / "environment.json"
        try:
            m = path.stat().st_mtime
        except OSError:
            return
        if m != self.env_mtime:
            self.env_mtime = m
            try:
                self.env = Environment.load(settings.ENVIRONMENTS_DIR, env_slug)
            except (OSError, ValueError):
                pass

    def run(self):
        try:
            self._run()
        except Exception as exc:  # keep the message for the page
            traceback.print_exc()
            self._error("%s: %s" % (type(exc).__name__, exc))
        finally:
            self.ended = time.monotonic()

    def _run(self):
        try:
            src_kwargs, env, cam_id = resolve_feed(self.feed_id)
            source = FrameSource(**src_kwargs)
        except (KeyError, SourceError) as exc:
            self._error(str(exc))
            return
        self.env, self.camera_id = env, cam_id
        env_slug = env.slug if env else None
        reg = registry()
        try:
            det = detector()
        except RuntimeError as exc:
            self._error(str(exc))
            source.close()
            return
        labels = [l for l in reg.detector_labels() if l in {x.lower() for x in det.labels}]
        tracker = Tracker(groups=reg.groups())
        estimator = ObjectEstimator(reg)
        min_interval = 1.0 / max(settings.OVM_MAX_FPS, 0.5)
        last_loop = 0
        last_tick = time.monotonic()
        last_env_check = 0.0
        try:
            while not self._stop_event.is_set():
                if time.monotonic() - self.last_access > settings.OVM_FEED_IDLE_SECONDS:
                    break
                frame, ts = source.read()
                started, captured_at = time.monotonic(), time.time()  # the frame is the camera's newest
                if frame is None:
                    self._error("The source ended or stopped sending frames")
                    break
                if source.kind == "file" and source.loops != last_loop:
                    last_loop = source.loops
                    tracker.reset()
                if env_slug and started - last_env_check > 1.0:
                    last_env_check = started
                    self._reload_env(env_slug)
                h, w = frame.shape[:2]
                camera, cam_info, ground_z, hfov, to_pos = None, None, 0.0, 60.0, None
                if self.env is not None:
                    try:
                        cam = self.env.camera(self.camera_id)
                        cs = self.env.coordinates
                        camera = cam.model(cs, w, h)
                        ground_z = cs.ground_z()
                        hfov = cam.hfov
                        to_pos = cs.from_world
                        cam_info = {"id": cam.id, "name": cam.name, "calibrated": bool(cam.calibration.solved),
                                    "rmse_px": cam.calibration.solved.get("rmse_px")}
                    except KeyError:
                        pass

                geo_cam = None
                if self.feed_id.startswith("@"):
                    geo_cam, camera, cam_info = self._geo_camera(w, h)

                detections = det.detect(frame, labels=labels, min_score=min(MIN_SCORE.values()))
                detect_ms = (time.monotonic() - started) * 1000
                for d in detections:
                    c = reg.for_label(d.label)
                    d.class_key = c.key if c else d.label
                detections = [d for d in detections if d.score >= MIN_SCORE.get(d.class_key, MIN_SCORE["default"])]
                tracking = getattr(self, "_tracking", False)
                if tracking:
                    tracks = tracker.update(detections)
                else:
                    tracker.reset()  # so turning tracking on starts fresh
                    tracks = untracked(detections)
                # Uncalibrated cameras get no world positions: an unsolved pose would put objects in the wrong place.
                use_cam = camera if (cam_info and cam_info["calibrated"]) else None
                ground_fn = homography_ground_fn(geo_cam, w, h) if geo_cam is not None else None
                objects = [estimator.estimate(t, ts, w, h, use_cam, ground_z, hfov, to_pos, ground_fn) for t in tracks]
                if geo_cam is not None:
                    for o in objects:
                        x1, _, x2, y2 = o["box"]
                        key = "geo" if geo_cam.space.kind == "geo" else "plan"
                        try:  # the ground contact point: bottom-centre of the box
                            p = geo_cam.pixel_to_map(((x1 + x2) / 2, y2), frame_size=(w, h))
                            o[key] = ({"lat": round(p.y, 8), "lon": round(p.x, 8)} if key == "geo"
                                      else {"x": round(p.x, 3), "y": round(p.y, 3)})
                        except ValueError:
                            o[key] = None
                if self.feed_id.startswith("@"):
                    self._record(objects, "geo" if geo_cam is None or geo_cam.space.kind == "geo" else "plan")

                self.frame_id += 1
                now = time.monotonic()
                dt = now - last_tick
                last_tick = now
                self.fps_measured = 0.8 * self.fps_measured + 0.2 * (1.0 / dt if dt > 0 else 0.0)
                counts = collections.Counter(o["class"] for o in objects)
                self._publish({
                    "frame_id": self.frame_id, "status": "running", "width": w, "height": h,
                    "time": round(ts % 1e6, 3), "fps": round(self.fps_measured, 1),
                    "inference_ms": round((now - started) * 1000), "detect_ms": round(detect_ms),
                    "captured_at": round(captured_at, 3), "objects": objects, "counts": counts,
                    "camera": cam_info, "environment": env_slug, "source": source.label if source.kind != "url" else "stream",
                    "detector": det.name, "runs_on": getattr(det, "runs_on", ""), "tracking": tracking,
                }, frame)
                # Pace: never above OVM_MAX_FPS, and files/stills at their own rate.
                wait = max(min_interval - (time.monotonic() - started), source.wait_time())
                if wait > 0:
                    self._stop_event.wait(wait)
        finally:
            if getattr(self, "_recorder", None) is not None:
                self._recorder.close()  # finishes a clip in progress
                self._recorder = None
            source.close()
            self._publish(dict(self.latest, status="stopped"))
