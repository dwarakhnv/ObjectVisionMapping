"""Environment pages (/environments/...) and JSON API (/api/environments/...)."""
import json

import numpy as np
from django.conf import settings
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .environment import CoordinateSystem, Environment, EnvironmentError, slugify

ROOT = settings.ENVIRONMENTS_DIR
RESERVED = {"preview", "convert", "new"}


def load_or_404(slug):
    try:
        return Environment.load(ROOT, slug)
    except (FileNotFoundError, OSError, KeyError):
        raise Http404("No environment '%s'" % slug)


def _body(request):
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        raise EnvironmentError("request body must be JSON")


def _error(message, status=400):
    return JsonResponse({"error": str(message)}, status=status)


def payload(env):
    return {"environment": env.to_dict(), "world": env.world_dict()}


def summary(env):
    cs = env.coordinates
    return {"slug": env.slug, "name": env.name, "description": env.description, "kind": cs.kind,
            "units": cs.units if cs.kind == "local" else "lat/lon, m",
            "reference_points": len(env.reference_points), "cameras": len(env.cameras),
            "calibrated": sum(1 for c in env.cameras if c.calibration.solved), "updated": env.updated}


# ---------------------------------------------------------------- pages

def environment_list(request):
    return render(request, "environments/list.html",
                  {"environments": [summary(e) for e in Environment.list(ROOT)]})


def environment_detail(request, slug):
    env = load_or_404(slug)
    return render(request, "environments/detail.html", {"env": env, "slug": env.slug})


# ---------------------------------------------------------------- API

@require_http_methods(["GET", "POST"])
def api_environments(request):
    if request.method == "GET":
        return JsonResponse({"environments": [summary(e) for e in Environment.list(ROOT)]})
    try:
        data = _body(request)
        data.setdefault("slug", slugify(data.get("name", "")))
        if data["slug"] in RESERVED:
            return _error("'%s' is a reserved name; please pick another" % data["slug"])
        if (ROOT / data["slug"] / "environment.json").exists():
            return _error("An environment called '%s' already exists" % data["slug"], 409)
        env = Environment.from_dict(data)
    except EnvironmentError as exc:
        return _error(exc)
    env.save(ROOT)
    return JsonResponse(payload(env), status=201)


@require_http_methods(["GET", "PUT", "DELETE"])
def api_environment(request, slug):
    env = load_or_404(slug)
    if request.method == "GET":
        return JsonResponse(payload(env))
    if request.method == "DELETE":
        Environment.delete(ROOT, slug)
        return JsonResponse({"deleted": slug})
    try:
        data = _body(request)
        data["slug"] = slug
        data["created"] = env.created
        new = Environment.from_dict(data)
    except EnvironmentError as exc:
        return _error(exc)
    new.save(ROOT)
    return JsonResponse(payload(new))


@require_http_methods(["POST"])
def api_preview(request):
    """Validate an environment without saving it and return its world-space view."""
    try:
        data = _body(request)
        data.setdefault("slug", "preview")
        env = Environment.from_dict(data)
    except EnvironmentError as exc:
        return _error(exc)
    return JsonResponse(payload(env))


@require_http_methods(["POST"])
def api_convert(request):
    """Convert between positions and world metres: {coordinates, world:[x,y,z]} or {coordinates, position}."""
    try:
        data = _body(request)
        cs = CoordinateSystem.from_dict(data.get("coordinates"))
        if "world" in data:
            w = np.asarray([float(v) for v in data["world"]][:3])
            return JsonResponse({"position": cs.from_world(w)})
        pos = cs.parse_position(data.get("position"))
        return JsonResponse({"world": cs.to_world(pos).tolist()})
    except (EnvironmentError, TypeError, ValueError) as exc:
        return _error(exc)
