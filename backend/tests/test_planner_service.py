"""Planner service and autopilot tests.

The executor tests are the ones that matter: each is a way autopilot could move
the antenna when it should not, or fight an operator, or keep driving after the
human who consented to it has gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.services.planner import Candidate, Plan
from app.services.planner_service import (
    AutopilotRefused,
    PlanExecutor,
    PlannerService,
    parse_priorities,
)

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


@dataclass
class FakeState:
    mode: str = "idle"
    target_norad: int | None = None


class FakeControl:
    """Records every command. `gates_closed` simulates the interlock."""

    def __init__(self):
        self.armed = True
        self.gates_closed: list[str] = []
        self.mode = "idle"
        self.target = None
        self.calls: list[tuple] = []

    def blocked_by(self):
        return list(self.gates_closed) + ([] if self.armed else ["armed"])

    def state(self):
        return FakeState(self.mode, self.target)

    async def goto(self, az, el):
        assert not self.blocked_by(), "goto issued through a closed gate"
        self.calls.append(("goto", round(az, 1), el))
        self.mode = "manual"

    async def track(self, norad):
        assert not self.blocked_by(), "track issued through a closed gate"
        self.calls.append(("track", norad))
        self.mode, self.target = "track", norad

    async def stop(self):
        self.calls.append(("stop",))
        self.mode, self.target = "idle", None


@dataclass
class FakeSample:
    az_raw: float = 0.0
    el: float = 0.0


class FakeClient:
    caps = None


class FakeRotator:
    def __init__(self):
        self.last = FakeSample()
        self.client = FakeClient()


def settings(**kw) -> Settings:
    base = dict(rotator_control_enabled=True, planner_lead_s=90,
                planner_setup_s=30, rotator_az_rate_deg_s=2.0,
                rotator_el_rate_deg_s=2.0, min_culmination_deg=10.0,
                gate_guard_s=300)
    return Settings(**{**base, **kw})


def planned(key, norad, aos, minutes=8, aos_az=120.0) -> Candidate:
    return Candidate(key=key, norad=norad, name=f"sat{norad}",
                     aos=aos, tca=aos + timedelta(minutes=minutes / 2),
                     los=aos + timedelta(minutes=minutes),
                     max_el=45.0, aos_az=aos_az, los_az=300.0,
                     priority=1.0, score=1.0)


class StubPlanner:
    def __init__(self, cands, s):
        self.plan = Plan(built_at=NOW, horizon_h=24, planned=cands)
        self.s = s

    @property
    def slew(self):
        from app.services.planner import SlewModel
        return SlewModel(az_rate_deg_s=2.0, el_rate_deg_s=2.0, setup_s=30.0)


def make_executor(cands, **kw):
    s = settings(**kw)
    control = FakeControl()
    ex = PlanExecutor(s, StubPlanner(cands, s), control, FakeRotator())
    return ex, control


# --------------------------------------------------------------------------
# consent
# --------------------------------------------------------------------------

def test_autopilot_cannot_be_engaged_without_a_lease():
    ex, control = make_executor([])
    control.armed = False
    with pytest.raises(AutopilotRefused, match="arm"):
        ex.enable()
    assert not ex.state.enabled


def test_autopilot_cannot_be_engaged_with_control_disabled():
    ex, _ = make_executor([], rotator_control_enabled=False)
    with pytest.raises(AutopilotRefused):
        ex.enable()


@pytest.mark.asyncio
async def test_losing_the_lease_disengages_autopilot():
    """The lease is the operator's consent. Autopilot must not outlive it."""
    ex, control = make_executor([planned("p", 1, NOW + timedelta(minutes=30))])
    ex.enable()
    control.armed = False
    await ex.step(NOW)
    assert not ex.state.enabled
    assert "lease" in ex.state.disengaged_because
    assert control.calls == []


@pytest.mark.asyncio
async def test_autopilot_never_arms_or_extends_a_lease():
    ex, control = make_executor([planned("p", 1, NOW + timedelta(minutes=1))])
    ex.enable()
    for minute in range(20):
        await ex.step(NOW + timedelta(minutes=minute))
    # No arm/extend call exists on the fake; any attempt would AttributeError.
    assert all(c[0] in ("goto", "track", "stop") for c in control.calls)


# --------------------------------------------------------------------------
# the sequence
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_waits_then_positions_then_tracks_then_stops():
    aos = NOW + timedelta(minutes=10)
    p = planned("p", 67683, aos, minutes=8, aos_az=120.0)
    ex, control = make_executor([p])
    ex.enable()

    await ex.step(NOW)                                  # 10 min out
    assert ex.state.phase == "waiting" and control.calls == []

    await ex.step(aos - timedelta(seconds=80))          # inside the lead
    assert ex.state.phase == "positioning"
    assert control.calls == [("goto", 120.0, 0.0)]

    await ex.step(aos - timedelta(seconds=40))          # still positioning
    assert control.calls == [("goto", 120.0, 0.0)], "goto must be issued once"

    await ex.step(aos + timedelta(seconds=1))           # pass under way
    assert ex.state.phase == "tracking"
    assert control.calls[-1] == ("track", 67683)

    await ex.step(aos + timedelta(minutes=4))           # mid-pass: no re-issue
    assert control.calls.count(("track", 67683)) == 1

    await ex.step(p.los + timedelta(seconds=1))         # over
    assert control.calls[-1] == ("stop",)
    assert ex.state.phase == "idle"


@pytest.mark.asyncio
async def test_back_to_back_passes_are_worked_in_order():
    a = planned("a", 1, NOW + timedelta(minutes=5), minutes=6, aos_az=90.0)
    b = planned("b", 2, a.los + timedelta(minutes=6), minutes=6, aos_az=200.0)
    ex, control = make_executor([a, b])
    ex.enable()

    for second in range(0, 60 * 30, 10):
        await ex.step(NOW + timedelta(seconds=second))

    tracks = [c for c in control.calls if c[0] == "track"]
    assert tracks == [("track", 1), ("track", 2)]
    assert control.calls[-1] == ("stop",)


@pytest.mark.asyncio
async def test_pre_positioning_starts_early_enough_for_a_long_slew():
    """The antenna at 0°, the pass rising at 180°: 90 s of slew + 30 s setup
    is 120 s, more than the 90 s configured lead. Move at 120 s, not 90 s."""
    aos = NOW + timedelta(minutes=10)
    ex, control = make_executor([planned("p", 1, aos, aos_az=180.0)])
    ex.enable()

    await ex.step(aos - timedelta(seconds=110))
    assert ("goto", 180.0, 0.0) in control.calls


# --------------------------------------------------------------------------
# the interlock still decides
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_closed_gate_blocks_autopilot_without_disengaging_it():
    aos = NOW + timedelta(minutes=1)
    ex, control = make_executor([planned("p", 1, aos)])
    ex.enable()
    control.gates_closed = ["satnogs_idle"]

    await ex.step(aos + timedelta(seconds=5))
    assert ex.state.enabled
    assert ex.state.phase == "blocked"
    assert "satnogs_idle" in ex.state.detail
    assert control.calls == []


@pytest.mark.asyncio
async def test_a_gate_closing_mid_track_is_not_mistaken_for_an_operator_stop():
    """ControlService ends its own track when a gate closes. When the gate
    reopens, autopilot must resume — not conclude that a human pressed STOP."""
    aos = NOW + timedelta(minutes=1)
    p = planned("p", 1, aos, minutes=10)
    ex, control = make_executor([p])
    ex.enable()

    await ex.step(aos + timedelta(seconds=5))
    assert control.mode == "track"

    # The interlock closes and ControlService's track loop drops to idle.
    control.gates_closed = ["no_imminent_pass"]
    control.mode, control.target = "idle", None
    await ex.step(aos + timedelta(seconds=30))
    assert ex.state.phase == "blocked"

    control.gates_closed = []
    await ex.step(aos + timedelta(seconds=60))
    assert ex.state.enabled, ex.state.disengaged_because
    assert control.calls.count(("track", 1)) == 2


# --------------------------------------------------------------------------
# the operator always wins
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_operator_stop_disengages_autopilot():
    aos = NOW + timedelta(minutes=1)
    ex, control = make_executor([planned("p", 1, aos, minutes=10)])
    ex.enable()
    await ex.step(aos + timedelta(seconds=5))

    control.mode, control.target = "idle", None       # operator pressed STOP
    await ex.step(aos + timedelta(seconds=6))
    assert not ex.state.enabled
    assert "stopped" in ex.state.disengaged_because
    assert control.calls.count(("track", 1)) == 1, "must not re-start the track"


@pytest.mark.asyncio
async def test_an_operator_taking_manual_control_disengages_autopilot():
    aos = NOW + timedelta(minutes=1)
    ex, control = make_executor([planned("p", 1, aos, minutes=10)])
    ex.enable()
    await ex.step(aos + timedelta(seconds=5))

    control.mode, control.target = "manual", None     # operator drove it by hand
    await ex.step(aos + timedelta(seconds=6))
    assert not ex.state.enabled


@pytest.mark.asyncio
async def test_disabling_stops_only_a_track_autopilot_started():
    ex, control = make_executor([])
    ex.enable()
    control.mode, control.target = "track", 25544     # the operator's own track
    await ex.disable()
    assert ("stop",) not in control.calls


@pytest.mark.asyncio
async def test_disabling_stops_autopilots_own_track():
    aos = NOW + timedelta(minutes=1)
    ex, control = make_executor([planned("p", 1, aos, minutes=10)])
    ex.enable()
    await ex.step(aos + timedelta(seconds=5))
    await ex.disable()
    assert control.calls[-1] == ("stop",)


@pytest.mark.asyncio
async def test_an_internal_error_disengages_rather_than_continuing():
    ex, control = make_executor([planned("p", 1, NOW + timedelta(seconds=30))])
    ex.enable()

    async def boom(norad):
        raise RuntimeError("rotctld exploded")
    control.track = boom

    # run() wraps step(); emulate one iteration of it.
    try:
        await ex.step(NOW + timedelta(seconds=40))
    except RuntimeError as exc:
        await ex.disable(f"internal error: {exc}")
    assert not ex.state.enabled
    assert "internal error" in ex.state.disengaged_because


# --------------------------------------------------------------------------
# cable wrap
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pre_positioning_picks_the_bearing_nearest_the_antenna():
    """Sitting at 350°, a pass rising at 10° should be met at 370°, not by
    winding 340° back round to 10."""
    aos = NOW + timedelta(minutes=5)
    ex, control = make_executor([planned("p", 1, aos, aos_az=10.0)])
    ex.rotator.last = FakeSample(az_raw=350.0, el=0.0)
    ex.enable()
    await ex.step(aos - timedelta(seconds=60))
    assert ("goto", 370.0, 0.0) in control.calls


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


@pytest.mark.asyncio
async def test_a_tie_never_parks_the_antenna_on_an_end_stop():
    """From 0°, 180° and -180° are equally near. -180 is the rotator's limit,
    and starting a pass against a limit leaves no room to follow it one way."""
    aos = NOW + timedelta(minutes=5)
    ex, control = make_executor([planned("p", 1, aos, aos_az=180.0)])
    ex.rotator.last = FakeSample(az_raw=0.0, el=0.0)
    ex.enable()
    # inside the 120 s lead (90 s slew + 30 s setup)
    await ex.step(aos - timedelta(seconds=100))
    gotos = [c for c in control.calls if c[0] == "goto"]
    assert gotos == [("goto", 180.0, 0.0)]


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
