"""Chaining the worldwide KNACKSAT-2 campaign onto each Station Schedule
auto-run slot, and the own-station status the campaign is gated on.

With auto_run_chain_campaign on, every auto-run slot that actually runs is
followed by CampaignService.run_chained_cycle(): a preview, then a commit of
exactly that preview under the campaign's own caps, recorded with trigger
"chained". What matters:

* it runs only after a slot that ran, and only while the toggle is on - not
  on the busy path, where the slot has not happened and is retried;
* it runs whatever the station's own run did (5024's run failing says nothing
  about the community stations; the own-station gate covers an Offline 5024);
* a campaign failure never breaks the station loop, never touches the slot
  mark, and is never retried;
* the toggle is its own consent: campaign_auto_commit_enabled is not consulted.

Nothing here touches the network.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import scheduler as scheduler_module
from app.config import Settings
from app.services import campaign_service as cs
from app.services.campaign_service import CampaignService
from app.services.predictor import Predictor
from app.services.tle_store import TleStore

MISSION = 67683


class _Stop(Exception):
    pass


class StubSchedule:
    """The Scheduler-facing half of ScheduleService: one due slot, then stop."""

    def __init__(self, *, chain: bool, run_status: str = "ok"):
        self.chain = chain
        self.run_status = run_status
        self.events: list = []
        self._due_calls = 0

    def next_auto_run(self):
        self._due_calls += 1
        if self._due_calls > 1:
            # Reaching this again means the loop survived the slot.
            self.events.append("next")
            raise _Stop
        return datetime.now(timezone.utc) - timedelta(seconds=1)

    async def mark_auto_run(self):
        self.events.append("mark")
        return "previous"

    async def restore_auto_run_mark(self, previous):
        self.events.append(("restore", previous))

    async def run_plan(self, *, trigger):
        self.events.append(("run", trigger))
        return {"status": self.run_status}

    def auto_run_chain_campaign(self):
        return self.chain


class StubCampaign:
    def __init__(self, events: list, outcome=None, error: Exception | None = None):
        self.events = events
        self.outcome = outcome if outcome is not None else {"status": "ok", "accepted": 3}
        self.error = error

    async def run_chained_cycle(self):
        self.events.append("chain")
        if self.error is not None:
            raise self.error
        return self.outcome


def make_loop_owner(schedule, campaign):
    owner = scheduler_module.Scheduler.__new__(scheduler_module.Scheduler)
    owner.schedule_service = schedule
    owner.campaign_service = campaign
    return owner


async def run_loop(owner, monkeypatch) -> None:
    async def fake_sleep(seconds):
        owner.schedule_service.events.append(("sleep", seconds))
        raise _Stop
    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        await owner._schedule_loop()


# --- the scheduler side ------------------------------------------------------------------

async def test_a_slot_that_ran_chains_the_campaign_when_the_toggle_is_on(monkeypatch):
    schedule = StubSchedule(chain=True)
    owner = make_loop_owner(schedule, StubCampaign(schedule.events))

    await run_loop(owner, monkeypatch)

    assert schedule.events == ["mark", ("run", "auto"), "chain", "next"]


async def test_with_the_toggle_off_nothing_is_chained(monkeypatch):
    schedule = StubSchedule(chain=False)
    owner = make_loop_owner(schedule, StubCampaign(schedule.events))

    await run_loop(owner, monkeypatch)

    assert schedule.events == ["mark", ("run", "auto"), "next"]


async def test_the_busy_path_chains_nothing(monkeypatch):
    """run_plan refused (a manual run in flight): the slot has not happened,
    its mark is put back and it is retried - chaining now would book the
    campaign for a slot that never ran."""
    schedule = StubSchedule(chain=True, run_status="running")
    owner = make_loop_owner(schedule, StubCampaign(schedule.events))

    await run_loop(owner, monkeypatch)

    assert schedule.events == [
        "mark", ("run", "auto"), ("restore", "previous"),
        ("sleep", scheduler_module.Scheduler._AUTO_RUN_BUSY_RETRY_S),
    ]


async def test_a_failed_station_run_still_chains_the_campaign(monkeypatch):
    """5024's own run failing (e.g. 'neither in online nor in testing mode')
    says nothing about the community stations; the campaign's own-station gate
    is what decides whether it may book."""
    schedule = StubSchedule(chain=True, run_status="error")
    owner = make_loop_owner(schedule, StubCampaign(schedule.events))

    await run_loop(owner, monkeypatch)

    assert "chain" in schedule.events


async def test_a_campaign_crash_neither_breaks_the_loop_nor_refires_the_slot(monkeypatch):
    schedule = StubSchedule(chain=True)
    owner = make_loop_owner(schedule, StubCampaign(schedule.events,
                                                   error=RuntimeError("SatNOGS exploded")))

    await run_loop(owner, monkeypatch)

    assert schedule.events == ["mark", ("run", "auto"), "chain", "next"], (
        "the loop must carry on to the next slot: no restore, no retry, no re-run")


@pytest.mark.parametrize("outcome", [{"status": "running"}, {"status": "skipped", "reason": "x"},
                                     {"status": "blocked", "stopped_reason": "offline"}])
async def test_a_busy_or_skipped_campaign_is_logged_and_left(monkeypatch, outcome):
    schedule = StubSchedule(chain=True)
    owner = make_loop_owner(schedule, StubCampaign(schedule.events, outcome=outcome))

    await run_loop(owner, monkeypatch)

    assert schedule.events.count("chain") == 1
    assert schedule.events[-1] == "next"


def test_the_scheduler_hands_the_campaign_our_polled_station(tmp_path):
    """The gate reads SatnogsService's own poll of our station - no extra read."""
    settings = Settings(data_dir=tmp_path, mock=True, offline=True)
    tles = TleStore(settings)
    owner = scheduler_module.Scheduler(settings, tles, Predictor(settings, tles))

    assert owner.campaign_service.own_station_state()["blocks_booking"] is False, (
        "before the first poll the status is unknown, which never blocks")

    owner.satnogs.station = {"id": 5024, "status": "Offline", "last_seen": "2026-10-02T09:12:41Z"}
    owner.satnogs._station_at = time.monotonic()
    state = owner.campaign_service.own_station_state()
    assert (state["station_id"], state["status"], state["last_seen"], state["blocks_booking"]) == (
        5024, "Offline", "2026-10-02T09:12:41Z", True)

    owner.satnogs._station_at = time.monotonic() - 3600
    assert owner.campaign_service.own_station_state()["blocks_booking"] is False, (
        "an hour-old poll is no longer evidence")


# --- CampaignService.run_chained_cycle -------------------------------------------------------

def make_campaign(tmp_path, monkeypatch, *, mock=True, own_station=None):
    settings = Settings(data_dir=tmp_path, mock=mock, campaign_mock=mock, default_norad=MISSION)

    class Schedule:
        cache_dir = tmp_path / "cache"

        def campaign_transmitter_policy(self):
            return "preferred"

        def campaign_max_per_station(self):
            return 3

        def campaign_max_total(self):
            return 600

        def campaign_loop_until_exhausted(self):
            return False

        def campaign_auto_commit_enabled(self):
            # The chain is its own consent; this must not be what decides.
            return False

    monkeypatch.setattr(cs, "NetworkClient", lambda *a, **k: pytest.fail("no network"))
    return CampaignService(settings, Schedule(), own_station=own_station)


async def test_a_chained_cycle_commits_its_preview_and_history_says_chained(tmp_path, monkeypatch):
    svc = make_campaign(tmp_path, monkeypatch)

    result = await svc.run_chained_cycle()

    assert result["status"] == "ok" and result["trigger"] == "chained"
    assert result["accepted"] == len(svc.get_last_preview()["items"]) > 0
    assert svc.get_last_run()["trigger"] == "chained"
    assert svc.get_history()[-1]["trigger"] == "chained"


async def test_a_chained_cycle_with_our_station_offline_is_blocked(tmp_path, monkeypatch):
    svc = make_campaign(tmp_path, monkeypatch, own_station=lambda: {
        "id": 5024, "status": "Offline", "last_seen": "2026-10-02T09:12:41Z", "age_s": 20.0})

    result = await svc.run_chained_cycle()

    assert result["status"] == "blocked" and result["submitted"] == 0
    assert svc.get_history()[-1]["trigger"] == "chained"
    assert svc.get_history()[-1]["status"] == "blocked"


async def test_a_chained_cycle_does_not_queue_behind_a_manual_run(tmp_path, monkeypatch):
    svc = make_campaign(tmp_path, monkeypatch)
    svc._running = True   # a manual preview/commit/cross-check in flight
    commits: list = []
    monkeypatch.setattr(svc, "commit_campaign", lambda **k: commits.append(k))

    assert await svc.run_chained_cycle() == {"status": "running"}
    assert commits == []


@pytest.mark.parametrize("preview, reason", [
    ({"status": "error", "error": "DB down"}, "the campaign preview failed: DB down"),
    ({"status": "ok", "items": []}, "the preview found nothing to book"),
    ({"status": "ok", "items": [], "stopped_early": {"reason": "rate limited"}},
     "the preview was cut short before it planned anything (rate limited)"),
])
async def test_nothing_to_commit_is_skipped(tmp_path, monkeypatch, preview, reason):
    svc = make_campaign(tmp_path, monkeypatch)

    async def fake_preview():
        return preview
    monkeypatch.setattr(svc, "preview_campaign", fake_preview)
    monkeypatch.setattr(svc, "commit_campaign", lambda **k: pytest.fail("nothing to commit"))

    assert await svc.run_chained_cycle() == {"status": "skipped", "reason": reason}
