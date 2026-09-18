"""Client for network.satnogs.org - stations, observations and booking.

The booking contract, confirmed against the live API:

    POST /api/observations/
    Authorization: Token <network api token>
    [{"ground_station": 5024, "transmitter_uuid": "<22 chars>",
      "start": "2026-09-18 07:13:20", "end": "2026-09-18 07:22:07"}]

Three details bite if you get them wrong:

* The body is a **list**. Posting a bare object is rejected with
  "Expected a list of items but got type \"dict\"".
* Datetimes are ``%Y-%m-%d %H:%M:%S`` in UTC. ISO-8601 with ``T`` and ``Z`` is
  rejected outright - the serializer names those two formats and no others.
* The station must be connected and have a location, or the whole batch fails.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .cache import Cache
from .config import HISTORY_TTL_S, STATIONS_ALL_TTL_S, STATION_TTL_S, Settings
from .http import SatnogsHTTPError, make_session, paginate, request

log = logging.getLogger(__name__)

API_DATETIME = "%Y-%m-%d %H:%M:%S"


def format_api_datetime(when: datetime) -> str:
    """Render a datetime the way the Network API's serializer demands."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime(API_DATETIME)


def parse_api_datetime(text: str) -> datetime:
    """Parse the ISO-8601 the API hands back when reading observations."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Antenna:
    band: str
    low_hz: float
    high_hz: float
    kind: str


@dataclass
class Station:
    id: int
    name: str
    lat: float
    lng: float
    altitude_m: float
    min_horizon: float
    min_culmination: float
    status: str
    is_connected: bool
    # When a limit is "hard" the station owner means it, and we must not go
    # below it however the run was invoked. When it is soft, a CLI value wins -
    # which is what the official tool does with -m, and why a `-m 3` run
    # against this station records 3 degree grazing passes.
    horizon_hard_limit: bool = False
    min_culmination_hard_limit: bool = False
    antennas: list[Antenna] = field(default_factory=list)
    observations: int = 0
    future_observations: int = 0
    success_rate: int = 0
    owner: str = ""
    qthlocator: str = ""

    @property
    def segments(self) -> list[tuple[float, float]]:
        return [(a.low_hz, a.high_hz) for a in self.antennas]

    @property
    def schedulable(self) -> bool:
        # The API enforces exactly this before accepting a booking.
        return self.is_connected and self.lat is not None and self.lng is not None

    @classmethod
    def from_api(cls, raw: dict) -> "Station":
        antennas = [
            Antenna(
                band=a.get("band", ""),
                low_hz=float(a["frequency"]),
                high_hz=float(a["frequency_max"]),
                kind=a.get("antenna_type_name", ""),
            )
            for a in raw.get("antenna", [])
            if a.get("frequency") and a.get("frequency_max")
        ]
        return cls(
            id=int(raw["id"]),
            name=raw.get("name", ""),
            lat=float(raw["lat"]),
            lng=float(raw["lng"]),
            altitude_m=float(raw.get("altitude") or 0.0),
            # These two come from the station, never from a constant. Using a
            # 5 degree default against a station that publishes 0 makes every
            # predicted AOS about 80 seconds late.
            min_horizon=float(raw.get("min_horizon") or 0.0),
            min_culmination=float(raw.get("min_culmination") or 0.0),
            status=raw.get("status", ""),
            is_connected=bool(raw.get("is_connected")),
            horizon_hard_limit=bool(raw.get("horizon_hard_limit")),
            min_culmination_hard_limit=bool(raw.get("min_culmination_hard_limit")),
            antennas=antennas,
            observations=int(raw.get("observations") or 0),
            future_observations=int(raw.get("future_observations") or 0),
            success_rate=int(raw.get("success_rate") or 0),
            owner=raw.get("owner", ""),
            qthlocator=raw.get("qthlocator", ""),
        )


@dataclass(frozen=True)
class Booking:
    """An observation already on the station's calendar."""
    id: int
    norad_cat_id: int
    start: datetime
    end: datetime
    status: str


@dataclass
class ScheduleResult:
    submitted: int = 0
    accepted: int = 0
    errors: list[str] = field(default_factory=list)


class NetworkClient:
    def __init__(self, settings: Settings, cache: Cache) -> None:
        self.s = settings
        self.cache = cache
        self.session = make_session(settings.network_token)

    # -- reads ---------------------------------------------------------------

    def raw_station(self, station_id: int) -> dict:
        def fetch() -> dict:
            url = f"{self.s.network_base_url}/stations/"
            resp = request(self.session, "GET", url, params={"id": station_id, "format": "json"})
            rows = resp.json()
            if not rows:
                raise RuntimeError(f"station {station_id} does not exist on SatNOGS Network")
            return rows[0]

        return self.cache.get_or_fetch(f"network-station-{station_id}", STATION_TTL_S, fetch)

    def get_station(self, station_id: int) -> Station:
        return Station.from_api(self.raw_station(station_id))

    def all_stations(self) -> list[Station]:
        """Every station SatNOGS Network knows about, schedulable ones only.

        Used by the network campaign scheduler to find candidate stations for
        the mission satellite - there is no server-side "list stations that
        can hear this transmitter" filter, so this fetches the whole
        catalogue (~160+ stations, walked across several cursor pages, same
        pagination style as future_bookings()) and the caller filters by
        antenna coverage itself via DbClient.transmitters_for_station().
        """
        def fetch() -> list[dict]:
            url = f"{self.s.network_base_url}/stations/"
            return list(paginate(self.session, url, params={"format": "json"}, max_pages=50))

        raw = self.cache.get_or_fetch("network-stations-all", STATIONS_ALL_TTL_S, fetch)
        stations = []
        for row in raw:
            try:
                stations.append(Station.from_api(row))
            except (KeyError, ValueError, TypeError) as exc:
                # A station with no location set yet (lat/lng missing) is not
                # schedulable anyway - skip it rather than losing the whole
                # ~160-station fetch to one malformed record.
                log.warning("skipping unparseable station %r: %s", row.get("id"), exc)
        return [s for s in stations if s.schedulable]

    def future_bookings(self, station_id: int, now: datetime | None = None) -> list[Booking]:
        """Every observation already booked on this station that has not ended.

        The feed is ordered by start descending - the furthest-future
        observation first - so we can stop walking as soon as we reach one that
        started in the past. That keeps this to two or three pages instead of
        the station's entire history.
        """
        now = now or datetime.now(timezone.utc)
        url = f"{self.s.network_base_url}/observations/"
        params = {"ground_station": station_id, "format": "json"}

        def reached_the_past(raw: dict) -> bool:
            return parse_api_datetime(raw["start"]) < now

        bookings: list[Booking] = []
        for raw in paginate(self.session, url, params=params, stop=reached_the_past, max_pages=20):
            try:
                bookings.append(
                    Booking(
                        id=int(raw["id"]),
                        norad_cat_id=int(raw.get("norad_cat_id") or 0),
                        start=parse_api_datetime(raw["start"]),
                        end=parse_api_datetime(raw["end"]),
                        status=raw.get("status", ""),
                    )
                )
            except (KeyError, ValueError) as exc:
                log.warning("skipping an unparseable observation: %s", exc)
        log.info("station %d has %d observation(s) already booked", station_id, len(bookings))
        return bookings

    def observation_history(self, station_id: int, pages: int = 12) -> Counter:
        """How many times this station has observed each satellite.

        This is our "under-observed" signal, and it is deliberately
        station-local. Network-wide counts are not reachable at a sane cost:
        ``/observations/?norad_cat_id=X`` takes about 17 seconds per query and
        pages 25 at a time, so scoring a hundred candidates that way would take
        the better part of an hour. The DB's ``reception_status`` field is no
        help either - 2776 of 2779 satellites report "Unknown".

        Counting our own history is cheap by comparison, cached for a day, and
        answers the question that actually matters for one station: what have
        we been neglecting?
        """
        # The depth is part of the identity: a 40-page crawl is a different
        # answer from a 12-page one, and asking for more must not be served
        # a shallower cached result.
        key = f"history-{station_id}-p{pages}"

        def crawl() -> dict[str, int]:
            url = f"{self.s.network_base_url}/observations/"
            params = {"ground_station": station_id, "format": "json"}
            counts: Counter = Counter()
            seen = 0
            for raw in paginate(self.session, url, params=params, max_pages=pages):
                norad = raw.get("norad_cat_id")
                if norad:
                    counts[str(int(norad))] += 1
                seen += 1
            log.info("crawled %d past observations for station %d", seen, station_id)
            return dict(counts)

        raw_counts = self.cache.get_or_fetch(key, HISTORY_TTL_S, crawl)
        return Counter({int(k): v for k, v in raw_counts.items()})

    # -- the write ------------------------------------------------------------

    def schedule(self, items: list[dict], execute: bool = False) -> ScheduleResult:
        """Book observations. Without ``execute`` this does nothing at all.

        On a batch rejection we retry one at a time, because the API fails the
        whole list if any single entry is bad - usually a pass that somebody
        else booked in the seconds since we read the calendar - and losing
        nineteen good bookings to one stale one is not a good trade.

        This is the *only* place ``settings.network_token`` matters anywhere
        in this package - every read call (``get_station``, ``future_bookings``,
        ``observation_history``) is unauthenticated. The dashboard's
        ScheduleService never calls this method (it only ever calls ``plan()``,
        never ``schedule --execute``), so a network token entered there has no
        effect and booking stays off regardless of what is configured.
        """
        result = ScheduleResult(submitted=len(items))
        if not items:
            return result
        if not execute:
            log.info("dry run: %d observation(s) not submitted", len(items))
            return result
        if not self.s.network_token:
            raise RuntimeError(
                "SATNOGS_NETWORK_TOKEN is not set, so nothing can be booked. "
                "Put it in .env or the environment."
            )

        url = f"{self.s.network_base_url}/observations/"
        try:
            request(self.session, "POST", url, json_body=items)
        except SatnogsHTTPError as exc:
            log.warning("the batch was rejected (%s); retrying one at a time", exc.status)
            log.debug("batch rejection body: %s", exc.body)
            for item in items:
                try:
                    request(self.session, "POST", url, json_body=[item])
                except SatnogsHTTPError as single:
                    result.errors.append(
                        f"{item['start']} norad-transmitter {item['transmitter_uuid']}: "
                        f"HTTP {single.status} {single.body[:300]}"
                    )
                else:
                    result.accepted += 1
        else:
            result.accepted = len(items)
        return result


def to_schedule_item(station_id: int, transmitter_uuid: str,
                     start: datetime, end: datetime) -> dict:
    """Build one entry of the POST body, in the only format the API accepts."""
    return {
        "ground_station": station_id,
        "transmitter_uuid": transmitter_uuid,
        "start": format_api_datetime(start),
        "end": format_api_datetime(end),
    }
