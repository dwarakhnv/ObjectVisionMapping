"""One live picture of an environment: every object its cameras see, in map coordinates.

Detection runs per camera (vision/feeds.py). Asking for the picture starts the
cameras' detection if needed and keeps it alive; when nobody asks for 30 s
(OVM_FEED_IDLE_SECONDS) it stops by itself, so an environment nobody is
connected to uses no compute.

Objects seen by several cameras are merged when they are of the same kind and
close together on the ground. With the environment's tracking on, an object keeps
one id while any camera still tracks it; with tracking off (the default) ids only
number the objects in the current picture, and there is no speed or prediction.

Speed: the merged picture is built once per new camera result and shared by
every client (current()); each client only filters it by class (view()).
wait_update() returns the moment a newer picture exists, and view(predict=True)
moves moving objects along their heading to "now" between camera results.
"""
import itertools
import math
import threading
import time
from datetime import datetime, timezone

from django.conf import settings
from shapely.geometry import Point

from simulation.environment import Environment, GeoSpace
from vision import feeds

ENV_DIR = settings.DATA_DIR / "environments"
MERGE_M = 1.5          # objects of one kind closer than this (plus half their length) are the same object
FORGET_S = 10.0        # a camera's track id is forgotten this long after it was last seen
PREDICT_MAX_S = 1.0    # never move an object further ahead than this


class _EnvState:
    """Per environment: global ids across cameras, who is connected, and the cached picture."""

    def __init__(self):
        self.ids = itertools.count(1)
        self.global_ids = {}          # (camera id, track id) -> (global id, last seen)
        self.clients = {}             # subscription id -> {"name", "last", "streams"}
        self.lock = threading.Lock()
        self.build_lock = threading.Lock()
        self.env = (None, None)       # (file mtime, Environment)
        self.key = ()                 # the environment and camera results the picture was built from
        self.picture = None
        self.seq = 0


_states = {}
_states_lock = threading.Lock()


def state(slug):
    with _states_lock:
        return _states.setdefault(slug, _EnvState())


def touch_client(slug, sub, streaming_delta=0):
    st = state(slug)
    with st.lock:
        c = st.clients.setdefault(sub["id"], {"name": sub["name"], "last": 0.0, "streams": 0})
        c["last"] = time.time()
        c["streams"] = max(0, c["streams"] + streaming_delta)


def clients(slug):
    """Who used this environment recently: {subscription id: {"last", "streams", "active"}}."""
    st = state(slug)
    now = time.time()
    with st.lock:
        return {k: dict(v, active=v["streams"] > 0 or now - v["last"] < settings.OVM_FEED_IDLE_SECONDS)
                for k, v in st.clients.items()}


def _space(env):
    """The environment-wide frame objects are merged in (metres)."""
    if env.space.kind == "plan":
        return env.space
    pts = [rp.point for cam in env.cameras for rp in cam.reference_points]
    space = GeoSpace()
    if pts:
        space.prepare(pts)
    return space


def _environment(slug):
    """The environment, re-read only when its file changes. Raises FileNotFoundError."""
    st = state(slug)
    mtime = (ENV_DIR / (slug + ".json")).stat().st_mtime
    if st.env[0] != mtime:
        st.env = (mtime, Environment.load(ENV_DIR, slug))
    return st.env[1]


def current(slug):
    """The environment's live picture (starts / keeps alive its detection). Raises FileNotFoundError.

    Built once per new camera result and shared by all clients:
    {"seq", "env", "space", "cameras", "objects", "captured_at"}. Objects also carry their
    position in metres ("_en") and ground velocity ("_v") for prediction; view() drops those.
    """
    st = state(slug)
    env = _environment(slug)
    workers = []
    for cam in env.cameras:
        if cam.source:
            try:
                workers.append((cam, feeds.get_worker("@%s.%s" % (slug, cam.id))))
            except KeyError:
                pass
    with st.build_lock:
        latest = [(cam, w.latest) for cam, w in workers]
        key = (env,) + tuple(l for _, l in latest)  # each published result is a new dict
        if len(key) == len(st.key) and all(a is b for a, b in zip(key, st.key)):
            return st.picture
        space = _space(env)
        where_key = "geo" if env.space.kind == "geo" else "plan"
        cams, seen, captured = {}, [], []
        for cam, l in latest:
            cams[cam.id] = {"name": cam.name, "status": l.get("status"), "error": l.get("error"),
                            "located": len(cam.reference_points) >= 4}
            if l.get("status") != "running":
                continue
            if l.get("captured_at"):
                captured.append(l["captured_at"])
            for o in l.get("objects") or []:
                where = o.get(where_key)
                if not where:
                    continue
                p = Point(where["lon"], where["lat"]) if where_key == "geo" else Point(where["x"], where["y"])
                e, n = space.to_local(p)
                seen.append((cam.id, o, e, n))
        st.seq += 1
        st.key = key
        st.picture = {"seq": st.seq, "env": env, "space": space, "cameras": cams,
                      "objects": _merge(slug, seen, space, env), "captured_at": min(captured) if captured else None}
        return st.picture


def wait_update(slug, after, timeout):
    """current(slug), waiting up to `timeout` s for a picture newer than sequence number `after`."""
    deadline = time.monotonic() + timeout
    while True:
        mark = feeds.seq()
        pic = current(slug)
        left = deadline - time.monotonic()
        if pic["seq"] > after or left <= 0:
            return pic
        feeds.wait_change(mark, left)


def view(pic, slug, classes=None, predict=False):
    """What a client gets: the picture as JSON-ready data, only `classes`, optionally moved ahead to now.

    With predict, moving objects are placed where their speed and heading take them by now
    (at most PREDICT_MAX_S ahead) and marked "predicted": true.
    """
    env, space, now = pic["env"], pic["space"], time.time()
    ahead = min(PREDICT_MAX_S, max(0.0, now - pic["captured_at"])) if predict and pic["captured_at"] else 0.0
    objects = []
    for o in pic["objects"]:
        if classes and o["class"] not in classes:
            continue
        out = {k: v for k, v in o.items() if not k.startswith("_")}
        if predict:
            out["predicted"] = False
            if ahead and o["_v"]:
                (e, n), (ve, vn) = o["_en"], o["_v"]
                out["position"] = _position(space, env, e + ve * ahead, n + vn * ahead)
                out["predicted"] = True
        objects.append(out)
    stamp = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")
    return {"environment": slug, "name": env.name, "seq": pic["seq"], "time": stamp(now),
            "captured_at": stamp(pic["captured_at"]) if pic["captured_at"] else None,
            "age_ms": round((now - pic["captured_at"]) * 1000) if pic["captured_at"] else None,
            "map": env.space.to_dict() if env.space.kind == "plan" else {"kind": "geo"},
            "tracking": env.tracking, "cameras": pic["cameras"], "objects": objects}


def snapshot(slug, classes=None, predict=False):
    """Current objects in an environment, as clients get them. Raises FileNotFoundError."""
    return view(current(slug), slug, classes, predict)


def _merge(slug, seen, space, env):
    """Merge sightings of one object by several cameras; give each merged object a stable id."""
    groups = feeds.registry().groups()
    clusters = []
    for cam_id, o, e, n in sorted(seen, key=lambda s: -s[1].get("score", 0)):
        radius = MERGE_M + 0.5 * (o.get("dimensions") or {}).get("length", 0)
        best = None
        for c in clusters:
            if cam_id in c["cams"] or groups.get(c["lead"]["class"], c["lead"]["class"]) != groups.get(o["class"], o["class"]):
                continue  # a camera never sees one object twice; and a person isn't a car
            d = math.hypot(c["e"] - e, c["n"] - n)
            if d <= radius and (best is None or d < best[1]):
                best = (c, d)
        if best is None:
            clusters.append({"lead": o, "members": [(cam_id, o)], "cams": {cam_id}, "e": e, "n": n})
        else:
            c = best[0]
            c["members"].append((cam_id, o))
            c["cams"].add(cam_id)
            k = len(c["members"])
            c["e"], c["n"] = c["e"] + (e - c["e"]) / k, c["n"] + (n - c["n"]) / k
    st, now, used, out = state(slug), time.time(), set(), []
    if not env.tracking:  # ids just number this picture's objects
        return [_describe(i, c, space, env) for i, c in enumerate(clusters, 1)]
    with st.lock:
        for c in clusters:
            keys = [(cam, m["id"]) for cam, m in c["members"]]
            known = sorted(st.global_ids[k][0] for k in keys if k in st.global_ids and st.global_ids[k][0] not in used)
            gid = known[0] if known else next(st.ids)
            used.add(gid)
            for k in keys:
                st.global_ids[k] = (gid, now)
            out.append(_describe(gid, c, space, env))
        st.global_ids = {k: v for k, v in st.global_ids.items() if now - v[1] < FORGET_S}
    out.sort(key=lambda o: o["id"])
    return out


def _position(space, env, e, n):
    p = space.from_local(e, n)
    if env.space.kind == "geo":
        return {"lat": round(p.y, 8), "lon": round(p.x, 8)}
    return {"x": round(p.x, 3), "y": round(p.y, 3), "units": env.space.units}


def _describe(gid, cluster, space, env):
    lead = cluster["lead"]
    moving = lead.get("direction") not in (None, "stationary", "unknown")
    speed, heading = lead.get("speed_mps"), lead.get("heading")
    velocity = None  # metres per second east, north: only for real motion (a parked car's axis isn't a direction)
    if moving and speed and heading is not None and lead.get("heading_source") == "motion":
        velocity = (speed * math.sin(math.radians(heading)), speed * math.cos(math.radians(heading)))
    return {
        "id": gid, "class": lead["class"], "label": lead["label"], "subtype": lead.get("subtype"),
        "confidence": round(max(m.get("score", 0) for _, m in cluster["members"]), 3),
        "position": _position(space, env, cluster["e"], cluster["n"]),
        "heading": heading, "heading_source": lead.get("heading_source"),
        "speed_mps": speed, "moving": moving, "direction": lead.get("direction"),
        "size_m": lead.get("dimensions"),
        "cameras": sorted(cluster["cams"]),
        "tracked_for_s": max(m.get("age_s", 0) for _, m in cluster["members"]) if env.tracking else None,
        "_en": (cluster["e"], cluster["n"]), "_v": velocity,
    }
