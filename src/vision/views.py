"""Home page (live camera feed with detection) and the feeds / sources API."""
from pathlib import Path

from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from . import feeds
from .sources import IMAGE_EXT, VIDEO_EXT, list_sources, safe_name


def home(request):
    return render(request, "vision/home.html", {"classes": feeds.registry().to_list()})


def _worker_or_404(feed_id, start=True):
    try:
        w = feeds.get_worker(feed_id, start=start)
    except KeyError:
        raise Http404("No feed '%s'" % feed_id)
    if w is None:
        raise Http404("Feed '%s' is not running" % feed_id)
    return w


def api_feeds(request):
    return JsonResponse({"feeds": feeds.list_feeds()})


def api_feed_latest(request, feed_id):
    """Latest analysis. ?after=<frame_id> waits (up to ~1.5 s) for a newer frame."""
    w = _worker_or_404(feed_id)
    try:
        after = int(request.GET.get("after", "0"))
    except ValueError:
        after = 0
    return JsonResponse(w.wait_for(after))


def api_feed_video(request, feed_id):
    """The feed's camera live (MJPEG), at full frame rate; detections are drawn over it in the browser."""
    from simulation.views import mjpeg_response

    key = feeds.video_camera(feed_id)
    if key is None:
        raise Http404("No live video for '%s' (files and stills come frame by frame)" % feed_id)
    return mjpeg_response(key)


def api_feed_frame(request, feed_id, frame_id):
    w = _worker_or_404(feed_id, start=False)
    data = w.jpeg(int(frame_id))
    if data is None:
        raise Http404("Frame no longer available")
    resp = HttpResponse(data, content_type="image/jpeg")
    resp["Cache-Control"] = "private, max-age=60"
    return resp


def api_feed_snapshot(request, feed_id):
    """Newest frame (starts the feed if needed and waits briefly for a first frame)."""
    w = _worker_or_404(feed_id)
    latest = w.wait_for(0, timeout=20)
    data = w.snapshot()
    if data is None:
        return JsonResponse({"error": latest.get("error") or "No frame yet - try again in a moment"}, status=503)
    resp = HttpResponse(data, content_type="image/jpeg")
    resp["Cache-Control"] = "no-store"
    resp["X-Frame-Size"] = "%sx%s" % (latest.get("width"), latest.get("height"))
    return resp


@require_http_methods(["POST"])
def api_feed_stop(request, feed_id):
    feeds.stop_worker(feed_id)
    return JsonResponse({"stopped": feed_id})


@require_http_methods(["GET", "POST"])
def api_sources(request):
    if request.method == "GET":
        return JsonResponse({"sources": list_sources()})
    upload = request.FILES.get("file")
    if upload is None:
        return JsonResponse({"error": "Choose a file to upload"}, status=400)
    name = safe_name(upload.name)
    ext = Path(name).suffix.lower()
    if ext not in VIDEO_EXT and ext not in IMAGE_EXT:
        return JsonResponse({"error": "Unsupported file type %s" % ext}, status=400)
    if upload.size > settings.MAX_SOURCE_UPLOAD_BYTES:
        return JsonResponse({"error": "File is too large"}, status=400)
    folder = Path(settings.SOURCES_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    stem, n = target.stem, 1
    while target.exists():
        n += 1
        target = folder / ("%s_%d%s" % (stem, n, ext))
    with open(target, "wb") as fh:
        for chunk in upload.chunks():
            fh.write(chunk)
    return JsonResponse({"name": target.name, "feed": "~" + target.name}, status=201)


def api_object_classes(request):
    return JsonResponse({"classes": feeds.registry().to_list()})


def api_detector(request):
    try:
        return JsonResponse(feeds.detector().info())
    except RuntimeError as exc:
        return JsonResponse({"error": str(exc)}, status=503)
