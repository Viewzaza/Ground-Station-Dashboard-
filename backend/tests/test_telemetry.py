"""Decoded telemetry frames from SatNOGS DB, and the public source behind them.

Two things here cannot be checked against the live service: `/telemetry/`
answers 401 without a token, and this station has none. So the tests stand in
for the API, and each one names the wrong thing it exists to prevent — the
worst of which is not an empty panel but a *populated* one, showing another
satellite's frames because a query parameter was accepted and ignored.

The store has two sources and they are different services — the DB's
`/telemetry/`, and Network's `/observations/` with the frame objects behind it
(see `test_frames.py`, which tests that source on its own). Everything here is
about which one gets asked, in what order, and what the panel is told about it.
The rule the file exists to hold down is that **a missing token is a
configuration state and not an empty panel**: the DB is never called without
one, and the public source answers instead.

Nothing in this file touches the network. `answer_with` stands in for *both*
services — a test that stubbed only one would make real HTTP calls out of the
other, which is how a suite starts depending on the weather — and `data_dir` is
always a tmp_path, so a real cache on the machine running the tests cannot leak
into them.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.config import Settings
from app.routes.radio import telemetry as telemetry_endpoint
from app.services import frames as frm
from app.services import telemetry as tel
from app.services.telemetry import TelemetryStore, _summarise

KNACKSAT2 = 67683
ISS = 25544

# A record shaped like the one db.satnogs.org/api/telemetry/ returns, with the
# frame shortened. `decoded: "influxdb"` is what the live API sends for a frame
# whose decode lives in their time-series database rather than in the record.
FRAME = {
    "sat_id": "XWPO-6754-1203-8829-0332",
    "norad_cat_id": KNACKSAT2,
    "transmitter": "5bZHPjVmCPPhBMyqbQ4SnR",
    "app_source": "network",
    "decoded": "influxdb",
    "frame": "968870A6A0A66086A240404040E103F0ABCD0000004203F500B475E215EA5F",
    "observer": "HS0ZLM-OK03gt",
    "timestamp": "2026-09-16T02:28:09Z",
    "station_id": 5024,
    "observation_id": 12345678,
}


def frame(**overrides) -> dict:
    return {**FRAME, **overrides}


def page(*frames: dict) -> dict:
    """The cursor-paginated envelope: there is no ?page= on this endpoint."""
    return {"next": None, "previous": None, "results": list(frames)}


def store(tmp_path, **overrides) -> TelemetryStore:
    base = dict(data_dir=tmp_path, station_id=5024, default_norad=KNACKSAT2,
                satnogs_db_token="secret-token")
    base.update(overrides)
    return TelemetryStore(Settings(**base))


# Captured before anything patches it, so a test that changes its mind about
# what the API answers does not end up wrapping its own stand-in.
REAL_CLIENT = httpx.AsyncClient


def heard_nothing(request: httpx.Request) -> httpx.Response:
    """Network's answer for a satellite nobody has heard: an empty list.

    The default, so that a test about the DB does not have to say anything
    about the fallback in order not to reach the real internet through it.
    """
    return httpx.Response(200, json=[])


def answer_with(monkeypatch, handler, network=heard_nothing) -> list[httpx.Request]:
    """Point every client EITHER source builds at a handler.

    `handler` answers db.satnogs.org's `/telemetry/`; `network` answers
    everything else, which is Network's `/observations/` and the frame objects
    it points at. Returns every request both of them made, in order.
    """
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        chosen = handler if request.url.path.endswith("/telemetry/") else network
        return chosen(request)

    def factory(**kwargs):
        return REAL_CLIENT(transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(tel.httpx, "AsyncClient", factory)
    monkeypatch.setattr(frm.httpx, "AsyncClient", factory)
    return seen


def no_network(monkeypatch) -> None:
    """Building a client at all is the failure, so fail at construction."""
    class Exploding:
        def __init__(self, **kwargs):
            raise AssertionError("the store reached for the network")

    monkeypatch.setattr(tel.httpx, "AsyncClient", Exploding)
    monkeypatch.setattr(frm.httpx, "AsyncClient", Exploding)


def sent_to(seen: list[httpx.Request], suffix: str) -> list[httpx.Request]:
    """Just the requests aimed at one of the two services."""
    return [r for r in seen if r.url.path.endswith(suffix)]


def ok(payload):
    return lambda request: httpx.Response(200, json=payload)


# An observation carrying one frame, shaped the way Network returns it. Enough
# to prove the fallback ran; `test_frames.py` is where its parsing is pinned.
def observation(norad: int = KNACKSAT2, station: int = 5024) -> dict:
    return {
        "id": 15001807,
        "norad_cat_id": norad,
        "ground_station": station,
        "station_name": "INSTED-Ground Station(UHF)",
        "observer": "HS0ZLM",
        "transmitter_description": "UHF Telemetry",
        "transmitter_mode": "FSK",
        "demoddata": [{
            "payload_demod":
                "https://example.invalid/data_15001807_2026-09-17T10-28-06",
        }],
    }


def heard_once(request: httpx.Request) -> httpx.Response:
    """Network has one frame, and the object behind it is a real AX.25 beacon."""
    if request.url.path.endswith("/observations/"):
        return httpx.Response(200, json=[observation()])
    return httpx.Response(200, content=bytes.fromhex(
        "90a6608296407690a6609640406103f0") + b"\x00" * 16)


# --------------------------------------------------------------------------
# no token: the state this station is actually in
# --------------------------------------------------------------------------

async def test_an_empty_token_does_not_call_an_endpoint_that_must_refuse_it(
        tmp_path, monkeypatch):
    """A guaranteed 401 every poll is a crash loop with extra steps."""
    seen = answer_with(monkeypatch, ok(page()), network=heard_once)
    await store(tmp_path, satnogs_db_token="").refresh(KNACKSAT2)

    assert sent_to(seen, "/telemetry/") == []


async def test_an_empty_token_gets_the_public_source_rather_than_an_empty_panel(
        tmp_path, monkeypatch):
    """The whole reason `frames.py` exists. This station has no token, and for
    as long as the DB was the only source that meant a panel that had never
    once had anything on it — while the frames themselves were public all
    along."""
    answer_with(monkeypatch, ok(page()), network=heard_once)
    s = store(tmp_path, satnogs_db_token="")

    assert await s.refresh(KNACKSAT2) == tel.OK
    snap = s.snapshot(KNACKSAT2)
    assert snap["available"] is True
    assert snap["source"] == tel.NETWORK
    assert snap["frames"][0]["ax25"]["src"] == "HS0K"


async def test_an_empty_token_is_still_reported_even_though_frames_arrived(
        tmp_path, monkeypatch):
    """Frames on screen must not hide the fact that they are the lesser kind.
    The panel is showing bytes off the air because nobody set the token that
    would get it decoded fields, and it has to be able to say which variable
    that is — "no data" sends an operator to the logs for one line of .env."""
    answer_with(monkeypatch, ok(page()), network=heard_once)
    s = store(tmp_path, satnogs_db_token="")
    await s.refresh(KNACKSAT2)

    snap = s.snapshot(KNACKSAT2)
    assert snap["status"] == tel.OK
    assert "GS_SATNOGS_DB_TOKEN" in snap["detail"]


async def test_a_missing_token_is_logged_once_and_not_once_per_poll(
        tmp_path, monkeypatch, caplog):
    """This runs for months on a wall display. A line per poll is how a real
    fault ends up invisible in the journal."""
    answer_with(monkeypatch, ok(page()), network=heard_once)
    caplog.set_level(logging.INFO, logger="app.services.telemetry")
    s = store(tmp_path, satnogs_db_token="")
    for _ in range(5):
        await s.refresh(KNACKSAT2, force=True)

    said = [r for r in caplog.records if tel.NO_TOKEN in r.getMessage()]
    assert len(said) == 1


async def test_offline_does_not_reach_the_network_even_with_a_token(
        tmp_path, monkeypatch):
    no_network(monkeypatch)
    s = store(tmp_path, offline=True)
    assert await s.refresh(KNACKSAT2) == tel.OFFLINE


async def test_a_rejected_token_does_not_become_a_retry_loop(tmp_path, monkeypatch):
    """A 401 answered at panel rate is a request every few seconds, forever,
    against someone else's service. The fallback must not turn that into two
    requests every few seconds: once the public source has answered, the TTL
    holds the DB off exactly as it would after a success."""
    seen = answer_with(monkeypatch,
                       lambda r: httpx.Response(401, json={"detail": "no"}),
                       network=heard_once)
    s = store(tmp_path)

    assert await s.refresh(KNACKSAT2) == tel.OK
    assert await s.refresh(KNACKSAT2) == tel.OK
    assert len(sent_to(seen, "/telemetry/")) == 1, "the second poll retried the token"
    assert s.snapshot(KNACKSAT2)["source"] == tel.NETWORK
    assert "GS_SATNOGS_DB_TOKEN" in s.snapshot(KNACKSAT2)["detail"]


async def test_a_rejected_token_is_said_once_rather_than_on_every_poll(
        tmp_path, monkeypatch, caplog):
    """The panel carries on working off the public source, which is exactly how
    a rejected token goes unnoticed. It gets a line — one line."""
    caplog.set_level(logging.WARNING, logger="app.services.telemetry")
    answer_with(monkeypatch, lambda r: httpx.Response(401, json={"detail": "no"}),
                network=heard_once)
    s = store(tmp_path)
    for _ in range(5):
        await s.refresh(KNACKSAT2, force=True)

    said = [r for r in caplog.records if tel.BAD_TOKEN in r.getMessage()]
    assert len(said) == 1


async def test_a_snapshot_before_anything_is_fetched_is_serialisable(tmp_path):
    """A browser connecting before the first fetch must still get a frame."""
    snap = store(tmp_path).snapshot(KNACKSAT2)
    assert snap["status"] == tel.UNKNOWN
    assert snap["frames"] == []
    assert snap["last_heard"] is None
    json.dumps(snap)


# --------------------------------------------------------------------------
# the filter, which is the thing this API lies about
# --------------------------------------------------------------------------

async def test_the_norad_filter_is_satellite_and_not_either_other_spelling(
        tmp_path, monkeypatch):
    """`norad_cat_id` is Network's spelling and `satellite__norad_cat_id` is
    /transmitters/'s. Sending either one here filters nothing, silently."""
    seen = answer_with(monkeypatch, ok(page(frame())))
    await store(tmp_path).refresh(KNACKSAT2)

    params = seen[0].url.params
    assert params["satellite"] == str(KNACKSAT2)
    assert "norad_cat_id" not in params
    assert "satellite__norad_cat_id" not in params
    assert seen[0].url.path.endswith("/telemetry/")


async def test_another_satellites_frames_are_dropped_rather_than_drawn(
        tmp_path, monkeypatch):
    """The shape an ignored filter arrives in: 200, plausible records, wrong
    spacecraft. Verified per record because a 200 proves nothing here."""
    answer_with(monkeypatch, ok(page(
        frame(),
        frame(norad_cat_id=ISS, observation_id=999),
        frame(norad_cat_id=None, observation_id=1000),
    )))
    s = store(tmp_path)
    await s.refresh(KNACKSAT2)

    frames = s.get(KNACKSAT2)
    assert [f["norad"] for f in frames] == [KNACKSAT2]


async def test_a_filter_that_stopped_filtering_is_said_out_loud(
        tmp_path, monkeypatch, caplog):
    """Silently showing four frames instead of forty is how this gets missed
    for a month. The log line names the parameter to go and check."""
    caplog.set_level(logging.WARNING, logger="app.services.telemetry")
    answer_with(monkeypatch, ok(page(frame(), frame(norad_cat_id=ISS))))
    await store(tmp_path).refresh(KNACKSAT2)

    assert any("satellite" in r.getMessage() for r in caplog.records)


async def test_the_token_is_sent_the_way_the_db_asks_for_it(tmp_path, monkeypatch):
    """The prefix is `Token`, not `Bearer`; the DB's own schema says so."""
    seen = answer_with(monkeypatch, ok(page(frame())))
    await store(tmp_path, satnogs_db_token="abc123").refresh(KNACKSAT2)

    assert seen[0].headers["authorization"] == "Token abc123"
    assert "knacksat2-ground-station" in seen[0].headers["user-agent"]


# --------------------------------------------------------------------------
# what reaches the panel
# --------------------------------------------------------------------------

def test_the_summary_drops_the_frame_and_keeps_its_size():
    """Forty frames of raw hex is the whole payload, and it goes over the
    WebSocket to every display. A length and a head is what a panel draws."""
    long_hex = "AB" * 400
    summary = _summarise(frame(frame=long_hex))

    assert "frame" not in summary
    assert summary["bytes"] == 400
    assert len(summary["head"]) == tel.FRAME_HEAD_CHARS
    assert summary["observation_id"] == 12345678
    assert summary["station_id"] == 5024


def test_a_decoded_blob_does_not_carry_its_nesting_over_the_websocket():
    """A decoder emits whatever it likes. The panel prints scalars."""
    summary = _summarise(frame(decoded={
        "battery_v": 3.92,
        "safe_mode": False,
        "callsign": "KNACKSAT-2",
        "raw": {"packet": ["x"] * 500},
        "history": list(range(500)),
        "note": "y" * 500,
    }))

    assert summary["values"]["battery_v"] == 3.92
    assert summary["values"]["safe_mode"] is False
    assert "raw" not in summary["values"]
    assert "history" not in summary["values"]
    assert len(summary["values"]["note"]) == tel.MAX_VALUE_CHARS
    assert summary["decoded"] is True


def test_influxdb_is_a_marker_that_the_decode_is_elsewhere_not_a_value():
    """`decoded: "influxdb"` means the fields exist, in their database. Reading
    it as data would put the word influxdb on the wall."""
    summary = _summarise(frame(decoded="influxdb"))
    assert summary["values"] == {}
    assert summary["decoded_in"] == "influxdb"
    assert summary["decoded"] is True


def test_an_undecoded_frame_says_so():
    summary = _summarise(frame(decoded=None))
    assert summary["decoded"] is False
    assert summary["values"] == {}


async def test_at_most_a_panel_of_frames_is_kept(tmp_path, monkeypatch):
    """A satellite with a busy month must not turn one poll into a megabyte."""
    many = [frame(timestamp=f"2026-09-16T02:{i:02d}:00Z", observation_id=i)
            for i in range(60)]
    answer_with(monkeypatch, ok(page(*many)))
    s = store(tmp_path)
    await s.refresh(KNACKSAT2)

    assert len(s.get(KNACKSAT2)) == tel.MAX_FRAMES


# --------------------------------------------------------------------------
# ordering, and the clocks that are not there
# --------------------------------------------------------------------------

async def test_frames_are_newest_first_whatever_order_the_api_used(
        tmp_path, monkeypatch):
    """The panel draws the top of the list as the last thing heard."""
    answer_with(monkeypatch, ok(page(
        frame(timestamp="2026-09-14T01:00:00Z", observation_id=1),
        frame(timestamp="2026-09-16T02:28:09Z", observation_id=3),
        frame(timestamp="2026-09-15T22:10:00Z", observation_id=2),
    )))
    s = store(tmp_path)
    await s.refresh(KNACKSAT2)

    assert [f["observation_id"] for f in s.get(KNACKSAT2)] == [3, 2, 1]
    assert s.snapshot(KNACKSAT2)["last_heard"] == "2026-09-16T02:28:09Z"


async def test_a_frame_with_a_missing_or_broken_timestamp_does_not_raise(
        tmp_path, monkeypatch):
    """One bad clock must not take the whole panel down with a TypeError in
    the sort. A frame is evidence of contact even when its time is not."""
    answer_with(monkeypatch, ok(page(
        frame(timestamp=None, observation_id=1),
        frame(timestamp="yesterday", observation_id=2),
        frame(timestamp="2026-09-16T02:28:09Z", observation_id=3),
    )))
    s = store(tmp_path)
    await s.refresh(KNACKSAT2)

    ids = [f["observation_id"] for f in s.get(KNACKSAT2)]
    assert ids[0] == 3, "the frame that does have a time is not first"
    assert sorted(ids) == [1, 2, 3], "a frame was dropped for its clock"


async def test_a_timestamp_with_no_zone_sorts_against_one_that_has_one(
        tmp_path, monkeypatch):
    """Comparing a naive datetime with an aware one raises; SatNOGS is UTC, so
    the assumption is made once rather than discovered in a traceback."""
    answer_with(monkeypatch, ok(page(
        frame(timestamp="2026-09-16T02:00:00", observation_id=1),
        frame(timestamp="2026-09-16T03:00:00Z", observation_id=2),
    )))
    s = store(tmp_path)
    await s.refresh(KNACKSAT2)

    assert [f["observation_id"] for f in s.get(KNACKSAT2)] == [2, 1]


# --------------------------------------------------------------------------
# nothing heard, and nothing reachable
# --------------------------------------------------------------------------

async def test_an_empty_result_set_is_not_an_error(tmp_path, monkeypatch):
    """A satellite nobody has decoded this week is a fact, not a failure — and
    it must not be dressed up as one. Both sources answering "nothing" used to
    reach the panel as "SatNOGS could not be reached", which sends somebody to
    check a network that is working perfectly well."""
    answer_with(monkeypatch, ok(page()))
    s = store(tmp_path)

    assert await s.refresh(KNACKSAT2) == tel.OK
    snap = s.snapshot(KNACKSAT2)
    assert snap["count"] == 0
    assert snap["available"] is False
    assert snap["detail"] == ""


async def test_a_bare_list_is_read_as_well_as_a_paginated_page(tmp_path, monkeypatch):
    """/transmitters/ on this same service returns a bare list. If /telemetry/
    ever does, that is not a reason to show nothing."""
    answer_with(monkeypatch, ok([frame()]))
    s = store(tmp_path)

    assert await s.refresh(KNACKSAT2) == tel.OK
    assert len(s.get(KNACKSAT2)) == 1


async def test_an_unreachable_satnogs_keeps_the_frames_it_already_had(
        tmp_path, monkeypatch):
    """The last contact is still the last contact when the uplink drops.

    Both sources are cut, because that is what losing the uplink means — one
    host being unreachable while the other answers is a different state, and
    it is the test below.
    """
    answer_with(monkeypatch, ok(page(frame())))
    s = store(tmp_path)
    await s.refresh(KNACKSAT2)

    def dead(request):
        raise httpx.ConnectError("no route to host")

    answer_with(monkeypatch, dead, network=dead)
    assert await s.refresh(KNACKSAT2, force=True) == tel.UNREACHABLE
    assert len(s.get(KNACKSAT2)) == 1
    assert s.snapshot(KNACKSAT2)["available"] is True


async def test_the_db_being_down_alone_is_not_the_panel_being_down(
        tmp_path, monkeypatch):
    """Two services, two ways to be unreachable. db.satnogs.org refusing while
    network.satnogs.org answers is the ordinary case on this station — it is
    every poll, because there is no token — and it must not paint the panel as
    an outage when the public source is answering perfectly well."""
    def dead(request):
        raise httpx.ConnectError("no route to host")

    answer_with(monkeypatch, dead, network=heard_once)
    s = store(tmp_path)

    assert await s.refresh(KNACKSAT2) == tel.OK
    assert s.snapshot(KNACKSAT2)["source"] == tel.NETWORK


async def test_a_page_of_junk_is_not_read_as_no_frames(tmp_path, monkeypatch):
    """Overwriting good frames with a parse failure would lose the contact
    history to a captive portal's login page. Both sources have to hold this
    line — a portal intercepts every host on the LAN, not one of them."""
    answer_with(monkeypatch, ok(page(frame())))
    s = store(tmp_path)
    await s.refresh(KNACKSAT2)

    junk = ok({"results": "not a list"})
    answer_with(monkeypatch, junk, network=junk)
    assert await s.refresh(KNACKSAT2, force=True) == tel.UNREACHABLE
    assert len(s.get(KNACKSAT2)) == 1


# --------------------------------------------------------------------------
# the cache
# --------------------------------------------------------------------------

async def test_a_fresh_cache_does_not_go_back_to_satnogs(tmp_path, monkeypatch):
    """Every display polls this route. Without the TTL, polling the dashboard
    is polling SatNOGS."""
    seen = answer_with(monkeypatch, ok(page(frame())))
    s = store(tmp_path)
    await s.refresh(KNACKSAT2)
    await s.refresh(KNACKSAT2)

    assert len(seen) == 1


async def test_frames_survive_a_restart_with_no_token_at_all(tmp_path, monkeypatch):
    """The station this runs on has no token today. Frames fetched when it had
    one are still the newest anybody has, and a restart must not lose them."""
    answer_with(monkeypatch, ok(page(frame())))
    await store(tmp_path).refresh(KNACKSAT2)

    # Nothing to fetch and nowhere to fetch it from: the cache is on its own.
    def dead(request):
        raise httpx.ConnectError("no route to host")

    answer_with(monkeypatch, dead, network=dead)
    after = store(tmp_path, satnogs_db_token="")
    await after.refresh(KNACKSAT2, force=True)

    snap = after.snapshot(KNACKSAT2)
    assert snap["available"] is True
    assert snap["count"] == 1
    assert "GS_SATNOGS_DB_TOKEN" in snap["detail"]


async def test_the_source_of_cached_frames_survives_a_restart(tmp_path, monkeypatch):
    """Which source the frames came from decides how the panel draws them —
    decoded fields or bytes. A display that comes up on cache alone must not
    relabel someone's telemetry as the other kind."""
    answer_with(monkeypatch, ok(page()), network=heard_once)
    await store(tmp_path, satnogs_db_token="").refresh(KNACKSAT2)

    after = store(tmp_path, satnogs_db_token="")
    assert after.snapshot(KNACKSAT2)["source"] == tel.NETWORK


def test_a_corrupt_cache_is_ignored_rather_than_fatal(tmp_path):
    """A half-written file from a power cut must not stop the backend."""
    (tmp_path / "telemetry.json").write_text("{not json", encoding="utf-8")
    assert store(tmp_path).get(KNACKSAT2) == []


# --------------------------------------------------------------------------
# the endpoint
# --------------------------------------------------------------------------

class FakeRequest:
    def __init__(self, app_state) -> None:
        self.app = type("App", (), {"state": app_state})


def app_state(**attrs):
    return type("State", (), attrs)


async def test_the_endpoint_answers_a_shaped_200_when_nothing_can_be_fetched(
        tmp_path, monkeypatch):
    """Not a 4xx, not a 503: an unconfigured token and an unreachable service
    are both states to render, and a fetch error thrown from here would leave
    the rest of the panel with nothing to say either."""
    def dead(request):
        raise httpx.ConnectError("no route to host")

    answer_with(monkeypatch, dead, network=dead)
    s = store(tmp_path, satnogs_db_token="")
    settings = Settings(data_dir=tmp_path, default_norad=KNACKSAT2)

    body = await telemetry_endpoint(FakeRequest(app_state(telemetry=s, settings=settings)))
    assert body["norad"] == KNACKSAT2
    assert body["frames"] == []
    assert "GS_SATNOGS_DB_TOKEN" in body["detail"]
    json.dumps(body)


async def test_the_endpoint_falls_back_to_the_configured_satellite(
        tmp_path, monkeypatch):
    answer_with(monkeypatch, ok(page(frame())))
    s = store(tmp_path)
    settings = Settings(data_dir=tmp_path, default_norad=KNACKSAT2)

    body = await telemetry_endpoint(FakeRequest(app_state(telemetry=s, settings=settings)))
    assert body["norad"] == KNACKSAT2
    assert body["count"] == 1
    assert body["status"] == tel.OK


async def test_a_backend_without_the_store_wired_in_says_so(tmp_path):
    """A silent empty list would look exactly like a satellite nobody has
    heard, which is the wrong thing to debug."""
    from fastapi import HTTPException

    settings = Settings(data_dir=tmp_path, default_norad=KNACKSAT2)
    with pytest.raises(HTTPException):
        await telemetry_endpoint(FakeRequest(app_state(settings=settings)))
