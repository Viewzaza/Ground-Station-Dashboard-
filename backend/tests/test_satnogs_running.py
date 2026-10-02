"""A SatNOGS observation that is recording right now must stay visible.

/api/jobs/ filters on start >= now, so a job disappears from it the instant it
starts. Built on jobs alone, the interlock's pass gate and the planner both
went blind to a recording in progress — the one moment the antenna is
unquestionably taken. These tests pin the two ways it is now recovered, and
check that the gate actually closes because of it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.services.satnogs import FEED_LIMIT, SatnogsService


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def job(jid: int, start: datetime, minutes: float = 10, norad: int = 67683) -> dict:
    return {"id": jid, "norad_cat_id": norad, "start": iso(start),
            "end": iso(start + timedelta(minutes=minutes))}


class FakeResponse:
    def __init__(self, data):
        self._data = data
        self.headers = {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeClient:
    def __init__(self, data):
        self.data = data

    async def get(self, url, params=None):
        return FakeResponse(self.data)


def service() -> SatnogsService:
    return SatnogsService(Settings(station_id=5024))


@pytest.mark.asyncio
async def test_a_job_that_starts_between_polls_is_carried_over_as_running():
    now = datetime.now(timezone.utc)
    svc = service()

    # Poll 1: the job is upcoming and listed.
    soon = job(1, now - timedelta(minutes=2))    # its window already open...
    await svc.refresh_jobs(FakeClient([soon]))
    assert svc.jobs and not svc.running

    # Poll 2: SatNOGS has dropped it from /api/jobs/ because it started.
    await svc.refresh_jobs(FakeClient([]))
    assert svc.jobs == []
    assert 1 in svc.running, "a started job must not simply vanish"


@pytest.mark.asyncio
async def test_the_pass_gate_stays_shut_while_satnogs_is_recording():
    now = datetime.now(timezone.utc)
    svc = service()
    await svc.refresh_jobs(FakeClient([job(1, now - timedelta(minutes=2))]))
    await svc.refresh_jobs(FakeClient([]))

    # In progress reads as 0.0 — "now" — never as "nothing scheduled".
    assert svc.seconds_to_next_job() == 0.0


@pytest.mark.asyncio
async def test_a_finished_job_is_released():
    now = datetime.now(timezone.utc)
    svc = service()
    finished = job(1, now - timedelta(minutes=20), minutes=10)
    svc.running[1] = finished
    await svc.refresh_jobs(FakeClient([]))
    assert svc.running == {}
    assert svc.seconds_to_next_job() is None


@pytest.mark.asyncio
async def test_a_job_that_vanishes_before_its_start_is_not_kept():
    """Cancelled before it began: nothing is recording, so nothing to hold."""
    now = datetime.now(timezone.utc)
    svc = service()
    await svc.refresh_jobs(FakeClient([job(1, now + timedelta(minutes=30))]))
    await svc.refresh_jobs(FakeClient([]))
    assert svc.running == {}


@pytest.mark.asyncio
async def test_a_running_observation_past_the_display_limit_is_still_seen():
    """Observations come back newest-start first, so on a busy station the
    future ones fill the display feed and a recording in progress falls off
    the end of it. The commitment must be read from the whole page."""
    now = datetime.now(timezone.utc)
    future = [dict(job(100 + k, now + timedelta(hours=1 + k)), status="future")
              for k in range(FEED_LIMIT + 3)]
    running = dict(job(7, now - timedelta(minutes=3)), status="future")
    page = future[::-1] + [running]        # newest start first

    svc = service()
    await svc.refresh_observations(FakeClient(page))

    assert all(o["id"] != 7 for o in svc.observations), "precondition: off the feed"
    assert 7 in svc.running
    assert svc.seconds_to_next_job() == 0.0


@pytest.mark.asyncio
async def test_a_restart_mid_recording_is_covered_by_observations():
    """After a restart there is no previous job list to carry over from —
    the observations feed is the only witness."""
    now = datetime.now(timezone.utc)
    svc = service()
    await svc.refresh_jobs(FakeClient([]))              # nothing upcoming
    await svc.refresh_observations(FakeClient([job(9, now - timedelta(minutes=1))]))
    assert svc.seconds_to_next_job() == 0.0


def test_commitments_do_not_double_count_a_job_listed_in_both():
    now = datetime.now(timezone.utc)
    svc = service()
    j = job(5, now + timedelta(minutes=30))
    svc.jobs = [j]
    svc.running[5] = j
    assert len(svc.commitments) == 1


@pytest.mark.asyncio
async def test_the_interlock_pass_gate_closes_for_a_running_observation():
    """End to end: ControlService's no_imminent_pass gate must shut."""
    from app.services.control import ControlService

    now = datetime.now(timezone.utc)
    s = Settings(station_id=5024, rotator_control_enabled=True)
    svc = SatnogsService(s)
    await svc.refresh_station(FakeClient([{"id": 5024, "is_connected": False}]))
    await svc.refresh_jobs(FakeClient([job(1, now - timedelta(minutes=2))]))
    await svc.refresh_jobs(FakeClient([]))      # it started; gone from /jobs/

    class _Rot:
        verified = True
        last = None
        client = type("C", (), {"caps": None})()

    control = ControlService(s, _Rot(), svc, predictor=None)
    gates = control.gates()
    assert gates["satnogs_idle"] is True          # client is disconnected...
    assert gates["no_imminent_pass"] is False     # ...but a recording is running


def test_the_planner_reserves_a_running_observation():
    from app.services.planner_service import PlannerService

    now = datetime.now(timezone.utc)
    svc = service()
    svc.running[3] = job(3, now - timedelta(minutes=4), norad=25544)

    class _Pred:
        def passes(self, norad, hours=24.0):
            return []

    planner = PlannerService(Settings(), _Pred(), svc)
    assert [r.job_id for r in planner.reservations()] == [3]
