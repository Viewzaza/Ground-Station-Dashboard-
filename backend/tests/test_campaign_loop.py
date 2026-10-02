"""Looping a campaign commit until nothing is left to book.

With campaign_loop_until_exhausted on, one commit submits a batch of up to
campaign_max_total, recomputes, and submits again until a round comes back
empty. The properties that matter:

* it actually stops once no bookings are left;
* the per-station cap holds across the whole loop, not per round, so looping
  reaches more stations instead of stacking passes onto the same ones;
* a round where SatNOGS rejects everything ends the loop instead of hammering it;
* with the setting off, a commit is still exactly one batch;
* station calendars are read ONCE per commit: a preview's reads are reused by
  the commit that follows it and by every later round, until they age past
  campaign_calendar_ttl_s or this process books onto those stations. Every
  round re-reading every calendar is what used to spend the whole 240/hour
  observation budget (682 pages for a 4-round commit that needed 175);
* a build cut short by the read limit is reported as such, never as "no
  bookings left";
* a round goes out one transmitter uuid per POST, at most CAMPAIGN_POST_CHUNK
  items each (a mixed POST can fail whole on SatNOGS's 2 s DB lookup; a huge
  one can die mid-save), uncertain items are reported apart and never resent,
  and nothing more is sent once SatNOGS is unreachable;
* the timer does not fire a cycle at startup when its own last cycle ran
  recently - every uvicorn --reload used to fire a full real preview - and
  previews from other triggers (the auto-run chain, the operator) do not move
  it, or every reload would land it on the next day's auto-run slot;
* a cycle refused because another campaign operation holds the guard (a
  minutes-long cross-check, typically) is retried a minute later, not a day.

Nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import inspect
from collections import Counter

import pytest

from app import scheduler as scheduler_module
from app.config import Settings
from app.services import campaign_service as cs
from app.services.campaign_service import CampaignService
from app.vendor.autoscheduler.campaign import build_campaign
from app.vendor.autoscheduler.network_client import Booking, ScheduleResult

MISSION = 67683
TELEMETRY = "UatCXtfDnoBPeVBGHgj4Bc"
DIGIPEATER = "JR28wAEjmpuDQ4FrPWAiwf"


def item(station_id: int, n: int) -> dict:
    start = datetime(2026, 9, 24, tzinfo=timezone.utc) + timedelta(hours=station_id, minutes=n * 20)
    return {
        "station_id": station_id, "station_name": f"S{station_id}",
        "transmitter_uuid": "tx", "start": start.isoformat(),
        "end": (start + timedelta(minutes=8)).isoformat(),
    }


class StubNetwork:
    def __init__(self, reject_all: bool = False):
        self.reject_all = reject_all
        self.batches: list[int] = []
        # Calendar reads, in order, as build_campaign would make them.
        self.reads: list[int] = []
        self.calendar_sources: Counter = Counter()

    def schedule(self, items, execute=False):
        assert execute
        self.batches.append(len(items))
        if self.reject_all:
            return ScheduleResult(submitted=len(items), errors=["x: HTTP 400 no"] * len(items))
        return ScheduleResult(submitted=len(items), accepted=len(items), accepted_items=list(items))

    def future_bookings(self, station_id, now=None):
        self.reads.append(station_id)
        self.calendar_sources["jobs"] += 1
        return []


def make_service(tmp_path, monkeypatch, *, loop: bool, network: StubNetwork,
                 policy: str = "preferred", **settings_overrides):
    settings = Settings(data_dir=tmp_path, mock=False, campaign_mock=False,
                        default_norad=MISSION, **settings_overrides)

    class StubSchedule:
        cache_dir = tmp_path / "cache"

        def _build_autoscheduler_settings(self, hours):
            return SimpleNamespace(network_token="", buffer_s=30.0)

        def _effective_network_token(self):
            return "token"

        def _effective_station_id(self):
            return None

        def campaign_loop_until_exhausted(self):
            return loop

        def campaign_transmitter_policy(self):
            return policy

        def campaign_max_per_station(self):
            return 2

        def campaign_max_total(self):
            return 5

    monkeypatch.setattr(cs, "Cache", lambda *a, **k: None)
    monkeypatch.setattr(cs, "NetworkClient", lambda *a, **k: network)
    monkeypatch.setattr(cs, "DbClient", lambda *a, **k: None)
    return CampaignService(settings, StubSchedule())


def fake_builder(stations: int, per_station: int, max_total: int):
    """Stands in for _build_items: every station has `per_station` passes,
    booked_counts removes the ones already taken, capped at max_total."""
    def build(network, db, auto_settings, booked_counts=None, calendar_cache=None):
        out = []
        for sid in range(1, stations + 1):
            for n in range((booked_counts or {}).get(sid, 0), per_station):
                if len(out) >= max_total:
                    return {"items": out, "stopped_early": None}
                out.append(item(sid, n))
        return {"items": out, "stopped_early": None}
    return build


class FakeBuildCampaign:
    """Stands in for build_campaign itself, honouring the calendar_cache
    contract: a station's calendar comes from the cache when it is there, is
    otherwise read with network.future_bookings() and stored into the cache.

    Every station has two passes; booked_counts takes back the ones already
    given, max_total caps a build. Records the kwargs of every call.
    """

    def __init__(self, stations: int = 4, max_total: int = 3):
        self.stations = stations
        self.max_total = max_total
        self.calls: list[dict] = []

    def __call__(self, network, db, **kwargs):
        self.calls.append(kwargs)
        cache = kwargs.get("calendar_cache")
        booked = kwargs.get("booked_counts") or {}
        occupied = kwargs.get("recent_attempts") or {}
        out = []
        for sid in range(1, self.stations + 1):
            if cache is not None and sid in cache:
                pass
            else:
                bookings = network.future_bookings(sid, now=kwargs["now"])
                if cache is not None:
                    cache[sid] = list(bookings)
            for n in range(booked.get(sid, 0), 2):
                row = item(sid, n)
                taken = any(datetime.fromisoformat(row["start"]) == start
                            for start, _end in occupied.get(sid, []))
                if taken or len(out) >= self.max_total:
                    continue
                out.append(row)
        return {"status": "ok", "items": out, "stopped_early": None,
                "generated_utc": kwargs["now"].isoformat()}


@pytest.fixture
def fake_build_campaign(monkeypatch):
    fake = FakeBuildCampaign()
    monkeypatch.setattr(cs, "build_campaign", fake)
    # The fake already returns the payload shape.
    monkeypatch.setattr(cs, "_campaign_preview_payload", lambda payload: dict(payload))
    return fake


def test_loop_keeps_submitting_until_nothing_is_left(tmp_path, monkeypatch):
    network = StubNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=True, network=network)
    monkeypatch.setattr(svc, "_build_items", fake_builder(stations=7, per_station=2, max_total=5))

    result = svc._commit_sync(None, "manual")

    assert network.batches == [5, 5, 4]
    assert result["accepted"] == 14 == len(result["accepted_items"])
    assert result["rounds"] == 3
    assert result["stopped_reason"] == "no bookings left"
    # Every accepted booking is also in the recent-attempts guard, so a later
    # preview cannot resubmit any of them.
    assert sum(len(v) for v in svc._recent_bookings_by_station(datetime.now(timezone.utc)).values()) == 14


def test_loop_off_is_a_single_batch(tmp_path, monkeypatch):
    network = StubNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=False, network=network)
    monkeypatch.setattr(svc, "_build_items", fake_builder(stations=7, per_station=2, max_total=5))

    result = svc._commit_sync(None, "manual")

    assert network.batches == [5]
    assert result["rounds"] == 1


def test_loop_stops_when_a_whole_round_is_rejected(tmp_path, monkeypatch):
    network = StubNetwork(reject_all=True)
    svc = make_service(tmp_path, monkeypatch, loop=True, network=network)
    monkeypatch.setattr(svc, "_build_items", fake_builder(stations=7, per_station=2, max_total=5))

    result = svc._commit_sync(None, "manual")

    assert network.batches == [5]
    assert result["accepted"] == 0
    assert "rejected" in result["stopped_reason"]


def test_loop_has_a_round_limit(tmp_path, monkeypatch):
    network = StubNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=True, network=network)
    # A builder that never runs dry - only the backstop can end this.
    monkeypatch.setattr(svc, "_build_items", lambda *a, **k: {"items": [item(1, 0)]})

    result = svc._commit_sync(None, "manual")

    assert result["rounds"] == cs.MAX_LOOP_ROUNDS
    assert "safety limit" in result["stopped_reason"]


def test_previewed_items_are_the_first_round(tmp_path, monkeypatch):
    network = StubNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=True, network=network)
    seen: list[dict] = []

    def build(network, db, auto_settings, booked_counts=None, calendar_cache=None):
        seen.append(dict(booked_counts or {}))
        return {"items": []}
    monkeypatch.setattr(svc, "_build_items", build)

    result = svc._commit_sync([item(3, 0), item(3, 1), item(4, 0)], "manual")

    assert network.batches == [3]
    # The follow-up round was told what the reviewed batch already gave each station.
    assert seen == [{3: 2, 4: 1}]
    assert result["stopped_reason"] == "no bookings left"


def test_build_campaign_skips_stations_already_at_their_cap():
    stations = [SimpleNamespace(id=1, name="full", segments=[]),
                SimpleNamespace(id=2, name="other", segments=[])]
    touched: list[int] = []

    class Net:
        def all_stations(self):
            return stations

    class Db:
        def tles(self):
            return {MISSION: object()}

        def transmitters_for_station(self, segments):
            touched.append(id(segments))
            return {}

    preview = build_campaign(
        Net(), Db(), mission_norad=MISSION, transmitter_uuid=None,
        now=datetime.now(timezone.utc), exclude_station_id=None,
        max_per_station=2, max_total=150, booked_counts={1: 2},
    )

    reasons = {s["station_id"]: s["reason"] for s in preview.skipped}
    assert "per-station cap" in reasons[1]
    # Station 1 was dropped before any further lookups; only station 2 got that far.
    assert touched == [id(stations[1].segments)]


# --- the calendar cache ---------------------------------------------------------

def test_a_preview_and_every_round_of_the_commit_after_it_read_each_calendar_once(
        tmp_path, monkeypatch, fake_build_campaign):
    """The whole point of the cache. The preview reads the four calendars; the
    commit it feeds submits what was reviewed, then keeps looping - and none
    of those rounds reads a calendar again."""
    network = StubNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=True, network=network)

    preview = svc._preview_sync()
    assert network.reads == [1, 2, 3, 4]

    result = svc._commit_sync(preview["items"], "manual")

    assert result["rounds"] >= 3, result
    assert result["accepted"] == 8, "every station's two passes, over several rounds"
    assert network.reads == [1, 2, 3, 4], (
        f"rounds 2+ must plan from the calendars the preview read, got reads {network.reads}"
    )
    # Every round after the first handed build_campaign the same cache dict.
    commit_caches = {id(call["calendar_cache"]) for call in fake_build_campaign.calls[1:]}
    assert len(commit_caches) == 1
    assert commit_caches == {id(fake_build_campaign.calls[0]["calendar_cache"])}


def test_a_commit_that_builds_its_own_first_round_reads_once_too(
        tmp_path, monkeypatch, fake_build_campaign):
    network = StubNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=True, network=network)

    result = svc._commit_sync(None, "auto")

    assert result["accepted"] == 8
    assert network.reads == [1, 2, 3, 4]
    assert result["calendar_sources"] == {"jobs": 4}, (
        "the commit reports which feed its reads came from"
    )


def test_calendars_older_than_the_ttl_are_read_again(tmp_path, monkeypatch, fake_build_campaign):
    network = StubNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=False, network=network,
                       campaign_calendar_ttl_s=900)
    svc._preview_sync()
    assert network.reads == [1, 2, 3, 4]

    # Within the TTL a second preview is free...
    svc._preview_sync()
    assert network.reads == [1, 2, 3, 4]

    # ...but once the reads are older than the TTL they are not trusted.
    svc._calendar_cache_at -= timedelta(seconds=901)
    svc._preview_sync()
    assert network.reads == [1, 2, 3, 4, 1, 2, 3, 4]


def test_the_cache_is_dropped_once_this_process_books_onto_those_stations(
        tmp_path, monkeypatch, fake_build_campaign):
    """cap_counts_existing only counts what is ON a calendar. Keep the
    pre-booking calendars and a second click inside the TTL would see none of
    the first click's bookings and stack another max_per_station on top."""
    network = StubNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=False, network=network)
    preview = svc._preview_sync()

    svc._commit_sync(preview["items"], "manual")
    assert svc._calendar_cache == {}

    svc._preview_sync()
    assert network.reads == [1, 2, 3, 4, 1, 2, 3, 4], "the next preview reads fresh calendars"


def test_calendar_cache_expiry_rules(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch, loop=False, network=StubNetwork(),
                       campaign_calendar_ttl_s=900)
    t0 = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)

    first = svc._calendar_cache_for(t0)
    first[1] = ["cached"]
    assert svc._calendar_cache_for(t0 + timedelta(seconds=899)) is first
    assert svc._calendar_cache_for(t0 + timedelta(seconds=900)) is not first, "expired at the TTL"

    second = svc._calendar_cache_for(t0)
    second[1] = ["cached"]
    assert svc._calendar_cache_for(t0 - timedelta(seconds=1)) is not second, (
        "a clock stepped backwards cannot vouch for the cache's age"
    )


def test_a_zero_ttl_still_shares_calendars_between_the_rounds_of_one_commit(
        tmp_path, monkeypatch, fake_build_campaign):
    network = StubNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=True, network=network,
                       campaign_calendar_ttl_s=0)
    preview = svc._preview_sync()

    result = svc._commit_sync(preview["items"], "manual")

    assert result["rounds"] >= 3
    # One set of reads for the preview, one for the commit's first rebuild -
    # and nothing more, however many rounds follow.
    assert network.reads == [1, 2, 3, 4, 1, 2, 3, 4]


# --- builds cut short --------------------------------------------------------------

def test_a_round_cut_short_by_the_read_limit_says_so(tmp_path, monkeypatch):
    """A truncated rebuild used to end the run as "no bookings left", which
    reads as "the network is full" when it only means "we stopped looking"."""
    network = StubNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=True, network=network)
    builds = iter([
        {"items": [item(5, 0)], "stopped_early": {
            "reason": "SatNOGS is rate-limiting reads (next slot in 900s)",
            "unread_stations": 37}},
        {"items": [], "stopped_early": {
            "reason": "SatNOGS is rate-limiting reads (next slot in 880s)",
            "unread_stations": 36}},
    ])
    monkeypatch.setattr(svc, "_build_items", lambda *a, **k: next(builds))

    result = svc._commit_sync([item(1, 0)], "manual")

    assert network.batches == [1, 1]
    reason = result["stopped_reason"]
    assert not reason.startswith("no bookings left"), reason
    assert reason.startswith("nothing more could be planned"), reason
    assert "round 2's plan was cut short" in reason and "37 station(s) not read" in reason
    assert "round 3's plan was cut short" in reason and "36 station(s) not read" in reason


def test_a_first_build_cut_short_is_surfaced_with_looping_off(tmp_path, monkeypatch):
    network = StubNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=False, network=network)
    monkeypatch.setattr(svc, "_build_items", lambda *a, **k: {
        "items": [item(1, 0)],
        "stopped_early": {"reason": "SatNOGS is rate-limiting reads", "unread_stations": 12}})

    result = svc._commit_sync(None, "manual")

    assert result["stopped_reason"] == (
        "one batch per commit (looping is off); round 1's plan was cut short "
        "(SatNOGS is rate-limiting reads; 12 station(s) not read)"
    )


def test_nothing_to_submit_is_not_called_a_rejection(tmp_path, monkeypatch):
    network = StubNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=True, network=network)
    monkeypatch.setattr(svc, "_build_items", lambda *a, **k: {"items": []})

    result = svc._commit_sync(None, "manual")

    assert network.batches == []
    assert result["stopped_reason"] == "no bookings to submit"


# --- the service hands build_campaign what the contract says ------------------------

def test_the_build_kwargs_are_ones_build_campaign_accepts(tmp_path, monkeypatch):
    """Bound against the real signature, so a renamed or missing parameter on
    either side fails here rather than as a TypeError in the first live preview."""
    svc = make_service(tmp_path, monkeypatch, loop=False, network=StubNetwork())
    kwargs = svc._build_kwargs(datetime.now(timezone.utc), SimpleNamespace(buffer_s=30.0), {})

    inspect.signature(build_campaign).bind(object(), object(), booked_counts=None, **kwargs)
    assert kwargs["cap_counts_existing"] is True


@pytest.mark.parametrize("policy, expected", [
    ("pinned", (TELEMETRY, [])),
    ("preferred", (TELEMETRY, [DIGIPEATER])),
    ("any", (None, [])),
])
def test_the_transmitter_policy_maps_onto_build_campaign(tmp_path, monkeypatch,
                                                          fake_build_campaign, policy, expected):
    svc = make_service(tmp_path, monkeypatch, loop=False, network=StubNetwork(), policy=policy)

    svc._preview_sync()

    (call,) = fake_build_campaign.calls
    assert (call["transmitter_uuid"], call["fallback_transmitter_uuids"]) == expected
    assert call["calendar_cache"] is svc._calendar_cache
    assert call["cap_counts_existing"] is True


def test_an_unset_primary_means_the_automatic_pick_whatever_the_policy(
        tmp_path, monkeypatch, fake_build_campaign):
    svc = make_service(tmp_path, monkeypatch, loop=False, network=StubNetwork(),
                       policy="preferred", campaign_transmitter_uuid=None)

    svc._preview_sync()

    (call,) = fake_build_campaign.calls
    assert (call["transmitter_uuid"], call["fallback_transmitter_uuids"]) == (None, [])


def test_the_fallback_list_never_repeats_the_primary(tmp_path, monkeypatch, fake_build_campaign):
    svc = make_service(tmp_path, monkeypatch, loop=False, network=StubNetwork(),
                       campaign_fallback_transmitter_uuids=f"{TELEMETRY}, {DIGIPEATER},,{DIGIPEATER}")

    svc._preview_sync()

    assert fake_build_campaign.calls[0]["fallback_transmitter_uuids"] == [DIGIPEATER]


def test_the_preview_reports_where_its_calendars_came_from(tmp_path, monkeypatch,
                                                           fake_build_campaign):
    network = StubNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=False, network=network)

    preview = svc._preview_sync()

    assert preview["calendar_sources"] == {"jobs": 4}


# --- the timer after a restart ---------------------------------------------------------

def _write_preview(svc, generated_utc):
    svc._write_json(svc.preview_path, {"status": "ok", "generated_utc": generated_utc, "items": []})


def test_the_first_cycle_waits_out_what_is_left_of_the_poll_period(tmp_path, monkeypatch):
    # No record of the timer's own cycle here (a data dir from before it was
    # kept), so the last preview stands in for it - the original rule.
    svc = make_service(tmp_path, monkeypatch, loop=False, network=StubNetwork(),
                       campaign_poll_s=86400)
    now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)

    assert svc.auto_cycle_delay_s(now) == 0.0, "never previewed: run now"

    _write_preview(svc, (now - timedelta(hours=1)).isoformat())
    assert svc.auto_cycle_delay_s(now) == pytest.approx(23 * 3600)

    _write_preview(svc, (now - timedelta(hours=25)).isoformat())
    assert svc.auto_cycle_delay_s(now) == 0.0, "overdue: run now"

    _write_preview(svc, (now + timedelta(days=3)).isoformat())
    assert svc.auto_cycle_delay_s(now) == 86400, "a future stamp delays at most one period"

    _write_preview(svc, "not a timestamp")
    assert svc.auto_cycle_delay_s(now) == 0.0

    # A preview that errored still went out to SatNOGS; it counts.
    svc._write_json(svc.preview_path, {"status": "error", "error": "x",
                                       "generated_utc": (now - timedelta(hours=2)).isoformat()})
    assert svc.auto_cycle_delay_s(now) == pytest.approx(22 * 3600)


def test_a_chained_preview_does_not_move_the_timer_onto_the_auto_run_slot(tmp_path,
                                                                          monkeypatch):
    """With the chain on, the last preview is usually the chain's: started at
    a Station Schedule slot plus that day's own run (23:02:10Z here). Anchored
    on it, a reload at 04:00Z put the timer's next cycle at 23:02:10Z - inside
    the next slot, where whichever previewed first made the other answer
    "running", and with auto-commit off the chain lost that slot outright."""
    svc = make_service(tmp_path, monkeypatch, loop=False, network=StubNetwork(),
                       campaign_poll_s=86400)
    timer_cycle = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)
    svc._write_json(tmp_path / "campaign_last_auto_cycle.json",
                    {"generated_utc": timer_cycle.isoformat()})
    _write_preview(svc, datetime(2026, 10, 2, 23, 2, 10, tzinfo=timezone.utc).isoformat())
    reload_at = datetime(2026, 10, 3, 4, 0, tzinfo=timezone.utc)

    first_cycle = reload_at + timedelta(seconds=svc.auto_cycle_delay_s(reload_at))

    assert first_cycle == datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc), (
        "the timer keeps its own phase, a day after its own last cycle")


def _stamped_previews(svc, monkeypatch, stamps):
    """Each preview is built at the next of `stamps`."""
    stamps = iter(stamps)
    monkeypatch.setattr(svc, "_preview_sync", lambda: {
        "status": "ok", "generated_utc": next(stamps).isoformat(), "items": [item(1, 0)]})

    async def commit(**kwargs):
        return {"status": "ok"}
    monkeypatch.setattr(svc, "commit_campaign", commit)


async def test_the_timer_records_its_own_cycle_and_nothing_else_does(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch, loop=False, network=StubNetwork())
    svc.schedule_service.campaign_auto_commit_enabled = lambda: False
    t = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)
    _stamped_previews(svc, monkeypatch, [t + timedelta(hours=h) for h in range(4)])

    await svc.preview_campaign()       # the operator's PREVIEW, 06:00
    assert not svc.auto_cycle_path.exists()

    svc._running = True                # a refused cycle did not happen
    assert await svc.run_auto_cycle() == cs.AUTO_CYCLE_BUSY
    assert not svc.auto_cycle_path.exists()
    svc._running = False

    timer_cycle = {"generated_utc": "2026-10-02T07:00:00+00:00"}
    assert await svc.run_auto_cycle() == "done"           # the timer, 07:00
    assert svc._read_json(svc.auto_cycle_path, None) == timer_cycle

    await svc.run_chained_cycle()                          # a chain slot, 08:00
    await svc.preview_campaign()
    assert svc._read_json(svc.auto_cycle_path, None) == timer_cycle


async def test_before_the_timer_s_first_cycle_a_chain_slot_cannot_become_its_anchor(
        tmp_path, monkeypatch):
    """A data dir from before the timer kept its own record: the last preview
    is the fallback. A chain slot overwriting that preview, then a reload,
    must not hand the timer the slot's phase - which its own record would
    then carry forward every day."""
    svc = make_service(tmp_path, monkeypatch, loop=False, network=StubNetwork(),
                       campaign_poll_s=86400)
    _write_preview(svc, datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc).isoformat())
    _stamped_previews(svc, monkeypatch, [datetime(2026, 10, 2, 23, 2, 10, tzinfo=timezone.utc)])

    await svc.run_chained_cycle()
    assert svc.get_last_preview()["generated_utc"] == "2026-10-02T23:02:10+00:00"
    reload_at = datetime(2026, 10, 3, 4, 0, tzinfo=timezone.utc)

    first_cycle = reload_at + timedelta(seconds=svc.auto_cycle_delay_s(reload_at))

    assert first_cycle == datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)


class _Stop(Exception):
    pass


async def test_a_cross_check_in_flight_delays_the_timer_a_minute_not_a_day(tmp_path,
                                                                         monkeypatch):
    """The cross-check shares the single-flight guard and reads calendars
    serially (~6 min for ~220 stations). A timer cycle that came due during
    one used to answer "running", and the loop slept campaign_poll_s - the
    day's automatic cycle lost even with auto-commit on."""
    reading, release = threading.Event(), threading.Event()

    class SlowNetwork(StubNetwork):
        def future_bookings(self, station_id, now=None):
            reading.set()
            release.wait(5)        # a /jobs/ read in progress
            return []

    svc = make_service(tmp_path, monkeypatch, loop=False, network=SlowNetwork())
    svc.schedule_service.campaign_auto_commit_enabled = lambda: True
    soon = datetime.now(timezone.utc) + timedelta(hours=5)
    svc._write_json(svc.result_path, {"status": "ok", "accepted_items": [{
        "station_id": 40, "transmitter_uuid": "tx", "start": soon.isoformat(),
        "end": (soon + timedelta(minutes=8)).isoformat()}]})
    monkeypatch.setattr(svc, "_preview_sync", lambda: {
        "status": "ok", "generated_utc": datetime.now(timezone.utc).isoformat(),
        "items": [item(1, 0)]})
    commits: list[dict] = []

    async def commit(**kwargs):
        commits.append(kwargs)
        return {"status": "ok"}
    monkeypatch.setattr(svc, "commit_campaign", commit)

    verify = asyncio.create_task(svc.verify_last_run())
    while not reading.is_set():
        await asyncio.sleep(0.01)

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1 and seconds == 60.0:
            release.set()          # the cross-check finishes during the retry wait
            await verify
            return
        raise _Stop

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fake_sleep)
    owner = scheduler_module.Scheduler.__new__(scheduler_module.Scheduler)
    owner.campaign_service = svc
    owner.s = svc.s
    try:
        with pytest.raises(_Stop):
            await owner._campaign_loop()
    finally:
        release.set()
        await verify

    assert sleeps == [60.0, 86400]
    assert [c["trigger"] for c in commits] == ["auto"], "the day's cycle still committed"


async def test_a_guard_that_never_frees_costs_one_cycle_not_a_retry_loop(monkeypatch):
    events: list = []

    class BusyCampaign:
        def auto_cycle_delay_s(self):
            return 0.0

        async def run_auto_cycle(self):
            events.append("cycle")
            return cs.AUTO_CYCLE_BUSY

    async def fake_sleep(seconds):
        events.append(("sleep", seconds))
        if seconds == 86400:
            raise _Stop

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fake_sleep)
    owner = scheduler_module.Scheduler.__new__(scheduler_module.Scheduler)
    owner.campaign_service = BusyCampaign()
    owner.s = SimpleNamespace(campaign_poll_s=86400)
    with pytest.raises(_Stop):
        await owner._campaign_loop()

    assert scheduler_module.Scheduler._CAMPAIGN_BUSY_MAX_RETRIES == 30
    assert events == ["cycle", ("sleep", 60.0)] * 30 + ["cycle", ("sleep", 86400)]


async def _run_campaign_loop(monkeypatch, delay_s: float) -> list:
    events: list = []

    class StubCampaign:
        def auto_cycle_delay_s(self):
            return delay_s

        async def run_auto_cycle(self):
            events.append("cycle")

    async def fake_sleep(seconds):
        events.append(("sleep", seconds))
        if "cycle" in events:
            raise _Stop

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fake_sleep)
    owner = scheduler_module.Scheduler.__new__(scheduler_module.Scheduler)
    owner.campaign_service = StubCampaign()
    owner.s = SimpleNamespace(campaign_poll_s=86400)
    with pytest.raises(_Stop):
        await owner._campaign_loop()
    return events


async def test_a_restart_soon_after_a_preview_does_not_fire_a_cycle(monkeypatch):
    """Every edit under backend/app restarts the live backend (uvicorn
    --reload), and the loop used to open with a full real preview."""
    events = await _run_campaign_loop(monkeypatch, delay_s=3600.0)

    assert events == [("sleep", 3600.0), "cycle", ("sleep", 86400)]


async def test_with_no_recent_preview_the_timer_still_starts_at_once(monkeypatch):
    events = await _run_campaign_loop(monkeypatch, delay_s=0.0)

    assert events == ["cycle", ("sleep", 86400)]


# --- POSTs: one transmitter each, at most CAMPAIGN_POST_CHUNK items -------------------

class RecordingNetwork(StubNetwork):
    """Keeps every POST body, and answers each through `answer(items)` - by
    default "all accepted"."""

    def __init__(self, answer=None):
        super().__init__()
        self.posts: list[list[dict]] = []
        self.answer = answer

    def schedule(self, items, execute=False):
        assert execute
        self.posts.append(list(items))
        self.batches.append(len(items))
        if self.answer is not None:
            return self.answer(list(items))
        return ScheduleResult(submitted=len(items), accepted=len(items),
                              accepted_items=list(items))


def tx_item(station_id: int, uuid: str, n: int = 0) -> dict:
    row = item(station_id, n)
    row["transmitter_uuid"] = uuid
    return row


def test_a_round_is_posted_one_transmitter_at_a_time_in_chunks_of_at_most_50(
        tmp_path, monkeypatch):
    """A POST mixing two uuids makes SatNOGS fetch the whole DB transmitter
    list on a 2 s timeout, which fails the entire batch; and a POST big enough
    to outlast gunicorn's 30 s timeout dies mid-save with an unknown outcome."""
    network = RecordingNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=False, network=network)
    # Interleaved, and the digipeater first, as a preview sorted by start would be.
    items = [tx_item(sid, DIGIPEATER if sid % 2 else TELEMETRY) for sid in range(1, 131)]

    result = svc._commit_sync(items, "manual")

    assert [len({row["transmitter_uuid"] for row in post}) for post in network.posts] == [1] * 4
    assert [(post[0]["transmitter_uuid"], len(post)) for post in network.posts] == [
        (TELEMETRY, 50), (TELEMETRY, 15), (DIGIPEATER, 50), (DIGIPEATER, 15),
    ], "primary first, then the fallback, each split at CAMPAIGN_POST_CHUNK"
    assert cs.CAMPAIGN_POST_CHUNK == 50
    assert sorted(p["ground_station"] for post in network.posts for p in post) == list(range(1, 131))
    assert result["submitted"] == 130 and result["accepted"] == 130
    assert [(t["uuid"], t["bookings"]) for t in result["accepted_by_transmitter"]] == [
        (TELEMETRY, 65), (DIGIPEATER, 65)]


def test_uncertain_items_are_surfaced_never_resubmitted_and_count_against_the_cap(
        tmp_path, monkeypatch):
    """No reliable answer means possibly booked: reported apart from accepted,
    kept out of every later round, and counted towards the station's cap."""
    def answer(posted):
        sure = [p for p in posted if p["ground_station"] != 2]
        unsure = [p for p in posted if p["ground_station"] == 2]
        return ScheduleResult(submitted=len(posted), accepted=len(sure), accepted_items=sure,
                              uncertain_items=unsure,
                              errors=["OUTCOME UNKNOWN for station 2. Do NOT resubmit"])
    network = RecordingNetwork(answer)
    svc = make_service(tmp_path, monkeypatch, loop=True, network=network)
    seen: list[dict] = []

    def build(network, db, auto_settings, booked_counts=None, calendar_cache=None):
        seen.append(dict(booked_counts or {}))
        return {"items": []}
    monkeypatch.setattr(svc, "_build_items", build)

    result = svc._commit_sync([item(1, 0), item(2, 0)], "manual")

    assert [r["station_id"] for r in result["uncertain_items"]] == [2]
    assert result["uncertain_items"][0]["start"] == item(2, 0)["start"]
    assert [r["station_id"] for r in result["accepted_items"]] == [1]
    assert seen == [{1: 1, 2: 1}], "the uncertain booking uses up station 2's allowance"
    # And its window stays occupied for later previews, so it is never resent.
    recent = svc._recent_bookings_by_station(datetime.now(timezone.utc))
    assert datetime.fromisoformat(item(2, 0)["start"]) in [s for s, _e in recent[2]]

    svc._append_history(result)
    assert svc.get_history()[-1]["uncertain"] == 1
    assert svc.get_history()[-1]["rejected"] == 0, "possibly booked is not rejected"


def test_no_more_posts_once_satnogs_is_unreachable(tmp_path, monkeypatch):
    network = RecordingNetwork(lambda posted: ScheduleResult(
        submitted=len(posted),
        errors=[f"could not reach SatNOGS to submit {len(posted)} item(s): boom. "
                "Nothing was booked."]))
    svc = make_service(tmp_path, monkeypatch, loop=True, network=network)
    items = [tx_item(1, "tx-a"), tx_item(2, "tx-b"), tx_item(3, "tx-b"), tx_item(4, "tx-c")]

    result = svc._commit_sync(items, "manual")

    assert len(network.posts) == 1
    assert result["not_sent"] == 3
    assert any("3 more item(s) in 2 later POST(s) were not sent" in e for e in result["errors"])
    assert result["stopped_reason"] == "round 1 lost its connection to SatNOGS"
    recent = svc._recent_bookings_by_station(datetime.now(timezone.utc))
    assert set(recent) == {1}, "only what was actually sent is held back from the next run"
