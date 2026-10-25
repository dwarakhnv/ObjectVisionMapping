"""Map camera pixels to places on a map using reference points.

A reference point says "this pixel shows this spot on the ground". With 4 or
more of them, a homography (a perspective mapping from the image to the
ground plane) turns any pixel on the ground into a map position.

The map ("space") is either
  * geo:  latitude / longitude (outdoors), or
  * plan: x / y on a floor plan in its units (m, cm, ft, in ...); x right, y down.
Internally both become metres east / north (a plan's "up" is north), so the
mapping, pose estimate and detection code doesn't care which it is.

Things to know:
  * Reference points must all lie on the ground (one flat plane), spread over
    the area you care about, and no 3 of them in a straight line.
  * Map an object's *ground contact point* (e.g. the bottom-centre of its box),
    not its centre: the homography only describes the ground.
  * Points use shapely's convention: Point(longitude, latitude), i.e. x = lon, y = lat;
    on a plan, Point(x, y) in plan units.
"""
import json
import math
import re
from pathlib import Path

import cv2
import numpy as np
from shapely.geometry import Point

EARTH_RADIUS_M = 6371008.8


def geo_to_local(lat, lon, origin):
    """(east, north) metres of lat/lon from origin=(lat0, lon0). Accurate to mm over a few km."""
    lat0, lon0 = origin
    return (math.radians(lon - lon0) * EARTH_RADIUS_M * math.cos(math.radians(lat0)),
            math.radians(lat - lat0) * EARTH_RADIUS_M)


def local_to_geo(east, north, origin):
    lat0, lon0 = origin
    return (lat0 + math.degrees(north / EARTH_RADIUS_M),
            lon0 + math.degrees(east / (EARTH_RADIUS_M * math.cos(math.radians(lat0)))))


# ---------------------------------------------------------------- spaces (what the map coordinates are)
class GeoSpace:
    """Latitude / longitude. Metres are measured from `origin` (set from the reference points)."""

    kind = "geo"

    def __init__(self, origin=None):
        self.origin = origin

    def prepare(self, points):
        self.origin = (float(np.mean([p.y for p in points])), float(np.mean([p.x for p in points])))

    def to_local(self, point):
        return geo_to_local(point.y, point.x, self.origin)

    def from_local(self, east, north):
        lat, lon = local_to_geo(east, north, self.origin)
        return Point(lon, lat)

    @staticmethod
    def point_to_dict(point):
        return {"lat": point.y, "lon": point.x}

    def to_dict(self):
        return {"kind": "geo"}


class PlanSpace:
    """A floor plan: x right, y down, in plan units. Metres east = x, north = -y (up on the plan)."""

    kind = "plan"

    def __init__(self, plan=None, units="m", meters_per_unit=1.0):
        self.plan, self.units, self.meters_per_unit = plan, units, float(meters_per_unit)

    def prepare(self, points):
        pass  # plan coordinates are already absolute

    def to_local(self, point):
        return point.x * self.meters_per_unit, -point.y * self.meters_per_unit

    def from_local(self, east, north):
        return Point(east / self.meters_per_unit, -north / self.meters_per_unit)

    @staticmethod
    def point_to_dict(point):
        return {"x": point.x, "y": point.y}

    def to_dict(self):
        return {"kind": "plan", "plan": self.plan, "units": self.units, "meters_per_unit": self.meters_per_unit}


def space_from_dict(d):
    d = d or {}
    if d.get("kind", "geo") == "geo":
        return GeoSpace()
    if d.get("kind") != "plan":
        raise ValueError("Unknown map kind '%s'" % d.get("kind"))
    m = float(d.get("meters_per_unit") or 0)
    if not d.get("plan") or m <= 0:
        raise ValueError("An indoor environment needs a floor plan")
    return PlanSpace(str(d["plan"]), str(d.get("units") or "m"), m)


def _degenerate(pts, ratio=0.02):
    """True if 2D points can't define a homography: all close to one line, or (with exactly 4) 3 in a line."""
    centred = pts - pts.mean(axis=0)
    s = np.linalg.svd(centred, compute_uv=False)
    if s[0] == 0 or s[1] / s[0] < ratio:
        return True
    if len(pts) == 4:
        for i in range(4):
            a, b, c = (pts[j] for j in range(4) if j != i)
            area2 = abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
            longest = max(np.hypot(*(b - a)), np.hypot(*(c - a)), np.hypot(*(c - b)))
            if area2 < ratio * longest ** 2:
                return True
    return False


class ReferencePoint:
    """A pixel in the camera image and the map location it shows."""

    def __init__(self, coords, point, name="", kind="geo"):
        self.pixel_coords = (float(coords[0]), float(coords[1]))  # (x, y) at the camera's resolution
        self.point = point                                       # shapely Point(lon, lat), or Point(x, y) on a plan
        self.name = name
        self.kind = kind                                         # "geo" or "plan"

    @property
    def lat(self):
        return self.point.y

    @property
    def lon(self):
        return self.point.x

    def to_dict(self):
        where = {"lat": self.lat, "lon": self.lon} if self.kind == "geo" else {"x": self.point.x, "y": self.point.y}
        return dict({"name": self.name, "pixel": list(self.pixel_coords)}, **where)

    @classmethod
    def from_dict(cls, d):
        px, py = (float(v) for v in d["pixel"])
        name = str(d.get("name", ""))
        if "lat" in d:
            lat, lon = float(d["lat"]), float(d["lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("Reference point '%s' has an invalid lat/lon" % name)
            return cls((px, py), Point(lon, lat), name, "geo")
        x, y = float(d["x"]), float(d["y"])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("Reference point '%s' has an invalid position" % name)
        return cls((px, py), Point(x, y), name, "plan")


class Camera:
    """A fixed camera, its reference points, and the pixel -> geo mapping they define.

    source: the camera's key in config/secrets.json "cameras" (e.g. "tapo_c100"),
            used by connect() to open the live feed.
    resolution: the (width, height) of the image the reference pixels were picked on.
    """

    def __init__(self, name="camera", resolution=(1920, 1080), reference_points=None, source=None, space=None):
        self.name = name
        self.resolution = tuple(resolution)
        self.reference_points = list(reference_points or [])
        self.source = source
        self.space = space or GeoSpace()  # what the map coordinates are (lat/lon or floor plan)
        self._homography = None   # pixels -> local metres, computed on first use
        self.errors_m = []        # how far each reference point lands from its true spot

    @property
    def _origin(self):
        """(lat, lon) the local metres are measured from (outdoor maps)."""
        return getattr(self.space, "origin", None)

    # ---------------------------------------------------------------- reference points
    def add_reference_point(self, reference_point):
        self.reference_points.append(reference_point)
        self._homography = None  # the mapping must be recomputed

    def remove_reference_point(self, index):
        del self.reference_points[index]
        self._homography = None

    # ---------------------------------------------------------------- saving
    def to_dict(self):
        return {"name": self.name, "source": self.source, "resolution": list(self.resolution),
                "reference_points": [rp.to_dict() for rp in self.reference_points]}

    @classmethod
    def from_dict(cls, d, space=None):
        name = str(d.get("name") or "").strip()
        if not name:
            raise ValueError("Every camera needs a name")
        w, h = (int(v) for v in d.get("resolution") or (1920, 1080))
        if w <= 0 or h <= 0:
            raise ValueError("Camera '%s' has an invalid resolution" % name)
        points = [ReferencePoint.from_dict(p) for p in d.get("reference_points") or []]
        space = space or GeoSpace()
        if any(p.kind != space.kind for p in points):
            raise ValueError("Camera '%s' has reference points for a different kind of map" % name)
        return cls(name=name, resolution=(w, h), reference_points=points, source=d.get("source") or None, space=space)

    @property
    def id(self):
        """URL-safe id derived from the name (unique within an environment)."""
        return slugify(self.name)

    # ---------------------------------------------------------------- live feed
    def connect(self):
        """The live camera behind this Camera (an RTSPCamera from simulation/camera.py)."""
        from .camera import RTSPCamera

        if not self.source:
            raise ValueError("Camera '%s' has no source; set it to a key in config/secrets.json" % self.name)
        return RTSPCamera.from_config(self.source)

    # ---------------------------------------------------------------- map <-> local metres
    # Fitting in degrees is badly conditioned (the points differ in the 5th decimal),
    # so we work in metres east/north and convert back at the end.
    def _to_local(self, point):
        return self.space.to_local(point)

    # ---------------------------------------------------------------- the mapping
    def _fit(self):
        """Compute the pixel -> ground homography from the reference points."""
        points = [rp for rp in self.reference_points if rp.pixel_coords is not None and rp.point is not None]
        if len(points) < 4:
            raise ValueError("At least 4 reference points are required (have %d)." % len(points))

        self.space.prepare([rp.point for rp in points])
        src = np.array([rp.pixel_coords for rp in points], dtype=np.float64)
        dst = np.array([self._to_local(rp.point) for rp in points], dtype=np.float64)
        for label, pts in (("pixels", src), ("map locations", dst)):
            if _degenerate(pts):
                raise ValueError("The reference %s are (nearly) in a straight line. Spread the points out "
                                 "so no 3 of them line up." % label)

        # Least squares over all points (method 0): with more than 4 points, small click errors average out.
        matrix, _ = cv2.findHomography(src, dst, 0)
        if matrix is None or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-12:
            raise ValueError("Could not compute the mapping. Make sure no 3 reference points are in a line "
                             "and that they are spread out in the image.")

        # Make the homogeneous scale positive on the reference pixels, so a negative
        # scale later means "above the horizon".
        w = matrix[2] @ np.array([src[0][0], src[0][1], 1.0])
        if w < 0:
            matrix = -matrix

        self._homography = matrix
        mapped = cv2.perspectiveTransform(src.reshape(-1, 1, 2), matrix).reshape(-1, 2)
        self.errors_m = [float(np.hypot(*(m - d))) for m, d in zip(mapped, dst)]

    def pixel_to_local(self, pixel_coords, frame_size=None):
        """(east, north) metres of a pixel on the ground, in this camera's local frame."""
        if self._homography is None:
            self._fit()
        x, y = pixel_coords
        if frame_size is not None and tuple(frame_size) != self.resolution:
            x = x * self.resolution[0] / frame_size[0]
            y = y * self.resolution[1] / frame_size[1]
        e, n, w = self._homography @ np.array([x, y, 1.0])
        if w <= 1e-12:
            raise ValueError("Pixel (%g, %g) is at or above the horizon; it doesn't show the ground." % (x, y))
        return e / w, n / w

    def pixel_to_map(self, pixel_coords, frame_size=None):
        """Map position of a pixel on the ground: Point(lon, lat) outdoors, Point(x, y) on a floor plan."""
        return self.space.from_local(*self.pixel_to_local(pixel_coords, frame_size))

    def pixel_to_geo(self, pixel_coords, frame_size=None):
        """Geo location (shapely Point(lon, lat)) of a pixel on the ground (same as pixel_to_map outdoors).

        frame_size: (width, height) of the image the pixel comes from, if it differs
        from the resolution the reference points were picked at (e.g. the Tapo's
        1280x720 stream2 vs a 1920x1080 snapshot). The pixel is scaled to match.
        """
        return self.pixel_to_map(pixel_coords, frame_size)

    def accuracy(self):
        """Average distance (metres) between where the reference points are and where the mapping puts them.

        With exactly 4 points this is always ~0 (4 points define the mapping exactly);
        with 5 or more it shows how consistent the points are.
        """
        if self._homography is None:
            self._fit()
        return float(np.mean(self.errors_m))

    # ---------------------------------------------------------------- camera position (estimated)
    def estimate_pose(self):
        """Estimate where the camera is and how it points, from the reference points alone.

        Returns {"lat", "lon" (or "x", "y" on a plan), "east", "north", "height_m", "heading", "tilt",
        "roll", "hfov", "error_px"}. east / north are metres in this camera's local frame.
        heading = degrees clockwise from north (the top of a plan), tilt = degrees below the horizon.

        1. Focal length: the ground->image homography of a real pinhole camera must
           satisfy two constraints (its first two columns, un-projected, are
           perpendicular and equally long). Each gives an estimate of the focal length.
        2. Pose: with that focal length, OpenCV's planar PnP solver finds the camera
           position and rotation that best reproject the reference points.
        """
        if self._homography is None:
            self._fit()
        w, h = self.resolution
        cx, cy = w / 2.0, h / 2.0
        ground_to_px = np.linalg.inv(self._homography)
        g = np.array([[1, 0, -cx], [0, 1, -cy], [0, 0, 1]]) @ ground_to_px
        (a1, b1, c1), (a2, b2, c2) = g[:, 0], g[:, 1]
        candidates = []
        if abs(c1 * c2) > 1e-12:
            candidates.append(-(a1 * a2 + b1 * b2) / (c1 * c2))
        if abs(c1 ** 2 - c2 ** 2) > 1e-12:
            candidates.append(-(a1 ** 2 + b1 ** 2 - a2 ** 2 - b2 ** 2) / (c1 ** 2 - c2 ** 2))
        f2 = [c for c in candidates if c > 0]
        focal = math.sqrt(np.median(f2)) if f2 else cx / math.tan(math.radians(30))  # fallback: 60 deg view
        hfov = math.degrees(2 * math.atan(cx / focal))
        if not 15 <= hfov <= 140:  # unreliable (e.g. camera looking straight down): use a typical lens
            hfov = 60.0
            focal = cx / math.tan(math.radians(hfov / 2))

        K = np.array([[focal, 0, cx], [0, focal, cy], [0, 0, 1]], dtype=np.float64)
        world = np.array([list(self._to_local(rp.point)) + [0.0] for rp in self.reference_points])
        pixels = np.array([rp.pixel_coords for rp in self.reference_points], dtype=np.float64)
        # Planar PnP has two mirror solutions; keep the one with the camera above the ground.
        _, rvecs, tvecs, errs = cv2.solvePnPGeneric(world, pixels, K, None, flags=cv2.SOLVEPNP_IPPE)
        best = None
        for rvec, tvec, err in zip(rvecs, tvecs, np.ravel(errs)):
            R, _ = cv2.Rodrigues(rvec)
            C = (-R.T @ tvec).ravel()
            if C[2] > 0 and (best is None or err < best[2]):
                best = (R, C, float(err))
        if best is None:
            raise ValueError("Could not place the camera above the ground; check the reference points.")
        R, C, err = best
        heading, tilt, roll = _angles(R)
        where = self.space.point_to_dict(self.space.from_local(C[0], C[1]))
        return dict(where, east=float(C[0]), north=float(C[1]), height_m=float(C[2]), heading=heading, tilt=tilt,
                    roll=roll, hfov=hfov, error_px=err)

    # ---------------------------------------------------------------- picture
    def visualize_reference_points(self, image=None, show=True):
        """Draw the reference points on `image` (e.g. a camera frame) or on a black canvas.

        Returns the drawn image (BGR); with show=True it is also displayed with matplotlib.
        """
        width, height = self.resolution
        if image is None:
            canvas = np.zeros((height, width, 3), dtype=np.uint8)
        else:
            canvas = cv2.resize(image, (width, height)) if image.shape[1::-1] != (width, height) else image.copy()

        for i, rp in enumerate(self.reference_points):
            x, y = int(round(rp.pixel_coords[0])), int(round(rp.pixel_coords[1]))
            cv2.circle(canvas, (x, y), 10, (0, 255, 0), -1)
            cv2.circle(canvas, (x, y), 12, (255, 255, 255), 2)
            label = rp.name or "P%d" % (i + 1)
            cv2.putText(canvas, "%s (%.6f, %.6f)" % (label, rp.point.y, rp.point.x), (x + 15, y + 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

        if show:
            import matplotlib.pyplot as plt

            plt.figure(figsize=(14, 8))
            plt.imshow(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))
            plt.title("%s reference points (%dx%d)" % (self.name, width, height))
            plt.show()
        return canvas


def _angles(R):
    """(heading, tilt, roll) in degrees from a world->camera rotation (world: x east, y north, z up)."""
    right, forward = R[0], R[2]
    heading = math.degrees(math.atan2(forward[0], forward[1])) % 360.0
    tilt = math.degrees(-math.asin(max(-1.0, min(1.0, forward[2]))))
    h = math.radians(heading)
    level_right = np.array([math.cos(h), -math.sin(h), 0.0])
    level_down = np.cross(forward, level_right)
    roll = math.degrees(math.atan2(float(right @ level_down), float(right @ level_right)))
    return heading, tilt, roll


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
    return s[:64] or "unnamed"


class Environment:
    """A named place with one or more cameras, saved as data/environments/<slug>.json.

    space: GeoSpace (outdoor map) or PlanSpace (floor plan); every camera's points use it.
    tracking: follow objects from frame to frame (stable ids, speed, motion heading).
    Off by default: each result then just reports what is in the latest frame.
    """

    def __init__(self, name, cameras=None, space=None, tracking=False):
        self.name = name
        self.cameras = list(cameras or [])
        self.space = space or GeoSpace()
        self.tracking = bool(tracking)

    @property
    def slug(self):
        return slugify(self.name)

    def camera(self, camera_id):
        for cam in self.cameras:
            if cam.id == camera_id:
                return cam
        raise KeyError(camera_id)

    def to_dict(self):
        return {"name": self.name, "space": self.space.to_dict(), "tracking": self.tracking,
                "cameras": [c.to_dict() for c in self.cameras]}

    @classmethod
    def from_dict(cls, d):
        name = str(d.get("name") or "").strip()
        if not name:
            raise ValueError("The environment needs a name")
        space = space_from_dict(d.get("space"))
        # Each camera gets its own copy: an outdoor camera's metres are measured from its own points.
        cameras = [Camera.from_dict(c, space_from_dict(space.to_dict())) for c in d.get("cameras") or []]
        ids = [c.id for c in cameras]
        if len(set(ids)) != len(ids):
            raise ValueError("Two cameras have the same name")
        return cls(name, cameras, space, tracking=d.get("tracking") is True)

    def save(self, folder):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (self.slug + ".json")
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(self.to_dict(), fh, indent=2)
        tmp.replace(path)  # write-then-rename: a crash never leaves a half-written file
        return path

    @classmethod
    def load(cls, folder, slug):
        if slugify(slug) != slug:
            raise FileNotFoundError(slug)
        with open(Path(folder) / (slug + ".json"), encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    @classmethod
    def list(cls, folder):
        envs = []
        for path in sorted(Path(folder).glob("*.json")):
            try:
                envs.append(cls.load(folder, path.stem))
            except (OSError, ValueError, KeyError):
                continue  # skip files that aren't environments
        return envs


if __name__ == "__main__":
    # Your example points, written as Point(lon, lat).
    reference_points = [
        ReferencePoint((1000, 500), Point(-78.87812352331412, 35.68580829730078), "top left"),
        ReferencePoint((1100, 900), Point(-78.87783719750352, 35.68580121696315), "top right"),
        ReferencePoint((800, 500), Point(-78.8781315699411, 35.68573858317978), "bottom left"),
        ReferencePoint((700, 900), Point(-78.87784122081702, 35.685730958194085), "bottom right"),
    ]
    camera = Camera(name="Tapo C100", resolution=(1920, 1080), reference_points=reference_points, source="tapo_c100")

    p = camera.pixel_to_geo((1910, 540))
    print("pixel (1910, 540) -> lat %.8f, lon %.8f" % (p.y, p.x))
    print("same spot in a 1280x720 frame ->", camera.pixel_to_geo((1910 * 2 / 3, 540 * 2 / 3), frame_size=(1280, 720)))
    print("reference point error: %.3f m" % camera.accuracy())
