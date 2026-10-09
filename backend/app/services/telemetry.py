"""Decoded telemetry frames from SatNOGS DB.

A frame is the one unambiguous answer to "did we actually hear it". A waterfall
can look busy with interference and vetting is a human judgement that often
never happens, but a demodulated frame is a frame. So this is the station's log
of contact rather than a second view of the radio.

**The NORAD filter here is `satellite`, which is a third convention.**
`/telemetry/` takes `satellite=<norad id>`. It does not take `norad_cat_id`
(Network's spelling) and it does not take `satellite__norad_cat_id` — which is
what `/transmitters/` wants, on this same host. Unknown query parameters are
dropped silently, so either wrong spelling returns 200 with every satellite's
frames in it: the failure this project has already paid for twice.

That could not be checked against live records from here, because `/telemetry/`
answers 401 without a token. It was established from the service's own schema,
which is public even though the endpoint is not:

    curl -s 'https://db.satnogs.org/api/schema/?format=json' | python -c \
      "import json,sys; print([p['name'] for p in
       json.load(sys.stdin)['paths']['/api/telemetry/']['get']['parameters']])"

    ['app_source', 'cursor', 'end', 'format', 'is_decoded', 'observer',
     'sat_id', 'satellite', 'start', 'transmitter']

`satellite` is described there as "NORAD ID of a satellite to filter telemetry
data for", and neither other spelling appears at all. A schema is evidence and
not proof, so `refresh()` re-checks the records themselves on every fetch and
drops any whose `norad_cat_id` is not the one that was asked for — an ignored
filter shows up as that count being non-zero, which is a line in the log rather
than someone else's telemetry on the wall. When a token does turn up, that is
the thing to look for.

**Without a token this endpoint is closed, so there is a second source.**
`/telemetry/` is the only SatNOGS endpoint this dashboard touches that is not
public, and station 5024 has no token — which for as long as that was the only
source meant this panel had never once had anything to draw. `frames.py` reads
the demodulated frames out of SatNOGS *Network* instead, where they are public,
and this store falls back to it. So the order here is: the DB when a token
exists, because it is the only source that carries decoded *fields*; the
public frames when it does not, because bytes off the air still answer "did we
hear it". An empty or rejected token is a configuration state, not a fault —
it is logged once, carried to the panel as `detail` so it can ask for the
token, and the panel keeps showing frames from the other source meanwhile.

Anything already on disk is served whatever happens, because a station that
had a token last month has frames worth showing today, and a station that lost
its uplink to the internet an hour ago has not stopped being a ground station.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

from ..config import Settings
from .frames import NetworkFrameSource

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 25.0
USER_AGENT = "knacksat2-ground-station-dashboard/0.1 (+github.com/Viewzaza)"

# One page is what a panel shows. The endpoint is cursor-paginated (there is no
# ?page=), and following the cursor to build a longer history would be a
# different feature with a different cache.
MAX_FRAMES = 40

# The hex payload is nearly all of a record — a KNACKSAT-2 beacon runs to a few
# hundred bytes — and these go out over the WebSocket to every wall display.
# The head is enough to see the address field change between frames.
FRAME_HEAD_CHARS = 32
MAX_DECODED_FIELDS = 24
MAX_VALUE_CHARS = 64

# After a failed fetch, wait this long before trying again. The panel polls; a
# rejected token answered at panel rate is a request every few seconds forever,
# and a journal line with each one.
RETRY_COOLDOWN_S = 120.0

# What the panel switches on. Only "ok" means something newer will arrive.
OK = "ok"
UNKNOWN = "unknown"
NO_TOKEN = "no_token"
BAD_TOKEN = "bad_token"
OFFLINE = "offline"
UNREACHABLE = "unreachable"
# The DB answered and has nothing for this satellite. Not a fault and not worth
# a word on the panel — a satellite nobody has decoded this week is a fact —
# but still a reason to go and ask the public source before giving up.
EMPTY = "empty"

# Which of the two sources the frames on screen came from. The panel draws them
# differently because they are not the same thing: the DB's carry decoded
# fields, Network's carry bytes. Saying which is on screen is the difference
# between "the battery is at 3.9 V" and "something transmitted 165 bytes".
DB = "satnogs-db"
NETWORK = "satnogs-network"

# Said in the operator's terms, because this is what the panel renders when
# something is not right. "No data" is not a useful thing for a wall display to
# say when the fix is one line of .env. NO_TOKEN and BAD_TOKEN are notes rather
# than statuses now — neither one means there is nothing to show, because the
# public source still answers.
DETAIL = {
    OK: "",
    UNKNOWN: "not fetched yet",
    NO_TOKEN: "no GS_SATNOGS_DB_TOKEN — showing demodulated frames from SatNOGS "
              "Network; decoded fields need a db.satnogs.org token",
    BAD_TOKEN: "GS_SATNOGS_DB_TOKEN was rejected by db.satnogs.org — showing "
               "demodulated frames from SatNOGS Network instead",
    OFFLINE: "GS_OFFLINE=1 — showing cached frames only",
    UNREACHABLE: "SatNOGS could not be reached",
    EMPTY: "",
}


class TelemetryStore:
    """Recent frames per satellite, cached on disk.

    Frames only arrive when the satellite is overhead and someone is listening,
    so the cache is what makes this panel non-empty for the 23 hours a day in
    between — and after a restart, and on a station that has lost its uplink to
    the internet.
    """

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._by_norad: dict[int, list[dict]] = {}
        self._fetched_at: dict[int, datetime] = {}
        self._failed_at: dict[int, datetime] = {}
        self._status: dict[int, str] = {}
        self._source: dict[int, str] = {}
        # The DB path's own last outcome, tracked apart from `_status` purely
        # so "your token is being rejected" is said once rather than on every
        # poll for the months this display stays up.
        self._db_status: dict[int, str] = {}
        # Why the DB path is not the one in use, when it is not. Kept apart
        # from `_status` because it describes the configuration rather than the
        # data: a station with no token and a full panel is in both states at
        # once, and the panel has to be able to say so.
        self._note: dict[int, str] = {}
        self._network = NetworkFrameSource(settings)
        self._lock = asyncio.Lock()
        self._load_cache()

    # --- cache -------------------------------------------------------------
    @property
    def _path(self):
        return self.s.data_dir / "telemetry.json"

    def _load_cache(self) -> None:
        if not self._path.exists():
            return
        try:
            blob = json.loads(self._path.read_text(encoding="utf-8"))
            self._by_norad = {int(k): v for k, v in blob.get("sats", {}).items()}
            self._fetched_at = {
                int(k): datetime.fromisoformat(v)
                for k, v in blob.get("fetched_at", {}).items()
            }
            # Which source wrote them, so a display that comes up on cache
            # alone still labels the frames correctly. Frames cached before
            # this field existed are the DB's, because it was the only source.
            self._source = {int(k): v for k, v in blob.get("source", {}).items()}
            log.info("loaded telemetry for %d satellites from cache",
                     len(self._by_norad))
        except Exception as exc:                  # a corrupt cache is not fatal
            log.warning("ignoring unreadable telemetry cache: %s", exc)

    def _save_cache(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({
                "sats": {str(k): v for k, v in self._by_norad.items()},
                "fetched_at": {str(k): v.isoformat()
                               for k, v in self._fetched_at.items()},
                "source": {str(k): v for k, v in self._source.items()},
            }, indent=1),
            encoding="utf-8",
        )

    def age_s(self, norad: int) -> float:
        stamp = self._fetched_at.get(norad)
        if stamp is None:
            return float("inf")
        return (datetime.now(timezone.utc) - stamp).total_seconds()

    def is_fresh(self, norad: int) -> bool:
        return self.age_s(norad) < self.s.telemetry_ttl_s

    # --- fetching ----------------------------------------------------------
    async def refresh(self, norad: int, force: bool = False) -> str:
        """Fetch if the cache is cold. Returns why it did not, or `OK`.

        Every path returns a status; none of them raise. The caller is a route
        on a display that reloads for months, so "the token is missing" and
        "the internet is gone" have to be states this reports rather than
        exceptions it throws.
        """
        if self.is_fresh(norad) and not force:
            return self._status.get(norad, OK)
        if self.s.offline:
            return self._set(norad, OFFLINE)
        if not force and self._cooling_off(norad):
            return self._status.get(norad, UNKNOWN)

        async with self._lock:
            # Re-checked under the lock: two panels selecting the same
            # satellite at the same moment should make one request, not two.
            if self.is_fresh(norad) and not force:
                return self._status.get(norad, OK)

            # The DB first, and only with a token, because it is the only
            # source that carries decoded fields. An empty token is not worth a
            # request that is guaranteed to come back 401 — that is a crash
            # loop with extra steps against somebody else's service.
            if self.s.satnogs_db_token:
                status = await self._from_db(norad)
                if status == OK:
                    return status
                # A missing or rejected token is worth saying out loud, but it
                # is not a reason to show nothing: the public source has frames
                # for this satellite either way.
                self._note[norad] = DETAIL.get(status, "")
            else:
                self._note[norad] = DETAIL[NO_TOKEN]
                self._note_only(norad, NO_TOKEN, "")

            return await self._from_network(norad)

    async def _from_db(self, norad: int) -> str:
        """Decoded frames from db.satnogs.org. Needs a token; may return none."""
        headers = {
            "User-Agent": USER_AGENT,
            # The prefix is the DB's own: "Token-based authentication with
            # required prefix Token".
            "Authorization": f"Token {self.s.satnogs_db_token}",
        }
        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT_S, headers=headers
            ) as client:
                resp = await client.get(
                    f"{self.s.satnogs_db}/telemetry/",
                    # `satellite` — not `norad_cat_id`, and not
                    # `satellite__norad_cat_id` as /transmitters/ takes. See
                    # the module docstring; the wrong one is ignored rather
                    # than refused. Nothing else is filtered on, so there is
                    # exactly one parameter that can be wrong.
                    params={"satellite": norad, "format": "json"},
                )
            if resp.status_code in (401, 403):
                return self._note_only(norad, BAD_TOKEN, f"HTTP {resp.status_code}")
            if resp.status_code != 200:
                return self._note_only(norad, UNREACHABLE, f"HTTP {resp.status_code}")
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return self._note_only(norad, UNREACHABLE, str(exc))

        # Cursor-paginated: {"next", "previous", "results"}. A bare list is
        # accepted too, because /transmitters/ on the same service returns one
        # and there is no reason for this to be the thing that breaks.
        records = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            return self._note_only(norad, UNREACHABLE, "unexpected payload shape")

        frames = self._only(norad, [_summarise(f) for f in records])
        for f in frames:
            # `ours` and `observation_url` are Network's fields. The panel draws
            # one row whichever source filled it, so the DB's frames are given
            # the same two rather than leaving the panel to reconstruct them:
            # it would have to guess, and on a frame submitted over SiDS there
            # is no observation page to guess at, only a link that 404s.
            f["ours"] = f.get("station_id") == self.s.station_id
            f["observation_url"] = (
                f"https://network.satnogs.org/observations/{f['observation_id']}/"
                if f.get("observation_id") else None
            )
        if not frames:
            # A working token and a satellite nobody has decoded look the same
            # from here, and neither is a fault. Falling through to the public
            # source is what turns this into frames rather than an empty panel.
            return self._note_only(norad, EMPTY, "no frames for this satellite")

        # Newest first: the panel draws the top of this list, and the API's own
        # ordering is not part of any contract.
        frames.sort(key=_ordering_key, reverse=True)
        self._db_status[norad] = OK
        self._note[norad] = ""
        return self._keep(norad, frames, DB)

    async def _from_network(self, norad: int) -> str:
        """Demodulated frames from SatNOGS Network. Public — no token at all."""
        try:
            frames = await self._network.latest(norad)
        except (httpx.HTTPError, ValueError) as exc:
            return self._fail(norad, UNREACHABLE, str(exc))
        if not frames:
            # Nothing heard is a real answer, and it has to stamp freshness
            # like any other or every poll re-asks. The panel says so from
            # `available`, and any cached frames stay on screen.
            return self._keep(norad, self.get(norad), self._source.get(norad, NETWORK))
        return self._keep(norad, frames, NETWORK)

    def _keep(self, norad: int, frames: list[dict], source: str) -> str:
        self._by_norad[norad] = frames[:MAX_FRAMES]
        self._source[norad] = source
        self._fetched_at[norad] = datetime.now(timezone.utc)
        self._failed_at.pop(norad, None)
        self._save_cache()
        log.info("telemetry: %d frames for NORAD %s from %s",
                 len(self._by_norad[norad]), norad, source)
        return self._set(norad, OK)

    def _only(self, norad: int, frames: list[dict]) -> list[dict]:
        """Frames that really are this satellite's.

        The filter is verified against the records rather than assumed from a
        200, because the way this API fails is by returning everything. A
        non-zero count here means `satellite` has stopped filtering and the
        parameter needs looking at again — see the module docstring.
        """
        ours = [f for f in frames if f.get("norad") == norad]
        if len(ours) != len(frames):
            log.warning(
                "telemetry: dropped %d of %d frames that were not NORAD %s — "
                "is `satellite` still the filter /telemetry/ honours?",
                len(frames) - len(ours), len(frames), norad,
            )
        return ours

    def _cooling_off(self, norad: int) -> bool:
        failed_at = self._failed_at.get(norad)
        if failed_at is None:
            return False
        return (datetime.now(timezone.utc) - failed_at).total_seconds() < RETRY_COOLDOWN_S

    def _fail(self, norad: int, status: str, note: str) -> str:
        self._failed_at[norad] = datetime.now(timezone.utc)
        return self._set(norad, status, note)

    def _note_only(self, norad: int, status: str, note: str) -> str:
        """The DB path gave up. Say why, but do not start the cooldown.

        The cooldown exists to stop a wall display hammering a service that is
        refusing it, and it is `refresh` as a whole that must respect it — set
        here, it would also suppress the public source that is about to answer
        perfectly well. So this only logs, and whether to back off is decided
        once the second source has had its turn.
        """
        if self._db_status.get(norad) != status:
            log.log(logging.WARNING if status == BAD_TOKEN else logging.INFO,
                    "telemetry %s for NORAD %s%s — trying SatNOGS Network",
                    status, norad, f": {note}" if note else "")
            self._db_status[norad] = status
        return status

    def _set(self, norad: int, status: str, note: str = "") -> str:
        """Record the state, and log it only when it changes.

        A wall display polls this endpoint for months. A station with no token
        would otherwise write the same line to the journal several times a
        minute, which is how a real fault becomes invisible.
        """
        if self._status.get(norad) != status:
            level = logging.WARNING if status in (BAD_TOKEN, UNREACHABLE) else logging.INFO
            log.log(level, "telemetry %s for NORAD %s%s",
                    status, norad, f": {note}" if note else "")
        self._status[norad] = status
        return status

    # --- access ------------------------------------------------------------
    def get(self, norad: int) -> list[dict]:
        return self._by_norad.get(norad, [])

    def status(self, norad: int) -> str:
        return self._status.get(norad, UNKNOWN)

    def snapshot(self, norad: int) -> dict:
        """What the panel draws, and why it is what it is.

        Three separate facts, deliberately not collapsed into one:

        `available` is about having frames to show. `status` is about whether
        anything newer will arrive. `source` is about what kind of thing the
        frames are — decoded fields, or bytes off the air.

        They are separate because the interesting cases are the ones where they
        disagree. A station whose token was removed still has its last frames,
        and should keep showing them while saying they have stopped moving. A
        station that never had a token has a full panel and a configuration
        worth mentioning, which is `detail` rather than an error.
        """
        frames = self.get(norad)
        status = self.status(norad)
        age = self.age_s(norad)
        return {
            "norad": norad,
            "source": self._source.get(norad),
            "available": bool(frames),
            "status": status,
            # The note outranks the status line: "ok, and by the way there is
            # no token" is more use to whoever is standing in front of this
            # than "ok".
            "detail": self._note.get(norad) or DETAIL.get(status, ""),
            "age_s": None if age == float("inf") else round(age, 1),
            "stale": not self.is_fresh(norad),
            "count": len(frames),
            # The one number an operator reads from across the room.
            "last_heard": frames[0]["timestamp"] if frames else None,
            "frames": frames,
        }


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------

def _summarise(frame: dict) -> dict:
    """Only what the panel draws.

    The hex payload is nearly the whole record and there can be forty of them,
    so it is reduced to a length and a head. The decoded blob goes the same
    way: whatever a spacecraft's decoder emits, a strip on a wall display draws
    a handful of scalars from it.
    """
    raw = frame.get("frame")
    hexed = raw if isinstance(raw, str) else ""
    decoded = frame.get("decoded")
    values = _decoded_values(decoded)
    # SatNOGS answers `decoded` three ways: a mapping of fields, the string
    # "influxdb" (the decode exists, but in their time-series database rather
    # than here) or nothing. Only the first is data.
    elsewhere = decoded.strip() if isinstance(decoded, str) and decoded.strip() else ""
    return {
        "norad": _as_int(frame.get("norad_cat_id")),
        "timestamp": _utc_stamp(frame.get("timestamp")),
        "observer": frame.get("observer") or "",
        "station_id": _as_int(frame.get("station_id")),
        "observation_id": _as_int(frame.get("observation_id")),
        "transmitter": frame.get("transmitter") or "",
        "app_source": frame.get("app_source") or "",
        "bytes": len(hexed) // 2,
        "head": hexed[:FRAME_HEAD_CHARS].upper(),
        "decoded": bool(values) or bool(elsewhere),
        "decoded_in": elsewhere,
        "values": values,
    }


def _decoded_values(decoded) -> dict:
    """The decoded payload reduced to printable scalars.

    Capped in both directions. A decoder is free to emit nested structures and
    long strings, and this travels over the WebSocket every time the panel
    updates.
    """
    if not isinstance(decoded, dict):
        return {}
    out: dict = {}
    for key, value in decoded.items():
        if len(out) >= MAX_DECODED_FIELDS:
            break
        if isinstance(value, bool) or isinstance(value, (int, float)):
            out[str(key)] = value
        elif isinstance(value, str):
            out[str(key)] = value[:MAX_VALUE_CHARS]
    return out


def _utc_stamp(raw) -> str | None:
    """The frame's time as an unambiguous UTC string.

    A browser reads an ISO timestamp with no zone as *local* time. On a station
    in Bangkok that is a silent seven-hour error on "last heard", which is the
    one number this panel exists to show, and it would be wrong in the
    direction that looks plausible. SatNOGS sends UTC, so the zone is asserted
    here rather than hoped for downstream.

    A timestamp that will not parse at all is passed through untouched: a frame
    is evidence of contact even when its clock is nonsense, and dropping it
    would lose the contact along with the clock.
    """
    stamp = _parse_ts(raw)
    if stamp is None:
        return raw or None
    return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Frames with no usable timestamp sort last under `reverse=True` rather than
# stopping the sort. A frame is still evidence of contact when its clock is
# missing, so it is kept and shown at the bottom.
_NO_TIME = datetime.min.replace(tzinfo=timezone.utc)


def _ordering_key(frame: dict) -> datetime:
    return _parse_ts(frame.get("timestamp")) or _NO_TIME


def _parse_ts(raw) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    # SatNOGS timestamps are UTC. One without an offset, compared against one
    # with, raises rather than sorts — so the assumption is made explicit here
    # instead of on the comparison.
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
