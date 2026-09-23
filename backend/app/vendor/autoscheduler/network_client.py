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

Reads are rate-limited by the server and writes are not; see
``RateLimitedSession`` below for the published budgets and for what this
client does to stay inside them.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from .cache import Cache
from .config import HISTORY_TTL_S, STATIONS_ALL_TTL_S, STATION_TTL_S, Settings
from .http import (
    SatnogsHTTPError, SatnogsOutcomeUnknown, make_session, paginate, request,
)

log = logging.getLogger(__name__)

API_DATETIME = "%Y-%m-%d %H:%M:%S"


# -- staying inside the Network API's published read budget -------------------

# satnogs-network throttles its *list* endpoints, and publishes the rates in
# its own settings - `DEFAULT_THROTTLE_RATES` in `network/settings.py`, with
# the scopes wired to views in `network/api/throttling.py` and
# `network/api/views.py`:
#
#     /api/observations/  list    60/hour anonymous, 240/hour with a token
#     /api/stations/      list   256/hour anonymous, unthrottled with a token
#
# Only the `list` action carries a throttle class - fetching one observation
# by id is not throttled - and both observation throttles return early for
# POST and PUT, so booking itself never spends any of this. Every read this
# client makes is a list request, `raw_station()` included: `/stations/?id=N`
# is a *filtered list*, not a detail lookup, and counts like one.
OBSERVATION_LIST_PER_HOUR_ANON = 60
OBSERVATION_LIST_PER_HOUR_AUTH = 240
# Authenticated station reads are not throttled at all - that scope only has
# an AnonRateThrottle - but there is no reason to burst harder merely because
# a token is present, so the anonymous ceiling applies either way.
STATION_LIST_PER_HOUR = 256

# The server counts with a sliding window an hour wide, so we do too. A fixed
# bucket would let us fire two full budgets back to back across its boundary.
THROTTLE_WINDOW_S = 3600.0

# The longest this will block a caller waiting for budget to come free. Past
# this the budget is genuinely spent, and sleeping on it would hang the
# dashboard for the better part of an hour; the caller is told instead and
# gets to decide - see `build_campaign`, which stops and keeps its partial run.
MAX_PACING_WAIT_S = 30.0

# A 429 names its own wait in `Retry-After`, which DRF writes as whole
# seconds. Honour it, but not unboundedly: a very long one is a signal to stop
# for now, not to sit blocked on a socket.
MAX_RETRY_AFTER_WAIT_S = 120.0

# How many times a single request may be re-sent after a 429. Deliberately
# small - the point of honouring Retry-After is to stop asking, not to ask
# politely in a loop.
MAX_THROTTLE_RETRIES = 2

# Used only when a 429 arrives without a `Retry-After` we can read.
THROTTLE_BACKOFF_BASE_S = 5.0


class RateLimitedError(SatnogsHTTPError):
    """The Network API is rate-limiting us, or is about to be.

    Distinct from a plain `SatnogsHTTPError` because the two mean opposite
    things to a caller: a 400 is about the one request that was sent, and the
    next request may well be fine, whereas a throttle is about the budget and
    so applies to every request still to come.
    """


class _Gate:
    """The read budget one credential has spent, shared across clients.

    CampaignService builds a new NetworkClient - and so a new session - for
    every preview, commit and verify. When each one kept its own count, the
    count started from zero every time while the server's did not, so a verify
    or a second preview in the same hour walked straight into real 429s: after
    a full preview the next one returned 0 items four minutes later, and a
    cross-check could hang for the better part of an hour. The server counts
    per token (per IP when anonymous), so this counts per credential too.

    `blocked_until` holds a Retry-After deadline the server set, so that once
    SatNOGS has said "not for an hour" nothing in this process asks again
    before then - it is refused here, locally, without sending anything.
    """

    def __init__(self) -> None:
        self.spent: dict[str, deque] = {}
        self.blocked_until: dict[str, float] = {}
        self.lock = threading.Lock()


_GATES: dict[str, _Gate] = {}
_GATES_LOCK = threading.Lock()


def _shared_gate(key: str) -> _Gate:
    with _GATES_LOCK:
        gate = _GATES.get(key)
        if gate is None:
            gate = _GATES[key] = _Gate()
        return gate


def gate_key(base_url: str, token: str) -> str:
    """Which shared budget a client belongs to. The token is hashed, never kept."""
    digest = hashlib.sha256(token.encode()).hexdigest()[:16] if token else "anonymous"
    return f"{base_url}|{digest}"


class RateLimitedSession:
    """A `requests.Session` that keeps this client inside network.satnogs.org's
    published read budget, and obeys a 429 rather than arguing with it.

    Wrapping the session rather than the call sites is deliberate:
    `http.paginate()` walks cursor pages by calling `session.request()` itself,
    so anything hooked onto `http.request()` alone would pace the first page of
    a crawl and none of the rest - and a crawl is where the budget actually goes.

    The count kept here is this process's own traffic only. The server counts
    per IP when anonymous and per token when not, and the dashboard's waterfall
    and telemetry panels reach SatNOGS over their own httpx clients without
    passing through here, so this count is a floor and never a guarantee. That
    is precisely why the 429 path below has to be right as well: the pacing is
    the courtesy, and honouring Retry-After is the part that keeps the account.
    """

    def __init__(self, session: requests.Session, *, authenticated: bool,
                 sleep=time.sleep, clock=time.monotonic,
                 share_key: str | None = None) -> None:
        self._session = session
        self._budgets = {
            "observations": (OBSERVATION_LIST_PER_HOUR_AUTH if authenticated
                             else OBSERVATION_LIST_PER_HOUR_ANON),
            "stations": STATION_LIST_PER_HOUR,
        }
        # With a share_key the count lives in a process-wide gate that every
        # client on the same credential uses; without one (tests, one-offs) it
        # is private to this session, as before.
        self._gate = _shared_gate(share_key) if share_key else _Gate()
        for scope in self._budgets:
            self._gate.spent.setdefault(scope, deque())
        self._spent = self._gate.spent
        self._sleep = sleep
        self._clock = clock

    def __getattr__(self, name):
        # `headers`, `close()` and the rest still belong to the real session.
        return getattr(self._session, name)

    @staticmethod
    def _scope(method: str, url: str) -> str | None:
        """Which server-side budget this request spends, if any."""
        if method.upper() not in ("GET", "HEAD"):
            # POST and PUT are explicitly exempted by the server's own throttle
            # classes, so a booking must never be delayed or refused by us.
            return None
        if "/observations" in url:
            return "observations"
        if "/stations" in url:
            return "stations"
        return None

    def _claim(self, scope: str) -> None:
        """Take a slot in `scope`'s budget, waiting briefly if one is close."""
        now = self._clock()
        blocked = self._gate.blocked_until.get(scope, 0.0)
        if now < blocked:
            raise RateLimitedError(
                f"SatNOGS asked for no {scope} reads for another "
                f"{blocked - now:.0f}s; not asking again before then",
                status=429,
            )
        with self._gate.lock:
            self._claim_locked(scope)

    def _claim_locked(self, scope: str) -> None:
        limit = self._budgets[scope]
        spent = self._spent[scope]
        now = self._clock()
        while spent and now - spent[0] >= THROTTLE_WINDOW_S:
            spent.popleft()
        if len(spent) >= limit:
            wait = THROTTLE_WINDOW_S - (now - spent[0])
            if wait > MAX_PACING_WAIT_S:
                raise RateLimitedError(
                    f"the {scope} read budget ({limit}/hour) is spent; the next "
                    f"slot is {wait:.0f}s away",
                    status=429,
                )
            self._sleep(wait)
            spent.popleft()
        spent.append(self._clock())

    @staticmethod
    def _retry_after(resp: requests.Response, attempt: int) -> float:
        """How long the server asked us to wait, in seconds.

        DRF writes `Retry-After` as whole seconds. RFC 9110 also allows an
        HTTP-date there; nothing on this API sends one, and if something ever
        does, the unparseable value falls through to a backoff rather than
        being read as zero.
        """
        try:
            wait = float(resp.headers.get("Retry-After", ""))
        except ValueError:
            wait = -1.0
        if wait < 0:
            wait = THROTTLE_BACKOFF_BASE_S * (2 ** attempt)
        # Returned uncapped. The caller decides: a short wait is slept out, a
        # long one is a stop - capping it here is what turned "come back in an
        # hour" into "try again in two minutes, twice".
        return wait

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        scope = self._scope(method, url)
        if scope is None:
            return self._session.request(method, url, **kwargs)

        resp = None
        for attempt in range(MAX_THROTTLE_RETRIES + 1):
            self._claim(scope)
            resp = self._session.request(method, url, **kwargs)
            if resp.status_code != 429:
                return resp
            wait = self._retry_after(resp, attempt)
            if wait > MAX_RETRY_AFTER_WAIT_S:
                # A wait longer than we are prepared to hold a thread open for
                # is the server saying "stop for now", and it has to be obeyed
                # as that: sleeping MAX_RETRY_AFTER_WAIT_S and re-sending, as
                # this used to, just earns another 429 each time. Remember the
                # deadline so nothing in this process asks again before it.
                self._gate.blocked_until[scope] = self._clock() + wait
                raise RateLimitedError(
                    f"{method} {url} -> HTTP 429 with Retry-After {wait:.0f}s; "
                    "not waiting it out and not asking again before then",
                    status=429,
                    body=resp.text[:2000],
                )
            if attempt >= MAX_THROTTLE_RETRIES:
                break
            log.warning(
                "SatNOGS answered 429 on a %s read; waiting %.0fs before "
                "attempt %d of %d", scope, wait, attempt + 2, MAX_THROTTLE_RETRIES + 1,
            )
            self._sleep(wait)

        raise RateLimitedError(
            f"{method} {url} -> HTTP 429 after {MAX_THROTTLE_RETRIES + 1} attempt(s)",
            status=429,
            body=(resp.text[:2000] if resp is not None else ""),
        )


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
        # The API enforces exactly this before accepting a booking. A
        # "Testing" station rejects scheduling from anyone but its owner -
        # surfaces from the real API as "No permission to schedule
        # observations on station: N" - and an "Offline" one will never
        # actually perform whatever gets booked on it.
        return (
            self.is_connected and self.lat is not None and self.lng is not None
            and self.status == "Online"
        )

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
    # The very dicts that were passed in, for the ones the API took. Counts
    # alone cannot answer "which of my bookings actually landed?", which is
    # what the caller needs to cross-check a run against the real calendar
    # afterwards - and on a partial batch rejection the accepted set is not
    # derivable from `errors` without parsing its prose back apart.
    accepted_items: list[dict] = field(default_factory=list)
    # Sent, but no reliable answer came back - they may be on the station's
    # calendar or not. Kept apart from accepted_items (which would claim a
    # booking nobody confirmed) and from a plain error (which would invite a
    # resubmit that duplicates any that did land). These are what a
    # cross-check must read back before anyone tries again.
    uncertain_items: list[dict] = field(default_factory=list)


class NetworkClient:
    def __init__(self, settings: Settings, cache: Cache) -> None:
        self.s = settings
        self.cache = cache
        # Every read below goes through the rate-limit gate. Whether a token is
        # configured changes the budget the server applies to us (60/hour
        # anonymous against 240/hour authenticated on the observation feed), so
        # the gate is told which one it is working with.
        self.session = RateLimitedSession(
            make_session(settings.network_token),
            authenticated=bool(settings.network_token),
            # One budget per credential for the whole process, however many
            # clients are built - see _Gate.
            share_key=gate_key(settings.network_base_url, settings.network_token),
        )

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
        """Every observation on this station that has not yet STARTED.

        Not "has not ended" - the stop predicate below ends the walk at the
        first observation whose start is already past, and does not yield it.
        An observation currently in progress is therefore both excluded and a
        hard stop, so nothing after it in the feed is seen either. That is
        fine for the two things that use this - checking for conflicts before
        booking, and reconciling a run's own just-booked future passes - but
        it is not a picture of the live calendar, and code that needs one has
        to ask differently.

        The feed is ordered by start descending - the furthest-future
        observation first - which is what makes that early stop safe: every
        future observation has already been yielded by the time we reach a
        past one. It keeps this to two or three pages instead of the station's
        entire history.
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
        except SatnogsOutcomeUnknown as exc:
            # Caught before SatnogsHTTPError, which it subclasses. The batch
            # may have been applied, so the one-at-a-time fallback below - which
            # exists for a batch the server REJECTED - would re-create every
            # observation that did land. Stop, and say what is not known.
            log.error("booking outcome unknown for all %d item(s): %s", len(items), exc)
            result.uncertain_items = list(items)
            result.errors.append(
                f"OUTCOME UNKNOWN for all {len(items)} submitted item(s): {exc}. "
                "Some or all may be booked. Do NOT resubmit - cross-check the "
                "station calendars first."
            )
            return result
        except SatnogsHTTPError as exc:
            if exc.status is None:
                # No status means the connection never completed on any
                # attempt, so nothing reached the server. Trying each item on
                # its own would only fail the same way len(items) more times.
                log.warning("could not reach SatNOGS to submit %d item(s): %s", len(items), exc)
                result.errors.append(
                    f"could not reach SatNOGS to submit {len(items)} item(s): {exc}. "
                    "Nothing was booked."
                )
                return result
            log.warning("the batch was rejected (%s); retrying one at a time", exc.status)
            log.debug("batch rejection body: %s", exc.body)
            for position, item in enumerate(items):
                where = (f"station {item.get('ground_station')} {item['start']} "
                         f"transmitter {item['transmitter_uuid']}")
                try:
                    request(self.session, "POST", url, json_body=[item])
                except SatnogsOutcomeUnknown as single:
                    result.uncertain_items.append(item)
                    result.errors.append(f"{where}: OUTCOME UNKNOWN - {single}")
                except SatnogsHTTPError as single:
                    if single.status is None:
                        # No status: the connection never opened on any attempt,
                        # so SatNOGS became unreachable part-way through. Every
                        # remaining item would fail the same way, three tries
                        # each; say what happened to them instead.
                        rest = items[position:]
                        result.errors.append(
                            f"could not reach SatNOGS for the last {len(rest)} item(s) "
                            f"({single}); they were not sent and nothing was booked "
                            "for them"
                        )
                        break
                    # The station id is in the message now. With 77 stations in
                    # a run, "HTTP 409" alone did not say which one refused.
                    result.errors.append(
                        f"{where}: HTTP {single.status} {single.body[:300]}"
                    )
                else:
                    result.accepted += 1
                    result.accepted_items.append(item)
        else:
            result.accepted = len(items)
            result.accepted_items = list(items)
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
