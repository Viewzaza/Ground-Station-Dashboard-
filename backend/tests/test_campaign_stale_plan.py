"""A previewed plan is not submitted once something else has booked since.

A commit of previewed items sends round 1 exactly as previewed: nothing
re-plans it, so nothing re-checks it against the per-station cap. The
auto-run chain made that a routine hazard. The operator presses PREVIEW at
10:58Z and reads the plan; the 11:00Z slot previews afresh (another shuffle,
so other passes on the same stations) and books that; at 11:03Z CONFIRM lands
the older plan on top - twice max_per_station on those stations, with no
overlap for SatNOGS to refuse. Measured in review with a cap of 1: two per
station.

The preview's stamp (preview_generated_utc) now goes with the commit, and a
plan this process has POSTed over since is answered "stale": nothing sent, no
run record, no history row - the route answers it at once, since a panel
polling for a run record would otherwise wait out its whole timeout.

Nothing here touches the network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.routes import schedule as schedule_routes
from app.services import campaign_service as cs
from app.services.campaign_service import CampaignService
from app.vendor.autoscheduler import campaign as camp
from app.vendor.autoscheduler.db_client import Tle, Transmitter
from app.vendor.autoscheduler.network_client import Booking, ScheduleResult, Station
from app.vendor.autoscheduler.predictor import Pass

MISSION = 67683
TELEMETRY = "UatCXtfDnoBPeVBGHgj4Bc"
TLE1 = "1 67683U 26001A   26256.50000000  .00010000  00000-0  50000-3 0  9990"
TLE2 = "2 67683  97.4000 100.0000 0010000  90.0000 270.0000 15.20000000 10000"
ONLINE = {"id": 5024, "status": "Online", "last_seen": "2026-10-02T10:57:00Z", "age_s": 10.0}


def stub_schedule(tmp_path, *, cap: int = 1):
    class StubSchedule:
        cache_dir = tmp_path / "cache"

        def _build_autoscheduler_settings(self, hours):
            return SimpleNamespace(network_token="", buffer_s=30.0)

        def _effective_network_token(self):
            return "token"

        def _effective_station_id(self):
            return 5024

        def campaign_loop_until_exhausted(self):
            return False

        def campaign_transmitter_policy(self):
            return "pinned"

        def campaign_max_per_station(self):
            return cap

        def campaign_max_total(self):
            return 600

        def campaign_auto_commit_enabled(self):
            # The chain is its own consent; nothing here relies on this.
            return False

    return StubSchedule()


# --- the incident, end to end: real build_campaign, zero calendar lag ----------------------

STATIONS = [101, 102]


class Db:
    def __init__(self, *a, **k):
        pass

    def tles(self):
        return {MISSION: Tle(MISSION, "KNACKSAT-2", TLE1, TLE2, "", "frozen")}

    def transmitters_for_station(self, segments):
        return {MISSION: [Transmitter(uuid=TELEMETRY, norad_cat_id=MISSION, description="TLM",
                                      mode="FSK", baud=9600.0, downlink_hz=400_630_000,
                                      type="Transmitter", status="active", service="Amateur")]}


class CalendarServer:
    """Station calendars plus SatNOGS's own overlap rule (409). A booking is
    on the calendar the moment it is accepted - no read lag at all, so
    nothing but the plan itself can push a station past its cap."""

    def __init__(self):
        self.cal: dict[int, list[tuple[datetime, datetime]]] = {s: [] for s in STATIONS}
        self.posts: list[list[int]] = []

    def client(self, *a, **k):
        server = self

        class Client:
            calendar_sources: dict = {}

            def all_stations(self):
                return [Station(id=s, name=f"S{s}", lat=float(s), lng=100.0, altitude_m=10.0,
                                min_horizon=0.0, min_culmination=0.0, status="Online",
                                is_connected=True) for s in STATIONS]

            def future_bookings(self, sid, now=None):
                return [Booking(id=i, norad_cat_id=MISSION, start=s, end=e, status="future")
                        for i, (s, e) in enumerate(server.cal[sid])]

            def schedule(self, items, execute=False):
                assert execute
                server.posts.append([it["ground_station"] for it in items])
                res = ScheduleResult(submitted=len(items))
                for it in items:
                    s = datetime.strptime(it["start"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    e = datetime.strptime(it["end"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    if any(s <= oe and e >= os_ for os_, oe in server.cal[it["ground_station"]]):
                        res.errors.append(f"station {it['ground_station']}: HTTP 409 overlap")
                        continue
                    server.cal[it["ground_station"]].append((s, e))
                    res.accepted += 1
                    res.accepted_items.append(it)
                return res
        return Client()


@pytest.fixture
def two_previews_pick_differently(monkeypatch):
    """Two good passes per station, and every second preview visits the
    stations in the other order - as two previews seeded from different
    seconds do in production - so band balancing picks the other pass."""
    t0 = datetime.now(timezone.utc).replace(microsecond=0)

    class P:
        def __init__(self, lat, lng, alt):
            pass

        def load_tles(self, tles, now=None):
            return {}

        def passes_for(self, norad, start, end, min_horizon):
            a, b = t0 + timedelta(hours=2), t0 + timedelta(hours=8)
            return [Pass(MISSION, "K2", a, a, a + timedelta(minutes=9), 70.0, 0.0, 180.0),
                    Pass(MISSION, "K2", b, b, b + timedelta(minutes=9), 52.0, 0.0, 180.0)]
    monkeypatch.setattr(camp, "Predictor", P)

    calls = {"n": 0}

    class Rand:
        def __init__(self, seed):
            calls["n"] += 1
            self.flip = calls["n"] % 2 == 0

        def shuffle(self, seq):
            if self.flip:
                seq.reverse()
    monkeypatch.setattr(camp, "random", SimpleNamespace(Random=Rand))


def real_planner_service(tmp_path, monkeypatch, server):
    monkeypatch.setattr(cs, "Cache", lambda *a, **k: None)
    monkeypatch.setattr(cs, "NetworkClient", server.client)
    monkeypatch.setattr(cs, "DbClient", Db)
    settings = Settings(data_dir=tmp_path, mock=False, campaign_mock=False, default_norad=MISSION)
    return CampaignService(settings, stub_schedule(tmp_path, cap=1), own_station=lambda: ONLINE)


async def test_a_confirm_after_the_chain_booked_sends_nothing_and_the_cap_holds(
        tmp_path, monkeypatch, two_previews_pick_differently):
    server = CalendarServer()
    svc = real_planner_service(tmp_path, monkeypatch, server)

    p1 = await svc.preview_campaign()                 # 10:58Z - the operator reads this
    chained = await svc.run_chained_cycle()           # 11:00Z - the slot books its own plan
    assert chained["status"] == "ok" and chained["accepted"] == 2
    # The hazard is real: the two plans hold different passes on the same
    # stations, so SatNOGS would take both (no overlap) at a cap of 1.
    plan1 = {(i["station_id"], i["start"]) for i in p1["items"]}
    plan2 = {(i["station_id"], i["start"]) for i in chained["accepted_items"]}
    assert plan1.isdisjoint(plan2)
    assert {sid for sid, _ in plan1} == {sid for sid, _ in plan2} == set(STATIONS)
    posts = list(server.posts)

    late = await svc.commit_campaign(items=p1["items"], trigger="manual",   # 11:03Z - CONFIRM
                                     preview_generated_utc=p1["generated_utc"])

    assert late["status"] == "stale"
    assert server.posts == posts, "nothing was sent"
    assert {sid: len(server.cal[sid]) for sid in STATIONS} == {101: 1, 102: 1}, "cap 1 holds"
    assert "the Station Schedule auto-run chain has booked since" in late["stopped_reason"]
    assert late["stopped_reason"].endswith("run PREVIEW again")
    assert (late["submitted"], late["accepted"], late["errors"]) == (0, 0, [])


async def test_a_confirm_with_nothing_booked_in_between_still_submits(
        tmp_path, monkeypatch, two_previews_pick_differently):
    server = CalendarServer()
    svc = real_planner_service(tmp_path, monkeypatch, server)

    p1 = await svc.preview_campaign()
    result = await svc.commit_campaign(items=p1["items"], trigger="manual",
                                       preview_generated_utc=p1["generated_utc"])

    assert result["status"] == "ok" and result["accepted"] == 2


# --- the rule, case by case ------------------------------------------------------------------

def item(station_id: int, n: int = 0) -> dict:
    start = datetime.now(timezone.utc) + timedelta(hours=2, minutes=20 * n + station_id % 50)
    return {
        "station_id": station_id, "station_name": f"S{station_id}",
        "transmitter_uuid": TELEMETRY, "start": start.isoformat(),
        "end": (start + timedelta(minutes=8)).isoformat(), "max_elevation_deg": 40.0,
    }


class Network:
    def __init__(self, fail: Exception | None = None):
        self.posts: list[list[dict]] = []
        self.fail = fail
        self.calendar_sources: dict = {}

    def schedule(self, items, execute=False):
        assert execute
        self.posts.append(list(items))
        if self.fail is not None:
            raise self.fail
        return ScheduleResult(submitted=len(items), accepted=len(items), accepted_items=list(items))

    def future_bookings(self, station_id, now=None):
        return []


def stub_service(tmp_path, monkeypatch, network, *, own_station=lambda: ONLINE):
    monkeypatch.setattr(cs, "Cache", lambda *a, **k: None)
    monkeypatch.setattr(cs, "NetworkClient", lambda *a, **k: network)
    monkeypatch.setattr(cs, "DbClient", lambda *a, **k: None)
    settings = Settings(data_dir=tmp_path, mock=False, campaign_mock=False, default_norad=MISSION)
    states: list = []
    svc = CampaignService(settings, stub_schedule(tmp_path, cap=3),
                          on_state=lambda c, s, d="": states.append((s, d)),
                          own_station=own_station)
    svc.test_states = states
    return svc


def preview_returns(svc, monkeypatch, items):
    """Every preview plans `items`, stamped when it is built - as a real one is."""
    monkeypatch.setattr(svc, "_preview_sync", lambda: {
        "status": "ok", "generated_utc": datetime.now(timezone.utc).isoformat(),
        "items": [dict(i) for i in items]})


async def operator_preview_then_chain(svc, monkeypatch):
    preview_returns(svc, monkeypatch, [item(40)])
    p1 = await svc.preview_campaign()
    preview_returns(svc, monkeypatch, [item(41)])
    chained = await svc.run_chained_cycle()
    assert chained["status"] == "ok"
    return p1


async def test_a_stale_answer_leaves_the_run_record_history_and_attempts_alone(tmp_path,
                                                                                 monkeypatch):
    """The last run on disk is the commit that made the plan stale - and the
    one the cross-check reads back - so a refusal must not replace it."""
    network = Network()
    svc = stub_service(tmp_path, monkeypatch, network)
    p1 = await operator_preview_then_chain(svc, monkeypatch)
    last_run, history = svc.get_last_run(), svc.get_history()
    attempts, states, posts = list(svc._recent_attempts), list(svc.test_states), len(network.posts)

    late = await svc.commit_campaign(items=p1["items"], preview_generated_utc=p1["generated_utc"])

    assert late["status"] == "stale"
    assert len(network.posts) == posts
    assert svc.get_last_run() == last_run and last_run["trigger"] == "chained"
    assert svc.get_history() == history
    assert svc._recent_attempts == attempts
    assert svc.test_states == states, "the health chip is not touched either"


async def test_a_plan_previewed_after_the_last_submit_goes_out(tmp_path, monkeypatch):
    network = Network()
    svc = stub_service(tmp_path, monkeypatch, network)
    await operator_preview_then_chain(svc, monkeypatch)
    preview_returns(svc, monkeypatch, [item(42)])
    fresh = await svc.preview_campaign()

    result = await svc.commit_campaign(items=fresh["items"],
                                       preview_generated_utc=fresh["generated_utc"])

    assert result["status"] == "ok" and network.posts[-1][0]["ground_station"] == 42


async def test_the_last_submit_survives_a_restart(tmp_path, monkeypatch):
    """uvicorn --reload restarts the live backend on every edit; a reload
    between the chain's commit and CONFIRM must not make the plan fresh."""
    network = Network()
    svc = stub_service(tmp_path, monkeypatch, network)
    p1 = await operator_preview_then_chain(svc, monkeypatch)

    reloaded = stub_service(tmp_path, monkeypatch, network)
    late = await reloaded.commit_campaign(items=p1["items"],
                                          preview_generated_utc=p1["generated_utc"])

    assert late["status"] == "stale"
    assert json.loads(reloaded.last_submit_path.read_text())["trigger"] == "chained"


async def test_a_post_that_never_answered_still_makes_older_plans_stale(tmp_path, monkeypatch):
    """Noted before the POST, like the recent attempts: one that died on the
    way back may still have booked."""
    network = Network(fail=RuntimeError("connection reset mid-reply"))
    svc = stub_service(tmp_path, monkeypatch, network)
    preview_returns(svc, monkeypatch, [item(40)])
    p1 = await svc.preview_campaign()
    preview_returns(svc, monkeypatch, [item(41)])
    assert (await svc.run_chained_cycle())["status"] == "error"

    late = await svc.commit_campaign(items=p1["items"], preview_generated_utc=p1["generated_utc"])

    assert late["status"] == "stale"


async def operator_preview_then_chain_blocked(svc, monkeypatch, gate):
    preview_returns(svc, monkeypatch, [item(40)])
    p1 = await svc.preview_campaign()
    preview_returns(svc, monkeypatch, [item(41)])
    assert (await svc.run_chained_cycle())["status"] == "blocked"
    gate["status"] = "Online"   # the station is back before the operator confirms
    return p1


async def test_a_blocked_commit_sent_nothing_so_it_makes_nothing_stale(tmp_path, monkeypatch):
    network = Network()
    gate = {"status": "Offline"}
    svc = stub_service(tmp_path, monkeypatch, network,
                       own_station=lambda: {**ONLINE, "status": gate["status"]})
    p1 = await operator_preview_then_chain_blocked(svc, monkeypatch, gate)

    result = await svc.commit_campaign(items=p1["items"], preview_generated_utc=p1["generated_utc"])

    assert result["status"] == "ok" and len(network.posts) == 1


async def test_an_older_client_without_a_stamp_is_not_checked(tmp_path, monkeypatch):
    network = Network()
    svc = stub_service(tmp_path, monkeypatch, network)
    p1 = await operator_preview_then_chain(svc, monkeypatch)

    result = await svc.commit_campaign(items=p1["items"])

    assert result["status"] == "ok", "as before the stamp existed"


async def test_an_unreadable_stamp_is_refused(tmp_path, monkeypatch):
    network = Network()
    svc = stub_service(tmp_path, monkeypatch, network)
    p1 = await operator_preview_then_chain(svc, monkeypatch)

    late = await svc.commit_campaign(items=p1["items"], preview_generated_utc="yesterday-ish")

    assert late["status"] == "stale" and "could not be read" in late["stopped_reason"]


async def test_the_chain_and_the_timer_hand_their_own_preview_stamp_on(tmp_path, monkeypatch):
    svc = stub_service(tmp_path, monkeypatch, Network())
    svc.schedule_service.campaign_auto_commit_enabled = lambda: True
    preview_returns(svc, monkeypatch, [item(40)])
    seen: list[dict] = []

    async def spy(**kwargs):
        seen.append(kwargs)
        return {"status": "ok"}
    monkeypatch.setattr(svc, "commit_campaign", spy)

    await svc.run_chained_cycle()
    await svc.run_auto_cycle()

    assert [k["trigger"] for k in seen] == ["chained", "auto"]
    stamp = svc.get_last_preview()["generated_utc"]
    assert seen[-1]["preview_generated_utc"] == stamp
    assert all(k["preview_generated_utc"] for k in seen)


# --- the route ---------------------------------------------------------------------------

def route_client(svc):
    app = FastAPI()
    app.include_router(schedule_routes.router, prefix="/api")
    app.state.campaign_service = svc
    return TestClient(app)


def body_item(row: dict) -> dict:
    return {k: row[k] for k in ("station_id", "station_name", "transmitter_uuid", "start", "end",
                                "max_elevation_deg")}


async def test_the_route_answers_stale_at_once_and_starts_nothing(tmp_path, monkeypatch):
    svc = stub_service(tmp_path, monkeypatch, Network())
    p1 = await operator_preview_then_chain(svc, monkeypatch)
    monkeypatch.setattr(svc, "commit_campaign",
                        lambda **k: pytest.fail("a stale plan must not start a commit"))

    resp = route_client(svc).post("/api/schedule/campaign/commit", json={
        "items": [body_item(i) for i in p1["items"]],
        "preview_generated_utc": p1["generated_utc"]})

    assert resp.status_code == 200
    assert resp.json()["status"] == "stale"
    assert "run PREVIEW again" in resp.json()["stopped_reason"]


async def test_the_route_hands_the_stamp_to_the_commit(tmp_path, monkeypatch):
    svc = stub_service(tmp_path, monkeypatch, Network())
    calls: list[dict] = []

    async def done():
        return {"status": "ok"}

    def commit(**kwargs):
        calls.append(kwargs)
        return done()
    monkeypatch.setattr(svc, "commit_campaign", commit)
    stamp = datetime.now(timezone.utc).isoformat()

    resp = route_client(svc).post("/api/schedule/campaign/commit", json={
        "items": [body_item(item(40))], "preview_generated_utc": stamp})

    assert resp.json() == {"status": "started"}
    assert calls and calls[0]["preview_generated_utc"] == stamp
    assert calls[0]["trigger"] == "manual"
