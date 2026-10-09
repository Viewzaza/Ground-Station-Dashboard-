"""Who has the antenna — the ANTENNA line in the header — and what the ERR
readout and the next-pass card are measured against.

Everything here is display. The tests that matter most are therefore the ones
that keep it display: a stale SatNOGS answer reads "unknown" and never "idle",
an hour-old goto with no lease is nobody's, and the modules that decide whether
the antenna may move do not import this one at all.

The ownership rules are table-driven against fakes, because they are a pure
function of what the services report. The pointing and pass_next tests use the
real RotatorService and Scheduler with a fake predictor and a captured hub.
"""

from __future__ import annotations

import ast
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from skyfield.api import EarthSatellite

import app.scheduler as scheduler_mod
from app.config import Settings
from app.hub import hub
from app.routes import rotator as rotator_routes
from app.schemas import ControlState, Frame, Pass, RotatorSample, SatPos, TleInfo
from app.scheduler import Scheduler
from app.services.antenna import (
    TleCache,
    antenna_state,
    beam_error_deg,
    fingerprint,
    focus_look,
)
from app.services.planner import Candidate, Plan
from app.services.planner_service import ExecutorState
from app.services.predictor import Predictor
from app.services.rotator_service import RotatorService
from app.services.tle_store import parse_tle_epoch

KN, ISS, XIV, TEMP = 67683, 25544, 28895, 98329
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
SETTINGS = Settings(default_norad=KN, gate_max_stale_s=120, mock=True)

# The same frozen KNACKSAT-2 elements test_predictor.py uses; here they also
# stand in for a job's own TLE under a temporary catalogue number.
TLE1 = "1 67683U 98067XZ  26255.31192122  .00056149  00000+0  48789-3 0  9994"
TLE2 = "2 67683  51.6258 213.5681 0007959 152.6345 207.5073 15.68476422 33916"

NAMES = {KN: "KNACKSAT-2", ISS: "ISS (ZARYA)", XIV: "CUBESAT XI-V"}


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakePredictor:
    """The catalogue knows KNACKSAT-2, ISS and XI-V — never a temporary number."""

    def __init__(self, los: datetime | None = None) -> None:
        self.los = los or NOW + timedelta(minutes=8)
        self.next_pass_calls: list[int] = []

    def satellite(self, norad):
        return SimpleNamespace(name=NAMES[norad]) if norad in NAMES else None

    def next_pass(self, norad, hours: float = 24.0):
        self.next_pass_calls.append(norad)
        return SimpleNamespace(los=self.los) if norad in NAMES else None


class FakeSatnogs:
    def __init__(self, *, connected: bool | None = False, age: float | None = 10.0,
                 jobs=(), running=(), jobs_age: float | None = 10.0) -> None:
        self.is_connected = connected
        self.station_age_s = age
        self.jobs_age_s = jobs_age
        self.jobs = list(jobs)
        self.running_jobs = list(running)

    @property
    def commitments(self) -> list[dict]:
        return self.jobs + self.running_jobs


class FakeControl:
    def __init__(self, *, mode="idle", target=None, origin="none", armed=False,
                 track_id=0, last_command="") -> None:
        self.mode, self.target = mode, target
        self.last_origin = origin
        self.armed = armed
        self.track_id = track_id
        self._last_command = last_command

    def state(self) -> ControlState:
        return ControlState(enabled=True, armed=self.armed, mode=self.mode,
                            target_norad=self.target)


def job(job_id, norad, start_min, end_min, *, elements=True, name=None) -> dict:
    out = {"id": job_id, "norad_cat_id": norad,
           "start": iso(NOW + timedelta(minutes=start_min)),
           "end": iso(NOW + timedelta(minutes=end_min))}
    if elements:
        out.update(tle0=name or NAMES.get(norad, "TEMP OBJECT"), tle1=TLE1, tle2=TLE2)
    return out


def cand(key, norad, aos_min, los_min) -> Candidate:
    aos = NOW + timedelta(minutes=aos_min)
    los = NOW + timedelta(minutes=los_min)
    return Candidate(key=key, norad=norad, name=NAMES.get(norad, ""), aos=aos,
                     tca=aos + (los - aos) / 2, los=los, max_el=45.0,
                     aos_az=200.0, los_az=20.0, priority=1.0)


def plan_of(*cands) -> Plan:
    return Plan(built_at=NOW, horizon_h=24.0, planned=list(cands))


def run(*, control=None, executor=None, satnogs=None, plan=None, predictor=None,
        tlecache=None, los_cache=None, now=NOW) -> dict:
    control = control or FakeControl()
    return antenna_state(
        control_state=control.state(), control=control,
        executor_state=executor or ExecutorState(),
        satnogs=satnogs or FakeSatnogs(), plan=plan, settings=SETTINGS,
        predictor=predictor or FakePredictor(), tlecache=tlecache or TleCache(),
        now=now, los_cache=los_cache,
    )


# --------------------------------------------------------------------------
# ownership, table-driven
# --------------------------------------------------------------------------

def _cached(*jobs) -> TleCache:
    cache = TleCache()
    cache.update(jobs, now=NOW - timedelta(minutes=30))
    return cache


CASES = [
    pytest.param(
        dict(satnogs=FakeSatnogs(connected=True, running=[job(101, ISS, -2, 6)])),
        dict(owner="satnogs", warn=False, standby=False, focus_norad=ISS,
             until=(NOW + timedelta(minutes=6)).isoformat(), job_id=101,
             focus_reason="SatNOGS recording", focus_source="catalogue"),
        ["recording ISS (ZARYA) until 12:06Z"],
        id="running-job-client-connected",
    ),
    pytest.param(
        dict(satnogs=FakeSatnogs(connected=False, running=[job(101, ISS, -2, 6)])),
        dict(owner="satnogs", warn=True, focus_norad=ISS),
        ["client disconnected", "#101", "nothing is recording"],
        id="running-job-client-disconnected",
    ),
    pytest.param(
        dict(satnogs=FakeSatnogs(connected=False, age=600.0)),
        dict(owner="unknown", warn=False, standby=False),
        ["10 min old", "owner unknown"],
        id="stale-station-while-idle-is-unknown-not-none",
    ),
    pytest.param(
        dict(satnogs=FakeSatnogs(connected=None, age=None, jobs_age=None)),
        dict(owner="unknown"),
        ["not received yet", "owner unknown"],
        id="no-answer-yet-is-unknown",
    ),
    pytest.param(
        dict(control=FakeControl(mode="track", target=XIV, origin="autopilot",
                                 armed=True, track_id=3),
             executor=ExecutorState(enabled=True, phase="tracking",
                                    detail="CUBESAT XI-V until 12:05:00Z",
                                    current="xiv-1"),
             plan=plan_of(cand("xiv-1", XIV, -3, 5))),
        dict(owner="autopilot", norad=XIV, focus_norad=XIV,
             focus_reason="track target",
             until=(NOW + timedelta(minutes=5)).isoformat()),
        ["tracking CUBESAT XI-V until LOS 12:05Z"],
        id="autopilot-track-los-from-the-plan",
    ),
    pytest.param(
        dict(control=FakeControl(mode="manual", origin="operator", armed=True,
                                 last_command="goto az=120.0 el=30.0")),
        dict(owner="operator", focus_norad=KN),
        ["manual: goto az=120.0 el=30.0"],
        id="operator-manual-while-armed",
    ),
    pytest.param(
        dict(control=FakeControl(mode="manual", origin="operator", armed=False,
                                 last_command="goto az=120.0 el=30.0")),
        dict(owner="none", activity="idle"),
        [],
        id="manual-with-the-lease-gone-is-not-the-operator",
    ),
    pytest.param(
        dict(control=FakeControl(mode="manual", origin="autopilot", armed=True,
                                 last_command="goto az=210.0 el=0.0"),
             executor=ExecutorState(enabled=True, phase="positioning",
                                    detail="at 210° for ISS (ZARYA), AOS 12:03:00Z",
                                    current="iss-1"),
             plan=plan_of(cand("kn-1", KN, 60, 70), cand("iss-1", ISS, 3, 12))),
        dict(owner="autopilot", norad=ISS, focus_norad=ISS,
             focus_reason="autopilot positioning",
             until=(NOW + timedelta(minutes=3)).isoformat()),
        ["positioning: at 210° for ISS (ZARYA)"],
        id="autopilot-positioning-focuses-the-planned-pass",
    ),
    pytest.param(
        dict(satnogs=FakeSatnogs(connected=True)),
        dict(owner="satnogs", standby=True, warn=False, focus_norad=KN),
        ["client connected", "no job scheduled"],
        id="client-connected-no-job-is-standby",
    ),
    pytest.param(
        dict(satnogs=FakeSatnogs(connected=True, jobs=[job(202, ISS, 40, 48)])),
        dict(owner="satnogs", standby=True, job_id=202, focus_norad=KN,
             until=(NOW + timedelta(minutes=40)).isoformat()),
        ["next job ISS (ZARYA) 12:40Z"],
        id="client-connected-next-job-later-keeps-the-default-focus",
    ),
    pytest.param(
        dict(satnogs=FakeSatnogs(connected=True, jobs=[job(203, ISS, 5, 13)])),
        dict(owner="satnogs", standby=True, focus_norad=ISS,
             focus_reason="SatNOGS job within 10 min"),
        [],
        id="job-within-ten-minutes-takes-the-focus",
    ),
    pytest.param(
        dict(),
        dict(owner="none", activity="idle", standby=False, warn=False,
             focus_norad=KN, focus_reason="default satellite",
             focus_has_elements=True, focus_source="catalogue"),
        [],
        id="nothing-at-all",
    ),
    pytest.param(
        dict(satnogs=FakeSatnogs(connected=True,
                                 running=[job(304, TEMP, -1, 7, elements=False)])),
        dict(owner="satnogs", focus_norad=TEMP, focus_has_elements=False,
             focus_source="none", focus_job_id=None),
        ["recording #98329"],
        id="temporary-number-with-no-elements",
    ),
    pytest.param(
        dict(satnogs=FakeSatnogs(connected=True,
                                 running=[job(304, TEMP, -1, 7, elements=False)]),
             tlecache=_cached(job(304, TEMP, -1, 7, name="TEMP OBJECT"))),
        dict(owner="satnogs", focus_norad=TEMP, focus_has_elements=True,
             focus_source="job_tle", focus_job_id=304, focus_name="TEMP OBJECT"),
        ["recording TEMP OBJECT"],
        id="temporary-number-with-the-jobs-own-elements",
    ),
    pytest.param(
        dict(control=FakeControl(mode="track", target=ISS, origin="operator",
                                 armed=True, track_id=9),
             satnogs=FakeSatnogs(connected=False, age=600.0)),
        dict(owner="operator", focus_norad=ISS),
        ["tracking ISS (ZARYA) until LOS 12:08Z"],
        id="our-own-track-stays-ours-while-satnogs-ages",
    ),
]


@pytest.mark.parametrize("given, expected, phrases", CASES)
def test_ownership(given, expected, phrases):
    state = run(**given)
    for key, value in expected.items():
        assert state[key] == value, (key, state)
    for phrase in phrases:
        assert phrase in state["activity"], state["activity"]


def test_stale_status_is_never_idle_whatever_else_is_quiet():
    """Unknown is not permission, on the wall any more than at the gate."""
    for age in (None, 121.0, 3600.0):
        state = run(satnogs=FakeSatnogs(connected=False, age=age))
        assert state["owner"] == "unknown"
        assert "idle" not in state["activity"]


def test_track_los_is_predicted_once_per_track_not_every_second():
    predictor = FakePredictor()
    cache: dict = {}
    control = FakeControl(mode="track", target=ISS, origin="operator",
                          armed=True, track_id=7)
    for _ in range(5):
        state = run(control=control, predictor=predictor, los_cache=cache)
    assert predictor.next_pass_calls == [ISS]
    assert state["until"] == predictor.los.isoformat()

    control.track_id = 8                    # a new track is a new prediction
    run(control=control, predictor=predictor, los_cache=cache)
    assert predictor.next_pass_calls == [ISS, ISS]


def test_output_carries_every_documented_field():
    state = run()
    for key in ("owner", "standby", "warn", "activity", "norad", "name", "until",
                "job_id", "focus_norad", "focus_reason", "focus_has_elements",
                "focus_source", "satnogs_age_s", "updated_at"):
        assert key in state


def test_fingerprint_ignores_the_clock_and_the_status_age():
    a = run(satnogs=FakeSatnogs(age=10.0))
    b = run(satnogs=FakeSatnogs(age=11.0), now=NOW + timedelta(seconds=1))
    assert fingerprint(a) == fingerprint(b)
    c = run(satnogs=FakeSatnogs(connected=True))
    assert fingerprint(a) != fingerprint(c)


# --------------------------------------------------------------------------
# job elements
# --------------------------------------------------------------------------

def test_tle_cache_outlives_the_job_list_and_the_minimal_running_entry():
    cache = TleCache()
    full = job(404, TEMP, 5, 13)
    cache.update([full], now=NOW)
    assert cache.get(404).tle1 == TLE1

    # The job starts: it leaves /api/jobs/, and the observation feed replaces
    # its running entry with a dict that carries no elements.
    minimal = {"id": 404, "norad_cat_id": TEMP, "start": full["start"],
               "end": full["end"], "running": True}
    cache.update([minimal], now=NOW + timedelta(minutes=6))
    assert cache.get(404) is not None and cache.get(404).tle2 == TLE2
    cache.update([], now=NOW + timedelta(minutes=8))
    assert cache.for_norad(TEMP).job_id == 404

    end = NOW + timedelta(minutes=13)
    cache.update([], now=end + timedelta(minutes=9))
    assert cache.get(404) is not None
    cache.update([], now=end + timedelta(minutes=10, seconds=1))
    assert cache.get(404) is None


def test_tle_cache_ignores_entries_that_are_not_elements():
    cache = TleCache()
    cache.update([{"id": 1, "tle1": "garbage", "tle2": "more", "end": iso(NOW)},
                  {"id": None, "tle1": TLE1, "tle2": TLE2},
                  "not a dict"], now=NOW - timedelta(minutes=1))
    assert len(cache) == 0


# --------------------------------------------------------------------------
# pointing error
# --------------------------------------------------------------------------

def test_beam_error_is_great_circle_not_pythagoras():
    # At 85° elevation, 40° of azimuth is a few degrees of beam.
    err = beam_error_deg(0.0, 85.0, 40.0, 85.0)
    assert err == pytest.approx(3.42, abs=0.05)
    assert beam_error_deg(10.0, 0.0, 350.0, 0.0) == pytest.approx(20.0)
    assert beam_error_deg(123.0, 45.0, 123.0, 45.0) == pytest.approx(0.0, abs=1e-6)


class PointingPredictor:
    """A different azimuth per satellite, all of them up at 40°."""

    AZ = {KN: 100.0, ISS: 220.0}

    def position(self, norad, when=None):
        if norad not in self.AZ:
            return None
        return SatPos(norad=norad, name=NAMES[norad], lat=0.0, lon=0.0,
                      alt_km=500.0, vel_km_s=7.6, az=self.AZ[norad], el=40.0,
                      range_km=1000.0, range_rate_km_s=0.0, footprint_km=2000.0)


@pytest.fixture
def frames(monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(hub, "publish", lambda t, d: captured.append((t, d)))
    return captured


def _pointing(frames):
    return [d for t, d in frames if t == "pointing"]


def test_pointing_follows_the_focus(frames):
    svc = RotatorService(SETTINGS, PointingPredictor())
    sample = RotatorSample(az_raw=220.0, el=40.0, az_rose=220.0, source="mock")

    svc._publish_pointing(sample)               # default: KNACKSAT-2 at az 100
    svc.focus = lambda: (ISS, None)
    svc._publish_pointing(sample)               # ISS at az 220: dead on
    kn, iss = _pointing(frames)

    assert kn["valid"] and kn["norad"] == KN and kn["name"] == "KNACKSAT-2"
    assert kn["az_error_deg"] == pytest.approx(120.0)
    assert kn["beam_error_deg"] == pytest.approx(
        beam_error_deg(220.0, 40.0, 100.0, 40.0), abs=0.01)
    assert "total_error_deg" in kn               # kept for compatibility
    assert iss["valid"] and iss["norad"] == ISS
    assert iss["beam_error_deg"] == pytest.approx(0.0, abs=0.01)


def test_pointing_reads_the_beam_not_pythagoras_near_zenith(frames):
    class High:
        def position(self, norad, when=None):
            return SatPos(norad=norad, name="HIGH", lat=0.0, lon=0.0, alt_km=500.0,
                          vel_km_s=7.6, az=40.0, el=85.0, range_km=500.0,
                          range_rate_km_s=0.0, footprint_km=2000.0)

    svc = RotatorService(SETTINGS, High())
    svc._publish_pointing(RotatorSample(az_raw=0.0, el=85.0, az_rose=0.0, source="mock"))
    (frame,) = _pointing(frames)
    assert frame["total_error_deg"] == pytest.approx(40.0)
    assert frame["beam_error_deg"] == pytest.approx(3.42, abs=0.05)


def test_pointing_without_elements_says_so(frames):
    svc = RotatorService(SETTINGS, PointingPredictor())
    svc.focus = lambda: (TEMP, None)
    svc._publish_pointing(RotatorSample(az_raw=0.0, el=0.0, az_rose=0.0, source="mock"))
    (frame,) = _pointing(frames)
    assert frame["valid"] is False
    assert frame["norad"] == TEMP
    assert frame["reason"] == "no elements for #98329"


class FrozenTleStore:
    def get(self, norad):
        if norad != KN:
            return None
        return TleInfo(norad=KN, name="KNACKSAT-2", tle1=TLE1, tle2=TLE2,
                       source="frozen", fetched_at=NOW, epoch=parse_tle_epoch(TLE1))


def test_pointing_against_a_jobs_own_elements(frames):
    """A temporary catalogue number: the predictor has nothing, the job's TLE
    is enough for an error — or for an honest "below the horizon"."""
    predictor = Predictor(Settings(), FrozenTleStore())
    override = EarthSatellite(TLE1, TLE2, "TEMP OBJECT", predictor.ts)
    look = focus_look(predictor, TEMP, override)
    assert look is not None and look[0] == "TEMP OBJECT"

    svc = RotatorService(SETTINGS, predictor)
    svc.focus = lambda: (TEMP, override)
    svc._publish_pointing(RotatorSample(az_raw=0.0, el=0.0, az_rose=0.0, source="mock"))
    (frame,) = _pointing(frames)
    assert frame["norad"] == TEMP and frame["name"] == "TEMP OBJECT"
    if frame["valid"]:
        assert "beam_error_deg" in frame
    else:
        assert frame["reason"] == "TEMP OBJECT is below the horizon"


# --------------------------------------------------------------------------
# the scheduler: pass_next, the focus, the frame
# --------------------------------------------------------------------------

class SchedPredictor:
    def __init__(self, passes: dict | None = None) -> None:
        self.passes = passes or {}
        self.ts = None

    def position(self, norad, when=None):
        return None

    def next_pass(self, norad, hours: float = 24.0):
        return self.passes.get(norad)

    def satellite(self, norad):
        return SimpleNamespace(name=NAMES[norad]) if norad in NAMES else None


class StopLoop(Exception):
    pass


async def _one_satpos_tick(sched, monkeypatch) -> list[tuple[str, dict]]:
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(hub, "publish", lambda t, d: captured.append((t, d)))

    async def stop(*_a, **_k):
        raise StopLoop

    # Only the scheduler's sleep: the loop body runs once, then stops.
    monkeypatch.setattr(scheduler_mod, "asyncio", SimpleNamespace(sleep=stop))
    with pytest.raises(StopLoop):
        await sched._satpos_loop()
    return [d for t, d in captured if t == "pass_next"]


def _scheduler(predictor) -> Scheduler:
    return Scheduler(SETTINGS, SimpleNamespace(), predictor)


async def test_pass_next_names_its_satellite_even_with_no_pass(monkeypatch):
    """Today every browser applied an unlabelled pass_next — so an operator
    who selected ISS had the card snap back to KNACKSAT-2 within 5 s."""
    sched = _scheduler(SchedPredictor())
    (frame,) = await _one_satpos_tick(sched, monkeypatch)
    assert frame == {"norad": KN}


def _pass(norad, minutes) -> Pass:
    aos = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return Pass(pass_id=f"{norad}-{int(aos.timestamp())}", norad=norad,
                name=NAMES[norad], aos=aos, tca=aos + timedelta(minutes=4),
                los=aos + timedelta(minutes=8), duration_s=480.0, max_el=50.0,
                aos_az=10.0, los_az=200.0)


async def test_pass_next_follows_a_catalogued_focus(monkeypatch):
    sched = _scheduler(SchedPredictor({KN: _pass(KN, 90), ISS: _pass(ISS, 5)}))
    sched.antenna_last = {"focus_norad": ISS, "focus_source": "catalogue"}
    (frame,) = await _one_satpos_tick(sched, monkeypatch)
    assert frame["norad"] == ISS and frame["pass_id"].startswith(f"{ISS}-")


async def test_pass_next_stays_on_the_default_for_a_focus_it_cannot_draw(monkeypatch):
    sched = _scheduler(SchedPredictor({KN: _pass(KN, 90)}))
    sched.antenna_last = {"focus_norad": TEMP, "focus_source": "job_tle",
                          "focus_job_id": 5}
    (frame,) = await _one_satpos_tick(sched, monkeypatch)
    assert frame["norad"] == KN


def test_rotator_measures_against_the_schedulers_focus():
    sched = _scheduler(SchedPredictor())
    assert sched.rotator.focus == sched._focus
    assert sched._focus() == (KN, None)          # nothing computed yet
    sched.antenna_last = {"focus_norad": ISS, "focus_source": "catalogue"}
    assert sched._focus() == (ISS, None)


def test_focus_builds_a_jobs_satellite_once_per_job():
    from skyfield.api import load
    predictor = SchedPredictor()
    predictor.ts = load.timescale()
    sched = _scheduler(predictor)
    end = datetime.now(timezone.utc) + timedelta(minutes=8)
    sched.antenna_tles.update([{"id": 555, "norad_cat_id": TEMP, "tle0": "TEMP OBJECT",
                                "tle1": TLE1, "tle2": TLE2, "end": end.isoformat()}])
    sched.antenna_last = {"focus_norad": TEMP, "focus_source": "job_tle",
                          "focus_job_id": 555}
    norad, first = sched._focus()
    _, second = sched._focus()
    assert norad == TEMP and isinstance(first, EarthSatellite)
    assert first is second


def _fresh_satnogs(sched, connected: bool) -> None:
    sched.satnogs.station = {"id": 5024, "is_connected": connected}
    sched.satnogs._station_at = time.monotonic()
    sched.satnogs._jobs_at = time.monotonic()


def test_antenna_frame_is_published_on_change_only(monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(hub, "publish", lambda t, d: captured.append((t, d)))
    sched = _scheduler(SchedPredictor())
    _fresh_satnogs(sched, connected=False)

    sched._publish_antenna()
    sched._publish_antenna()                    # only the clock moved
    _fresh_satnogs(sched, connected=True)
    sched._publish_antenna()

    sent = [d for t, d in captured if t == "antenna"]
    assert [d["owner"] for d in sent] == ["none", "satnogs"]
    assert sched.antenna_last["standby"] is True


def test_antenna_frames_pass_the_wire_contract():
    Frame(type="antenna", data={"owner": "none"})


async def test_route_returns_the_last_state_or_computes_one():
    computed = {"owner": "none", "activity": "idle"}
    sched = SimpleNamespace(antenna_last=None, compute_antenna=lambda: computed)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(scheduler=sched)))
    assert await rotator_routes.antenna(request) == computed
    sched.antenna_last = {"owner": "satnogs"}
    assert await rotator_routes.antenna(request) == {"owner": "satnogs"}


# --------------------------------------------------------------------------
# display only
# --------------------------------------------------------------------------

@pytest.mark.parametrize("module", ["control.py", "planner_service.py"])
def test_nothing_that_decides_imports_the_display(module):
    """Ownership is a sentence for a person. If the interlock or the executor
    ever read it, "SatNOGS has the antenna" would become a reason to move or
    not to — computed from the same stale data the gates already refuse."""
    path = Path(__file__).resolve().parent.parent / "app" / "services" / module
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "antenna" not in (node.module or ""), module
            assert all("antenna" not in a.name for a in node.names), module
        elif isinstance(node, ast.Import):
            assert all("antenna" not in a.name for a in node.names), module
