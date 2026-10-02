"""Demodulated frames from SatNOGS Network.

These tests stand in for two services that cannot be asserted against from
here. `network.satnogs.org/api/observations/` is public but live — its answer
changes with every pass flown, so a test that asked it would pass or fail on
what the sky did last night. The frame objects it links to live in somebody
else's Wasabi bucket, and a test suite that downloaded a few of them on every
run would be the rudest thing this repo does.

So the API is a handler defined a few lines above each assertion, and each test
is named for the wrong thing it exists to prevent. The worst of those is not an
empty panel: it is a *populated* one that is quietly wrong — the newest frame
not first because `demoddata` is not in time order, another spacecraft's frames
drawn because a query parameter was accepted and ignored, or a callsign printed
with full confidence because a loose parser found one in sixteen bytes of
noise. Every one of those renders as a working display.

Nothing here touches the network. `frames.py` builds exactly one
`httpx.AsyncClient` and uses it for both the observations listing and the
payload GETs, so the stand-in dispatches on the request path, and `data_dir` is
always a tmp_path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import Settings
from app.services import frames as fr
from app.services.frames import NetworkFrameSource, decode_ax25, frame_stamp

KNACKSAT2 = 67683
ISS = 25544
OUR_STATION = 5024
SOMEONE_ELSE = 2660

# The first 16 bytes of a real KNACKSAT-2 beacon, off observation 15001807 on
# 2026-09-17. HS0AK-11 is the spacecraft, HS0K the sender, then the UI control
# byte and the "no layer 3" PID. This is the anchor for the whole decoder: it
# is bytes that were actually on the air, not bytes shaped like the parser.
BEACON_HEADER = bytes.fromhex("90a6608296407690a6609640406103f0")
BEACON_INFO = bytes.fromhex("03f5004203b475e215ea5f42007e0091")
BEACON = BEACON_HEADER + BEACON_INFO

# A real ORIGAMISAT-2 frame: some spacecraft do beacon plain ASCII.
ASCII_FRAME = b"TGTEEK5SLADAC "

# The order one observation's `demoddata` actually came back in. The newest
# frame is FOURTH.
SCRAMBLED = (
    "2026-09-17T10-28-06",
    "2026-09-17T10-27-36",
    "2026-09-17T10-27-06",
    "2026-09-17T10-31-06",
)


# --------------------------------------------------------------------------
# stand-ins
# --------------------------------------------------------------------------

def payload_url(observation_id: int, stamp: str) -> str:
    """The shape of a `payload_demod` URL — the frame's only clock is in it."""
    return (f"https://network.satnogs.org/media/data_obs/{observation_id}/"
            f"data_{observation_id}_{stamp}")


def observation(obs_id: int, *, stamps: tuple[str, ...] = (),
                norad: int = KNACKSAT2, station: int = OUR_STATION,
                **overrides) -> dict:
    """A record shaped like the one /observations/ returns, trimmed."""
    record = {
        "id": obs_id,
        "norad_cat_id": norad,
        "ground_station": station,
        "station_name": "INSTED-Ground Station(UHF)",
        "observer": "HS0ZLM-OK03gt",
        "transmitter_description": "UHF Telemetry",
        "transmitter_mode": "GMSK",
        "status": "good",
        "vetted_status": "good",
        "start": "2026-09-17T10:26:00Z",
        "end": "2026-09-17T10:33:00Z",
        "demoddata": [{"payload_demod": payload_url(obs_id, s)} for s in stamps],
    }
    record.update(overrides)
    return record


def source(tmp_path, **overrides) -> NetworkFrameSource:
    return NetworkFrameSource(Settings(
        **{**dict(data_dir=tmp_path, station_id=OUR_STATION,
                  default_norad=KNACKSAT2), **overrides}))


# Captured before anything patches it, so a test that changes its mind about
# what the API answers does not end up wrapping its own stand-in.
REAL_CLIENT = httpx.AsyncClient


def answer_with(monkeypatch, handler) -> list[httpx.Request]:
    """Point every client the source builds at `handler`. Returns the requests."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request):
        seen.append(request)
        return handler(request)

    def factory(**kwargs):
        return REAL_CLIENT(transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(fr.httpx, "AsyncClient", factory)
    return seen


def frames_ok(payload=BEACON):
    return lambda request: httpx.Response(200, content=payload)


def network(observations: list[dict], payload=None):
    """One handler for the one client: the listing and the objects share it.

    Dispatching on the path is not decoration — `latest()` opens a single
    client for both, so a handler that answered everything with the listing
    would hand JSON to the AX.25 decoder and never notice.
    """
    payload = payload or frames_ok()

    def handle(request: httpx.Request):
        if request.url.path.endswith("/observations/"):
            return httpx.Response(200, json=observations)
        return payload(request)

    return handle


def listing(seen: list[httpx.Request]) -> httpx.Request:
    return next(r for r in seen if r.url.path.endswith("/observations/"))


def downloads(seen: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in seen if not r.url.path.endswith("/observations/")]


def address(call: str, ssid: int = 0, last: bool = False) -> bytes:
    """One 7-byte AX.25 address field, built the way a TNC builds one."""
    padded = f"{call:<6}"
    return (bytes(ord(c) << 1 for c in padded)
            + bytes([((ssid & 0x0F) << 1) | 0x60 | (1 if last else 0)]))


def ax25(*addresses: bytes, control: int = 0x03, pid: int = 0xF0,
         info: bytes = b"PAYLOAD") -> bytes:
    return b"".join(addresses) + bytes([control, pid]) + info


# --------------------------------------------------------------------------
# AX.25: the one field in a frame that can be read without the spacecraft's
# own format document, and the one that can most convincingly be read wrong
# --------------------------------------------------------------------------

def test_the_helper_that_builds_headers_here_agrees_with_the_real_beacon():
    """Every negative test below builds its header with `address()`. If that
    drifts from what a TNC actually emits, those tests go on passing while
    proving nothing, so it is pinned against sixteen bytes off the air: two
    7-byte address fields, then the UI control byte and the no-layer-3 PID."""
    built = (address("HS0AK", 11) + address("HS0K", 0, last=True)
             + bytes([fr.AX25_UI, fr.AX25_PID_NO_L3]))
    assert built == BEACON_HEADER


def test_a_real_beacons_header_names_the_spacecraft_that_sent_it():
    """This is the whole reason the header is parsed at all. Getting `src` and
    `dest` the wrong way round, or reading the SSID out of the wrong bits,
    puts a callsign on a wall display that belongs to nobody."""
    decoded = decode_ax25(BEACON)

    assert decoded == {
        "dest": "HS0AK-11",
        "src": "HS0K",
        "via": [],
        "info_bytes": len(BEACON_INFO),
    }


def test_a_digipeater_path_is_kept_apart_from_the_two_endpoints():
    """A repeater in the path is not the spacecraft. Folding it into `src`
    would credit the frame to whatever relayed it."""
    raw = ax25(address("HS0AK", 11), address("HS0K"),
               address("WIDE2", 1, last=True))
    decoded = decode_ax25(raw)

    assert decoded["src"] == "HS0K"
    assert decoded["via"] == ["WIDE2-1"]


def test_random_binary_does_not_decode_to_a_confidently_wrong_callsign():
    """The failure this decoder is strict to avoid: a loose parser finds a
    plausible callsign in any sixteen bytes of noise and prints it with
    exactly the confidence a real one gets. A fixed seed so a failure here is
    a failure you can reproduce rather than a flake."""
    rng = random.Random(20260917)
    for _ in range(3000):
        blob = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 64)))
        assert decode_ax25(blob) is None, blob.hex()


def test_a_run_of_zero_bytes_is_not_a_frame_from_a_station_called_nothing():
    """Zeros are what a truncated or failed demodulation looks like."""
    assert decode_ax25(bytes(64)) is None
    assert decode_ax25(b"") is None


def test_bytes_too_few_to_hold_a_header_are_refused_rather_than_indexed():
    """Reading past the end of a short frame is an IndexError in the poller,
    which takes down the panel, not just the row."""
    for length in range(len(BEACON_HEADER)):
        assert decode_ax25(BEACON[:length]) is None, length


def test_a_header_without_the_ui_and_no_layer_3_pair_is_refused():
    """Two plausible addresses followed by two arbitrary bytes is the single
    likeliest way for noise to look like a frame. Control 0x03 / PID 0xF0 is
    what says this is a UI frame and not sixteen bytes that rhyme with one."""
    good = address("HS0AK", 11) + address("HS0K", 0, last=True)
    assert decode_ax25(good + bytes([0x03, 0xF0]) + b"x") is not None

    assert decode_ax25(good + bytes([0x00, 0xF0]) + b"x") is None
    assert decode_ax25(good + bytes([0x03, 0x00]) + b"x") is None
    assert decode_ax25(good + bytes([0x3E, 0xCC]) + b"x") is None


def test_address_characters_that_are_not_shifted_ascii_are_refused():
    """Callsign bytes are ASCII shifted left one bit, so bit 0 is clear on
    every one of them and the shifted value is A-Z, 0-9 or a pad. That check
    is the entire difference between reading a callsign and guessing one."""
    header = bytearray(BEACON_HEADER)
    header[2] |= 0x01                       # shift bit set where it cannot be
    assert decode_ax25(bytes(header) + b"x") is None

    header = bytearray(BEACON_HEADER)
    header[2] = ord("h") << 1               # lowercase: not a legal callsign
    assert decode_ax25(bytes(header) + b"x") is None

    header = bytearray(BEACON_HEADER)
    header[2] = 0x7C                        # '>' — punctuation, not a callsign
    assert decode_ax25(bytes(header) + b"x") is None


def test_a_callsign_padded_in_the_middle_is_refused():
    """Padding in AX.25 is trailing. An internal space means the field was
    never a callsign, and "HS 0AK" on a display is worse than hex."""
    raw = ax25(address("HS 0AK"[:6], 11), address("HS0K", 0, last=True))
    assert decode_ax25(raw) is None


def test_addresses_that_never_end_are_refused_rather_than_walked_forever():
    """The end-of-address bit has to land, exactly once. Without that check a
    long binary frame is an unbounded walk looking for a header."""
    never_ends = b"".join(address("HS0AK", 1) for _ in range(12))
    assert decode_ax25(never_ends + bytes([0x03, 0xF0]) + b"x") is None


def test_one_address_is_not_a_frame_because_nothing_sent_it():
    """A header with only a destination names no sender, and the sender is
    the only thing worth reading a header for here."""
    raw = ax25(address("HS0AK", 11, last=True))
    assert decode_ax25(raw) is None


def test_json_and_html_are_not_read_as_frames():
    """The bucket answering with an error document, or a captive portal
    answering with a login page, both arrive as a 200 full of bytes."""
    assert decode_ax25(b'{"detail": "Not found"}') is None
    assert decode_ax25(b"<!DOCTYPE html><html><head><title>502</title>") is None


# --------------------------------------------------------------------------
# the frame's own clock, which is in the object name and nowhere else
# --------------------------------------------------------------------------

def test_the_frames_time_is_read_out_of_the_object_name():
    """`demoddata` entries carry a URL and nothing else, so this is the only
    per-frame timestamp there is without fetching every observation's detail
    page. Reading it wrong puts "last heard" minutes or hours out."""
    stamp = frame_stamp(payload_url(15001807, "2026-09-17T10-28-06"))

    assert stamp == datetime(2026, 9, 17, 10, 28, 6, tzinfo=timezone.utc)
    assert stamp.tzinfo is not None, "a naive datetime raises when it is sorted"
    assert stamp.utcoffset() == timedelta(0), "SatNOGS writes UTC"


def test_a_url_with_no_stamp_has_no_time_rather_than_a_plausible_wrong_one():
    """If the object naming ever changes, the honest answer is "unknown". A
    fallback to now, or to the observation's start, is a display that says the
    satellite was heard at a moment nobody heard it."""
    assert frame_stamp("https://network.satnogs.org/media/data_obs/1/data_1") is None
    assert frame_stamp("") is None


def test_a_stamp_that_is_not_a_real_instant_is_not_invented():
    """The regex matches digits; only the calendar knows there is no 30th of
    February. This is the path where a ValueError would escape the poller."""
    assert frame_stamp("/data_1_2026-02-30T10-28-06") is None
    assert frame_stamp("/data_1_2026-09-17T99-28-06") is None


# --------------------------------------------------------------------------
# the query, which is the thing this API lies about
# --------------------------------------------------------------------------

async def test_the_observations_query_asks_for_networks_spelling_of_norad(
        tmp_path, monkeypatch):
    """`norad_cat_id` is Network's spelling. `satellite__norad_cat_id` is
    /transmitters/'s, and /observations/ accepts it, ignores it, and returns
    every satellite — which looks exactly like success."""
    seen = answer_with(monkeypatch, network([observation(1, stamps=SCRAMBLED[:1])]))
    await source(tmp_path).latest(KNACKSAT2)

    params = listing(seen).url.params
    assert params["norad_cat_id"] == str(KNACKSAT2)
    assert "satellite__norad_cat_id" not in params
    assert "satellite" not in params


async def test_only_vetted_good_observations_are_asked_for(tmp_path, monkeypatch):
    """Unfiltered, the first page is 25 scheduled `future` passes, every one
    with an empty `demoddata`, because a LEO satellite has far more passes
    booked than flown. The panel then looks like nobody has ever heard the
    spacecraft. `status=good` is what makes the page dense."""
    seen = answer_with(monkeypatch, network([observation(1, stamps=SCRAMBLED[:1])]))
    await source(tmp_path).latest(KNACKSAT2)

    assert listing(seen).url.params["status"] == fr.OBS_STATUS == "good"


async def test_observations_that_have_not_finished_yet_are_excluded(
        tmp_path, monkeypatch):
    """A pass still recording has only whatever it has demodulated so far, and
    its frame list grows under us between polls — so "the newest frame" keeps
    changing for a reason that is not new contact."""
    seen = answer_with(monkeypatch, network([observation(1, stamps=SCRAMBLED[:1])]))
    await source(tmp_path).latest(KNACKSAT2)

    bound = datetime.strptime(
        listing(seen).url.params["end"], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    assert bound <= now + timedelta(seconds=1), "the bound lets the future in"
    assert bound > now - timedelta(minutes=5), "the bound is stale, not now"


async def test_the_requests_say_who_is_asking(tmp_path, monkeypatch):
    """Both the API and the bucket are somebody else's. An anonymous poller
    that misbehaves gets the whole user agent blocked, and there is nobody to
    email about it."""
    seen = answer_with(monkeypatch, network([observation(1, stamps=SCRAMBLED)]))
    await source(tmp_path).latest(KNACKSAT2)

    assert seen, "no request was made at all"
    for request in seen:
        assert "knacksat2-ground-station" in request.headers["user-agent"]


# --------------------------------------------------------------------------
# the wrong satellite, which arrives as a 200 full of plausible records
# --------------------------------------------------------------------------

async def test_another_satellites_frames_are_dropped_rather_than_drawn(
        tmp_path, monkeypatch):
    """The shape an ignored filter arrives in: 200, well-formed records, wrong
    spacecraft. Checked per record, because a 200 proves nothing when the
    failure mode is a filter that returned everything."""
    seen = answer_with(monkeypatch, network([
        observation(1, stamps=("2026-09-17T10-28-06",)),
        observation(2, stamps=("2026-09-17T11-00-00",), norad=ISS),
        observation(3, stamps=("2026-09-17T12-00-00",), norad=None),
        observation(4, stamps=("2026-09-17T13-00-00",), norad="not a number"),
    ]))

    rows = await source(tmp_path).latest(KNACKSAT2)

    assert [row["observation_id"] for row in rows] == [1]
    assert {row["norad"] for row in rows} == {KNACKSAT2}
    assert len(downloads(seen)) == 1, "a wrong satellite's object was fetched"


async def test_a_filter_that_stopped_filtering_is_said_out_loud(
        tmp_path, monkeypatch, caplog):
    """Silently drawing one frame instead of forty is how this goes unnoticed
    for a month. The log line has to name the parameter to go and check, so
    the next person does not start by rereading the decoder."""
    caplog.set_level(logging.WARNING, logger="app.services.frames")
    answer_with(monkeypatch, network([
        observation(1, stamps=("2026-09-17T10-28-06",)),
        observation(2, stamps=("2026-09-17T11-00-00",), norad=ISS),
    ]))

    await source(tmp_path).latest(KNACKSAT2)

    said = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("norad_cat_id" in message for message in said), said


# --------------------------------------------------------------------------
# ordering, which is the headline bug this module exists to prevent
# --------------------------------------------------------------------------

async def test_frames_come_back_newest_first_although_the_api_does_not_sort_them(
        tmp_path, monkeypatch):
    """One real observation's `demoddata` came back 10:28:06, 10:27:36,
    10:27:06, 10:31:06 — the newest frame was FOURTH. Taking the head of the
    list therefore usually works and sometimes silently does not, and a "last
    heard" readout that is quietly wrong is worse than a blank one."""
    answer_with(monkeypatch, network([observation(15001807, stamps=SCRAMBLED)]))

    rows = await source(tmp_path).latest(KNACKSAT2)

    assert [row["timestamp"] for row in rows] == [
        "2026-09-17T10:31:06Z",
        "2026-09-17T10:28:06Z",
        "2026-09-17T10:27:36Z",
        "2026-09-17T10:27:06Z",
    ]


async def test_frames_are_sorted_across_observations_not_only_inside_one(
        tmp_path, monkeypatch):
    """Network returns observations newest `start` first, but a frame late in
    an earlier pass can still be newer than a frame early in a later one. The
    strip is a timeline, not a list of lists."""
    answer_with(monkeypatch, network([
        observation(2, stamps=("2026-09-17T12-00-00", "2026-09-17T12-30-00")),
        observation(1, stamps=("2026-09-17T12-15-00",)),
    ]))

    rows = await source(tmp_path).latest(KNACKSAT2)

    assert [row["timestamp"] for row in rows] == [
        "2026-09-17T12:30:00Z", "2026-09-17T12:15:00Z", "2026-09-17T12:00:00Z",
    ]


async def test_a_frame_with_no_readable_stamp_sorts_last_rather_than_raising(
        tmp_path, monkeypatch):
    """One unparseable object name must not take the panel down with a
    TypeError in the sort. A frame is evidence of contact even when its clock
    is not, so it is kept — just never at the top, where it would be read as
    the most recent thing heard."""
    answer_with(monkeypatch, network([observation(1, demoddata=[
        {"payload_demod": "https://network.satnogs.org/media/data_obs/1/data_1"},
        {"payload_demod": payload_url(1, "2026-09-17T10-28-06")},
    ])]))

    rows = await source(tmp_path).latest(KNACKSAT2)

    assert len(rows) == 2, "a frame was dropped for its clock"
    assert rows[0]["timestamp"] == "2026-09-17T10:28:06Z"
    assert rows[1]["timestamp"] is None


async def test_a_demoddata_entry_with_no_url_is_skipped_not_fetched(
        tmp_path, monkeypatch):
    """An entry with a null or empty `payload_demod` is not a frame. Turning
    it into a GET of "" is a request against the API's own root."""
    seen = answer_with(monkeypatch, network([observation(1, demoddata=[
        {"payload_demod": None},
        {"payload_demod": ""},
        {},
        None,
        {"payload_demod": payload_url(1, "2026-09-17T10-28-06")},
    ])]))

    rows = await source(tmp_path).latest(KNACKSAT2)

    assert len(rows) == 1
    assert len(downloads(seen)) == 1


# --------------------------------------------------------------------------
# ours, which is the question a network-wide panel still has to answer
# --------------------------------------------------------------------------

async def test_our_own_stations_frames_are_marked_as_ours(tmp_path, monkeypatch):
    """Station 5024 decodes KNACKSAT-2 on about 4 of its last 25 good passes,
    so a panel filtered to 5024 would be blank most of the week while the
    network heard the spacecraft several times a day. The panel is therefore
    network-wide and this flag is the whole of "did WE hear it"."""
    answer_with(monkeypatch, network([
        observation(1, stamps=("2026-09-17T10-28-06",), station=OUR_STATION),
        observation(2, stamps=("2026-09-17T09-00-00",), station=SOMEONE_ELSE,
                    station_name="Kiwi-SDR Wellington", observer="ZL2ABC"),
    ]))

    rows = await source(tmp_path).latest(KNACKSAT2)

    assert [row["ours"] for row in rows] == [True, False]
    assert [row["station_id"] for row in rows] == [OUR_STATION, SOMEONE_ELSE]
    assert rows[1]["station"] == "Kiwi-SDR Wellington"


async def test_another_stations_frames_are_kept_and_not_confused_with_ours(
        tmp_path, monkeypatch):
    """The opposite mistake to dropping the wrong satellite: dropping the
    right satellite because the wrong station heard it. "Is it alive" is
    answered by anybody's frame."""
    answer_with(monkeypatch, network([
        observation(2, stamps=("2026-09-17T09-00-00",), station=SOMEONE_ELSE),
    ]))

    rows = await source(tmp_path).latest(KNACKSAT2)

    assert len(rows) == 1
    assert rows[0]["ours"] is False


async def test_a_station_id_that_is_missing_is_not_read_as_ours(
        tmp_path, monkeypatch):
    """`None == 5024` is False, which is right — but only by accident of how
    it is written, so it is pinned. Attributing an anonymous frame to this
    station is a claim about our own hardware that is not true."""
    answer_with(monkeypatch, network([
        observation(1, stamps=("2026-09-17T10-28-06",), ground_station=None),
    ]))

    rows = await source(tmp_path).latest(KNACKSAT2)

    assert rows[0]["ours"] is False
    assert rows[0]["station_id"] is None


# --------------------------------------------------------------------------
# what a row carries, and what a frame turns into on the wire
# --------------------------------------------------------------------------

async def test_a_row_says_which_observation_it_came_from(tmp_path, monkeypatch):
    """Every claim on this display has to be checkable against SatNOGS by
    somebody who does not trust it. That is a link, not an id in a log."""
    answer_with(monkeypatch, network([
        observation(15001807, stamps=("2026-09-17T10-28-06",)),
    ]))

    row = (await source(tmp_path).latest(KNACKSAT2))[0]

    assert row["observation_id"] == 15001807
    assert row["observation_url"] == \
        "https://network.satnogs.org/observations/15001807/"
    assert row["mode"] == "GMSK"
    assert row["transmitter"] == "UHF Telemetry"
    json.dumps(row)             # it goes over the WebSocket to every display


async def test_a_frame_is_summarised_rather_than_shipped_whole(
        tmp_path, monkeypatch):
    """The bytes go to every connected display on every refresh. A fixed-width
    head is what a panel lays out; the whole frame is payload nobody reads."""
    answer_with(monkeypatch, network([
        observation(1, stamps=("2026-09-17T10-28-06",)),
    ]))

    row = (await source(tmp_path).latest(KNACKSAT2))[0]

    assert row["bytes"] == len(BEACON)
    assert len(row["head"]) == fr.FRAME_HEAD_CHARS
    assert row["head"] == BEACON[:16].hex().upper()
    assert row["ax25"]["src"] == "HS0K"


async def test_an_object_far_larger_than_a_frame_is_not_kept_whole(
        tmp_path, monkeypatch):
    """A demodulated frame is hundreds of bytes. Something answering with a
    megabyte is not a frame, and it must not become one row of the payload
    this backend holds in memory for the life of the process."""
    answer_with(monkeypatch, network(
        [observation(1, stamps=("2026-09-17T10-28-06",))],
        payload=frames_ok(BEACON + bytes(64_000)),
    ))

    row = (await source(tmp_path).latest(KNACKSAT2))[0]

    assert row["bytes"] == fr.MAX_FRAME_BYTES


# --------------------------------------------------------------------------
# the text preview, which is all-or-nothing on purpose
# --------------------------------------------------------------------------

def test_an_ascii_beacon_is_previewed_as_the_text_it_is():
    """Some spacecraft beacon plain ASCII and the whole frame is readable.
    This one is a real ORIGAMISAT-2 frame."""
    assert fr._printable(ASCII_FRAME) == "TGTEEK5SLADAC "


def test_a_binary_frame_gets_no_preview_rather_than_a_line_of_punctuation():
    """A "preview" of the printable bytes of a binary frame is a row of stray
    punctuation that reads as a decode and is not one. Nothing is the honest
    rendering; the hex head is right there beside it."""
    assert fr._printable(BEACON) == ""
    assert fr._printable(bytes(range(32))) == ""


def test_a_frame_that_is_mostly_text_is_still_not_text():
    """The threshold is deliberately near the top. "Mostly printable" is what
    a binary frame with a few ASCII fields looks like, and half a decode is
    the thing that gets believed."""
    assert fr._printable(b"HELLO WORLD THIS IS A BEACON" + bytes(4)) == ""


def test_an_empty_body_previews_as_nothing_and_does_not_divide_by_zero():
    assert fr._printable(b"") == ""


def test_a_preview_is_never_longer_than_the_panel_reserved_for_it():
    """A 900-byte ASCII beacon on one line is a display with one row on it."""
    preview = fr._printable(b"BEACON " * 200)
    assert len(preview) == fr.TEXT_PREVIEW_CHARS


# --------------------------------------------------------------------------
# one frame failing is not the panel failing
# --------------------------------------------------------------------------

async def test_a_frame_object_that_404s_still_leaves_the_row_that_says_it_existed(
        tmp_path, monkeypatch):
    """The listing said a frame was demodulated at that moment, and that is
    contact — it is true whether or not the bucket will hand over the bytes.
    Dropping the row loses the contact; faking bytes for it is worse."""
    def payload(request):
        if "10-27-36" in str(request.url):
            return httpx.Response(404, text="Not found")
        return httpx.Response(200, content=BEACON)

    answer_with(monkeypatch, network(
        [observation(1, stamps=SCRAMBLED)], payload=payload))

    rows = await source(tmp_path).latest(KNACKSAT2)
    missing = [r for r in rows if r["timestamp"] == "2026-09-17T10:27:36Z"]

    assert len(rows) == 4, "the whole panel went down with one object"
    assert len(missing) == 1
    assert missing[0]["bytes"] is None, "a frame we do not have was given a size"
    assert missing[0]["ax25"] is None
    assert missing[0]["head"] == ""
    assert missing[0]["observation_id"] == 1
    assert [r for r in rows if r["bytes"]], "the frames that did come are gone"


async def test_a_payload_fetch_that_raises_does_not_take_the_panel_with_it(
        tmp_path, monkeypatch):
    """A dropped connection mid-refresh is a Tuesday on this link. One socket
    failing must cost one row's bytes, not the strip."""
    def payload(request):
        if "10-27-36" in str(request.url):
            raise httpx.ConnectError("connection reset by peer")
        return httpx.Response(200, content=BEACON)

    answer_with(monkeypatch, network(
        [observation(1, stamps=SCRAMBLED)], payload=payload))

    rows = await source(tmp_path).latest(KNACKSAT2)

    assert len(rows) == 4
    assert sum(1 for r in rows if r["bytes"] is None) == 1


async def test_every_row_has_the_same_keys_whether_its_bytes_arrived_or_not(
        tmp_path, monkeypatch):
    """The panel indexes these. A row missing `text` because its object 404'd
    is a KeyError in the renderer, which is the whole display, not one row."""
    def payload(request):
        if "10-27-36" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, content=BEACON)

    answer_with(monkeypatch, network(
        [observation(1, stamps=SCRAMBLED)], payload=payload))

    rows = await source(tmp_path).latest(KNACKSAT2)

    assert len({frozenset(r) for r in rows}) == 1, "rows are not the same shape"


async def test_an_unreachable_network_api_is_raised_rather_than_drawn_as_silence(
        tmp_path, monkeypatch):
    """The opposite of the rule above, and the reason it is written down: a
    failed *listing* means we do not know, and an empty list means we do. An
    outage that renders as "nothing heard" is the panel lying."""
    def dead(request):
        raise httpx.ConnectError("no route to host")

    answer_with(monkeypatch, dead)

    with pytest.raises(httpx.HTTPError):
        await source(tmp_path).latest(KNACKSAT2)


async def test_a_listing_that_answers_an_error_status_is_not_read_as_no_frames(
        tmp_path, monkeypatch):
    """429 is the status this project has actually earned from SatNOGS. It has
    to be distinguishable from a quiet week."""
    answer_with(monkeypatch, lambda r: httpx.Response(429, text="slow down"))

    with pytest.raises(httpx.HTTPStatusError):
        await source(tmp_path).latest(KNACKSAT2)


async def test_a_listing_that_is_not_a_list_is_not_read_as_nobody_heard_it(
        tmp_path, monkeypatch):
    """A captive portal's login page, an error document, or a paginated
    envelope if this endpoint ever grows one, all arrive as a 200 whose body
    is not the bare list Network answers. Reading any of them as an empty
    result would let the caller stamp a successful fetch and erase the
    contact history already on screen — so this raises instead, and the
    caller keeps the frames it had."""
    seen = answer_with(monkeypatch, lambda r: httpx.Response(
        200, json={"results": [], "next": None}))

    with pytest.raises(ValueError):
        await source(tmp_path).latest(KNACKSAT2)

    assert downloads(seen) == [], "a login page was walked for frame objects"


# --------------------------------------------------------------------------
# what this costs somebody else
# --------------------------------------------------------------------------

async def test_no_more_objects_are_downloaded_than_the_panel_will_draw(
        tmp_path, monkeypatch):
    """Each one is a request against somebody else's bucket. A busy day for
    the spacecraft must not turn one refresh into a hundred GETs, so the cap
    has to be applied before the fetch and not after it."""
    busy = [f"2026-09-17T{h:02d}-{m:02d}-00" for h in range(6) for m in (0, 30)]
    seen = answer_with(monkeypatch, network([
        observation(1, stamps=tuple(busy)),
        observation(2, stamps=tuple(busy)),
    ]))

    rows = await source(tmp_path).latest(KNACKSAT2, limit=5)

    assert len(rows) == 5
    assert len(downloads(seen)) == 5, "objects were fetched and then thrown away"


async def test_the_cap_keeps_the_newest_frames_and_not_the_first_it_saw(
        tmp_path, monkeypatch):
    """A cap applied before the sort would download the oldest few frames and
    show them as the latest contact — the ordering bug wearing a hat."""
    answer_with(monkeypatch, network([observation(1, stamps=SCRAMBLED)]))

    rows = await source(tmp_path).latest(KNACKSAT2, limit=2)

    assert [r["timestamp"] for r in rows] == [
        "2026-09-17T10:31:06Z", "2026-09-17T10:28:06Z"]


async def test_the_default_cap_applies_when_nobody_passes_one(
        tmp_path, monkeypatch):
    """The route calls this with no limit. If the default were absent, the
    knob that decides what this feature costs would not be connected."""
    many = tuple(f"2026-09-17T10-{m:02d}-00" for m in range(40))
    seen = answer_with(monkeypatch, network([observation(1, stamps=many)]))

    rows = await source(tmp_path).latest(KNACKSAT2)

    assert len(rows) == fr.DEFAULT_LIMIT
    assert len(downloads(seen)) == fr.DEFAULT_LIMIT


async def test_a_display_that_has_just_booted_does_not_open_a_dozen_sockets(
        tmp_path, monkeypatch):
    """The bucket is fine with more; the wall display coming back after a
    power cut, with every panel refreshing at once, is what is being kept
    polite here."""
    live = peak = 0

    async def payload(request):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return httpx.Response(200, content=BEACON)

    answer_with(monkeypatch, network(
        [observation(1, stamps=tuple(f"2026-09-17T10-{m:02d}-00" for m in range(8)))],
        payload=payload))

    await source(tmp_path).latest(KNACKSAT2)

    assert peak <= fr.FETCH_CONCURRENCY, "the limiter is not limiting"
    assert peak > 1, "the fetches are serialised; a refresh takes eight round trips"


# --------------------------------------------------------------------------
# nothing heard, which is a fact and not a failure
# --------------------------------------------------------------------------

async def test_an_observation_with_no_frames_contributes_nothing(
        tmp_path, monkeypatch):
    """A good pass that demodulated nothing is most passes. It is not a row."""
    seen = answer_with(monkeypatch, network([
        observation(1, stamps=()),
        observation(2, demoddata=None),
        observation(3, stamps=("2026-09-17T10-28-06",)),
    ]))

    rows = await source(tmp_path).latest(KNACKSAT2)

    assert [row["observation_id"] for row in rows] == [3]
    assert len(downloads(seen)) == 1


async def test_no_observations_at_all_fetches_nothing_and_is_not_an_error(
        tmp_path, monkeypatch):
    """A satellite nobody has heard this week is a fact. Asking the bucket
    about it anyway is a request that cannot answer anything."""
    seen = answer_with(monkeypatch, network([]))

    assert await source(tmp_path).latest(KNACKSAT2) == []
    assert downloads(seen) == []
    assert len(seen) == 1


# --------------------------------------------------------------------------
# junk in the listing
# --------------------------------------------------------------------------

async def test_a_demoddata_entry_that_is_not_a_record_does_not_take_the_panel_down(
        tmp_path, monkeypatch):
    """This module steps over junk everywhere else — a null entry, a missing
    URL, an unparseable clock, an object that will not download. One field
    arriving as a bare string instead of an object should cost that frame,
    not every frame."""
    answer_with(monkeypatch, network([observation(1, demoddata=[
        payload_url(1, "2026-09-17T10-27-06"),          # a bare string
        {"payload_demod": payload_url(1, "2026-09-17T10-28-06")},
    ])]))

    rows = await source(tmp_path).latest(KNACKSAT2)

    assert [row["timestamp"] for row in rows] == ["2026-09-17T10:28:06Z"]


async def test_an_observation_that_is_not_a_record_does_not_take_the_panel_down(
        tmp_path, monkeypatch):
    """The same exposure one level up. `/observations/` is checked for being a
    list, which does not make its elements records — and a page of 25 with one
    junk element among them would otherwise cost all 25."""
    answer_with(monkeypatch, network([
        "this is not an observation",
        observation(1, stamps=("2026-09-17T10-28-06",)),
    ]))

    rows = await source(tmp_path).latest(KNACKSAT2)

    assert [row["timestamp"] for row in rows] == ["2026-09-17T10:28:06Z"]
