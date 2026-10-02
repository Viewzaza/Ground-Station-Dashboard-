"""The campaign stops booking while our own station is not Online.

SatNOGS lets an account book someone else's station only while it owns at
least one connected, available, located, non-testing station - which for us
means station 5024 being "Online". On 2026-10-02 5024 lost power, and the
automatic campaign sent 100 bookings: all 100 came back HTTP 400 "No
permission to schedule observations on station: N". Two layers keep that
from happening again:

* **the gate** - a commit whose own-station status is FRESH and not "Online"
  sends nothing and is recorded as "blocked". Unknown or stale status never
  blocks: a poller that stopped must not switch the campaign off silently;
* **the backstop** - if a whole POST still comes back "No permission", the
  commit stops there, and that POST's attempts are forgotten so its slots are
  bookable the moment the station is back.

Nothing here touches the network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services import campaign_service as cs
from app.services.campaign_service import CampaignService
from app.vendor.autoscheduler.network_client import ScheduleResult

MISSION = 67683
TELEMETRY = "UatCXtfDnoBPeVBGHgj4Bc"
LAST_SEEN = "2026-10-02T09:12:41Z"
NO_PERMISSION = ('HTTP 400 {"non_field_errors":["No permission to schedule observations '
                 'on station: {sid}"]}')


def item(station_id: int, uuid: str = TELEMETRY, n: int = 0) -> dict:
    start = datetime(2026, 10, 3, tzinfo=timezone.utc) + timedelta(hours=station_id, minutes=n * 20)
    return {
        "station_id": station_id, "station_name": f"S{station_id}",
        "transmitter_uuid": uuid, "start": start.isoformat(),
        "end": (start + timedelta(minutes=8)).isoformat(), "max_elevation_deg": 40.0,
    }


def own(status: str | None = "Online", age_s: float | None = 30.0, sid: int = 5024):
    """What scheduler.Scheduler._own_station hands the service."""
    return lambda: {"id": sid, "status": status, "last_seen": LAST_SEEN, "age_s": age_s}


class Network:
    """Records every POST; `answer(posted)` decides the reply (default: all taken)."""

    def __init__(self, answer=None):
        self.posts: list[list[dict]] = []
        self.answer = answer
        self.calendar_sources: dict = {}

    def schedule(self, items, execute=False):
        assert execute
        self.posts.append(list(items))
        if self.answer is not None:
            return self.answer(list(items))
        return ScheduleResult(submitted=len(items), accepted=len(items), accepted_items=list(items))

    def future_bookings(self, station_id, now=None):
        return []


def make_service(tmp_path, monkeypatch, network, *, own_station=None, loop=False,
                 mock=False):
    settings = Settings(data_dir=tmp_path, mock=mock, campaign_mock=mock, default_norad=MISSION)

    class StubSchedule:
        cache_dir = tmp_path / "cache"

        def _build_autoscheduler_settings(self, hours):
            return SimpleNamespace(network_token="", buffer_s=30.0)

        def _effective_network_token(self):
            return "token"

        def _effective_station_id(self):
            return 5024

        def campaign_loop_until_exhausted(self):
            return loop

        def campaign_transmitter_policy(self):
            return "preferred"

        def campaign_max_per_station(self):
            return 3

        def campaign_max_total(self):
            return 600

        def campaign_auto_commit_enabled(self):
            return False

    clients: list = []

    def network_client(*args, **kwargs):
        clients.append(1)
        return network

    monkeypatch.setattr(cs, "Cache", lambda *a, **k: None)
    monkeypatch.setattr(cs, "NetworkClient", network_client)
    monkeypatch.setattr(cs, "DbClient", lambda *a, **k: None)
    states: list[tuple] = []
    svc = CampaignService(settings, StubSchedule(),
                          on_state=lambda c, s, d="": states.append((c, s, d)),
                          own_station=own_station)
    svc.test_states = states
    svc.test_clients = clients
    return svc


# --- own_station_state ---------------------------------------------------------------

@pytest.mark.parametrize("snapshot, blocks", [
    (own("Online"), False),
    (own("Offline"), True),
    (own("Testing"), True),            # testing stations do not grant the permission
    (own("Offline", age_s=600.0), True),
    (own("Offline", age_s=600.5), False),   # stale: we no longer know
    (own("Offline", age_s=None), False),    # never polled
    (own(None), False),                     # no status in the payload
    (own(""), False),
    (lambda: None, False),                  # SatnogsService has no station yet
])
def test_only_a_fresh_status_other_than_online_blocks(tmp_path, monkeypatch, snapshot, blocks):
    svc = make_service(tmp_path, monkeypatch, Network(), own_station=snapshot)

    state = svc.own_station_state()

    assert state["blocks_booking"] is blocks
    assert set(state) == {"station_id", "status", "last_seen", "age_s", "fresh", "blocks_booking"}


def test_no_wiring_and_a_failing_reader_are_unknown_not_blocked(tmp_path, monkeypatch):
    assert make_service(tmp_path, monkeypatch, Network()).own_station_state() == {
        "station_id": None, "status": None, "last_seen": None, "age_s": None,
        "fresh": False, "blocks_booking": False,
    }

    def broken():
        raise RuntimeError("poller exploded")

    svc = make_service(tmp_path, monkeypatch, Network(), own_station=broken)
    assert svc.own_station_state()["blocks_booking"] is False


def test_the_state_reports_what_the_gate_looked_at(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch, Network(), own_station=own("Offline", age_s=42.0))

    assert svc.own_station_state() == {
        "station_id": 5024, "status": "Offline", "last_seen": LAST_SEEN, "age_s": 42.0,
        "fresh": True, "blocks_booking": True,
    }


# --- the gate on commit -------------------------------------------------------------------

@pytest.mark.parametrize("mock", [False, True])
@pytest.mark.parametrize("trigger", ["manual", "auto", "chained"])
async def test_a_commit_with_our_station_offline_sends_nothing(tmp_path, monkeypatch,
                                                              mock, trigger):
    network = Network()
    svc = make_service(tmp_path, monkeypatch, network, own_station=own("Offline"), mock=mock)
    monkeypatch.setattr(svc, "_build_items", lambda *a, **k: pytest.fail("must not even plan"))

    result = await svc.commit_campaign(items=[item(40), item(41)], trigger=trigger)

    assert network.posts == [] and svc.test_clients == [], "no POST, not even a client"
    assert result["status"] == "blocked"
    assert result["trigger"] == trigger
    assert (result["submitted"], result["accepted"], result["errors"],
            result["accepted_items"], result["rounds"]) == (0, 0, [], [], 0)
    assert result["stopped_reason"] == (
        f"station 5024 is Offline (last seen {LAST_SEEN}) - SatNOGS refuses bookings on "
        "other people's stations until one of ours is Online, so nothing was sent")
    assert result["own_station"]["blocks_booking"] is True
    # Nothing was tried, so nothing is held back from the next run.
    assert svc._recent_attempts == []
    assert not svc.recent_attempts_path.exists()
    # Recorded like any other run.
    assert svc.get_last_run() == result
    assert svc.get_history()[-1]["status"] == "blocked"
    assert svc.get_history()[-1]["trigger"] == trigger
    assert svc.test_states[-1] == ("campaign", "degraded", "blocked: station 5024 is Offline")


async def test_a_commit_that_would_build_its_own_items_is_blocked_too(tmp_path, monkeypatch):
    network = Network()
    svc = make_service(tmp_path, monkeypatch, network, own_station=own("Testing"))
    monkeypatch.setattr(svc, "_build_items", lambda *a, **k: pytest.fail("must not even plan"))

    result = await svc.commit_campaign(items=None, trigger="auto")

    assert result["status"] == "blocked" and network.posts == []


@pytest.mark.parametrize("snapshot", [
    own("Online"), own("Offline", age_s=3600.0), own(None), lambda: None, None,
], ids=["fresh-online", "stale-offline", "no-status", "not-polled-yet", "not-wired"])
async def test_online_stale_or_unknown_still_books(tmp_path, monkeypatch, snapshot):
    network = Network()
    svc = make_service(tmp_path, monkeypatch, network, own_station=snapshot)

    result = await svc.commit_campaign(items=[item(40)], trigger="auto")

    assert result["status"] == "ok"
    assert len(network.posts) == 1 and result["accepted"] == 1


# --- the preview carries the gate's view ---------------------------------------------------

async def test_the_preview_says_whether_our_station_would_block_a_commit(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch, Network(), own_station=own("Offline"))
    monkeypatch.setattr(svc, "_preview_sync", lambda: {"status": "ok", "items": [item(40)]})

    preview = await svc.preview_campaign()

    assert preview["own_station"]["blocks_booking"] is True
    assert svc.get_last_preview()["own_station"]["status"] == "Offline", "and it is cached"
    assert svc.test_states[-1] == ("campaign", "degraded", "blocked: station 5024 is Offline")


async def test_the_mock_preview_carries_it_too(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch, Network(), own_station=own("Online"), mock=True)

    preview = await svc.preview_campaign()

    assert preview["own_station"] == {
        "station_id": 5024, "status": "Online", "last_seen": LAST_SEEN, "age_s": 30.0,
        "fresh": True, "blocks_booking": False,
    }
    assert preview["items"], "the mock preview still plans"


# --- the backstop: a POST refused for permission ends the commit ------------------------

def refuse(uuid: str):
    """Accept every POST except those for `uuid`, whose items are each refused
    for permission - as schedule() reports a 400'd batch after retrying singly."""
    def answer(posted):
        if posted[0]["transmitter_uuid"] != uuid:
            return ScheduleResult(submitted=len(posted), accepted=len(posted),
                                  accepted_items=posted)
        return ScheduleResult(submitted=len(posted), errors=[
            f"station {p['ground_station']} {p['start']} transmitter {uuid}: "
            + NO_PERMISSION.replace("{sid}", str(p["ground_station"]))
            for p in posted])
    return answer


async def test_a_post_refused_for_permission_stops_the_commit(tmp_path, monkeypatch):
    network = Network(refuse("tx-b"))
    # Stale status: the gate let it through, so the server is the one to say no.
    svc = make_service(tmp_path, monkeypatch, network, loop=True,
                       own_station=own("Offline", age_s=7200.0))
    monkeypatch.setattr(svc, "_build_items", lambda *a, **k: pytest.fail("no rebuild"))
    # An earlier commit's attempt at the very window chunk b is about to try.
    earlier = (41, datetime.fromisoformat(item(41, "tx-b")["start"]),
               datetime.fromisoformat(item(41, "tx-b")["end"]),
               datetime.now(timezone.utc) - timedelta(minutes=30))
    svc._recent_attempts = [earlier]
    items = [item(40, "tx-a"), item(41, "tx-b"), item(42, "tx-b"), item(43, "tx-c"),
             item(44, "tx-c")]

    result = await svc.commit_campaign(items=items, trigger="auto")

    assert [post[0]["transmitter_uuid"] for post in network.posts] == ["tx-a", "tx-b"], (
        "chunk c must not be sent")
    assert result["no_permission"] is True
    assert result["accepted"] == 1 and result["not_sent"] == 2
    assert any("2 more item(s) in 1 later POST(s) were not sent" in e
               and "not Online" in e for e in result["errors"])
    assert "refused permission" in result["stopped_reason"]
    assert result["rounds"] == 1
    assert svc.test_states[-1][1] == "degraded"

    # Chunk a's real booking stays held; chunk b's refused tries are forgotten
    # (bookable once the station is back); chunk c was never tried at all.
    held = sorted((sid, start) for sid, start, _end, _at in svc._recent_attempts)
    assert held == sorted([(40, datetime.fromisoformat(item(40, "tx-a")["start"])),
                           (41, earlier[1])])
    assert earlier in svc._recent_attempts, "an identical earlier attempt is not ours to drop"
    on_disk = json.loads(svc.recent_attempts_path.read_text())
    assert sorted((sid, start) for sid, start, _e, _a in on_disk) == sorted(
        (sid, start.isoformat()) for sid, start in held), "and the file says the same"


async def test_a_single_batch_commit_says_why_it_stopped(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch, Network(refuse(TELEMETRY)), loop=False)

    result = await svc.commit_campaign(items=[item(40), item(41)], trigger="manual")

    assert result["no_permission"] is True
    assert "refused permission" in result["stopped_reason"]
    assert svc._recent_attempts == []


async def test_a_permission_error_among_other_outcomes_changes_nothing(tmp_path, monkeypatch):
    """Only a clean sweep of permission refusals is the account-wide verdict;
    one among overlaps or bookings is left to the per-item handling."""
    def answer(posted):
        if posted[0]["transmitter_uuid"] != "tx-b":
            return ScheduleResult(submitted=len(posted), accepted=len(posted),
                                  accepted_items=posted)
        return ScheduleResult(submitted=len(posted), errors=[
            "station 41 ...: " + NO_PERMISSION.replace("{sid}", "41"),
            "station 42 ...: HTTP 409 One or more observations of station 42 overlap",
        ])
    network = Network(answer)
    svc = make_service(tmp_path, monkeypatch, network)

    result = await svc.commit_campaign(
        items=[item(40, "tx-a"), item(41, "tx-b"), item(42, "tx-b"), item(43, "tx-c")],
        trigger="manual")

    assert len(network.posts) == 3
    assert result["no_permission"] is False and result["not_sent"] == 0
    assert {sid for sid, *_ in svc._recent_attempts} == {40, 41, 42, 43}
