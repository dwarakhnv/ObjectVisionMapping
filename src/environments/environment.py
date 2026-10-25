"""The Environment: a 3D space with reference points and fixed cameras.

An environment has a coordinate system that is either

* ``geo``   - positions are latitude/longitude (degrees) with an altitude in
              metres; an origin (lat, lon, alt) anchors a local frame, or
* ``local`` - positions are x (east), y (north), z (up) in custom units
              (metres, feet, "grid squares" ...), with ``meters_per_unit``.

Internally every position is converted to a local East-North-Up frame in
metres (``world`` coordinates). The ground is the plane z = ``ground``
(metres, default 0), which is where detected objects are placed.

Everything is stored as JSON at ``data/environments/<slug>/environment.json``.
"""
import json
import math
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import geometry

SCHEMA_VERSION = 1
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SOURCE_KINDS = ("none", "file", "url", "device", "image")

# WGS84 ellipsoid
_A = 6378137.0
_E2 = 6.69437999014e-3


class EnvironmentError(ValueError):
    """Invalid environment data."""


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
    return s[:64] or "environment"


def _num(value, name, default=None, lo=None, hi=None):
    if value is None or value == "":
        if default is None:
            raise EnvironmentError("%s is required" % name)
        return float(default)
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise EnvironmentError("%s must be a number" % name)
    if not math.isfinite(v):
        raise EnvironmentError("%s must be finite" % name)
    if lo is not None and v < lo or hi is not None and v > hi:
        raise EnvironmentError("%s must be between %s and %s" % (name, lo, hi))
    return v


def _ident(value, name):
    s = str(value or "").strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", s):
        raise EnvironmentError("%s id '%s' must be letters, digits, '_', '-' or '.'" % (name, s))
    return s


# ---------------------------------------------------------------- coordinates

@dataclass
class CoordinateSystem:
    """Converts environment positions to local ENU metres and back."""

    kind: str = "local"            # "geo" or "local"
    units: str = "m"               # name of the custom unit (local only)
    meters_per_unit: float = 1.0   # local only
    origin: dict = field(default_factory=lambda: {"lat": 0.0, "lon": 0.0, "alt": 0.0})  # geo only
    ground: float = 0.0            # ground level, in position units (geo: metres altitude)

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        kind = d.get("kind", "local")
        if kind not in ("geo", "local"):
            raise EnvironmentError("coordinate system must be 'geo' or 'local'")
        cs = cls(kind=kind)
        if kind == "geo":
            o = d.get("origin") or {}
            cs.origin = {"lat": _num(o.get("lat"), "origin latitude", None, -90, 90),
                         "lon": _num(o.get("lon"), "origin longitude", None, -180, 180),
                         "alt": _num(o.get("alt"), "origin altitude", 0.0)}
            cs.units, cs.meters_per_unit = "m", 1.0
            cs.ground = _num(d.get("ground"), "ground", cs.origin["alt"])
        else:
            cs.units = str(d.get("units") or "m")[:24]
            cs.meters_per_unit = _num(d.get("meters_per_unit"), "meters per unit", 1.0, 1e-6, 1e6)
            cs.ground = _num(d.get("ground"), "ground", 0.0)
        return cs

    def to_dict(self):
        if self.kind == "geo":
            return {"kind": "geo", "origin": dict(self.origin), "ground": self.ground}
        return {"kind": "local", "units": self.units, "meters_per_unit": self.meters_per_unit,
                "ground": self.ground}

    # A position is {"x","y","z"} (local) or {"lat","lon","alt"} (geo).
    def parse_position(self, p, what="position"):
        p = p or {}
        if self.kind == "geo":
            return {"lat": _num(p.get("lat"), what + " latitude", None, -90, 90),
                    "lon": _num(p.get("lon"), what + " longitude", None, -180, 180),
                    "alt": _num(p.get("alt"), what + " altitude", self.ground)}
        return {"x": _num(p.get("x"), what + " x", None), "y": _num(p.get("y"), what + " y", None),
                "z": _num(p.get("z"), what + " z", self.ground)}

    def ground_z(self):
        """Ground plane height in world metres."""
        if self.kind == "geo":
            return self.ground - self.origin["alt"]
        return self.ground * self.meters_per_unit

    def height_to_m(self, h):
        """A height/length in position units -> metres."""
        return h * (1.0 if self.kind == "geo" else self.meters_per_unit)

    def to_world(self, p):
        """Position dict -> np.array([east, north, up]) in metres."""
        if self.kind == "local":
            k = self.meters_per_unit
            return np.array([p["x"] * k, p["y"] * k, p["z"] * k])
        return _geodetic_to_enu(p["lat"], p["lon"], p["alt"], self.origin)

    def from_world(self, w):
        """np.array([east, north, up]) metres -> position dict."""
        if self.kind == "local":
            k = self.meters_per_unit
            return {"x": float(w[0]) / k, "y": float(w[1]) / k, "z": float(w[2]) / k}
        lat, lon, alt = _enu_to_geodetic(w, self.origin)
        return {"lat": lat, "lon": lon, "alt": alt}


def _ecef(lat, lon, alt):
    la, lo = math.radians(lat), math.radians(lon)
    n = _A / math.sqrt(1 - _E2 * math.sin(la) ** 2)
    return np.array([(n + alt) * math.cos(la) * math.cos(lo),
                     (n + alt) * math.cos(la) * math.sin(lo),
                     (n * (1 - _E2) + alt) * math.sin(la)])


def _enu_basis(lat, lon):
    la, lo = math.radians(lat), math.radians(lon)
    e = np.array([-math.sin(lo), math.cos(lo), 0.0])
    n = np.array([-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)])
    u = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
    return np.vstack([e, n, u])


def _geodetic_to_enu(lat, lon, alt, origin):
    o = _ecef(origin["lat"], origin["lon"], origin["alt"])
    return _enu_basis(origin["lat"], origin["lon"]) @ (_ecef(lat, lon, alt) - o)


def _enu_to_geodetic(w, origin):
    x = _ecef(origin["lat"], origin["lon"], origin["alt"]) + _enu_basis(origin["lat"], origin["lon"]).T @ np.asarray(w)
    lon = math.atan2(x[1], x[0])
    p = math.hypot(x[0], x[1])
    lat = math.atan2(x[2], p * (1 - _E2))
    for _ in range(6):
        n = _A / math.sqrt(1 - _E2 * math.sin(lat) ** 2)
        alt = p / math.cos(lat) - n
        lat = math.atan2(x[2], p * (1 - _E2 * n / (n + alt)))
    n = _A / math.sqrt(1 - _E2 * math.sin(lat) ** 2)
    alt = p / math.cos(lat) - n
    return math.degrees(lat), math.degrees(lon), alt


# ---------------------------------------------------------------- parts

@dataclass
class ReferencePoint:
    """A known, surveyed point in the environment (a corner, a pole base, a lane marking ...)."""

    id: str
    name: str
    position: dict
    description: str = ""

    @classmethod
    def from_dict(cls, d, cs):
        return cls(id=_ident(d.get("id"), "reference point"), name=str(d.get("name") or d.get("id"))[:80],
                   position=cs.parse_position(d.get("position"), "reference point"),
                   description=str(d.get("description") or "")[:500])

    def to_dict(self):
        return {"id": self.id, "name": self.name, "position": self.position, "description": self.description}


@dataclass
class CameraSource:
    """Where the camera's pictures come from (paired with a real camera later).

    kind: none | file (video file under data/sources or an absolute path) |
          url (rtsp/http stream) | device (webcam index) | image (a still)
    ``secret`` names an entry in config/secrets.json whose values fill
    ``{username}``/``{password}``-style placeholders in the URI, so credentials
    are never stored with the environment.
    """

    kind: str = "none"
    uri: str = ""
    secret: str = ""
    loop: bool = True

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        kind = d.get("kind") or "none"
        if kind not in SOURCE_KINDS:
            raise EnvironmentError("camera source kind must be one of " + ", ".join(SOURCE_KINDS))
        return cls(kind=kind, uri=str(d.get("uri") or "")[:1000], secret=str(d.get("secret") or "")[:64],
                   loop=bool(d.get("loop", True)))

    def to_dict(self):
        return {"kind": self.kind, "uri": self.uri, "secret": self.secret, "loop": self.loop}


@dataclass
class Correspondence:
    """A reference point seen at a pixel. Stored normalised (0-1) so it holds at any resolution."""

    point_id: str
    u: float  # x / image width
    v: float  # y / image height

    @classmethod
    def from_dict(cls, d):
        return cls(point_id=_ident(d.get("point_id"), "reference point"),
                   u=_num(d.get("u"), "u", None, -0.5, 1.5), v=_num(d.get("v"), "v", None, -0.5, 1.5))

    def to_dict(self):
        return {"point_id": self.point_id, "u": self.u, "v": self.v}

    def pixel(self, width, height):
        return self.u * width, self.v * height


@dataclass
class Calibration:
    """Pixel <-> reference point pairs, and the result of the last pose solve."""

    resolution: list = field(default_factory=list)  # [w, h] of the image the clicks were made on
    points: list = field(default_factory=list)      # [Correspondence]
    solved: dict = field(default_factory=dict)      # {"rmse_px", "at", "fov_estimated", ...}

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        res = d.get("resolution") or []
        if res and (len(res) != 2 or min(int(res[0]), int(res[1])) <= 0):
            raise EnvironmentError("calibration resolution must be [width, height]")
        return cls(resolution=[int(r) for r in res], points=[Correspondence.from_dict(p) for p in d.get("points") or []],
                   solved=dict(d.get("solved") or {}))

    def to_dict(self):
        return {"resolution": self.resolution, "points": [p.to_dict() for p in self.points], "solved": self.solved}


@dataclass
class Camera:
    """A fixed camera: where it is, which way it looks, and what it sees."""

    id: str
    name: str
    position: dict              # ground location (local x/y/z or geo lat/lon/alt of the ground below it)
    height: float = 5.0         # mount height above that position, in position units (geo: metres)
    heading: float = 0.0        # degrees clockwise from north, 0-360
    tilt: float = 10.0          # degrees below the horizon
    roll: float = 0.0           # degrees
    hfov: float = 60.0          # horizontal field of view, degrees
    resolution: list = field(default_factory=lambda: [1920, 1080])
    source: CameraSource = field(default_factory=CameraSource)
    calibration: Calibration = field(default_factory=Calibration)
    description: str = ""

    @classmethod
    def from_dict(cls, d, cs):
        res = d.get("resolution") or [1920, 1080]
        try:
            res = [int(res[0]), int(res[1])]
        except (TypeError, ValueError, IndexError):
            raise EnvironmentError("camera resolution must be [width, height]")
        if min(res) < 16 or max(res) > 16384:
            raise EnvironmentError("camera resolution is out of range")
        return cls(
            id=_ident(d.get("id"), "camera"), name=str(d.get("name") or d.get("id"))[:80],
            position=cs.parse_position(d.get("position"), "camera"),
            height=_num(d.get("height"), "camera height", 5.0, -1e4, 1e5),
            heading=_num(d.get("heading"), "heading", 0.0, -360, 720) % 360.0,
            tilt=_num(d.get("tilt"), "tilt", 10.0, -90, 90),
            roll=_num(d.get("roll"), "roll", 0.0, -180, 180),
            hfov=_num(d.get("hfov"), "field of view", 60.0, 1, 179),
            resolution=res, source=CameraSource.from_dict(d.get("source")),
            calibration=Calibration.from_dict(d.get("calibration")),
            description=str(d.get("description") or "")[:500])

    def to_dict(self):
        return {"id": self.id, "name": self.name, "position": self.position, "height": self.height,
                "heading": self.heading, "tilt": self.tilt, "roll": self.roll, "hfov": self.hfov,
                "resolution": self.resolution, "source": self.source.to_dict(),
                "calibration": self.calibration.to_dict(), "description": self.description}

    def world_position(self, cs):
        w = cs.to_world(self.position)
        w[2] += cs.height_to_m(self.height)
        return w

    def model(self, cs, width=None, height=None):
        """A geometry.CameraModel at the given (or native) resolution."""
        cam = geometry.CameraModel(self.world_position(cs), self.heading, self.tilt, self.roll, self.hfov,
                                   self.resolution[0], self.resolution[1])
        if width and height and (int(width), int(height)) != tuple(self.resolution):
            cam = cam.scaled(width, height)
        return cam

    def vfov(self):
        return geometry.vertical_fov(self.hfov, *self.resolution)


# ---------------------------------------------------------------- environment

@dataclass
class Environment:
    name: str
    slug: str
    coordinates: CoordinateSystem = field(default_factory=CoordinateSystem)
    reference_points: list = field(default_factory=list)
    cameras: list = field(default_factory=list)
    description: str = ""
    extent: float = 100.0  # suggested viewing size, in position units (geo: metres)
    created: float = 0.0
    updated: float = 0.0

    # -- serialisation
    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            raise EnvironmentError("environment must be a JSON object")
        name = str(d.get("name") or "").strip()[:80]
        if not name:
            raise EnvironmentError("name is required")
        slug = d.get("slug") or slugify(name)
        if not SLUG_RE.match(slug):
            raise EnvironmentError("slug must be lowercase letters, digits, '_' or '-'")
        cs = CoordinateSystem.from_dict(d.get("coordinates"))
        env = cls(name=name, slug=slug, coordinates=cs, description=str(d.get("description") or "")[:2000],
                  extent=_num(d.get("extent"), "extent", 100.0, 1e-3, 1e7),
                  created=float(d.get("created") or 0), updated=float(d.get("updated") or 0))
        env.reference_points = [ReferencePoint.from_dict(p, cs) for p in d.get("reference_points") or []]
        env.cameras = [Camera.from_dict(c, cs) for c in d.get("cameras") or []]
        for label, items in (("reference point", env.reference_points), ("camera", env.cameras)):
            ids = [i.id for i in items]
            dup = {i for i in ids if ids.count(i) > 1}
            if dup:
                raise EnvironmentError("duplicate %s id: %s" % (label, ", ".join(sorted(dup))))
        known = {p.id for p in env.reference_points}
        for cam in env.cameras:
            cam.calibration.points = [c for c in cam.calibration.points if c.point_id in known]
        return env

    def to_dict(self):
        return {"schema": SCHEMA_VERSION, "name": self.name, "slug": self.slug, "description": self.description,
                "coordinates": self.coordinates.to_dict(), "extent": self.extent,
                "reference_points": [p.to_dict() for p in self.reference_points],
                "cameras": [c.to_dict() for c in self.cameras], "created": self.created, "updated": self.updated}

    def world_dict(self):
        """The environment in world metres - what viewers draw."""
        cs = self.coordinates
        out = {"ground": cs.ground_z(), "extent_m": cs.height_to_m(self.extent) if cs.kind == "local" else self.extent,
               "reference_points": [], "cameras": []}
        for p in self.reference_points:
            out["reference_points"].append({"id": p.id, "name": p.name, "world": cs.to_world(p.position).tolist()})
        for c in self.cameras:
            out["cameras"].append({"id": c.id, "name": c.name, "world": c.world_position(cs).tolist(),
                                   "ground": cs.to_world(c.position).tolist(), "heading": c.heading,
                                   "tilt": c.tilt, "roll": c.roll, "hfov": c.hfov, "vfov": c.vfov(),
                                   "resolution": c.resolution})
        return out

    # -- lookups
    def camera(self, camera_id):
        for c in self.cameras:
            if c.id == camera_id:
                return c
        raise KeyError(camera_id)

    def reference_point(self, point_id):
        for p in self.reference_points:
            if p.id == point_id:
                return p
        raise KeyError(point_id)

    # -- calibration
    def solve_camera(self, camera_id, estimate_fov=False, use_current=True):
        """Solve a camera's pose from its calibration points; updates the camera and returns a report."""
        cam = self.camera(camera_id)
        cs = self.coordinates
        cal = cam.calibration
        w, h = cam.resolution
        world = [cs.to_world(self.reference_point(c.point_id).position) for c in cal.points]
        pix = [c.pixel(w, h) for c in cal.points]
        hfov = cam.hfov
        if estimate_fov:
            r = geometry.solve_pose_and_fov(world, pix, w, h, cam.hfov)
            hfov = r["hfov"]
        else:
            initial = None
            if use_current:
                model = cam.model(cs)
                initial = (model.R, model.C)
            r = geometry.solve_pose(world, pix, geometry.intrinsics(hfov, w, h), initial)
            if use_current:  # fall back to a fresh solve if seeding made it worse
                fresh = geometry.solve_pose(world, pix, geometry.intrinsics(hfov, w, h))
                if fresh["rmse"] < r["rmse"] - 1e-6:
                    r = fresh
        heading, tilt, roll = geometry.angles_from_rotation(r["R"])
        new_pos = cs.from_world(np.array([r["C"][0], r["C"][1], cs.ground_z()]))
        height_m = r["C"][2] - cs.ground_z()
        cam.position = new_pos
        cam.height = height_m / (1.0 if cs.kind == "geo" else cs.meters_per_unit)
        cam.heading, cam.tilt, cam.roll, cam.hfov = heading, tilt, roll, hfov
        scale = max(w, h) / 1000.0
        cal.solved = {"rmse_px": r["rmse"], "rmse_per_1000px": r["rmse"] / scale, "residuals_px": r["residuals"],
                      "fov_estimated": bool(estimate_fov), "points": len(cal.points), "at": time.time()}
        return cal.solved

    def reprojection(self, camera_id, width=None, height=None):
        """Where every reference point lands in the camera image (normalised), for overlays."""
        cam = self.camera(camera_id)
        model = cam.model(self.coordinates)
        pts = [self.coordinates.to_world(p.position) for p in self.reference_points]
        out = []
        if pts:
            pix, front = model.project(np.array(pts))
            for p, (u, v), f in zip(self.reference_points, pix, front):
                out.append({"point_id": p.id, "u": float(u) / model.width, "v": float(v) / model.height,
                            "visible": bool(f)})
        return out

    # -- storage
    def save(self, root):
        folder = Path(root) / self.slug
        folder.mkdir(parents=True, exist_ok=True)
        now = time.time()
        self.created = self.created or now
        self.updated = now
        tmp = folder / "environment.json.tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(self.to_dict(), fh, indent=2)
        tmp.replace(folder / "environment.json")
        return folder

    @classmethod
    def load(cls, root, slug):
        if not SLUG_RE.match(slug or ""):
            raise FileNotFoundError(slug)
        path = Path(root) / slug / "environment.json"
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        d["slug"] = slug
        return cls.from_dict(d)

    @classmethod
    def list(cls, root):
        out = []
        root = Path(root)
        if not root.exists():
            return out
        for f in sorted(root.glob("*/environment.json")):
            try:
                out.append(cls.load(root, f.parent.name))
            except (OSError, ValueError, KeyError):
                continue
        return out

    @staticmethod
    def delete(root, slug):
        if not SLUG_RE.match(slug or ""):
            raise FileNotFoundError(slug)
        folder = Path(root) / slug
        if not (folder / "environment.json").exists():
            raise FileNotFoundError(slug)
        shutil.rmtree(folder)
