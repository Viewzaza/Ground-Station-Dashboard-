"""Orbital elements, and the requests this station is not allowed to make.

These tests stand in for Celestrak, which cannot be asserted against from here
for the reason the module exists: repeating a download before the data has
changed returns HTTP 403, and 50 errors in two hours puts the client's IP in
their firewall. That IP is the whole station's, in Bangkok, shared with
everything else on the LAN. A test suite that exercised the real fetch would be
the single most expensive thing in this repo to run twice.

So this file is not about fetching TLEs. It is about **not** fetching them.
`Scheduler.start()` calls `refresh()` eagerly on every process start, which
means the on-disk cache — `./backend/data:/data`, the rate limiter and not an
optimisation — is the only reason a restart costs zero requests. Every test
here is named for the wrong thing it prevents, and most of those wrong things
are a request, not a blank panel: a retry after a 403, a restart that refetches,
five panels booting into five downloads, a failure that stamps the cache it
could not refresh. The one kind of regression that is worse than a blank panel
is a populated one that is quietly wrong, and that is the rest of the file —
elements labelled with a source they did not come from, or a fortnight-old set
wearing a green chip.

Nothing here touches the network. `answer_with` stands in for *both* sources —
Celestrak and the SatNOGS DB fallback, because the fallback runs exactly when
Celestrak fails, so a test that stubbed only one would make real requests out of
the other on the very path it was testing — and `data_dir` is always a tmp_path,
so a real cache on the machine running the tests cannot leak into them.

Two of these started as `xfail(strict=True)` because they were bugs rather than
decisions — the provenance field being set before the body had parsed, and the
SatNOGS fallback raising out of `refresh()` on a body that was not the list the
DB promises. Both are fixed; the tests stayed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.routes.satellites import tle_refresh as tle_refresh_endpoint
from app.services import tle_store as tle
from app.services.tle_store import TleStore, _parse_3le, parse_tle_epoch

KNACKSAT2 = 67683
ISS = 25544
NOAA15 = 25338

# A real, frozen element set — the same pair `test_predictor.py` propagates. It
# is here for its *columns*: the catalog number lives at 3-7 and the epoch at
# 19-32, and splicing into a line that was actually published is the only way
# those offsets stay honest as this file builds variations.
KNACKSAT2_TLE1 = "1 67683U 98067XZ  26255.31192122  .00056149  00000+0  48789-3 0  9994"
KNACKSAT2_TLE2 = "2 67683  51.6258 213.5681 0007959 152.6345 207.5073 15.68476422 33916"


def elements(norad: int = KNACKSAT2, *, days_old: float | None = None) -> tuple[str, str]:
    """The real element set, with a catalog number and optionally an epoch.

    `days_old` is measured from now, because the age chip is measured from now:
    a frozen epoch would move from "fresh" to "stale" as this repo ages, and
    the test would start failing on a date rather than on a change.
    """
    tle1 = KNACKSAT2_TLE1[:2] + f"{norad:05d}" + KNACKSAT2_TLE1[7:]
    tle2 = KNACKSAT2_TLE2[:2] + f"{norad:05d}" + KNACKSAT2_TLE2[7:]
    if days_old is not None:
        moment = datetime.now(timezone.utc) - timedelta(days=days_old)
        jan1 = datetime(moment.year, 1, 1, tzinfo=timezone.utc)
        day = (moment - jan1).total_seconds() / 86400.0 + 1.0
        tle1 = tle1[:18] + f"{moment.year % 100:02d}{day:012.8f}" + tle1[32:]
    return tle1, tle2


def block(name: str, norad: int = KNACKSAT2, **kwargs) -> str:
    """One satellite as Celestrak's FORMAT=tle writes it: a name and two lines."""
    tle1, tle2 = elements(norad, **kwargs)
    return f"{name}\n{tle1}\n{tle2}\n"


def group(*blocks: str) -> str:
    return "".join(blocks)


def store(tmp_path, **overrides) -> TleStore:
    base = dict(data_dir=Path(tmp_path), offline=False, celestrak_group="amateur",
                pinned_norad=str(KNACKSAT2), satnogs_db_token="")
    base.update(overrides)
    return TleStore(Settings(**base))


def write_cache(tmp_path, *, age_s: float = 0.0, source: str = "celestrak:amateur",
                sats: dict | None = None) -> Path:
    """The cache file as the previous process left it behind.

    Written by hand on purpose: the point of most of these tests is a store that
    comes up holding elements it did not fetch in this process, and the on-disk
    shape is a contract with that other process. It is pinned against the real
    writer in `test_the_cache_file_is_the_shape_the_next_process_will_read`.
    """
    tle1, tle2 = elements()
    blob = {
        "fetched_at": (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat(),
        "source": source,
        "sats": {str(KNACKSAT2): {"name": "KNACKSAT-2", "tle1": tle1, "tle2": tle2}}
                if sats is None else sats,
    }
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    path = Path(tmp_path) / "tle_cache.json"
    path.write_text(json.dumps(blob, indent=1), encoding="utf-8")
    return path


# Captured before anything patches it, so a test that changes its mind about
# what a source answers does not end up wrapping its own stand-in.
REAL_CLIENT = httpx.AsyncClient


def nothing_pinned(request: httpx.Request) -> httpx.Response:
    """The SatNOGS DB with no elements for that satellite: an empty list.

    The default, so that a test about Celestrak does not have to say anything
    about the fallback in order not to reach the real internet through it.
    """
    return httpx.Response(200, json=[])


def answer_with(monkeypatch, celestrak, satnogs=nothing_pinned) -> list[httpx.Request]:
    """Point every client the store builds at a handler. Returns the requests.

    Dispatching on the host is not decoration: a 403 from Celestrak *is* the
    path that calls SatNOGS, so the two stand-ins are needed together in almost
    every failure test here.
    """
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        chosen = celestrak if "celestrak" in request.url.host else satnogs
        return chosen(request)

    def factory(**kwargs):
        return REAL_CLIENT(transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(tle.httpx, "AsyncClient", factory)
    return seen


def no_network(monkeypatch) -> None:
    """Building a client at all is the failure, so fail at construction."""
    class Exploding:
        def __init__(self, **kwargs):
            raise AssertionError("the store reached for the network")

    monkeypatch.setattr(tle.httpx, "AsyncClient", Exploding)


def to_celestrak(seen: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in seen if "celestrak" in r.url.host]


def to_satnogs(seen: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in seen if "celestrak" not in r.url.host]


def ok_tle(body: str):
    return lambda request: httpx.Response(200, text=body)


def forbidden(request: httpx.Request) -> httpx.Response:
    """What Celestrak answers when you ask again too soon. The one that counts."""
    return httpx.Response(403, text="Error: Requests too frequent")


def pinned_elements(request: httpx.Request) -> httpx.Response:
    """db.satnogs.org/api/tle/ — a list, with the 3LE "0 " marker on the name."""
    tle1, tle2 = elements()
    return httpx.Response(200, json=[{
        "norad_cat_id": KNACKSAT2,
        "tle0": "0 KNACKSAT-2",
        "tle1": tle1,
        "tle2": tle2,
        "updated": "2026-09-12T11:20:00Z",
    }])


# --------------------------------------------------------------------------
# the floor, which is the entire job
# --------------------------------------------------------------------------

async def test_a_fresh_cache_does_not_go_back_to_celestrak(tmp_path, monkeypatch):
    """The most important assertion in this file.

    Every poll, every panel and every restart runs through `refresh()`. Without
    the TTL gate in front of the fetch, a dashboard refresh is a Celestrak
    request, and Celestrak answers a request it has already served with a 403
    that counts toward the fifty that get this IP firewalled. The last call is
    made with the client class replaced by one that raises on construction, so
    "no request" means no socket was even reached for.
    """
    seen = answer_with(monkeypatch, ok_tle(group(block("KNACKSAT-2"))))
    s = store(tmp_path)

    assert await s.refresh() is True
    assert await s.refresh() is False
    assert len(to_celestrak(seen)) == 1

    no_network(monkeypatch)
    assert await s.refresh() is False


async def test_a_restart_costs_no_requests_because_the_cache_is_on_disk(
        tmp_path, monkeypatch):
    """`Scheduler.start()` fetches eagerly on every process start, so this is
    what stands between a `docker compose restart` and a Celestrak request —
    and between a crash loop and fifty of them. The second store is a second
    process: same volume, no memory of the first."""
    answer_with(monkeypatch, ok_tle(group(block("KNACKSAT-2"), block("ISS (ZARYA)", ISS))))
    await store(tmp_path).refresh()

    no_network(monkeypatch)
    after = store(tmp_path)

    assert await after.refresh() is False
    assert len(after) == 2
    assert after.get(KNACKSAT2).tle1 == elements()[0], "the elements did not survive"
    assert after.get(ISS).name == "ISS (ZARYA)"


async def test_the_source_of_cached_elements_survives_a_restart(tmp_path, monkeypatch):
    """Which source the elements came from is how anybody checks them by hand
    against the published catalogue. A display that comes up on cache alone
    must not relabel them."""
    answer_with(monkeypatch, forbidden, satnogs=pinned_elements)
    await store(tmp_path).refresh()

    no_network(monkeypatch)
    assert store(tmp_path).get(KNACKSAT2).source == "satnogs-db"


async def test_panels_booting_together_are_one_request_and_not_one_each(
        tmp_path, monkeypatch):
    """The wall display coming back after a power cut, with every panel
    refreshing at once, is the case the lock is for. Five concurrent refreshes
    that each got past the TTL check before any of them finished would be five
    downloads of the same file — which is four 403s, from one IP, in a second."""
    seen = answer_with(monkeypatch, ok_tle(group(block("KNACKSAT-2"))))
    s = store(tmp_path)

    results = await asyncio.gather(*(s.refresh() for _ in range(5)))

    assert len(to_celestrak(seen)) == 1, "the lock is not holding the floor"
    assert results.count(True) == 1


async def test_a_cache_just_under_the_floor_still_holds(tmp_path, monkeypatch):
    """Two hours means two hours. An off-by-one in the comparison is not
    visible on any panel and is worth a 403 every restart."""
    write_cache(tmp_path, age_s=7140)
    no_network(monkeypatch)

    s = store(tmp_path)
    assert s.is_fresh is True
    assert await s.refresh() is False


async def test_a_cache_that_has_aged_past_the_floor_is_allowed_to_fetch_again(
        tmp_path, monkeypatch):
    """The opposite failure, and the reason the floor is a floor and not a
    switch: a gate that never opens is a station flying week-old elements,
    which is a rotator pointed at the wrong patch of sky."""
    write_cache(tmp_path, age_s=7201)
    seen = answer_with(monkeypatch, ok_tle(group(block("KNACKSAT-2"))))

    s = store(tmp_path)
    assert s.is_fresh is False
    assert await s.refresh() is True
    assert len(to_celestrak(seen)) == 1


def test_the_floor_is_the_two_hours_celestrak_asks_for():
    """Pinned against the default in `config.py` rather than a live Settings,
    so an environment variable on the machine running the tests cannot make it
    pass. Lowering this is how the station gets firewalled, and it would
    otherwise be a one-character change with no test to stop it."""
    assert Settings.model_fields["tle_ttl_s"].default == 7200


async def test_force_is_the_only_way_past_the_floor(tmp_path, monkeypatch):
    """`force=True` exists for an operator who knows a new element set was just
    published. It is a deliberate 403 risk, so it must be exactly that — never
    the default, and never reachable from a browser (see the endpoint tests at
    the bottom of this file)."""
    seen = answer_with(monkeypatch, ok_tle(group(block("KNACKSAT-2"))))
    s = store(tmp_path)

    await s.refresh()
    assert await s.refresh() is False
    assert await s.refresh(force=True) is True
    assert len(to_celestrak(seen)) == 2


async def test_offline_does_not_reach_celestrak_even_when_forced(tmp_path, monkeypatch):
    """GS_OFFLINE=1 is the switch that makes this backend runnable on a laptop
    with no route out. A source that honoured it only on the polite path would
    make the setting a suggestion."""
    no_network(monkeypatch)
    s = store(tmp_path, offline=True)

    assert await s.refresh(force=True) is False
    assert len(s) == 0


# --------------------------------------------------------------------------
# a non-200 is terminal, because the fiftieth one is a firewall rule
# --------------------------------------------------------------------------

async def test_a_403_is_terminal_and_not_the_first_of_fifty(tmp_path, monkeypatch):
    """403 is the status Celestrak actually returns, and it means "you already
    have this data" — so a retry is guaranteed to earn another one. Fifty in
    two hours puts this IP in their firewall, and it is the station's IP,
    shared with the rest of the building. One 403 must cost exactly one
    request, and must not reach the caller as an exception either: the
    scheduler awaits this before the first pass is predicted."""
    seen = answer_with(monkeypatch, forbidden)

    assert await store(tmp_path).refresh() is False
    assert len(to_celestrak(seen)) == 1, "a 403 was retried"


async def test_no_error_status_at_all_turns_into_a_second_request(tmp_path, monkeypatch):
    """Every one of these counts toward the fifty — Celestrak's limit is on
    *errors*, not on 403s specifically. A 500 or a 429 that got its own retry
    branch would be a loop at exactly the moment the service is unhappy."""
    for status in (403, 404, 429, 500, 502, 503):
        seen = answer_with(
            monkeypatch, lambda request, code=status: httpx.Response(code, text="no"))
        s = store(tmp_path / f"status-{status}")

        assert await s.refresh() is False, status
        assert len(to_celestrak(seen)) == 1, status


async def test_the_status_celestrak_answered_is_in_the_log(tmp_path, monkeypatch, caplog):
    """The only warning before the firewall arrives is in the journal, and a
    403 (we asked too soon — stop) has to be distinguishable from a 500 (their
    problem — it will pass) without reading the code."""
    caplog.set_level(logging.ERROR, logger="app.services.tle_store")
    answer_with(monkeypatch, forbidden)

    await store(tmp_path).refresh()

    said = [r.getMessage() for r in caplog.records]
    assert any("403" in message for message in said), said


async def test_an_unreachable_celestrak_is_not_an_exception_in_the_scheduler(
        tmp_path, monkeypatch):
    """A dropped link is a Tuesday here. `refresh()` returns a bool and logs
    its failures; every caller is written for that, and one that raised would
    take `Scheduler.start()` down before the pass list was ever computed."""
    def dead(request):
        raise httpx.ConnectError("no route to host")

    write_cache(tmp_path, age_s=7201)
    answer_with(monkeypatch, dead, satnogs=dead)
    s = store(tmp_path)

    assert await s.refresh() is False
    assert len(s) == 1

    def slow(request):
        raise httpx.ReadTimeout("timed out")

    answer_with(monkeypatch, slow, satnogs=slow)
    assert await s.refresh(force=True) is False
    assert len(s) == 1


# --------------------------------------------------------------------------
# what a failed fetch must not destroy
# --------------------------------------------------------------------------

async def test_a_403_does_not_throw_away_the_elements_it_already_had(
        tmp_path, monkeypatch):
    """Stale elements still fly a satellite; an empty store does not. A
    day-old TLE points the antenna within a fraction of a degree, and the
    failure mode this prevents is the store emptying itself in response to
    somebody else's service saying no."""
    write_cache(tmp_path, age_s=7201)
    answer_with(monkeypatch, forbidden)
    s = store(tmp_path)

    assert len(s) == 1
    assert await s.refresh() is False
    assert len(s) == 1
    assert s.get(KNACKSAT2).tle1 == elements()[0]


async def test_a_failed_fetch_does_not_stamp_the_cache_it_could_not_refresh(
        tmp_path, monkeypatch):
    """Worse than the failure would be recording it as a success: a file
    written with `fetched_at = now` and nothing new in it would look fresh for
    two hours, and a restart would load that and skip the fetch again. The
    file is only written when something actually arrived."""
    path = write_cache(tmp_path, age_s=7201)
    before = path.read_text(encoding="utf-8")
    answer_with(monkeypatch, forbidden)

    assert await store(tmp_path).refresh() is False
    assert path.read_text(encoding="utf-8") == before


async def test_a_first_boot_that_fails_writes_no_cache_file_at_all(
        tmp_path, monkeypatch):
    """The same rule with nothing to fall back on. An empty cache stamped
    `now` is the worst state this module can be in: no elements, and a
    two-hour promise not to go and get any."""
    answer_with(monkeypatch, forbidden)
    s = store(tmp_path)

    assert await s.refresh() is False
    assert len(s) == 0
    assert not (tmp_path / "tle_cache.json").exists()
    assert s.is_fresh is False, "an empty store told the next caller not to fetch"


async def test_an_error_page_that_arrives_with_a_200_is_not_an_empty_catalogue(
        tmp_path, monkeypatch):
    """A captive portal answers every host on the LAN with 200 and a login
    page — this station is on a university network, and the same shape is
    pinned in `test_frames.py` for the same reason. Reading one as "Celestrak
    has no satellites today" would empty the store on a wifi glitch."""
    write_cache(tmp_path, age_s=7201)
    answer_with(monkeypatch, lambda request: httpx.Response(
        200, text="<!DOCTYPE html><html><head><title>Sign in</title></head></html>"))
    s = store(tmp_path)

    assert await s.refresh() is False
    assert len(s) == 1


async def test_a_fetch_that_returned_nothing_does_not_relabel_where_the_elements_came_from(
        tmp_path, monkeypatch):
    """The source is a provenance claim: it is how anybody checks these
    elements against the published catalogue, and how the fallback's transmitter
    metadata is known to apply. A captive portal's 200 currently renames a
    store full of satnogs-db elements to celestrak:amateur without one element
    having arrived — and then the panel cites a source that answered nothing."""
    write_cache(tmp_path, age_s=7201, source="satnogs-db")
    answer_with(monkeypatch,
                lambda request: httpx.Response(200, text="<html>Sign in</html>"),
                satnogs=nothing_pinned)
    s = store(tmp_path)

    assert await s.refresh() is False
    assert s.get(KNACKSAT2).source == "satnogs-db"


# --------------------------------------------------------------------------
# the fallback, which runs exactly when Celestrak has said no
# --------------------------------------------------------------------------

async def test_a_working_celestrak_is_not_followed_by_a_call_to_satnogs(
        tmp_path, monkeypatch):
    """Two services asked for every answer is two lots of somebody else's
    goodwill spent, and SatNOGS is a request per pinned satellite."""
    seen = answer_with(monkeypatch, ok_tle(group(block("KNACKSAT-2"))))

    await store(tmp_path).refresh()

    assert to_satnogs(seen) == []


async def test_the_fallback_keeps_the_satellite_flying_when_celestrak_403s(
        tmp_path, monkeypatch):
    """The whole reason there are two sources. A 403 on the day an element set
    is a fortnight old is otherwise a pass predicted off elements nobody
    should be pointing an antenna with."""
    answer_with(monkeypatch, forbidden, satnogs=pinned_elements)
    s = store(tmp_path)

    assert await s.refresh() is True
    info = s.get(KNACKSAT2)
    assert info.name == "KNACKSAT-2", "the 3LE \"0 \" marker is not part of the name"
    assert info.source == "satnogs-db"
    assert info.tle1 == elements()[0]


async def test_the_fallback_asks_only_about_the_satellites_that_are_pinned(
        tmp_path, monkeypatch):
    """It is a request per satellite, so it is not a catalogue download: it is
    the one or two this station actually tracks. Pointing it at the whole
    selector would be hundreds of requests on the failure path."""
    seen = answer_with(monkeypatch, forbidden, satnogs=pinned_elements)

    await store(tmp_path, pinned_norad="67683,25544").refresh()

    assert [r.url.params["norad_cat_id"] for r in to_satnogs(seen)] == ["67683", "25544"]


async def test_the_fallback_sends_the_token_the_way_the_db_asks_for_it(
        tmp_path, monkeypatch):
    """The prefix is `Token`, not `Bearer`; the DB's own schema says so. And an
    `Authorization: Token ` with nothing after it is a 401 that arrives looking
    exactly like "no elements for that satellite" — which is the state this
    station is in, since it has no token."""
    seen = answer_with(monkeypatch, forbidden, satnogs=pinned_elements)
    await store(tmp_path, satnogs_db_token="abc123").refresh()
    assert to_satnogs(seen)[0].headers["authorization"] == "Token abc123"

    seen = answer_with(monkeypatch, forbidden, satnogs=pinned_elements)
    await store(tmp_path / "no-token", satnogs_db_token="").refresh()
    assert "authorization" not in to_satnogs(seen)[0].headers
    assert "knacksat2-ground-station" in to_satnogs(seen)[0].headers["user-agent"]


async def test_a_one_satellite_fallback_does_not_replace_the_whole_catalogue(
        tmp_path, monkeypatch):
    """The fallback fetches the pinned satellites only. If it replaced the
    store instead of updating it, one Celestrak 403 would cut the selector from
    a thousand objects to one — and the selector is how an operator picks the
    next pass to watch."""
    answer_with(monkeypatch, ok_tle(group(
        block("KNACKSAT-2"), block("ISS (ZARYA)", ISS), block("NOAA 15", NOAA15))))
    s = store(tmp_path)
    await s.refresh()
    assert len(s) == 3

    answer_with(monkeypatch, forbidden, satnogs=pinned_elements)
    assert await s.refresh(force=True) is True
    assert len(s) == 3, "the fallback emptied the selector"
    assert s.get(NOAA15) is not None


async def test_a_fallback_that_has_nothing_either_leaves_the_elements_alone(
        tmp_path, monkeypatch):
    """Both sources saying no is the state the cache exists for, and it is a
    normal Tuesday: Celestrak 403s whenever the data has not changed."""
    write_cache(tmp_path, age_s=7201)
    answer_with(monkeypatch, forbidden, satnogs=nothing_pinned)
    s = store(tmp_path)

    assert await s.refresh() is False
    assert len(s) == 1


async def test_a_fallback_answering_junk_does_not_raise_out_of_the_refresh(
        tmp_path, monkeypatch):
    """`refresh()` returns a bool and logs its failures — that is the contract
    every caller is written for, and `Scheduler.start()` awaits it before the
    first pass is predicted. A captive portal intercepts the whole LAN, not one
    host, so the realistic failure is Celestrak 403ing and the fallback
    answering a login page with a 200 on it. That must be a cache hit, not a
    traceback on the startup path."""
    write_cache(tmp_path, age_s=7201)
    answer_with(monkeypatch, forbidden,
                satnogs=lambda request: httpx.Response(200, text="<html>Sign in</html>"))
    s = store(tmp_path)

    assert await s.refresh() is False
    assert len(s) == 1


# --------------------------------------------------------------------------
# parsing, where a bad block must cost a satellite and not the catalogue
# --------------------------------------------------------------------------

def test_a_well_formed_group_yields_every_satellite_in_it():
    """The happy path, pinned so that everything below it means something."""
    parsed = _parse_3le(group(
        block("KNACKSAT-2"), block("ISS (ZARYA)", ISS), block("NOAA 15", NOAA15)))

    assert list(parsed) == [KNACKSAT2, ISS, NOAA15]
    assert parsed[ISS]["name"] == "ISS (ZARYA)"
    assert parsed[ISS]["tle1"] == elements(ISS)[0]
    assert parsed[ISS]["tle2"] == elements(ISS)[1]


def test_a_download_cut_off_mid_catalogue_keeps_the_satellites_that_did_arrive():
    """A connection dropped partway through the amateur group leaves a block
    that is a name, or a name and one line. Losing that satellite is correct.
    Losing the nine hundred before it means the refresh has to be repeated —
    against a service that answers a repeat with a 403."""
    whole = group(block("KNACKSAT-2"), block("ISS (ZARYA)", ISS))
    name_only = whole + "NOAA 15\n"
    half_a_block = whole + "NOAA 15\n" + elements(NOAA15)[0] + "\n"
    mid_line = whole + "NOAA 15\n" + elements(NOAA15)[0][:37]

    assert list(_parse_3le(name_only)) == [KNACKSAT2, ISS]
    assert list(_parse_3le(half_a_block)) == [KNACKSAT2, ISS]
    assert list(_parse_3le(mid_line)) == [KNACKSAT2, ISS]


def test_a_block_that_is_not_two_element_lines_is_skipped_rather_than_stored():
    """The guard is what keeps a name line out of the `tle1` field. An entry
    with prose where its elements should be does not fail here — it fails
    inside Skyfield, while a pass is being predicted, which is both further
    from the cause and further from anybody watching."""
    junk = "SPACE JUNK\nthis is not an element line\nand neither is this one\n"

    assert _parse_3le(junk) == {}
    assert list(_parse_3le(junk + group(block("KNACKSAT-2")))) == [KNACKSAT2]


def test_a_block_missing_a_line_costs_the_catalogue_after_it_but_never_a_wrong_entry():
    """Pinned as a known limit of a fixed-stride parser, because the symptom is
    a half-empty selector with nothing in the log: a two-line block shifts every
    block after it and they are all dropped.

    What must never happen is the other outcome — a shifted block stored
    anyway, with a name line sitting in `tle1`. Fewer satellites is recovered
    by the next refresh; a satellite carrying garbage elements is an antenna
    driven at the wrong patch of sky, and nothing on the panel says so.
    """
    lost_a_line = "SAT-B\n" + elements(ISS)[0] + "\n"
    parsed = _parse_3le(
        group(block("KNACKSAT-2")) + lost_a_line + group(block("NOAA 15", NOAA15)))

    assert list(parsed) == [KNACKSAT2], "de-alignment now costs more or less than it did"
    for entry in parsed.values():
        assert entry["tle1"].startswith("1 ")
        assert entry["tle2"].startswith("2 ")


def test_a_catalog_number_that_is_not_a_number_costs_its_own_block_only():
    """Celestrak's Alpha-5 designators (`T7683`) appear in real groups, and the
    module's own docstring says the 6- and 9-digit numbers are simply left out
    of TLE-format responses. Either way the failure is one `int()` call, and it
    must not be the one that ends the parse."""
    tle1, tle2 = elements()
    alpha = "SAT-ALPHA\n" + tle1[:2] + "T7683" + tle1[7:] + "\n" \
            + tle2[:2] + "T7683" + tle2[7:] + "\n"

    assert list(_parse_3le(alpha + group(block("KNACKSAT-2")))) == [KNACKSAT2]


def test_the_3le_name_marker_is_not_part_of_the_satellites_name():
    """Celestrak's 3LE format and SatNOGS's `tle0` both prefix the name line
    with "0 ". A selector row reading "0 KNACKSAT-2" is a satellite nobody
    finds by typing its name into the search box."""
    tle1, tle2 = elements()
    parsed = _parse_3le(f"0 KNACKSAT-2   \n{tle1}\n{tle2}\n")

    assert parsed[KNACKSAT2]["name"] == "KNACKSAT-2"


def test_bytes_that_are_not_a_catalogue_are_no_satellites_rather_than_an_exception():
    """Everything that is not a TLE arrives here looking like a 200: an error
    document, a login page, an empty body from a proxy. None of them may raise
    — the caller reads an empty result as "keep the cache"."""
    assert _parse_3le("") == {}
    assert _parse_3le("<!DOCTYPE html><html><head><title>502</title></head>") == {}
    assert _parse_3le('{"detail": "Not found"}') == {}


# --------------------------------------------------------------------------
# the age chip, which is the operator's only warning that pointing is drifting
# --------------------------------------------------------------------------

def test_a_two_digit_year_is_not_read_as_the_wrong_century():
    """A TLE epoch carries two digits; 57-99 are 1957-1999 and 00-56 are
    2000-2056. That is the convention, not a guess, and reading 26 as 1926
    puts the epoch a century out — and every pass computed from it with it."""
    tle1, _ = elements()

    assert parse_tle_epoch(tle1[:18] + "26255.31192122" + tle1[32:]).year == 2026
    assert parse_tle_epoch(tle1[:18] + "56001.00000000" + tle1[32:]).year == 2056
    assert parse_tle_epoch(tle1[:18] + "57001.00000000" + tle1[32:]).year == 1957


def test_the_epoch_is_the_fractional_day_and_not_the_day_it_rounds_to():
    """Day 255.31192122 of 2026 is the 12th of September at 07:29 UTC, not
    midnight. Eight hours of error in the epoch is a pass prediction that is
    wrong by minutes, which is most of a pass."""
    epoch = parse_tle_epoch(KNACKSAT2_TLE1)

    assert epoch == datetime(2026, 9, 12, 7, 29, 9, 993408, tzinfo=timezone.utc)
    assert epoch.tzinfo is not None, "a naive epoch raises when it is subtracted from now"


def test_an_epoch_that_cannot_be_read_is_no_epoch_rather_than_an_exception():
    """This runs over every element set the store holds, including whatever the
    fallback stored without looking at it."""
    tle1, _ = elements()

    assert parse_tle_epoch(tle1[:18] + "not a date!!!!" + tle1[32:]) is None
    assert parse_tle_epoch("1 67683U") is None
    assert parse_tle_epoch("") is None


async def test_the_chip_moves_from_fresh_to_aging_to_stale_where_it_is_configured(
        tmp_path, monkeypatch):
    """The dashboard draws this as a colour, and it is the only thing telling
    an operator that the antenna is being pointed from elements nobody has
    refreshed. The thresholds are 7 and 14 days; a boundary that drifted would
    show a green chip over a fortnight-old TLE."""
    for days, expected in ((0.5, "fresh"), (6.9, "fresh"), (7.1, "aging"),
                           (13.9, "aging"), (14.1, "stale"), (30.0, "stale")):
        answer_with(monkeypatch, ok_tle(group(block("KNACKSAT-2", days_old=days))))
        s = store(tmp_path / f"age-{days}")
        await s.refresh()

        info = s.get(KNACKSAT2)
        assert info.state == expected, (days, info.age_days)
        assert abs(info.age_days - days) < 0.05, (days, info.age_days)


def test_an_element_set_nobody_can_date_is_still_served(tmp_path):
    """Pinned as it stands, including the part worth arguing about: an element
    set whose epoch columns cannot be read reports `age_days = 0.0` and a
    *fresh* chip — the same green as one published this morning. It is
    reachable, because the SatNOGS fallback stores `tle1` without parsing it.
    The row itself must exist (dropping it loses the satellite entirely), but
    if this is ever changed, it should be changed deliberately and here."""
    tle1, tle2 = elements()
    write_cache(tmp_path, sats={
        str(KNACKSAT2): {"name": "KNACKSAT-2", "tle1": "1 67683U no epoch here", "tle2": tle2}})

    info = store(tmp_path).get(KNACKSAT2)

    assert info is not None
    assert info.epoch is None
    assert info.age_days == 0.0
    assert info.state == "fresh"


def test_a_satellite_nobody_has_elements_for_is_none_rather_than_a_guess(tmp_path):
    """The route turns this into a 404. An empty TleInfo would be a pass
    predicted for a satellite parked at the origin of the coordinate system."""
    assert store(tmp_path).get(99999) is None


# --------------------------------------------------------------------------
# the selector
# --------------------------------------------------------------------------

async def test_the_pinned_satellite_sorts_first_whatever_the_group_contained(
        tmp_path, monkeypatch):
    """The amateur group is a thousand objects and this station tracks one.
    Alphabetical order buries KNACKSAT-2 somewhere in the middle of it."""
    answer_with(monkeypatch, ok_tle(group(
        block("AAUSAT-4", 40000), block("ZARYA", ISS), block("KNACKSAT-2"))))
    s = store(tmp_path)
    await s.refresh()

    items = s.catalog()

    assert items[0] == {"norad": KNACKSAT2, "name": "KNACKSAT-2", "pinned": True}
    assert [it["name"] for it in items[1:]] == ["AAUSAT-4", "ZARYA"]
    json.dumps(items)           # it goes to the browser as it is


def test_a_cached_satellite_with_no_name_is_listed_by_its_number(tmp_path):
    """A blank row in the selector cannot be clicked on purpose, and a cache
    written by an older version is exactly where a missing field comes from."""
    tle1, tle2 = elements(NOAA15)
    write_cache(tmp_path, sats={str(NOAA15): {"tle1": tle1, "tle2": tle2}})

    assert store(tmp_path).catalog() == [
        {"norad": NOAA15, "name": str(NOAA15), "pinned": False}]


# --------------------------------------------------------------------------
# the cache file itself, which is the rate limiter
# --------------------------------------------------------------------------

async def test_a_half_written_cache_file_is_ignored_rather_than_fatal(
        tmp_path, monkeypatch):
    """A host reboot SIGKILLs the container mid-`write_text` — the README says
    so, and accepts the cost, which is one extra fetch. What it cannot cost is
    a backend that will not start: this file is read in `__init__`, before
    anything else in the process exists."""
    (tmp_path / "tle_cache.json").write_text(
        '{\n "fetched_at": "2026-09-21T04:0', encoding="utf-8")

    s = store(tmp_path)
    assert len(s) == 0
    assert s.is_fresh is False, "a cache that could not be read must not hold off a fetch"

    answer_with(monkeypatch, ok_tle(group(block("KNACKSAT-2"))))
    assert await s.refresh() is True
    assert len(s) == 1


def test_a_cache_with_no_timestamp_is_not_taken_as_fresh_forever(tmp_path):
    """Infinite age is the safe reading of "I cannot tell how old this is": the
    alternative is a store that never refreshes because it does not know it
    should. The elements it did have are kept, because they are still the
    newest anybody here has."""
    tle1, tle2 = elements()
    (tmp_path / "tle_cache.json").write_text(json.dumps({
        "source": "celestrak:amateur",
        "sats": {str(KNACKSAT2): {"name": "KNACKSAT-2", "tle1": tle1, "tle2": tle2}},
    }), encoding="utf-8")

    s = store(tmp_path)

    assert s.cache_age_s == float("inf")
    assert s.is_fresh is False
    assert len(s) == 1


def test_a_store_with_no_cache_at_all_is_stale_rather_than_fresh(tmp_path):
    """First boot on a fresh volume. This is the one case that *must* fetch,
    and it is the reason `cache_age_s` is infinite rather than zero."""
    s = store(tmp_path)

    assert s.cache_age_s == float("inf")
    assert s.is_fresh is False
    assert len(s) == 0


async def test_the_cache_file_is_the_shape_the_next_process_will_read(
        tmp_path, monkeypatch):
    """The reader and the writer are two different processes, usually two
    different deploys. A key renamed on one side is a cache that is silently
    never a hit — a Celestrak fetch on every restart, with nothing failing.
    The hand-written fixture the rest of this file uses is checked against the
    real writer here, so those tests cannot pass on a shape nothing writes."""
    answer_with(monkeypatch, ok_tle(group(block("KNACKSAT-2"))))
    await store(tmp_path).refresh()

    written = json.loads((tmp_path / "tle_cache.json").read_text(encoding="utf-8"))
    assert set(written) == {"fetched_at", "source", "sats"}
    assert set(written["sats"][str(KNACKSAT2)]) == {"name", "tle1", "tle2"}
    assert written["source"] == "celestrak:amateur"
    stamp = datetime.fromisoformat(written["fetched_at"])
    assert stamp.tzinfo is not None, "a naive stamp raises when cache_age_s subtracts it"

    by_hand = json.loads(write_cache(tmp_path / "by-hand").read_text(encoding="utf-8"))
    assert set(by_hand) == set(written)
    assert set(by_hand["sats"][str(KNACKSAT2)]) == set(written["sats"][str(KNACKSAT2)])


async def test_a_data_directory_that_does_not_exist_yet_is_created(
        tmp_path, monkeypatch):
    """First boot against a bind mount that has never been written to. A store
    that raised here would fetch successfully and then throw the answer away —
    and do it again on the next start, and the one after that."""
    answer_with(monkeypatch, ok_tle(group(block("KNACKSAT-2"))))
    s = store(tmp_path / "data" / "gs")

    assert await s.refresh() is True
    assert (tmp_path / "data" / "gs" / "tle_cache.json").exists()


async def test_the_request_names_the_project_and_the_group_it_wants(
        tmp_path, monkeypatch):
    """Celestrak blocks by user agent as well as by IP, and an anonymous poller
    that misbehaves takes every anonymous poller down with it — there is also
    then nobody for them to email. GROUP is what keeps this to one small file
    rather than the whole catalogue, and FORMAT=tle is what `_parse_3le` reads."""
    seen = answer_with(monkeypatch, ok_tle(group(block("KNACKSAT-2"))))

    await store(tmp_path, celestrak_group="amateur").refresh()

    request = to_celestrak(seen)[0]
    assert request.url.scheme == "https"
    assert request.url.path == "/NORAD/elements/gp.php"
    assert request.url.params["GROUP"] == "amateur"
    assert request.url.params["FORMAT"] == "tle"
    assert "knacksat2-ground-station" in request.headers["user-agent"]


# --------------------------------------------------------------------------
# the button on the dashboard, which is the one caller a stranger can press
# --------------------------------------------------------------------------

class FakeRequest:
    def __init__(self, app_state) -> None:
        self.app = type("App", (), {"state": app_state})


def app_state(**attrs):
    return type("State", (), attrs)


async def test_the_refresh_button_refuses_rather_than_forcing_a_fetch(
        tmp_path, monkeypatch):
    """`POST /api/tle/refresh` is reachable from every browser in front of the
    wall display. If it passed `force=True`, holding the button down would
    firewall the station in under a minute, and the elements would not be any
    newer for it — Celestrak answers a repeat with the 403 that gets counted."""
    from fastapi import HTTPException

    write_cache(tmp_path, age_s=60)
    no_network(monkeypatch)
    s = store(tmp_path)

    with pytest.raises(HTTPException) as caught:
        await tle_refresh_endpoint(FakeRequest(app_state(
            tles=s, settings=Settings(data_dir=tmp_path))))

    assert caught.value.status_code == 429
    assert "7200" in caught.value.detail, "the answer does not say how long to wait"


async def test_the_refresh_button_does_fetch_once_the_floor_has_passed(
        tmp_path, monkeypatch):
    """The other half: a button that never does anything gets replaced by
    somebody with a button that always does."""
    write_cache(tmp_path, age_s=7201)
    seen = answer_with(monkeypatch, ok_tle(group(block("KNACKSAT-2"))))
    s = store(tmp_path)

    body = await tle_refresh_endpoint(FakeRequest(app_state(
        tles=s, settings=Settings(data_dir=tmp_path))))

    assert body == {"fetched": True, "count": 1}
    assert len(to_celestrak(seen)) == 1
