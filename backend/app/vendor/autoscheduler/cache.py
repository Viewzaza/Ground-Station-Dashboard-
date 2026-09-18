"""A small JSON-on-disk cache with per-key TTLs.

The SatNOGS APIs are public and slow rather than rate-limited, but the TLE
endpoint is regenerated only a few times a day and the observation feed costs
about six seconds a page. Caching is what makes repeated runs usable.

A corrupt cache entry is a warning, never a crash: we drop it and refetch.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


class Cache:
    def __init__(self, directory: Path, offline: bool = False) -> None:
        self.dir = Path(directory)
        self.offline = offline
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
        return self.dir / f"{safe}.json"

    def read(self, key: str, ttl_s: float) -> Any | None:
        """Return the cached payload, or None if absent, stale or unreadable."""
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            stored_at = float(blob["stored_at"])
            payload = blob["payload"]
        except (ValueError, KeyError, TypeError, OSError) as exc:
            log.warning("cache entry %s is unreadable (%s) - ignoring it", key, exc)
            return None
        age = time.time() - stored_at
        # Offline mode takes whatever is on disk, however old.
        if age > ttl_s and not self.offline:
            log.debug("cache %s is %.0fs old, past its %.0fs ttl", key, age, ttl_s)
            return None
        log.debug("cache hit %s (%.0fs old)", key, age)
        return payload

    def write(self, key: str, payload: Any) -> None:
        path = self._path(key)
        blob = {"stored_at": time.time(), "payload": payload}
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(blob), encoding="utf-8")
            tmp.replace(path)   # atomic, so an interrupted run cannot truncate
        except OSError as exc:
            log.warning("could not write cache %s: %s", key, exc)

    def get_or_fetch(self, key: str, ttl_s: float, fetch: Callable[[], Any]) -> Any:
        cached = self.read(key, ttl_s)
        if cached is not None:
            return cached
        if self.offline:
            raise RuntimeError(
                f"--offline was given but there is no cached {key!r} to work from. "
                "Run once without --offline first."
            )
        payload = fetch()
        self.write(key, payload)
        return payload

    def age_s(self, key: str) -> float | None:
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            stored_at = float(json.loads(path.read_text(encoding="utf-8"))["stored_at"])
        except (ValueError, KeyError, TypeError, OSError):
            return None
        return time.time() - stored_at

    def clear(self) -> int:
        removed = 0
        for path in self.dir.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                log.warning("could not remove %s: %s", path, exc)
        return removed
