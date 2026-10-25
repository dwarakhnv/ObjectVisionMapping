"""Remote access (subscriptions + public API) and Saved Recordings."""
import json
import re
import socket
import time

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from simulation.environment import Environment
from vision import feeds

from . import live, recorder, store

ENV_DIR = settings.DATA_DIR / "environments"


def _error(message, status=400):
    return JsonResponse({"error": str(message)}, status=status)


def _env_or_404(slug):
    try:
        return Environment.load(ENV_DIR, slug)
    except (FileNotFoundError, OSError, ValueError):
        raise Http404("No environment '%s'" % slug)


def _class_keys():
    return [c["key"] for c in feeds.registry().to_list()]


def _clean_classes(classes):
    known = set(_class_keys())
    bad = [c for c in classes or [] if c not in known]
    if bad:
        raise ValueError("Unknown object class: " + ", ".join(bad))
    return sorted(set(classes or []))


def _environments():
    return [{"slug": e.slug, "name": e.name, "kind": e.space.kind} for e in Environment.list(ENV_DIR)]


# ================================================================ pages

def remote_page(request):
    return render(request, "remote/remote.html", {"classes": feeds.registry().to_list(), "envs": _environments()})


def recordings_page(request):
    return render(request, "remote/recordings.html", {"classes": feeds.registry().to_list(), "envs": _environments()})


# ================================================================ settings API (used by the Remote access page)

def _base_urls(request):
    """Addresses other machines can use: the one this page was opened on, plus this computer's network addresses."""
    port = request.get_port()
    urls = [request.build_absolute_uri("/").rstrip("/")]
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                u = "http://%s:%s" % (ip, port)
                if u not in urls:
                    urls.append(u)
    except OSError:
        pass
    return urls


@require_http_methods(["GET"])
def api_remote(request, env):
    e = _env_or_404(env)
    activity = live.clients(env)
    subs = []
    for s in store.subscriptions(env):
        s = store.public(s)
        a = activity.get(s["id"], {})
        s.update(last_used=a.get("last"), streams=a.get("streams", 0), active=a.get("active", False))
        subs.append(s)
    cams = [{"id": c.id, "name": c.name, "source": c.source, "located": len(c.reference_points) >= 4} for c in e.cameras]
    return JsonResponse({"environment": {"slug": e.slug, "name": e.name, "map": e.space.to_dict(), "tracking": e.tracking},
                         "subscriptions": subs, "recording": store.recording(env), "cameras": cams,
                         "base_urls": _base_urls(request), "idle_seconds": settings.OVM_FEED_IDLE_SECONDS})


@require_http_methods(["POST"])
def api_subscriptions(request, env):
    _env_or_404(env)
    try:
        body = json.loads(request.body or b"{}")
        sub, token = store.create(body.get("name"), env, _clean_classes(body.get("classes")))
    except ValueError as exc:
        return _error(exc)
    return JsonResponse({"subscription": sub, "token": token}, status=201)


@require_http_methods(["PATCH", "DELETE"])
def api_subscription(request, sub_id):
    try:
        if request.method == "DELETE":
            store.delete(sub_id)
            return JsonResponse({"deleted": sub_id})
        body = json.loads(request.body or b"{}")
        changes = {k: body[k] for k in ("name", "enabled") if k in body}
        if "classes" in body:
            changes["classes"] = _clean_classes(body["classes"])
        return JsonResponse({"subscription": store.update(sub_id, **changes)})
    except KeyError:
        raise Http404("No subscription '%s'" % sub_id)
    except ValueError as exc:
        return _error(exc)


@require_http_methods(["PUT"])
def api_recording_settings(request, env):
    _env_or_404(env)
    try:
        body = json.loads(request.body or b"{}")
        return JsonResponse({"recording": store.set_recording(env, body.get("enabled", True), _clean_classes(body.get("classes")))})
    except ValueError as exc:
        return _error(exc)


# ================================================================ public API (other apps and services)

def _token(request):
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-API-Key") or request.GET.get("token", "")


def _authorize(request, env):
    """(subscription, classes to return) or an error response."""
    if not (ENV_DIR / (env + ".json")).exists() or not re.match(r"^[a-z0-9_]+$", env):
        return None, _error("No environment '%s'" % env, 404)
    token = _token(request)
    if not token:
        return None, _error("Missing API key: send 'Authorization: Bearer <key>' (or ?token=<key>)", 401)
    sub = store.authenticate(token, env)
    if sub is None:
        return None, _error("This key is not valid for this environment (revoked, disabled or for another one)", 403)
    allowed = set(sub["classes"])  # empty = everything
    asked = {c for c in request.GET.get("classes", "").split(",") if c}
    classes = (asked & allowed if allowed else asked) if asked else allowed
    if asked and allowed and not classes:
        return None, _error("This key only gives access to: " + ", ".join(sorted(allowed)), 403)
    return (sub, sorted(classes)), None


def _cors(resp):
    resp["Access-Control-Allow-Origin"] = "*"  # the key, not the browser origin, is what grants access
    resp["Cache-Control"] = "no-store"
    return resp


@require_http_methods(["GET"])
def v1_environment(request, env):
    auth, err = _authorize(request, env)
    if err:
        return _cors(err)
    sub, classes = auth
    e = _env_or_404(env)
    return _cors(JsonResponse({
        "environment": env, "name": e.name, "map": e.space.to_dict() if e.space.kind == "plan" else {"kind": "geo"},
        "tracking": e.tracking, "classes": classes or _class_keys(),
        "cameras": [{"id": c.id, "name": c.name, "located": len(c.reference_points) >= 4} for c in e.cameras if c.source],
        "endpoints": {"objects": "/api/v1/environments/%s/objects" % env, "stream": "/api/v1/environments/%s/stream" % env},
    }))


def _number(request, name, default, low, high):
    try:
        return min(high, max(low, float(request.GET.get(name, default))))
    except ValueError:
        return default


@require_http_methods(["GET"])
def v1_objects(request, env):
    """Current objects. Detection starts on the first call and stops ~30 s after the last one.

    ?after=<seq>&wait=<s>: wait (up to 25 s) for a result newer than `seq` - long polling.
    ?predict=1: move moving objects ahead to now along their heading.
    """
    auth, err = _authorize(request, env)
    if err:
        return _cors(err)
    sub, classes = auth
    live.touch_client(env, sub)
    after = int(_number(request, "after", 0, 0, 1e12))
    wait = _number(request, "wait", 0, 0, 25)
    pic = live.wait_update(env, after, wait) if wait else live.current(env)
    return _cors(JsonResponse(live.view(pic, env, set(classes), predict=request.GET.get("predict") in ("1", "true"))))


@require_http_methods(["GET"])
def v1_stream(request, env):
    """Server-Sent Events: an "objects" event the moment detections update.

    ?hz=<1-30>: instead, an event at that steady rate with moving objects predicted ahead to now.
    """
    auth, err = _authorize(request, env)
    if err:
        return _cors(err)
    sub, classes = auth
    wanted = set(classes)
    hz = _number(request, "hz", 0, 0, 30)

    def events():
        live.touch_client(env, sub, +1)
        try:
            yield "retry: 3000\n\n"
            last_sent, last_key, seq, tick = 0.0, None, 0, time.monotonic()
            while True:
                if hz:
                    tick = max(tick + 1.0 / hz, time.monotonic())
                    time.sleep(max(0.0, tick - time.monotonic()))
                    pic = live.current(env)  # also keeps detection running
                else:
                    pic = live.wait_update(env, seq, 5.0)
                seq = pic["seq"]
                snap = live.view(pic, env, wanted, predict=bool(hz))
                now = time.time()
                key = json.dumps([snap["objects"], snap["cameras"]])
                if key != last_key:
                    last_key, last_sent = key, now
                    yield "event: objects\ndata: %s\n\n" % json.dumps(snap)
                elif now - last_sent > 15:
                    last_sent = now
                    yield ": still here\n\n"  # keeps proxies from closing the connection
                live.touch_client(env, sub)
        finally:
            live.touch_client(env, sub, -1)  # the client went away

    resp = StreamingHttpResponse(events(), content_type="text/event-stream")
    resp["X-Accel-Buffering"] = "no"
    return _cors(resp)


# ================================================================ recordings

@require_http_methods(["GET"])
def api_recordings(request, env):
    _env_or_404(env)
    recs = recorder.list_recordings(env)
    return JsonResponse({"recordings": recs, "total_bytes": sum(r["bytes"] for r in recs), "settings": store.recording(env)})


@require_http_methods(["GET", "DELETE"])
def api_recording(request, env, rec_id):
    if request.method == "DELETE":
        try:
            recorder.delete_recording(env, rec_id)
        except FileNotFoundError:
            raise Http404("No recording '%s'" % rec_id)
        return JsonResponse({"deleted": rec_id})
    p = recorder.recording_path(env, rec_id, "meta")
    if p is None:
        raise Http404("No recording '%s'" % rec_id)
    return HttpResponse(p.read_bytes(), content_type="application/json")


def api_recording_thumb(request, env, rec_id):
    p = recorder.recording_path(env, rec_id, "thumb")
    if p is None:
        raise Http404("No thumbnail")
    return FileResponse(open(p, "rb"), content_type="image/jpeg")


def api_recording_video(request, env, rec_id):
    """The video, with HTTP Range support so the player can seek."""
    p = recorder.recording_path(env, rec_id, "video")
    if p is None:
        raise Http404("No video")
    size = p.stat().st_size
    m = re.match(r"bytes=(\d*)-(\d*)", request.headers.get("Range", ""))
    if not m:
        resp = FileResponse(open(p, "rb"), content_type="video/webm")
        resp["Accept-Ranges"] = "bytes"
        return resp
    start = int(m.group(1)) if m.group(1) else max(0, size - int(m.group(2) or 0))
    end = min(int(m.group(2)), size - 1) if m.group(1) and m.group(2) else size - 1
    if start >= size or start > end:
        resp = HttpResponse(status=416)
        resp["Content-Range"] = "bytes */%d" % size
        return resp
    with open(p, "rb") as fh:
        fh.seek(start)
        data = fh.read(end - start + 1)
    resp = HttpResponse(data, status=206, content_type="video/webm")
    resp["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
    resp["Accept-Ranges"] = "bytes"
    return resp
