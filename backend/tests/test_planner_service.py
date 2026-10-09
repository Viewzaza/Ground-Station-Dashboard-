"""Planner service tests: candidates, reservations, priorities, freshness.

Autopilot is tested in test_autopilot.py, against the real ControlService.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.services.planner_service import PlannerService, parse_priorities

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

@dataclass
class FakePass:
    pass_id: str
    name: str
    aos: datetime
    tca: datetime
    los: datetime
    max_el: float
    aos_az: float
    los_az: float

    @property
    def duration_s(self) -> float:
        return (self.los - self.aos).total_seconds()


class FakePredictor:
    def __init__(self, passes_by_norad):
        self.by = passes_by_norad

    def passes(self, norad, hours=24.0):
        return self.by.get(norad, [])


class FakeSatnogs:
    def __init__(self, jobs=None, observations=None):
        self.jobs = jobs or []
        self.observations = observations or []


def settings(**kw) -> Settings:
    base = dict(rotator_control_enabled=True, planner_lead_s=90,
                planner_setup_s=30, rotator_az_rate_deg_s=2.0,
                rotator_el_rate_deg_s=2.0, min_culmination_deg=10.0,
                gate_guard_s=300)
    return Settings(**{**base, **kw})


# --------------------------------------------------------------------------
# planner service
# --------------------------------------------------------------------------

def test_parse_priorities():
    assert parse_priorities("67683:10, 25544:2.5,99") == {67683: 10.0, 25544: 2.5, 99: 1.0}
    assert parse_priorities("") == {}
    assert parse_priorities("abc:1,67683:3") == {67683: 3.0}


def _fp(norad, start_min, minutes=8, max_el=45.0):
    aos = datetime.now(timezone.utc) + timedelta(minutes=start_min)
    return FakePass(f"{norad}-{start_min}", f"sat{norad}", aos,
                    aos + timedelta(minutes=minutes / 2),
                    aos + timedelta(minutes=minutes), max_el, 100.0, 250.0)


def test_satnogs_jobs_become_reservations_and_their_satellites_are_watched():
    job_start = datetime.now(timezone.utc) + timedelta(minutes=60)
    satnogs = FakeSatnogs(jobs=[{
        "id": 7, "norad_cat_id": 25544,
        "start": job_start.isoformat().replace("+00:00", "Z"),
        "end": (job_start + timedelta(minutes=8)).isoformat().replace("+00:00", "Z"),
    }])
    svc = PlannerService(settings(), FakePredictor({}), satnogs)
    assert 25544 in svc.priorities()
    (r,) = svc.reservations()
    assert r.norad == 25544 and r.job_id == 7


def test_an_unparsable_satnogs_job_reserves_defensively():
    """A job that cannot be read cannot be planned around — so it must not be
    silently ignored either."""
    satnogs = FakeSatnogs(jobs=[{"id": 9, "norad_cat_id": 1, "start": "garbage"}])
    svc = PlannerService(settings(), FakePredictor({}), satnogs)
    (r,) = svc.reservations()
    assert (r.end - r.start) >= timedelta(hours=svc.s.planner_horizon_h - 0.01)


def test_the_tracked_satellite_outranks_others_end_to_end():
    """KNACKSAT-2 at a modest 25° should win over an overlapping 80° pass of a
    priority-1 satellite."""
    predictor = FakePredictor({
        67683: [_fp(67683, 30, max_el=25.0)],
        11111: [_fp(11111, 32, max_el=80.0)],
    })
    s = settings(planner_priorities="67683:10,11111:1")
    svc = PlannerService(s, predictor, FakeSatnogs())
    plan = svc.rebuild()
    assert [c.norad for c in plan.planned] == [67683]
    loser = next(d for d in plan.decisions if d.candidate.norad == 11111)
    assert loser.status == "conflict"


def test_last_heard_ignores_failed_and_future_observations():
    now = datetime.now(timezone.utc)
    obs = [
        {"norad": 1, "start": (now - timedelta(hours=5)).isoformat(), "status": "good"},
        {"norad": 1, "start": (now - timedelta(hours=1)).isoformat(), "status": "failed"},
        {"norad": 1, "start": (now + timedelta(hours=1)).isoformat(), "status": "good"},
        {"norad": 2, "start": (now - timedelta(hours=2)).isoformat(),
         "status": "unknown", "demoddata": 3},
    ]
    svc = PlannerService(settings(), FakePredictor({}), FakeSatnogs(observations=obs))
    heard = svc.last_heard()
    assert (now - heard[1]).total_seconds() == pytest.approx(5 * 3600, abs=5)
    assert 2 in heard      # frames decoded counts as heard


def test_snapshot_explains_every_live_pass():
    predictor = FakePredictor({67683: [_fp(67683, 30), _fp(67683, 130)]})
    svc = PlannerService(settings(), predictor, FakeSatnogs())
    svc.rebuild()
    snap = svc.snapshot()
    assert snap["planned"]
    assert all(d["reason"] for d in snap["decisions"])
    assert snap["counts"]["planned"] == len(snap["planned"])


def test_satellites_without_elements_are_reported_not_silently_dropped():
    """SatNOGS schedules objects under temporary catalogue numbers that no
    public element set carries. The plan cannot predict them — and must say so."""

    class PredictorWithElements(FakePredictor):
        def satellite(self, norad):
            return object() if norad in self.by else None

    job_start = datetime.now(timezone.utc) + timedelta(hours=2)
    satnogs = FakeSatnogs(jobs=[{
        "id": 1, "norad_cat_id": 98329,
        "start": job_start.isoformat(), "end": (job_start + timedelta(minutes=8)).isoformat(),
    }])
    svc = PlannerService(settings(), PredictorWithElements({67683: [_fp(67683, 30)]}), satnogs)
    svc.rebuild()
    snap = svc.snapshot()
    assert [u["norad"] for u in snap["unplannable"]] == [98329]
    # Its window is still protected even though the plan cannot see the satellite.
    assert len(svc.reservations()) == 1
