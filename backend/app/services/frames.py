"""Demodulated frames from SatNOGS Network — the public half of "did we hear it".

`telemetry.py` reads db.satnogs.org/api/telemetry/, which carries *decoded*
fields — named scalars a decoder produced — and refuses anonymous requests.
Station 5024 has no token, so on this station that panel has never had anything
to draw. This module is the source that works without one.

The Network API publishes, for every observation, a `demoddata` list of URLs to
the frames that observation demodulated. Those objects are public: no token, no
session, plain HTTP 200 from the same Wasabi bucket the waterfalls come from.
So the frames themselves are reachable even though the decode of them is not,
and a frame is the thing that answers the operator's question. A waterfall can
look busy with interference; a demodulated frame is a frame.

What is lost by coming in this way is the decoding: these are the bytes off the
air, not `battery_v: 3.92`. What is recovered from them here is the AX.25
header, because that is a published standard rather than a per-spacecraft
guess — it names which spacecraft sent the frame. Anything past the header is
spacecraft-specific and is left as hex. Inventing field names for a beacon
whose format we do not have would put numbers on a wall display that nobody
can check.

Three things about this API cost time to find:

**The unfiltered first page is entirely `future` observations.** Network
returns scheduled passes alongside finished ones, newest `start` first, and a
LEO satellite has far more scheduled than flown — so a plain
`?norad_cat_id=<n>` answers 25 rows of things that have not happened yet, every
one with an empty `demoddata`, and looks exactly like a satellite nobody has
heard. `status=good` is what makes the page dense: 21 of 25 rows carried frames
when this was written, against 0 of 25 unfiltered.

**`demoddata` is not in time order.** A single observation's list came back
10:28:06, 10:27:36, 10:27:06, 10:31:06 — the newest frame was fourth. Taking
the head of the list as "the latest frame" therefore usually works and
sometimes silently does not, which is the worst kind of bug on a display whose
whole job is to say when something was last heard. Every URL is stamped and
sorted here.

**The frame's own time is in the object name, not the record.** `demoddata`
entries are `{"payload_demod": "<url>"}` and nothing else; the URL ends
`data_<observation>_2026-09-17T10-28-06`. That is the only per-frame timestamp
available without fetching every observation's detail page, so it is parsed
rather than approximated from the observation's start.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 25.0
USER_AGENT = "knacksat2-ground-station-dashboard/0.1 (+github.com/Viewzaza)"

# Vetted-good observations are the ones that produced something. See the module
# docstring: without this the first page is all scheduled passes.
OBS_STATUS = "good"

# How many frame objects to actually download per refresh. Each one is its own
# request to the bucket, so this is the knob that decides what this feature
# costs somebody else's bandwidth. A strip shows a handful.
DEFAULT_LIMIT = 8
# At most this many at once. The bucket is fine with more; a wall display that
# has just booted opening a dozen sockets at once is the thing being avoided.
FETCH_CONCURRENCY = 4
# A demodulated frame is hundreds of bytes; these run to 165. Anything far past
# that is not a frame, and only this much of it is kept — the rest would travel
# to every wall display to be drawn as sixteen bytes of hex. Note this truncates
# what was already received rather than capping the download: the objects are
# small enough that a streaming read would be ceremony, and the cap is here to
# bound what is cached and published, not what crosses the wire.
MAX_FRAME_BYTES = 8192

FRAME_HEAD_CHARS = 32          # matches telemetry.py, so the panel draws one width
TEXT_PREVIEW_CHARS = 32

# `data_15001807_2026-09-17T10-28-06` — date, then a time whose separators are
# dashes because a colon is not legal in an object name on every filesystem
# SatNOGS has ever written one to.
_STAMP = re.compile(r"_(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})")

# AX.25 UI frame: 2..n addresses of 7 bytes, then control and PID.
AX25_UI = 0x03
AX25_PID_NO_L3 = 0xF0
AX25_MAX_ADDRESSES = 10        # destination, source and up to 8 repeaters


class NetworkFrameSource:
    """The most recent demodulated frames for a satellite, from any station.

    Deliberately not restricted to our own station. Station 5024 has decoded
    KNACKSAT-2 on 4 of its last 25 good passes, so a panel filtered to 5024
    would be empty most of the week while the network as a whole was hearing
    the spacecraft several times a day. "Is it alive" is answered by anyone's
    frame; "did *we* hear it" is a different question, and it gets the `ours`
    flag on each row rather than an empty panel.
    """

    def __init__(self, settings: Settings) -> None:
        self.s = settings

    async def latest(self, norad: int, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """Newest frames first. Raises httpx.HTTPError if Network is unreachable."""
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_S, headers={"User-Agent": USER_AGENT}
        ) as client:
            observations = await self._observations(client, norad)
            wanted = _frame_index(observations, norad, self.s.station_id)[:limit]
            if not wanted:
                return []
            bodies = await self._bodies(client, [row["url"] for row in wanted])

        frames = []
        for row, body in zip(wanted, bodies):
            if body is None:
                # The listing said there is a frame and the object would not
                # come. That is worth a row — the frame existed, and when — but
                # not a pretence that we have its bytes.
                frames.append({**row, "bytes": None, "head": "", "text": "",
                               "ax25": None})
                continue
            frames.append({**row, **_summarise_payload(body)})
        return frames

    async def _observations(self, client: httpx.AsyncClient, norad: int) -> list[dict]:
        params = {
            # Network's spelling. `satellite__norad_cat_id` is /transmitters/'s
            # and is accepted-and-ignored here, which returns every satellite.
            "norad_cat_id": norad,
            "status": OBS_STATUS,
            # Belt and braces with status=good: an observation still running
            # has only whatever it has demodulated so far, and its frame list
            # grows under us between polls.
            "end": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "format": "json",
        }
        resp = await client.get(f"{self.s.satnogs_network}/observations/", params=params)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list):
            # Network answers a bare list. Anything else is a captive portal, an
            # error page or a changed API — none of which mean "nobody has heard
            # this satellite", so this must not be read as an empty result. The
            # caller keeps the frames it already had instead of erasing them.
            raise ValueError("unexpected payload shape from /observations/")
        return payload

    async def _bodies(self, client: httpx.AsyncClient,
                      urls: list[str]) -> list[bytes | None]:
        limiter = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def one(url: str) -> bytes | None:
            async with limiter:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        log.warning("frame object %s returned %s", url, resp.status_code)
                        return None
                    return resp.content[:MAX_FRAME_BYTES]
                except httpx.HTTPError as exc:
                    # One frame failing is not the panel failing. The row still
                    # says a frame was recorded and when.
                    log.warning("frame object %s: %s", url, exc)
                    return None

        return list(await asyncio.gather(*(one(u) for u in urls)))


def _frame_index(observations: list[dict], norad: int,
                 our_station: int) -> list[dict]:
    """Every published frame across these observations, newest first.

    Records whose `norad_cat_id` is not the one asked for are dropped, for the
    same reason the rest of this project checks: the way these APIs fail is by
    ignoring a filter and returning everything, and a frame attributed to the
    wrong spacecraft on a wall display is worse than no frame at all.
    """
    rows: list[dict] = []
    wrong = 0
    for obs in observations:
        # Every field below is stepped over rather than trusted, and the
        # records themselves are no different: one malformed row in a page of
        # 25 must cost that row, not the panel. An AttributeError raised here
        # would reach the operator as a blank strip with no frames at all.
        if not isinstance(obs, dict):
            wrong += 1
            continue
        if _as_int(obs.get("norad_cat_id")) != norad:
            wrong += 1
            continue
        station = _as_int(obs.get("ground_station"))
        for entry in obs.get("demoddata") or []:
            url = entry.get("payload_demod") if isinstance(entry, dict) else None
            if not isinstance(url, str) or not url:
                continue
            stamp = frame_stamp(url)
            rows.append({
                "url": url,
                "timestamp": stamp.isoformat().replace("+00:00", "Z") if stamp else None,
                "observation_id": _as_int(obs.get("id")),
                "observation_url":
                    f"https://network.satnogs.org/observations/{obs.get('id')}/",
                "station_id": station,
                "station": obs.get("station_name") or "",
                "observer": obs.get("observer") or "",
                "ours": station == our_station,
                "norad": norad,
                "transmitter": obs.get("transmitter_description") or "",
                "mode": obs.get("transmitter_mode") or "",
            })
    if wrong:
        log.warning(
            "frames: dropped %d observations that were not NORAD %s — is "
            "`norad_cat_id` still the filter /observations/ honours?", wrong, norad,
        )
    # A frame with no parseable stamp sorts last rather than stopping the sort.
    rows.sort(key=lambda r: r["timestamp"] or "", reverse=True)
    return rows


def frame_stamp(url: str) -> datetime | None:
    """The frame's own time, out of the object name. UTC; SatNOGS writes UTC."""
    match = _STAMP.search(url)
    if match is None:
        return None
    date, hour, minute, second = match.groups()
    try:
        return datetime.fromisoformat(f"{date}T{hour}:{minute}:{second}").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _summarise_payload(body: bytes) -> dict:
    return {
        "bytes": len(body),
        "head": body[:FRAME_HEAD_CHARS // 2].hex().upper(),
        "text": _printable(body),
        "ax25": decode_ax25(body),
    }


def _printable(body: bytes) -> str:
    """A preview, but only when the frame really is text.

    Some spacecraft beacon ASCII and the whole frame is readable; most send
    binary, where a "preview" of the printable bytes is a line of punctuation
    that looks like a decode and is not. So this is all-or-nothing: unless
    nearly every byte is printable, there is no text here.
    """
    head = body[:TEXT_PREVIEW_CHARS]
    if not head:
        return ""
    printable = sum(1 for b in head if 0x20 <= b <= 0x7E)
    if printable < len(head) * 0.9:
        return ""
    return "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in head)


def decode_ax25(raw: bytes) -> dict | None:
    """The AX.25 UI header, or None if these bytes are not one.

    Strict on purpose. This is the one part of a frame that can be read without
    the spacecraft's own format document, and it is worth reading because it
    names the sender — but a loose parser would find a callsign in any 16 bytes
    of binary and print it with exactly the same confidence. So every field is
    checked: the shift bit on each address character, the end-of-address marker
    landing exactly once, and a UI / no-layer-3 control pair. Anything else
    returns None and the panel shows hex, which is honest.
    """
    addresses: list[str] = []
    offset = 0
    for _ in range(AX25_MAX_ADDRESSES):
        if offset + 7 > len(raw):
            return None
        call, last = _address(raw[offset:offset + 7])
        if call is None:
            return None
        addresses.append(call)
        offset += 7
        if last:
            break
    else:
        return None                      # no address carried the end marker

    if len(addresses) < 2 or offset + 2 > len(raw):
        return None
    if raw[offset] != AX25_UI or raw[offset + 1] != AX25_PID_NO_L3:
        return None

    return {
        "dest": addresses[0],
        "src": addresses[1],
        "via": addresses[2:],
        "info_bytes": len(raw) - offset - 2,
    }


def _address(field: bytes) -> tuple[str | None, bool]:
    """One 7-byte AX.25 address: the callsign, and whether it ends the list.

    Callsign characters are ASCII shifted left one bit, so bit 0 is clear on
    every one of them; on the SSID byte that same bit is the end-of-address
    marker. That is what makes this checkable rather than a guess.
    """
    chars = []
    for byte in field[:6]:
        if byte & 0x01:
            return None, False
        ch = byte >> 1
        if ch != 0x20 and not (0x30 <= ch <= 0x39 or 0x41 <= ch <= 0x5A):
            return None, False           # callsigns are A-Z, 0-9, space padded
        chars.append(chr(ch))
    call = "".join(chars).rstrip()
    if not call or " " in call:          # padding is trailing, never internal
        return None, False
    ssid_byte = field[6]
    ssid = (ssid_byte >> 1) & 0x0F
    return (f"{call}-{ssid}" if ssid else call), bool(ssid_byte & 0x01)


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
