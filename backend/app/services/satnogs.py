"""SatNOGS Network API.

This serves two very different consumers, which is why it is one service and
not two. The activity panel wants to *show* what station 5024 has been doing;
the rotator interlock needs to *know* whether satnogs-client is about to move
the antenna out from under us. Both read the same three endpoints, and polling
them twice would double the request rate against a volunteer-run service.

Three things about this API cost time to find:

**`satellite__norad_cat_id` does not filter.** It is accepted and silently
ignored, so you get every satellite and no error. The working parameter is
`norad_cat_id`.

**Pagination is cursor-based through the `Link` header.** There is no `?page=`;
the next URL arrives in `Link: <...>; rel="next"`. We deliberately do not
follow it for the feed — the first page is the newest and that is all a wall
display shows — but the parser exists because a caller that walks history needs
it and will otherwise reinvent it wrongly.

**Staleness is a safety input, not a cosmetic one.** The interlock asks this
service whether satnogs-client is connected. If our answer is minutes old it is
not evidence of anything, so `station_age_s` is published and the gate fails
closed past `GS_GATE_MAX_STALE_S`. Never let a cached "all clear" authorise a
move.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

import httpx

from ..config import Settings
from ..hub import hub

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 20.0
FEED_LIMIT = 12
_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def next_page_url(link_header: str | None) -> str | None:
    """The `next` URL out of an RFC 5988 Link header, if there is one."""
    if not link_header:
        return None
    match = _LINK_NEXT.search(link_header)
    return match.group(1) if match else None


class SatnogsService:
    """Polls station status, scheduled jobs and recent observations."""

    def __init__(self, settings: Settings, on_state=None) -> None:
        self.s = settings
        self.on_state = on_state or (lambda component, state, detail="": None)

        self.station: dict | None = None
        self.jobs: list[dict] = []
        self.observations: list[dict] = []

        # Monotonic stamps: the gate reasons about age, and wall-clock would let
        # an NTP step or a DST change silently authorise a move.
        self._station_at: float | None = None
        self._jobs_at: float | None = None

    # --- freshness ---------------------------------------------------------
    def _age(self, stamp: float | None) -> float | None:
        return None if stamp is None else time.monotonic() - stamp

    @property
    def station_age_s(self) -> float | None:
        return self._age(self._station_at)

    @property
    def jobs_age_s(self) -> float | None:
        return self._age(self._jobs_at)

    @property
    def is_connected(self) -> bool | None:
        """Whether satnogs-client is talking to the network.

        None means we do not know, which callers must treat as "assume it is",
        never as "it is not".
        """
        if self.station is None:
            return None
        return bool(self.station.get("is_connected"))

    def seconds_to_next_job(self, now: datetime | None = None) -> float | None:
        """Time until the next scheduled observation starts, or None if idle.

        A job already in progress returns 0.0 rather than a negative number, so
        a caller comparing against a guard window cannot read "in progress" as
        "comfortably in the past".
        """
        now = now or datetime.now(timezone.utc)
        soonest: float | None = None
        for job in self.jobs:
            start = _parse_ts(job.get("start"))
            end = _parse_ts(job.get("end"))
            if start is None:
                continue
            if end is not None and start <= now <= end:
                return 0.0
            delta = (start - now).total_seconds()
            if delta >= 0 and (soonest is None or delta < soonest):
                soonest = delta
        return soonest

    # --- HTTP --------------------------------------------------------------
    async def _get(self, client: httpx.AsyncClient, path: str,
                   params: dict) -> tuple[list | dict | None, str | None]:
        url = f"{self.s.satnogs_network}{path}"
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json(), resp.headers.get("Link")

    async def refresh_station(self, client: httpx.AsyncClient) -> None:
        data, _ = await self._get(client, "/stations/", {"id": self.s.station_id})
        if isinstance(data, list) and data:
            self.station = data[0]
            self._station_at = time.monotonic()

    async def refresh_jobs(self, client: httpx.AsyncClient) -> None:
        data, _ = await self._get(
            client, "/jobs/", {"ground_station": self.s.station_id}
        )
        # An empty job list is a real, meaningful answer — the station has
        # nothing scheduled — so it must stamp freshness like any other.
        self.jobs = data if isinstance(data, list) else []
        self._jobs_at = time.monotonic()

    async def refresh_observations(self, client: httpx.AsyncClient) -> None:
        data, _ = await self._get(
            client, "/observations/", {"ground_station": self.s.station_id}
        )
        if not isinstance(data, list):
            return
        self.observations = [self._summarise(o) for o in data[:FEED_LIMIT]]

    @staticmethod
    def _summarise(obs: dict) -> dict:
        """Only the fields the panel renders; the raw records are large."""
        return {
            "id": obs.get("id"),
            "norad": obs.get("norad_cat_id"),
            "name": obs.get("tle0") or "",
            "transmitter": obs.get("transmitter_description") or "",
            "start": obs.get("start"),
            "end": obs.get("end"),
            "status": obs.get("status"),
            "vetted_status": obs.get("vetted_status"),
            "waterfall": bool(obs.get("waterfall")),
            "demoddata": len(obs.get("demoddata") or []),
            "url": f"https://network.satnogs.org/observations/{obs.get('id')}/",
        }

    # --- published state ---------------------------------------------------
    def snapshot(self) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "station": None if self.station is None else {
                "id": self.station.get("id"),
                "name": self.station.get("name"),
                "status": self.station.get("status"),
                "is_connected": self.station.get("is_connected"),
                "last_seen": self.station.get("last_seen"),
                "observations": self.station.get("observations"),
                "min_horizon": self.station.get("min_horizon"),
            },
            "station_age_s": self.station_age_s,
            "jobs_age_s": self.jobs_age_s,
            "jobs": [
                {
                    "id": j.get("id"),
                    "norad": j.get("norad_cat_id"),
                    "start": j.get("start"),
                    "end": j.get("end"),
                    "frequency": j.get("frequency"),
                    "mode": j.get("mode"),
                    "tle0": j.get("tle0"),
                }
                for j in self.jobs
            ],
            "seconds_to_next_job": self.seconds_to_next_job(now),
            "observations": self.observations,
        }

    def publish(self) -> None:
        hub.publish("satnogs", self.snapshot())

    # --- lifecycle ---------------------------------------------------------
    async def run(self) -> None:
        if self.s.offline:
            self.on_state("satnogs", "degraded", "offline mode")
            return

        # Each endpoint has its own cadence, so this loop ticks at the shortest
        # and runs each refresh when its own interval has elapsed.
        due = {"station": 0.0, "jobs": 0.0, "obs": 0.0}
        intervals = {
            "station": float(self.s.satnogs_station_poll_s),
            "jobs": float(self.s.satnogs_jobs_poll_s),
            "obs": float(self.s.satnogs_obs_poll_s),
        }
        tick = min(intervals.values())
        failures = 0

        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_S,
            headers={"User-Agent": f"knacksat2-groundstation/{self.s.station_id}"},
        ) as client:
            while True:
                now = time.monotonic()
                work = {
                    "station": self.refresh_station,
                    "jobs": self.refresh_jobs,
                    "obs": self.refresh_observations,
                }
                ran = False
                try:
                    for key, fn in work.items():
                        if now >= due[key]:
                            await fn(client)
                            due[key] = now + intervals[key]
                            ran = True
                    if ran:
                        failures = 0
                        self.on_state("satnogs", "ok")
                        self.publish()
                except httpx.HTTPError as exc:
                    failures += 1
                    # One blip on a volunteer-run service is not an outage, and
                    # flapping the health chip teaches operators to ignore it.
                    if failures >= 3:
                        self.on_state("satnogs", "down", str(exc))
                    log.warning("SatNOGS poll failed (%d): %s", failures, exc)
                    # Publish anyway: the interlock reads staleness off these
                    # frames, and silence would leave it showing a stale
                    # all-clear rather than an ageing one.
                    self.publish()

                await asyncio.sleep(tick)
