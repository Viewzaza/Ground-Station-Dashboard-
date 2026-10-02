"""Autopilot against the real ControlService.

The first version of these tests drove the executor through a hand-written
fake of ControlService, and every bug a later safety review found passed them:
the fake had no track loop to end a track on a closing gate, no `finally` to
clobber the mode, and no way for an operator to issue the same kind of command
autopilot had. So these run the real interlock, the real track loop and a
recording rotator client, and each test names the review scenario it pins.

Nothing here can move hardware: the "rotator" records the commands it is sent.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import app.services.control as control_mod
from app.config import Settings
from app.schemas import RotatorSample
from app.services.control import ControlService
from app.services.planner import Candidate, Plan, SlewModel
from app.services.planner_service import AutopilotRefused, PlanExecutor
from app.services.rotctld_client import Caps

NORAD_A, NORAD_B = 67683, 25544


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

class RecordingClient:
    """Stands where rotctld would. Records; honours the under-lock guard."""

    def __init__(self) -> None:
        self.caps = Caps(model=901, name="SPID Rot2Prog",
                         min_az=-180.0, max_az=540.0, min_el=-20.0, max_el=210.0,
                         is_rotator=True)
        self.commands: list[tuple] = []
        self.before_write = None        # hook: runs before the guard

    def limits(self):
        return (-90.0, 450.0, 0.0, 100.0)

    async def set_position(self, az, el, guard=None):
        if self.before_write is not None:
            self.before_write()
        if guard is not None:
            guard()
        self.commands.append(("set_pos", round(az, 1), round(el, 1)))

    async def stop(self):
        self.commands.append(("stop",))

    def moves(self):
        return [c for c in self.commands if c[0] == "set_pos"]


class Rotator:
    def __init__(self) -> None:
        self.verified = True
        self.client = RecordingClient()
        self.last = RotatorSample(az_raw=0.0, el=0.0, az_rose=0.0, source="mock")


class Satnogs:
    """The interlock's view of SatNOGS: idle and fresh unless told otherwise."""

    def __init__(self) -> None:
        self.connected = False
        self.next_job_s: float | None = None
        self.jobs: list = []
        self.observations: list = []
        self.commitments: list = []

    @property
    def is_connected(self):
        return self.connected

    @property
    def station_age_s(self):
        return 1.0

    @property
    def jobs_age_s(self):
        return 1.0

    def seconds_to_next_job(self, now=None):
        return self.next_job_s


class Pos:
    def __init__(self, az, el):
        self.az, self.el = az, el


class Predictor:
    def __init__(self) -> None:
        self.el = 30.0

    def satellite(self, norad):
        return object()

    def position(self, norad, when=None):
        return Pos(150.0 if norad == NORAD_A else 250.0, self.el)


class Planner:
    def __init__(self, cands, schedule_known=True) -> None:
        self.plan = Plan(built_at=NOW, horizon_h=24, planned=list(cands),
                         schedule_known=schedule_known)
        self.slew = SlewModel(az_rate_deg_s=2.0, el_rate_deg_s=2.0, setup_s=30.0,
                              min_az=-90.0, max_az=450.0)


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def cand(key, norad, aos, minutes=8, aos_az=120.0, start_bearing=None) -> Candidate:
    return Candidate(key=key, norad=norad, name=key,
                     aos=aos, tca=aos + timedelta(minutes=minutes / 2),
                     los=aos + timedelta(minutes=minutes),
                     max_el=45.0, aos_az=aos_az, los_az=300.0,
                     priority=1.0, score=1.0,
                     start_bearing=aos_az if start_bearing is None else start_bearing)


@pytest.fixture(autouse=True)
def fast_track_loop(monkeypatch):
    monkeypatch.setattr(control_mod, "TRACK_STEP_S", 0.01)


class Rig:
    def __init__(self, cands, **settings_kw):
        base = dict(rotator_control_enabled=True, control_lease_s=900,
                    gate_guard_s=300, gate_max_stale_s=120,
                    planner_lead_s=90, planner_setup_s=30,
                    rotator_az_rate_deg_s=2.0, rotator_el_rate_deg_s=2.0,
                    track_deadband_deg=0.5)
        self.s = Settings(**{**base, **settings_kw})
        self.rot = Rotator()
        self.satnogs = Satnogs()
        self.pred = Predictor()
        self.control = ControlService(self.s, self.rot, self.satnogs, self.pred)
        self.planner = Planner(cands)
        self.ex = PlanExecutor(self.s, self.planner, self.control, self.rot)

    async def settle(self, seconds=0.05):
        await asyncio.sleep(seconds)

    async def cleanup(self):
        task = self.control._track_task
        self.control._stop_track()
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


@pytest.fixture
async def make_rig():
    # Async, so track tasks are cancelled and awaited on the test's own loop
    # rather than after it has closed.
    rigs = []

    def _make(cands, **kw):
        rig = Rig(cands, **kw)
        rigs.append(rig)
        return rig

    yield _make
    for rig in rigs:
        await rig.cleanup()


# --------------------------------------------------------------------------
# consent
# --------------------------------------------------------------------------

def test_cannot_engage_without_a_lease(make_rig):
    rig = make_rig([])
    with pytest.raises(AutopilotRefused, match="arm"):
        rig.ex.enable()


def test_cannot_engage_with_control_switched_off(make_rig):
    rig = make_rig([], rotator_control_enabled=False)
    with pytest.raises(AutopilotRefused):
        rig.ex.enable()


@pytest.mark.asyncio
async def test_never_arms_or_extends_a_lease(make_rig):
    aos = NOW + timedelta(minutes=1)
    rig = make_rig([cand("p", NORAD_A, aos)])
    rig.control.arm()
    expiry = rig.control._lease_expires
    rig.control.arm = lambda *a, **k: pytest.fail("autopilot armed control")
    rig.ex.enable()
    for sec in range(0, 600, 10):
        await rig.ex.step(aos + timedelta(seconds=sec - 120))
    assert rig.control._lease_expires == expiry


@pytest.mark.asyncio
async def test_lease_expiry_mid_track_stops_the_antenna_and_disengages(make_rig):
    """Review #5: when the lease lapsed, the track loop exited on the closed
    gate *without* a stop, and the executor then saw idle — so neither stopped
    the antenna, and it finished whatever move it had last been given."""
    aos = NOW - timedelta(seconds=30)
    rig = make_rig([cand("p", NORAD_A, aos, minutes=10)])
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(NOW)
    await rig.settle()
    assert rig.control.state().mode == "track"

    rig.control._lease_expires = datetime.now(timezone.utc) - timedelta(seconds=1)
    await rig.settle()
    assert ("stop",) in rig.rot.client.commands, "track loop must stop on lost consent"

    await rig.ex.step(NOW + timedelta(seconds=2))
    assert not rig.ex.state.enabled
    assert "lease" in rig.ex.state.disengaged_because


# --------------------------------------------------------------------------
# the sequence
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_waits_positions_tracks_and_stops(make_rig):
    aos = NOW + timedelta(minutes=10)
    p = cand("p", NORAD_A, aos, aos_az=120.0)
    rig = make_rig([p])
    rig.control.arm()
    rig.ex.enable()

    await rig.ex.step(NOW)
    assert rig.ex.state.phase == "waiting"
    assert rig.rot.client.commands == []

    await rig.ex.step(aos - timedelta(seconds=80))
    assert rig.ex.state.phase == "positioning"
    assert rig.rot.client.moves() == [("set_pos", 120.0, 0.0)]

    await rig.ex.step(aos - timedelta(seconds=40))
    assert rig.rot.client.moves() == [("set_pos", 120.0, 0.0)], "goto once"

    await rig.ex.step(aos + timedelta(seconds=1))
    assert rig.ex.state.phase == "tracking"
    await rig.settle()
    assert rig.control.state().mode == "track"
    assert rig.control.state().target_norad == NORAD_A
    assert rig.control.last_origin == "autopilot"

    await rig.ex.step(p.los + timedelta(seconds=1))
    assert rig.rot.client.commands[-1] == ("stop",)
    assert rig.control.state().mode == "idle"
    assert rig.ex.state.enabled


@pytest.mark.asyncio
async def test_drives_to_the_bearing_the_plan_was_costed_on(make_rig):
    """The DAG chose 470 for this pass (it rises at 110 but the sequence needs
    the other branch). Autopilot must go to 470, not re-derive 110."""
    aos = NOW + timedelta(minutes=5)
    rig = make_rig([cand("p", NORAD_A, aos, aos_az=110.0, start_bearing=470.0 - 360.0 + 360.0)])
    rig.planner.plan.planned[0] = Candidate(**{**rig.planner.plan.planned[0].__dict__,
                                              "start_bearing": 430.0})
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(aos - timedelta(seconds=60))
    assert rig.rot.client.moves() == [("set_pos", 430.0, 0.0)]


@pytest.mark.asyncio
async def test_lead_time_covers_a_long_slew(make_rig):
    """From 0° to 180° at 2°/s is 90 s + 4 s ramp + 30 s setup = 124 s, more
    than the 90 s configured lead."""
    aos = NOW + timedelta(minutes=10)
    rig = make_rig([cand("p", NORAD_A, aos, aos_az=180.0)])
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(aos - timedelta(seconds=110))
    assert ("set_pos", 180.0, 0.0) in rig.rot.client.moves()


@pytest.mark.asyncio
async def test_back_to_back_passes(make_rig):
    a = cand("a", NORAD_A, NOW + timedelta(minutes=5), minutes=6, aos_az=90.0)
    b = cand("b", NORAD_B, a.los + timedelta(minutes=6), minutes=6, aos_az=200.0)
    rig = make_rig([a, b])
    rig.control.arm()
    rig.ex.enable()
    targets = []
    for sec in range(0, 60 * 30, 5):
        await rig.ex.step(NOW + timedelta(seconds=sec))
        st = rig.control.state()
        if st.mode == "track" and (not targets or targets[-1] != st.target_norad):
            targets.append(st.target_norad)
    assert targets == [NORAD_A, NORAD_B]
    assert rig.ex.state.enabled


# --------------------------------------------------------------------------
# the interlock still decides
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_closed_gate_blocks_without_disengaging(make_rig):
    aos = NOW + timedelta(minutes=1)
    rig = make_rig([cand("p", NORAD_A, aos)])
    rig.control.arm()
    rig.ex.enable()
    rig.satnogs.connected = True
    await rig.ex.step(aos + timedelta(seconds=5))
    assert rig.ex.state.enabled
    assert rig.ex.state.phase == "blocked"
    assert "satnogs_idle" in rig.ex.state.detail
    assert rig.rot.client.commands == []


@pytest.mark.asyncio
async def test_a_short_gate_flap_is_not_mistaken_for_an_operator_stop(make_rig):
    """Review R6. The gate closed and reopened between executor steps; the real
    track loop caught it and ended the track. The executor saw "idle with clear
    gates" and concluded a human had pressed STOP. It must resume instead."""
    aos = NOW - timedelta(seconds=10)
    rig = make_rig([cand("p", NORAD_A, aos, minutes=10)])
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(NOW)
    await rig.settle()
    first_track = rig.control.track_id

    rig.satnogs.connected = True          # gate closes...
    await rig.settle()                    # ...the real loop ends the track...
    assert rig.control.state().mode == "idle"
    rig.satnogs.connected = False         # ...and reopens before the next step

    await rig.ex.step(NOW + timedelta(seconds=1))
    assert rig.ex.state.enabled, rig.ex.state.disengaged_because
    assert rig.control.state().mode == "track"
    assert rig.control.track_id > first_track, "resumed with a new track"


@pytest.mark.asyncio
async def test_a_gate_that_closes_while_the_command_queues_is_a_refusal(make_rig):
    """The interlock is re-checked under the rotctld lock. A gate that closes
    after the executor checked it must turn the write into a refusal — and the
    executor must treat that as "blocked", not crash and disengage."""
    aos = NOW + timedelta(minutes=5)
    rig = make_rig([cand("p", NORAD_A, aos)])
    rig.control.arm()
    rig.ex.enable()

    def close_gate():
        rig.satnogs.connected = True
    rig.rot.client.before_write = close_gate

    await rig.ex.step(aos - timedelta(seconds=60))
    assert rig.rot.client.moves() == [], "no byte written through a closed gate"
    assert rig.ex.state.enabled
    assert rig.ex.state.phase == "blocked"


# --------------------------------------------------------------------------
# the operator always wins
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_operator_stop_mid_track_disengages_and_is_not_overridden(make_rig):
    aos = NOW - timedelta(seconds=10)
    rig = make_rig([cand("p", NORAD_A, aos, minutes=10)])
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(NOW)
    await rig.settle()

    await rig.control.stop()                              # operator
    await rig.ex.step(NOW + timedelta(seconds=1))
    assert not rig.ex.state.enabled
    assert "operator" in rig.ex.state.disengaged_because
    await rig.ex.step(NOW + timedelta(seconds=2))
    assert rig.control.state().mode == "idle"


@pytest.mark.asyncio
async def test_operator_stow_while_waiting_is_not_dragged_away(make_rig):
    """Review R3. The operator stowed the antenna while autopilot waited; at the
    lead time autopilot used to drive it off to the pass's start bearing."""
    aos = NOW + timedelta(minutes=10)
    rig = make_rig([cand("p", NORAD_A, aos, aos_az=200.0)])
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(NOW)

    await rig.control.goto(10.0, 5.0)                     # operator stows
    await rig.ex.step(aos - timedelta(seconds=60))
    assert not rig.ex.state.enabled
    assert rig.rot.client.moves() == [("set_pos", 10.0, 5.0)]


@pytest.mark.asyncio
async def test_operator_park_after_pre_position_is_respected(make_rig):
    """Review R4. Same mode before and after ("manual"), so mode-watching could
    not see it; at AOS autopilot started tracking anyway."""
    aos = NOW + timedelta(minutes=2)
    rig = make_rig([cand("p", NORAD_A, aos)])
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(aos - timedelta(seconds=60))         # autopilot pre-positions
    await rig.control.park()                                # operator parks
    await rig.ex.step(aos + timedelta(seconds=1))
    assert not rig.ex.state.enabled
    assert rig.control.state().mode != "track"


@pytest.mark.asyncio
async def test_operator_stop_while_blocked_is_still_seen(make_rig):
    """Review R5. A gate was closed, the operator pressed STOP; when the gate
    reopened autopilot re-issued the track."""
    aos = NOW - timedelta(seconds=10)
    rig = make_rig([cand("p", NORAD_A, aos, minutes=10)])
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(NOW)
    await rig.settle()

    rig.satnogs.connected = True
    await rig.settle()
    await rig.ex.step(NOW + timedelta(seconds=1))
    assert rig.ex.state.phase == "blocked"

    await rig.control.stop()                               # operator, during the block
    rig.satnogs.connected = False
    await rig.ex.step(NOW + timedelta(seconds=2))
    assert not rig.ex.state.enabled
    assert rig.control.state().mode == "idle"


@pytest.mark.asyncio
async def test_engaging_twice_does_not_blind_it_to_the_operator(make_rig):
    """Review R15. A second enable() mid-track reset what autopilot believed it
    had last done, and the operator's next STOP was overridden within a second."""
    aos = NOW - timedelta(seconds=10)
    rig = make_rig([cand("p", NORAD_A, aos, minutes=10)])
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(NOW)
    rig.ex.enable()                                        # double click
    await rig.control.stop()                               # operator
    await rig.ex.step(NOW + timedelta(seconds=1))
    assert not rig.ex.state.enabled


@pytest.mark.asyncio
async def test_release_then_rearm_between_steps_disengages(make_rig):
    """Review #11. Release revokes consent; a re-arm before the next step must
    not hide that."""
    aos = NOW + timedelta(minutes=10)
    rig = make_rig([cand("p", NORAD_A, aos)])
    rig.control.arm()
    rig.ex.enable()
    rig.control.release()
    rig.control.arm()
    await rig.ex.step(NOW)
    assert not rig.ex.state.enabled


@pytest.mark.asyncio
async def test_extending_the_lease_does_not_disengage(make_rig):
    aos = NOW + timedelta(minutes=10)
    rig = make_rig([cand("p", NORAD_A, aos)])
    rig.control.arm()
    rig.ex.enable()
    rig.control.arm()                                      # extend, not revoke
    await rig.ex.step(NOW)
    assert rig.ex.state.enabled


# --------------------------------------------------------------------------
# ownership
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_track_over_track_leaves_no_orphan(make_rig):
    """Review #2, the root cause. track() cancelled the old task without
    awaiting it; the old task's finally then saw mode == "track" — set by the
    new track — and reset it to idle while the new task kept driving."""
    rig = make_rig([])
    rig.control.arm()
    await rig.control.track(NORAD_A)
    await rig.settle()
    await rig.control.track(NORAD_B)
    await rig.settle()
    st = rig.control.state()
    assert st.mode == "track"
    assert st.target_norad == NORAD_B


@pytest.mark.asyncio
async def test_switching_satellites_mid_pass_keeps_control_truthful(make_rig):
    """Review R2. A rebuilt plan puts an overlapping pass of another satellite
    first; autopilot tracks it. Control must report that track, not idle, and
    autopilot must not then disengage over a phantom "operator stop"."""
    a = cand("a", NORAD_A, NOW - timedelta(seconds=30), minutes=10)
    rig = make_rig([a])
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(NOW)
    await rig.settle()

    b = cand("b", NORAD_B, NOW - timedelta(seconds=20), minutes=10)
    rig.planner.plan = Plan(built_at=NOW, horizon_h=24, planned=[b])
    await rig.ex.step(NOW + timedelta(seconds=1))
    await rig.settle()
    await rig.ex.step(NOW + timedelta(seconds=2))
    assert rig.ex.state.enabled, rig.ex.state.disengaged_because
    st = rig.control.state()
    assert st.mode == "track" and st.target_norad == NORAD_B


@pytest.mark.asyncio
async def test_a_pass_dropped_from_the_plan_is_stopped(make_rig):
    """Review #6. A rebuild rejected the pass being tracked (a new SatNOGS job
    reserves it); autopilot kept tracking it until its own LOS."""
    a = cand("a", NORAD_A, NOW - timedelta(seconds=30), minutes=10)
    rig = make_rig([a])
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(NOW)
    await rig.settle()

    rig.planner.plan = Plan(built_at=NOW, horizon_h=24, planned=[])
    await rig.ex.step(NOW + timedelta(seconds=1))
    assert rig.rot.client.commands[-1] == ("stop",)
    assert rig.control.state().mode == "idle"


@pytest.mark.asyncio
async def test_autopilot_never_stops_an_operators_track(make_rig):
    """Review R10. A stale owned-track reference made disengage kill the
    operator's own track."""
    a = cand("a", NORAD_A, NOW - timedelta(seconds=30), minutes=10)
    b = cand("b", NORAD_B, NOW + timedelta(seconds=60), minutes=8, aos_az=250.0)
    rig = make_rig([a, b])
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(NOW)
    await rig.settle()

    rig.planner.plan = Plan(built_at=NOW, horizon_h=24, planned=[b])  # A dropped
    await rig.ex.step(NOW + timedelta(seconds=1))                     # stop A, goto B
    stops_before = rig.rot.client.commands.count(("stop",))

    await rig.control.track(NORAD_A)                                  # operator's own
    await rig.ex.step(NOW + timedelta(seconds=2))
    assert not rig.ex.state.enabled
    assert rig.rot.client.commands.count(("stop",)) == stops_before
    assert rig.control.state().mode == "track"


@pytest.mark.asyncio
async def test_disengaging_during_a_pre_position_slew_stops_it(make_rig):
    """Review #5. disable() only stopped a *track*, so a minutes-long
    pre-position slew carried on after autopilot was switched off."""
    aos = NOW + timedelta(minutes=2)
    rig = make_rig([cand("p", NORAD_A, aos)])
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(aos - timedelta(seconds=60))
    assert rig.control.state().mode == "manual"
    await rig.ex.disable()
    assert rig.rot.client.commands[-1] == ("stop",)


@pytest.mark.asyncio
async def test_disengaging_after_the_operator_acted_leaves_their_command(make_rig):
    aos = NOW + timedelta(minutes=2)
    rig = make_rig([cand("p", NORAD_A, aos)])
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(aos - timedelta(seconds=60))
    await rig.control.goto(42.0, 10.0)                      # operator
    await rig.ex.disable()
    assert rig.rot.client.commands[-1] == ("set_pos", 42.0, 10.0)


# --------------------------------------------------------------------------
# failure handling
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_internal_error_disengages_even_if_cleanup_fails(make_rig):
    """Review #8. disable() called control.state() first; if that raised too,
    run() died, the supervisor restarted it, and autopilot was still on."""
    rig = make_rig([cand("p", NORAD_A, NOW + timedelta(minutes=5))])
    rig.control.arm()
    rig.ex.enable()

    async def boom(now=None):
        raise RuntimeError("step exploded")
    rig.ex.step = boom
    rig.control.state = lambda: (_ for _ in ()).throw(RuntimeError("state exploded"))

    task = asyncio.create_task(rig.ex.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not rig.ex.state.enabled
    assert "internal error" in rig.ex.state.disengaged_because


@pytest.mark.asyncio
async def test_an_empty_plan_does_not_skip_the_interlock(make_rig):
    """Review R12. The empty-plan return came before the blocked check, so a
    reopening gate was later read as an operator STOP."""
    rig = make_rig([])
    rig.control.arm()
    rig.ex.enable()
    rig.satnogs.connected = True
    await rig.ex.step(NOW)
    assert rig.ex.state.phase == "blocked"
    rig.satnogs.connected = False
    await rig.ex.step(NOW + timedelta(seconds=1))
    assert rig.ex.state.enabled
    assert rig.ex.state.phase == "idle"


@pytest.mark.asyncio
async def test_a_plan_built_before_the_schedule_loaded_is_not_acted_on(make_rig):
    """Review #6. At startup the plan is built from an empty job list before
    the first SatNOGS poll — unchecked, not conflict-free."""
    rig = make_rig([cand("p", NORAD_A, NOW + timedelta(seconds=30))])
    rig.planner.plan.schedule_known = False
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(NOW)
    assert rig.ex.state.phase == "blocked"
    assert "schedule" in rig.ex.state.detail
    assert rig.rot.client.commands == []


@pytest.mark.asyncio
async def test_a_rotator_link_down_is_blocked_not_fatal(make_rig):
    aos = NOW + timedelta(minutes=2)
    rig = make_rig([cand("p", NORAD_A, aos)])
    rig.control.arm()
    rig.ex.enable()
    rig.rot.last = RotatorSample(az_raw=0.0, el=0.0, az_rose=0.0,
                                 source="mock", link="down")
    await rig.ex.step(aos - timedelta(seconds=60))
    assert rig.ex.state.enabled
    assert rig.ex.state.phase == "blocked"
    assert "link" in rig.ex.state.detail
