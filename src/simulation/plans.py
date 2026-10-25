"""Floor plans: indoor maps for environments, drawn in the plan editor or made from an image.

A plan is a JSON file at data/plans/<slug>.json:

    {"name": "Office", "units": "ft", "width": 60, "height": 40,
     "background": {"file": "office.png", "x": 0, "y": 0, "width": 60, "height": 40},   # optional image
     "shapes": [{"type": "wall", "points": [[0, 0], [60, 0]], "thickness": 0.5}, ...]}

Coordinates are in the plan's units, x to the right and y down, origin at the
top-left. Shapes:

    wall    points (a polyline), thickness
    room    points (a polygon), name
    door    points (2: hinge, then the other end of the opening)
    window  points (2)
    box     points (2: opposite corners), name   e.g. furniture, shelving, a counter
    label   points (1), text
"""
import json
import math
import re
from pathlib import Path

from django.conf import settings

UNITS = {"m": 1.0, "cm": 0.01, "mm": 0.001, "ft": 0.3048, "in": 0.0254, "yd": 0.9144}
SHAPES = {"wall": (2, None), "room": (3, None), "door": (2, 2), "window": (2, 2), "box": (2, 2), "label": (1, 1)}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}  # no SVG: it can carry scripts
MAX_SHAPES = 5000


def plans_dir():
    return Path(settings.DATA_DIR) / "plans"


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
    return s[:64] or "plan"


def _num(v, what):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number" % what)
    if not math.isfinite(f):
        raise ValueError("%s must be a number" % what)
    return f


def validate(d):
    """A clean copy of a plan dict, or ValueError explaining what's wrong."""
    name = str(d.get("name") or "").strip()[:80]
    if not name:
        raise ValueError("The floor plan needs a name")
    units = d.get("units") or "m"
    if units not in UNITS:
        raise ValueError("Units must be one of: " + ", ".join(UNITS))
    width, height = _num(d.get("width"), "Width"), _num(d.get("height"), "Height")
    if width <= 0 or height <= 0:
        raise ValueError("Width and height must be more than 0")
    plan = {"name": name, "units": units, "width": width, "height": height, "shapes": []}
    bg = d.get("background")
    if bg:
        f = Path(str(bg.get("file") or "")).name
        if not f or Path(f).suffix.lower() not in IMAGE_TYPES:
            raise ValueError("Background image is missing or not a supported image")
        plan["background"] = {"file": f, "x": _num(bg.get("x", 0), "Image x"), "y": _num(bg.get("y", 0), "Image y"),
                              "width": _num(bg.get("width"), "Image width"), "height": _num(bg.get("height"), "Image height"),
                              "opacity": min(1.0, max(0.1, _num(bg.get("opacity", 1), "Image opacity")))}
        if plan["background"]["width"] <= 0 or plan["background"]["height"] <= 0:
            raise ValueError("The background image size must be more than 0")
    shapes = d.get("shapes") or []
    if len(shapes) > MAX_SHAPES:
        raise ValueError("Too many shapes (%d); the limit is %d" % (len(shapes), MAX_SHAPES))
    for i, sh in enumerate(shapes):
        kind = sh.get("type")
        if kind not in SHAPES:
            raise ValueError("Shape %d has an unknown type '%s'" % (i + 1, kind))
        lo, hi = SHAPES[kind]
        pts = [[_num(p[0], "x"), _num(p[1], "y")] for p in sh.get("points") or []]
        if len(pts) < lo or (hi and len(pts) > hi):
            raise ValueError("A %s needs %s point%s" % (kind, lo if lo == hi else "%d or more" % lo, "" if lo == 1 else "s"))
        clean = {"type": kind, "points": pts}
        if kind == "wall":
            clean["thickness"] = max(0.0, _num(sh.get("thickness", default_thickness(units)), "Wall thickness"))
        if kind in ("room", "box"):
            clean["name"] = str(sh.get("name") or "")[:80]
        if kind == "label":
            clean["text"] = str(sh.get("text") or "")[:120]
        plan["shapes"].append(clean)
    return plan


def default_thickness(units):
    """A typical interior wall (~15 cm) in these units."""
    return round(0.15 / UNITS[units], 3)


def load(slug):
    if slugify(slug) != slug:
        raise FileNotFoundError(slug)
    with open(plans_dir() / (slug + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def save(plan, slug=None):
    """Validate and write a plan; returns (slug, clean plan)."""
    clean = validate(plan)
    slug = slug or slugify(clean["name"])
    folder = plans_dir()
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (slug + ".json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(clean, fh, indent=2)
    tmp.replace(folder / (slug + ".json"))
    return slug, clean


def delete(slug):
    plan = load(slug)
    (plans_dir() / (slug + ".json")).unlink()
    bg = plan.get("background")
    if bg and not any(p.get("background", {}).get("file") == bg["file"] for _, p in all_plans()):
        (plans_dir() / bg["file"]).unlink(missing_ok=True)


def all_plans():
    out = []
    folder = plans_dir()
    for f in sorted(folder.glob("*.json")) if folder.exists() else []:
        try:
            out.append((f.stem, load(f.stem)))
        except (OSError, ValueError):
            continue
    return out


def summary(slug):
    """Size and units of a plan (what an environment needs), or None if it's gone."""
    try:
        p = load(slug)
    except (OSError, ValueError):
        return None
    return {"slug": slug, "name": p["name"], "units": p["units"], "width": p["width"], "height": p["height"],
            "meters_per_unit": UNITS[p["units"]]}


def save_image(upload):
    """Store an uploaded background image; returns its file name."""
    ext = Path(upload.name).suffix.lower()
    if ext not in IMAGE_TYPES:
        raise ValueError("Upload a PNG, JPEG, WebP or GIF image")
    folder = plans_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stem = slugify(Path(upload.name).stem)
    target, n = folder / (stem + ext), 1
    while target.exists():
        n += 1
        target = folder / ("%s_%d%s" % (stem, n, ext))
    with open(target, "wb") as fh:
        for chunk in upload.chunks():
            fh.write(chunk)
    return target.name
