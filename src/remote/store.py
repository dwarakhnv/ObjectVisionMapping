"""Remote access keys and per-environment recording settings, kept in data/remote.json.

    {"subscriptions": [{"id": "a1b2c3", "name": "Parking app", "env": "front_street",
                        "classes": ["person"],            # empty = every class
                        "token_hash": "<sha256>", "hint": "x9Qa", "created": 1792800000.0, "enabled": true}],
     "recording": {"front_street": {"enabled": true, "classes": []}}}

Keys are random 32-character tokens prefixed with "ovm_". Only their SHA-256 is
stored, so a key is shown once, when it is created; a lost key is revoked and
replaced, never recovered.
"""
import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path

from django.conf import settings

_lock = threading.RLock()


def _path():
    return Path(settings.DATA_DIR) / "remote.json"


def _read():
    p = _path()
    if not p.exists():
        return {"subscriptions": [], "recording": {}}
    with open(p, encoding="utf-8") as fh:
        data = json.load(fh)
    data.setdefault("subscriptions", [])
    data.setdefault("recording", {})
    return data


def _write(data):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2)
    tmp.replace(p)


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def public(sub):
    """A subscription without its token hash (safe to show in the UI)."""
    return {k: v for k, v in sub.items() if k != "token_hash"}


# ---------------------------------------------------------------- subscriptions
def subscriptions(env=None):
    with _lock:
        subs = _read()["subscriptions"]
    return [s for s in subs if env is None or s["env"] == env]


def create(name, env, classes):
    """New subscription; returns (subscription, token). The token is not stored and can't be shown again."""
    name = str(name or "").strip()[:80]
    if not name:
        raise ValueError("Give the subscription a name (who or what will use it)")
    token = "ovm_" + secrets.token_urlsafe(24)
    sub = {"id": secrets.token_hex(4), "name": name, "env": env, "classes": sorted(set(classes or [])),
           "token_hash": _hash(token), "hint": token[-4:], "created": time.time(), "enabled": True}
    with _lock:
        data = _read()
        data["subscriptions"].append(sub)
        data["recording"].setdefault(env, {"enabled": True, "classes": []})
        _write(data)
    return public(sub), token


def update(sub_id, **changes):
    with _lock:
        data = _read()
        for s in data["subscriptions"]:
            if s["id"] == sub_id:
                for k in ("name", "classes", "enabled"):
                    if k in changes:
                        s[k] = changes[k]
                _write(data)
                return public(s)
    raise KeyError(sub_id)


def delete(sub_id):
    with _lock:
        data = _read()
        before = len(data["subscriptions"])
        data["subscriptions"] = [s for s in data["subscriptions"] if s["id"] != sub_id]
        if len(data["subscriptions"]) == before:
            raise KeyError(sub_id)
        _write(data)


def authenticate(token, env):
    """The enabled subscription this token belongs to, if it may read `env`; else None."""
    if not token:
        return None
    digest = _hash(token)
    for s in subscriptions(env):
        if s.get("enabled") and hmac.compare_digest(s["token_hash"], digest):
            return s
    return None


# ---------------------------------------------------------------- recording settings
def recording(env):
    """{"enabled": bool, "classes": [...], "region": [points] or []} for an environment (enabled by default).

    region: the map area where movement counts ({lat, lon} or {x, y} corners); empty means everywhere.
    """
    with _lock:
        return dict({"enabled": True, "classes": [], "region": []}, **_read()["recording"].get(env, {}))


def set_recording(env, **changes):
    """Change some of enabled / classes / region; the rest stays as it was."""
    with _lock:
        data = _read()
        current = dict({"enabled": True, "classes": [], "region": []}, **data["recording"].get(env, {}))
        if "enabled" in changes:
            current["enabled"] = bool(changes["enabled"])
        if "classes" in changes:
            current["classes"] = sorted(set(changes["classes"] or []))
        if "region" in changes:
            current["region"] = list(changes["region"] or [])
        data["recording"][env] = current
        _write(data)
        return current
