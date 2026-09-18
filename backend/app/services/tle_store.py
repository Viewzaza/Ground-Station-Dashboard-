"""Orbital element storage.

Celestrak now *enforces* its fetch etiquette: repeating a download before the
data has changed returns HTTP 403, and 50 errors in two hours puts the client's
IP in their firewall. So the rule here is absolute — never hit the network if
the cache is younger than GS_TLE_TTL_S (7200 s), and treat any non-200 as
terminal rather than retrying.

SatNOGS DB is the fallback source, and is also the better source for amateur
satellites specifically, since it carries transmitter metadata.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx

from ..config import Settings
from ..schemas import TleInfo

log = logging.getLogger(__name__)

CELESTRAK_GROUP_URL = "https://celestrak.org/NORAD/elements/gp.php"
USER_AGENT = "knacksat2-ground-station-dashboard/0.1 (+github.com/Viewzaza)"


def parse_tle_epoch(tle1: str) -> datetime | None:
    """Epoch from columns 19-32 of line 1: two-digit year + fractional day."""
    try:
        raw = tle1[18:32].strip()
        year = int(raw[:2])
        day = float(raw[2:])
    except (ValueError, IndexError):
        return None
    year += 2000 if year < 57 else 1900
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=day - 1.0)


class TleStore:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._sats: dict[int, dict] = {}
        self._fetched_at: datetime | None = None
        self._source: str = "none"
        self._lock = asyncio.Lock()
        self._load_cache()

    # --- cache -------------------------------------------------------------
    def _load_cache(self) -> None:
        path = self.s.tle_cache_path
        if not path.exists():
            return
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            self._sats = {int(k): v for k, v in blob.get("sats", {}).items()}
            self._fetched_at = datetime.fromisoformat(blob["fetched_at"])
            self._source = blob.get("source", "cache")
            log.info("loaded %d TLEs from cache (fetched %s)",
                     len(self._sats), self._fetched_at)
        except Exception as exc:                      # corrupt cache is not fatal
            log.warning("ignoring unreadable TLE cache: %s", exc)

    def _save_cache(self) -> None:
        path = self.s.tle_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "fetched_at": (self._fetched_at or datetime.now(timezone.utc)).isoformat(),
                    "source": self._source,
                    "sats": {str(k): v for k, v in self._sats.items()},
                },
                indent=1,
            ),
            encoding="utf-8",
        )

    @property
    def cache_age_s(self) -> float:
        if self._fetched_at is None:
            return float("inf")
        return (datetime.now(timezone.utc) - self._fetched_at).total_seconds()

    @property
    def is_fresh(self) -> bool:
        return self.cache_age_s < self.s.tle_ttl_s

    # --- fetching ----------------------------------------------------------
    async def refresh(self, force: bool = False) -> bool:
        """Returns True if anything was fetched. Honours the 2 h floor."""
        async with self._lock:
            if self.is_fresh and not force:
                log.debug("tle cache hit (age %.0fs) — no request made", self.cache_age_s)
                return False
            if self.s.offline:
                log.info("GS_OFFLINE=1 — serving cached TLEs only")
                return False

            fetched = await self._fetch_celestrak_group()
            if not fetched:
                fetched = await self._fetch_satnogs_pinned()

            if fetched:
                self._sats.update(fetched)
                self._fetched_at = datetime.now(timezone.utc)
                self._save_cache()
                log.info("TLE refresh: %d satellites from %s", len(fetched), self._source)
                return True

            log.warning("TLE refresh failed; continuing on cache (age %.0fs)",
                        self.cache_age_s)
            return False

    async def _fetch_celestrak_group(self) -> dict[int, dict]:
        params = {"GROUP": self.s.celestrak_group, "FORMAT": "tle"}
        try:
            async with httpx.AsyncClient(
                timeout=30, headers={"User-Agent": USER_AGENT}
            ) as client:
                resp = await client.get(CELESTRAK_GROUP_URL, params=params)
            if resp.status_code != 200:
                # Terminal by policy: do not retry, do not fall into a loop.
                log.error("Celestrak returned %s — not retrying: %s",
                          resp.status_code, resp.text[:200])
                return {}
            self._source = f"celestrak:{self.s.celestrak_group}"
            return _parse_3le(resp.text)
        except httpx.HTTPError as exc:
            log.error("Celestrak unreachable: %s", exc)
            return {}

    async def _fetch_satnogs_pinned(self) -> dict[int, dict]:
        """Fallback: fetch only the pinned satellites from SatNOGS DB."""
        out: dict[int, dict] = {}
        headers = {"User-Agent": USER_AGENT}
        if self.s.satnogs_db_token:
            headers["Authorization"] = f"Token {self.s.satnogs_db_token}"
        try:
            async with httpx.AsyncClient(timeout=20, headers=headers) as client:
                for norad in self.s.pinned_norad_ids:
                    resp = await client.get(
                        f"{self.s.satnogs_db}/tle/", params={"norad_cat_id": norad}
                    )
                    if resp.status_code != 200:
                        continue
                    for entry in resp.json():
                        out[int(entry["norad_cat_id"])] = {
                            # SatNOGS prefixes the name with the 3LE "0 " marker.
                            "name": entry["tle0"].removeprefix("0 ").strip(),
                            "tle1": entry["tle1"],
                            "tle2": entry["tle2"],
                        }
        except httpx.HTTPError as exc:
            log.error("SatNOGS DB unreachable: %s", exc)
            return {}
        if out:
            self._source = "satnogs-db"
        return out

    # --- access ------------------------------------------------------------
    def get(self, norad: int) -> TleInfo | None:
        entry = self._sats.get(norad)
        if not entry:
            return None
        epoch = parse_tle_epoch(entry["tle1"])
        age_days = (
            (datetime.now(timezone.utc) - epoch).total_seconds() / 86400.0
            if epoch else 0.0
        )
        state = "fresh"
        if age_days >= self.s.tle_stale_crit_d:
            state = "stale"
        elif age_days >= self.s.tle_stale_warn_d:
            state = "aging"
        return TleInfo(
            norad=norad,
            name=entry.get("name", ""),
            tle1=entry["tle1"],
            tle2=entry["tle2"],
            source=self._source,
            fetched_at=self._fetched_at or datetime.now(timezone.utc),
            epoch=epoch,
            age_days=round(age_days, 2),
            state=state,
        )

    def catalog(self) -> list[dict]:
        """Everything available to the satellite selector, pinned entries first."""
        pinned = set(self.s.pinned_norad_ids)
        items = [
            {"norad": norad, "name": entry.get("name", str(norad)),
             "pinned": norad in pinned}
            for norad, entry in self._sats.items()
        ]
        items.sort(key=lambda it: (not it["pinned"], it["name"]))
        return items

    def __len__(self) -> int:
        return len(self._sats)


def _parse_3le(text: str) -> dict[int, dict]:
    """Parse Celestrak's 3-line format.

    Note: the TLE format cannot represent Celestrak's newer 6- and 9-digit
    catalog numbers, which are simply omitted from TLE-format responses. If we
    ever need those objects, switch to FORMAT=json and OMM parsing.
    """
    out: dict[int, dict] = {}
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    for i in range(0, len(lines) - 2, 3):
        name, tle1, tle2 = lines[i], lines[i + 1], lines[i + 2]
        if not (tle1.startswith("1 ") and tle2.startswith("2 ")):
            continue
        try:
            norad = int(tle1[2:7])
        except ValueError:
            continue
        out[norad] = {"name": name.removeprefix("0 ").strip(), "tle1": tle1, "tle2": tle2}
    return out
