import time
from pathlib import Path

from django.conf import settings

_cache = {"at": 0.0, "v": "0"}


def asset_version(request):
    """Template value that changes whenever a static file changes, for cache-busting ?v= URLs."""
    now = time.monotonic()
    if now - _cache["at"] > 2.0:
        newest = 0.0
        dirs = [Path(settings.SRC_DIR) / "static"] + [p for p in Path(settings.SRC_DIR).glob("*/static")]
        for d in dirs:
            for f in d.rglob("*"):
                if f.suffix in (".js", ".css") and "vendor" not in f.parts:
                    newest = max(newest, f.stat().st_mtime)
        _cache.update(at=now, v=str(int(newest)))
    return {"asset_v": _cache["v"]}
