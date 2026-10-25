"""Phase 3: calibrate cameras against reference points, and the live world map."""
import json
import math
import threading
from pathlib import Path

import numpy as np
from django.conf import settings
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from environments.environment import Calibration, EnvironmentError
from environments.views import load_or_404, payload
from vision import feeds

from .fusion import Fusion

ROOT = settings.ENVIRONMENTS_DIR
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".bmp": "image/bmp"}
_fusions = {}
_fusion_lock = threading.Lock()


def _error(message, status=400):
    return JsonResponse({"error": str(message)}, status=status)


def _camera_or_404(env, cam_id):
    try:
        return env.camera(cam_id)
    except KeyError:
        raise Http404("No camera '%s'" % cam_id)


# ---------------------------------------------------------------- pages

def index(request):
    from environments.environment import Environment
    envs = Environment.list(ROOT)
    return render(request, "mapping/index.html", {"environments": envs})


def calibrate(request, slug):
    env = load_or_404(slug)
    return render(request, "mapping/calibrate.html", {"env": env, "slug": slug})


def live(request, slug):
    env = load_or_404(slug)
    return render(request, "mapping/live.html", {"env": env, "slug": slug, "classes": feeds.registry().to_list()})


# ---------------------------------------------------------------- overlays

def overlay(env, cam):
    """What the camera should see if its pose is right: reference points and a ground grid (normalised coords)."""
    model = cam.model(env.coordinates)
    out = {"points": env.reprojection(cam.id), "grid": []}
    ground = env.coordinates.ground_z()
    wd = env.world_dict()
    pts = [p["world"] for p in wd["reference_points"]]
    centre = np.mean(pts, axis=0)[:2] if pts else model.C[:2]
    extent = wd["extent_m"]
    step = _nice(extent / 10)
    half = math.ceil(extent / step) * step
    cx, cy = round(centre[0] / step) * step, round(centre[1] / step) * step
    lines = []
    ticks = np.arange(-half, half + step / 2, step)
    for t in ticks:
        lines.append([[cx + t, cy - half, ground], [cx + t, cy + half, ground]])
        lines.append([[cx - half, cy + t, ground], [cx + half, cy + t, ground]])
    for a, b in lines:
        seg = np.linspace(a, b, 60)
        pix, front = model.project(seg)
        poly = []
        for (u, v), f in zip(pix, front):
            if f and -0.5 * model.width < u < 1.5 * model.width and -0.5 * model.height < v < 1.5 * model.height:
                poly.append([round(float(u) / model.width, 4), round(float(v) / model.height, 4)])
            elif len(poly) > 1:
                out["grid"].append(poly)
                poly = []
            else:
                poly = []
        if len(poly) > 1:
            out["grid"].append(poly)
    out["grid_step_m"] = step
    return out


def _nice(x):
    p = 10 ** math.floor(math.log10(max(x, 1e-9)))
    m = x / p
    return (1 if m < 1.5 else 2 if m < 3.5 else 5 if m < 7.5 else 10) * p


def _camera_payload(env, cam):
    return {"camera": cam.to_dict(), "overlay": overlay(env, cam), "environment": payload(env)}


# ---------------------------------------------------------------- calibration API

@require_http_methods(["GET"])
def api_camera(request, slug, cam_id):
    env = load_or_404(slug)
    return JsonResponse(_camera_payload(env, _camera_or_404(env, cam_id)))


@require_http_methods(["PUT"])
def api_points(request, slug, cam_id):
    """Save pixel <-> reference point pairs (normalised) without solving."""
    env = load_or_404(slug)
    cam = _camera_or_404(env, cam_id)
    try:
        data = json.loads(request.body or b"{}")
        cal = Calibration.from_dict({"resolution": data.get("resolution") or cam.calibration.resolution,
                                     "points": data.get("points") or [], "solved": cam.calibration.solved})
        known = {p.id for p in env.reference_points}
        missing = [c.point_id for c in cal.points if c.point_id not in known]
        if missing:
            raise EnvironmentError("Unknown reference point(s): " + ", ".join(missing))
    except (ValueError, EnvironmentError) as exc:
        return _error(exc)
    cam.calibration.points, cam.calibration.resolution = cal.points, cal.resolution
    env.save(ROOT)
    return JsonResponse(_camera_payload(env, cam))


@require_http_methods(["POST"])
def api_solve(request, slug, cam_id):
    """Solve the camera pose (and optionally the field of view) from its pairs, then save."""
    env = load_or_404(slug)
    cam = _camera_or_404(env, cam_id)
    try:
        data = json.loads(request.body or b"{}")
        if "points" in data:
            cal = Calibration.from_dict({"resolution": data.get("resolution") or cam.calibration.resolution,
                                         "points": data["points"]})
            cam.calibration.points, cam.calibration.resolution = cal.points, cal.resolution
        res = cam.calibration.resolution
        note = None
        if res and abs(res[0] / res[1] - cam.resolution[0] / cam.resolution[1]) > 0.01:
            # The image used has another shape than the camera's configured resolution: trust the image.
            note = "Camera resolution changed from %dx%d to %dx%d to match the calibration image." % (
                cam.resolution[0], cam.resolution[1], res[0], res[1])
            cam.resolution = list(res)
        report = env.solve_camera(cam_id, estimate_fov=bool(data.get("estimate_fov")), use_current=not data.get("fresh"))
    except (ValueError, EnvironmentError, KeyError) as exc:
        return _error(exc)
    env.save(ROOT)
    out = _camera_payload(env, cam)
    out["report"] = report
    out["note"] = note
    return JsonResponse(out)


@require_http_methods(["POST"])
def api_reset(request, slug, cam_id):
    env = load_or_404(slug)
    cam = _camera_or_404(env, cam_id)
    cam.calibration.solved = {}
    env.save(ROOT)
    return JsonResponse(_camera_payload(env, cam))


def _image_path(slug, cam_id):
    folder = Path(ROOT) / slug / "calibration"
    for ext in IMAGE_TYPES:
        p = folder / (cam_id + ext)
        if p.exists():
            return p
    return None


@require_http_methods(["GET", "POST", "DELETE"])
def api_image(request, slug, cam_id):
    """A still image to calibrate on, kept in data/environments/<slug>/calibration/."""
    env = load_or_404(slug)
    _camera_or_404(env, cam_id)
    if request.method == "GET":
        p = _image_path(slug, cam_id)
        if p is None:
            raise Http404("No calibration image")
        return FileResponse(open(p, "rb"), content_type=IMAGE_TYPES[p.suffix.lower()])
    if request.method == "DELETE":
        p = _image_path(slug, cam_id)
        if p:
            p.unlink()
        return JsonResponse({"deleted": True})
    upload = request.FILES.get("file")
    ext = Path(upload.name).suffix.lower() if upload else ""
    if upload is None or ext not in IMAGE_TYPES:
        return _error("Upload a JPEG, PNG, WebP or BMP image")
    old = _image_path(slug, cam_id)
    if old:
        old.unlink()
    folder = Path(ROOT) / slug / "calibration"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / (cam_id + ext), "wb") as fh:
        for chunk in upload.chunks():
            fh.write(chunk)
    return JsonResponse({"saved": True}, status=201)


# ---------------------------------------------------------------- live world API

def _fusion(slug):
    with _fusion_lock:
        if slug not in _fusions:
            _fusions[slug] = Fusion(feeds.registry())
        return _fusions[slug]


def api_world_objects(request, slug):
    """All objects in the environment right now, fused across its calibrated cameras."""
    env = load_or_404(slug)
    per_feed, cameras = {}, {}
    for cam in env.cameras:
        feed_id = "%s.%s" % (slug, cam.id)
        info = {"feed": feed_id, "source": cam.source.kind, "calibrated": bool(cam.calibration.solved), "status": "idle"}
        cameras[cam.id] = info
        if cam.source.kind == "none":
            info["status"] = "no source"
            continue
        try:
            w = feeds.get_worker(feed_id)
        except KeyError:
            continue
        latest = w.latest
        info["status"] = latest.get("status")
        info["error"] = latest.get("error")
        info["frame_id"] = latest.get("frame_id")
        info["count"] = len(latest.get("objects") or [])
        if info["calibrated"] and latest.get("status") == "running":
            per_feed[feed_id] = latest.get("objects") or []
    return JsonResponse({"objects": _fusion(slug).fuse(per_feed), "cameras": cameras})
