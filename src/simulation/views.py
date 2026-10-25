"""Pages and JSON API for cameras (live view) and environments (reference points -> geo)."""
import json
import math

import cv2
from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from shapely.geometry import Point

from . import camera as cameras
from . import plans
from .environment import Environment, GeoSpace

JPEG_QUALITY = 80
ENV_DIR = settings.DATA_DIR / "environments"


def _error(message, status=400):
    return JsonResponse({"error": str(message)}, status=status)


# ================================================================ cameras

def _camera(key):
    cam = cameras.shared_cameras().get(key)
    if cam is None:
        raise Http404("No camera '%s' in config/secrets.json" % key)
    return cam


def _info(cam):
    backend = "FFmpeg (RTSP over TCP)" if cam.kind == "network" else "Local device"
    return {"id": cam.key, "name": cam.name, "kind": cam.kind, "url": cam.safe_url, "backend": backend}


def home(request):
    """Overview: what OVM does, and how far this installation is through setting it up."""
    from remote import store

    envs = Environment.list(ENV_DIR)
    env_cams = [c for e in envs for c in e.cameras]
    located = [c for c in env_cams if len(c.reference_points) >= 4]
    keys = store.subscriptions()
    plan_count = sum(1 for _ in plans.all_plans())
    steps = [
        {"title": "Connect a camera", "url": "/cameras/", "link": "Cameras",
         "done": bool(cameras.load_camera_configs()),
         "status": "%d network camera(s) in config/secrets.json" % len(cameras.load_camera_configs()),
         "text": "Add network (RTSP) cameras such as a Tapo C100 to config/secrets.json under \"cameras\": host, "
                 "username, password and stream. USB, built-in and virtual cameras are found automatically."},
        {"title": "Draw a floor plan (indoors only)", "url": "/plans/", "link": "Floor plans", "optional": True,
         "done": plan_count > 0, "status": "%d floor plan(s)" % plan_count,
         "text": "For indoor spaces, draw walls, rooms and doors to scale, or trace over an uploaded image. "
                 "Outdoor environments use the satellite map instead."},
        {"title": "Create an environment and add its cameras", "url": "/environments/", "link": "Environments",
         "done": bool(env_cams), "status": "%d environment(s), %d camera(s)" % (len(envs), len(env_cams)),
         "text": "An environment is one place (a street, a warehouse floor) seen by one or more cameras. "
                 "Objects seen by several of its cameras are merged into one."},
        {"title": "Place each camera with reference points", "url": "/environments/", "link": "Environments",
         "done": bool(env_cams) and len(located) == len(env_cams),
         "status": "%d of %d camera(s) placed" % (len(located), len(env_cams)),
         "text": "Click a spot on the ground in the video, then the same spot on the map. With 4 or more points "
                 "every detection gets a real location, and the camera's position, height and angle are estimated."},
        {"title": "Watch detection live", "url": "/detect/", "link": "Detect", "done": None,
         "status": "", "text": "Pick an environment and camera to see people and vehicles outlined on the live video "
                               "and placed on the map or in 3D."},
        {"title": "Connect your apps", "url": "/remote/", "link": "Remote access",
         "done": bool(keys), "status": "%d access key(s)" % len(keys),
         "text": "Create an access key per app, optionally limited to some object classes, and poll or stream "
                 "the environment's objects. Recordings of activity can be switched on there too."},
    ]
    return render(request, "simulation/overview.html", {
        "steps": steps, "envs": envs, "located": len(located), "env_cams": len(env_cams),
        "tracking_on": [e.name for e in envs if e.tracking],
        "settings": [("OVM_DETECTOR", settings.OVM_DETECTOR or "auto", "Detection model, e.g. yolo26s-seg, yolo11n-seg"),
                     ("OVM_DEVICE", settings.OVM_DEVICE, "auto, intel:gpu, intel:cpu, cpu, cuda:0"),
                     ("OVM_IMGSZ", settings.OVM_IMGSZ, "Detection size: larger finds smaller, farther objects but is slower"),
                     ("OVM_MAX_FPS", settings.OVM_MAX_FPS, "Most detections per second per camera"),
                     ("OVM_FEED_IDLE_SECONDS", settings.OVM_FEED_IDLE_SECONDS, "Detection stops this long after nobody asks")],
    })


def cameras_page(request):
    return render(request, "simulation/home.html")


def api_cameras(request):
    return JsonResponse({"cameras": [dict(_info(c), streaming=c.is_running(), error=c.error)
                                     for c in cameras.shared_cameras().values()]})


def api_camera(request, camera_id):
    cam = _camera(camera_id)
    st = cam.status()
    flips = [n for n, on in (("vertical", st["v_flipped"]), ("horizontal", st["h_flipped"])) if on]
    out = {"info": _info(cam), "streaming": st["running"], "viewers": st["viewers"], "error": st["error"],
           "controls": {"flip": " + ".join(flips) if flips else "none"}}
    if st["width"]:
        g = math.gcd(st["width"], st["height"])
        out["format"] = {"width": st["width"], "height": st["height"], "aspect": "%d:%d" % (st["width"] // g, st["height"] // g),
                         "fps_measured": st["fps"], "fps_reported": -1,
                         "fourcc": "H.264" if cam.kind == "network" else "MJPG", "backend": _info(cam)["backend"]}
    return JsonResponse(out)


def api_check(request, camera_id):
    ok, message = _camera(camera_id).check()
    return JsonResponse({"ok": ok, "message": message})


def api_stream(request, camera_id):
    """MJPEG: an <img> pointed here shows the camera live."""
    _camera(camera_id)  # 404 for unknown cameras
    return mjpeg_response(camera_id)


def mjpeg_response(camera_id):
    """Every new frame of a shared camera as MJPEG, at the camera's own frame rate."""
    def frames():
        cam = cameras.acquire(camera_id)
        try:
            last = 0
            while True:
                frame_id, frame = cam.latest(last, timeout=10)
                if frame_id == last or frame is None:
                    if not cam.is_running():
                        break
                    continue  # still connecting / reconnecting
                last = frame_id
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                if ok:
                    jpeg = buf.tobytes()
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpeg)) + jpeg + b"\r\n"
        finally:
            cameras.release(cam)

    resp = StreamingHttpResponse(frames(), content_type="multipart/x-mixed-replace; boundary=frame")
    resp["Cache-Control"] = "no-store"
    return resp


def api_snapshot(request, camera_id):
    _camera(camera_id)
    cam = cameras.acquire(camera_id)
    try:
        _, frame = cam.latest(0, timeout=15)
    finally:
        cameras.release(cam)
    if frame is None:
        return _error(cam.error or "No frame from the camera yet", 503)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return HttpResponse(buf.tobytes(), content_type="image/jpeg")


@require_http_methods(["POST"])
def api_resolution(request, camera_id):
    return _error("Pick stream1 (full) or stream2 (low) in config/secrets.json")


# ================================================================ environments

def environments_page(request):
    return render(request, "simulation/environments.html")


def analyse(env):
    """Per-camera accuracy and estimated pose, plus a world view (metres) for the 3D display.

    Outdoors, world metres are measured from the middle of all reference points;
    on a floor plan they are the plan's own (x right = east, y down = south).
    """
    points = [rp.point for cam in env.cameras for rp in cam.reference_points]
    if env.space.kind == "geo":
        space = GeoSpace()
        if points:
            space.prepare(points)
    else:
        space = env.space
    info, world_cams, world_pts = [], [], []
    for cam in env.cameras:
        item = {"id": cam.id, "name": cam.name, "source": cam.source, "points": len(cam.reference_points),
                "resolution": list(cam.resolution)}
        for i, rp in enumerate(cam.reference_points):
            e, n = space.to_local(rp.point)
            label = rp.name or "P%d" % (i + 1)
            world_pts.append({"id": "%s:%d" % (cam.id, i), "name": label if len(env.cameras) == 1 else "%s %s" % (cam.name, label),
                              "world": [e, n, 0.0]})
        if len(cam.reference_points) < 4:
            item["error"] = "Needs at least 4 reference points (has %d)" % len(cam.reference_points)
        else:
            try:
                item["accuracy_m"] = cam.accuracy()
                item["errors_m"] = cam.errors_m
                pose = cam.estimate_pose()
                item["pose"] = pose
                where = Point(pose["lon"], pose["lat"]) if env.space.kind == "geo" else Point(pose["x"], pose["y"])
                e, n = space.to_local(where)
                w, h = cam.resolution
                vfov = math.degrees(2 * math.atan(math.tan(math.radians(pose["hfov"]) / 2) * h / w))
                world_cams.append({"id": cam.id, "name": cam.name, "world": [e, n, pose["height_m"]], "ground": [e, n, 0.0],
                                   "heading": pose["heading"], "tilt": pose["tilt"], "roll": pose["roll"],
                                   "hfov": pose["hfov"], "vfov": vfov, "resolution": [w, h]})
            except ValueError as exc:
                item["error"] = str(exc)
        info.append(item)
    out = {"cameras": info, "world": None, "origin": None, "space": env.space.to_dict(), "plan": None}
    spread = [math.hypot(*p["world"][:2]) for p in world_pts] + [math.hypot(*c["world"][:2]) for c in world_cams]
    if env.space.kind == "plan":
        plan = plans.summary(env.space.plan)
        out["plan"] = plan
        size_m = max(plan["width"], plan["height"]) * env.space.meters_per_unit if plan else 0.0
        out["world"] = {"ground": 0.0, "extent_m": max(5.0, size_m * 0.75, *(spread or [0])),
                        "reference_points": world_pts, "cameras": world_cams}
    elif points:
        out["origin"] = {"lat": space.origin[0], "lon": space.origin[1]}
        out["world"] = {"ground": 0.0, "extent_m": max(30.0, 1.4 * max(spread)), "reference_points": world_pts,
                        "cameras": world_cams}
    return out


def _with_plan(data):
    """Indoor environments take their units from their floor plan (so they stay in step with it)."""
    space = data.get("space") or {}
    if space.get("kind") == "plan":
        info = plans.summary(str(space.get("plan") or ""))
        if info is None:
            raise ValueError("Pick a floor plan for this indoor environment")
        data["space"] = {"kind": "plan", "plan": info["slug"], "units": info["units"],
                         "meters_per_unit": info["meters_per_unit"]}
    return data


def _env_payload(env):
    return {"environment": env.to_dict(), "slug": env.slug, "analysis": analyse(env)}


def _load_env(slug):
    try:
        return Environment.load(ENV_DIR, slug)
    except (FileNotFoundError, OSError):
        raise Http404("No environment '%s'" % slug)


@require_http_methods(["GET", "POST"])
def api_envs(request):
    if request.method == "GET":
        return JsonResponse({"environments": [{"slug": e.slug, "name": e.name, "cameras": len(e.cameras)}
                                              for e in Environment.list(ENV_DIR)]})
    try:
        env = Environment.from_dict(_with_plan(json.loads(request.body or b"{}")))
    except (ValueError, KeyError, TypeError) as exc:
        return _error(exc)
    if (ENV_DIR / (env.slug + ".json")).exists():
        return _error("An environment called '%s' already exists" % env.name, 409)
    env.save(ENV_DIR)
    return JsonResponse(_env_payload(env), status=201)


@require_http_methods(["GET", "PUT", "DELETE"])
def api_env(request, slug):
    env = _load_env(slug)
    if request.method == "GET":
        return JsonResponse(_env_payload(env))
    if request.method == "DELETE":
        (ENV_DIR / (slug + ".json")).unlink()
        return JsonResponse({"deleted": slug})
    try:
        new = Environment.from_dict(_with_plan(json.loads(request.body or b"{}")))
    except (ValueError, KeyError, TypeError) as exc:
        return _error(exc)
    if new.slug != slug:  # renamed
        if (ENV_DIR / (new.slug + ".json")).exists():
            return _error("An environment called '%s' already exists" % new.name, 409)
        (ENV_DIR / (slug + ".json")).unlink()
    new.save(ENV_DIR)
    return JsonResponse(_env_payload(new))


# ================================================================ floor plans

def plans_page(request):
    return render(request, "simulation/plans.html", {"units": list(plans.UNITS)})


@require_http_methods(["GET", "POST"])
def api_plans(request):
    if request.method == "GET":
        return JsonResponse({"plans": [dict(plans.summary(slug), shapes=len(p.get("shapes", [])),
                                            background=bool(p.get("background")))
                                       for slug, p in plans.all_plans()]})
    try:
        data = json.loads(request.body or b"{}")
        slug = plans.slugify(data.get("name", ""))
        if (plans.plans_dir() / (slug + ".json")).exists():
            return _error("A floor plan called '%s' already exists" % data.get("name"), 409)
        slug, plan = plans.save(data, slug)
    except (ValueError, TypeError, KeyError) as exc:
        return _error(exc)
    return JsonResponse({"slug": slug, "plan": plan}, status=201)


@require_http_methods(["GET", "PUT", "DELETE"])
def api_plan(request, slug):
    try:
        plan = plans.load(slug)
    except (FileNotFoundError, OSError):
        raise Http404("No floor plan '%s'" % slug)
    if request.method == "GET":
        return JsonResponse({"slug": slug, "plan": plan, "used_by": _plan_users(slug)})
    if request.method == "DELETE":
        users = _plan_users(slug)
        if users:
            return _error("This floor plan is used by: %s. Remove it from those environments first." % ", ".join(users), 409)
        plans.delete(slug)
        return JsonResponse({"deleted": slug})
    try:
        _, plan = plans.save(json.loads(request.body or b"{}"), slug)  # the slug stays even if renamed
    except (ValueError, TypeError, KeyError) as exc:
        return _error(exc)
    return JsonResponse({"slug": slug, "plan": plan, "used_by": _plan_users(slug)})


def _plan_users(slug):
    return [e.name for e in Environment.list(ENV_DIR) if e.space.kind == "plan" and e.space.plan == slug]


@require_http_methods(["POST"])
def api_plan_image_upload(request):
    upload = request.FILES.get("file")
    if upload is None:
        return _error("Choose an image to upload")
    if upload.size > 30 * 1024 * 1024:
        return _error("The image is too large (30 MB maximum)")
    try:
        name = plans.save_image(upload)
    except ValueError as exc:
        return _error(exc)
    return JsonResponse({"file": name}, status=201)


def api_plan_image(request, name):
    from pathlib import Path

    name = Path(name).name
    path = plans.plans_dir() / name
    ext = path.suffix.lower()
    if ext not in plans.IMAGE_TYPES or not path.is_file():
        raise Http404("No image '%s'" % name)
    resp = HttpResponse(path.read_bytes(), content_type=plans.IMAGE_TYPES[ext])
    resp["Cache-Control"] = "private, max-age=3600"
    resp["X-Content-Type-Options"] = "nosniff"
    return resp
