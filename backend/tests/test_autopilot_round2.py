"""Second adversarial round: every reproduced finding, pinned.

A multi-agent review of the first round of fixes raised 18 findings and every
one reproduced. They reduce to a handful of causes, and the worst were all
about *when* things happen relative to an await — the review's repros used a
rotator client whose writes take real time, the way a 600-baud SPID's do, and
that is what these tests do too.

Same harness as test_autopilot.py: the real ControlService and track loop, a
recording client, nothing that can move hardware.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone

import pytest

import app.services.control as control_mod
from app.config import Settings
from app.schemas import RotatorSample
from app.services.control import ControlRefused, ControlService
from app.services.planner import Candidate, Plan, SlewModel, select
from app.services.planner_service import PlanExecutor
from app.services.rotctld_client import Caps, RotctldError, RotctldClient

NORAD_A, NORAD_B = 67683, 25544
NOW = datetime.now(timezone.utc).replace(microsecond=0)


class SlowClient:
    """A rotator whose set_pos takes `delay` seconds, like a SPID's reply."""

    def __init__(self, delay: float = 0.0) -> None:
        self.caps = Caps(model=901, name="SPID Rot2Prog",
                         min_az=-180.0, max_az=540.0, min_el=-20.0, max_el=210.0,
                         is_rotator=True)
        self.delay = delay
        self.commands: list[tuple] = []
        self.fail_stop = False
        self.fail_set = False

    def limits(self):
        return (-90.0, 450.0, 0.0, 100.0)

    async def set_position(self, az, el, guard=None):
        if guard is not None:
            guard()
        if not (math.isfinite(az) and math.isfinite(el)):
            raise RotctldError(-1, "non-finite")
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_set:
            raise RotctldError(-5, "set_pos timed out")
        self.commands.append(("set_pos", round(az, 1), round(el, 1)))

    async def stop(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_stop:
            raise RotctldError(-5, "stop timed out")
        self.commands.append(("stop",))

    def moves(self):
        return [c for c in self.commands if c[0] == "set_pos"]


class Rotator:
    def __init__(self, client) -> None:
        self.verified = True
        self.client = client
        self.last = RotatorSample(az_raw=0.0, el=0.0, az_rose=0.0, source="mock")


class Satnogs:
    def __init__(self) -> None:
        self.connected = False
        self.jobs, self.observations, self.commitments = [], [], []

    @property
    def is_connected(self):
        return self.connected

    station_age_s = property(lambda self: 1.0)
    jobs_age_s = property(lambda self: 1.0)

    def seconds_to_next_job(self, now=None):
        return None


class Pos:
    def __init__(self, az, el):
        self.az, self.el = az, el


class Predictor:
    def __init__(self) -> None:
        self.az = None

    def satellite(self, norad):
        return object()

    def position(self, norad, when=None):
        if self.az is not None:
            return Pos(self.az, self.az)
        return Pos(150.0 if norad == NORAD_A else 250.0, 30.0)


class Planner:
    def __init__(self, cands):
        self.plan = Plan(built_at=NOW, horizon_h=24, planned=list(cands))
        self.slew = SlewModel(az_rate_deg_s=2.0, el_rate_deg_s=2.0, setup_s=30.0,
                              min_az=-90.0, max_az=450.0)


def cand(key, norad, aos, minutes=8, aos_az=120.0) -> Candidate:
    return Candidate(key=key, norad=norad, name=key, aos=aos,
                     tca=aos + timedelta(minutes=minutes / 2),
                     los=aos + timedelta(minutes=minutes),
                     max_el=45.0, aos_az=aos_az, los_az=300.0,
                     priority=1.0, score=1.0, start_bearing=aos_az)


@pytest.fixture(autouse=True)
def fast_track_loop(monkeypatch):
    monkeypatch.setattr(control_mod, "TRACK_STEP_S", 0.01)


class Rig:
    def __init__(self, cands, delay=0.0):
        self.s = Settings(rotator_control_enabled=True, control_lease_s=900,
                          gate_guard_s=300, gate_max_stale_s=120,
                          planner_lead_s=90, planner_setup_s=30,
                          rotator_az_rate_deg_s=2.0, rotator_el_rate_deg_s=2.0,
                          track_deadband_deg=0.5)
        self.client = SlowClient(delay)
        self.rot = Rotator(self.client)
        self.satnogs = Satnogs()
        self.pred = Predictor()
        self.control = ControlService(self.s, self.rot, self.satnogs, self.pred)
        self.planner = Planner(cands)
        self.ex = PlanExecutor(self.s, self.planner, self.control, self.rot)


@pytest.fixture
async def make_rig():
    rigs = []

    def _make(cands, **kw):
        rig = Rig(cands, **kw)
        rigs.append(rig)
        return rig

    yield _make
    for rig in rigs:
        task = rig.control._track_task
        rig.control._stop_track()
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


# --------------------------------------------------------------------------
# 1. journal attribution across awaits
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_operator_stop_during_autopilots_slow_goto_is_seen(make_rig):
    """The headline finding. Autopilot's pre-position goto waits on the SPID;
    the operator presses STOP meanwhile. Autopilot used to re-read the journal
    after its await and adopt the operator's entry as its own — then track the
    pass at AOS regardless."""
    aos = NOW + timedelta(minutes=2)
    rig = make_rig([cand("p", NORAD_A, aos)], delay=0.2)
    rig.control.arm()
    rig.ex.enable()

    step = asyncio.create_task(rig.ex.step(aos - timedelta(seconds=60)))
    await asyncio.sleep(0.05)                       # goto is in flight
    await rig.control.stop()                        # operator
    await step

    await rig.ex.step(aos + timedelta(seconds=1))
    assert not rig.ex.state.enabled
    assert "operator" in rig.ex.state.disengaged_because
    assert rig.control.state().mode != "track"


@pytest.mark.asyncio
async def test_operator_track_during_autopilots_goto_is_not_stopped_later(make_rig):
    """Review B: an operator track started while autopilot's goto was in
    flight was later stopped by autopilot's disable(), which believed the
    latest command was its own."""
    aos = NOW + timedelta(minutes=2)
    rig = make_rig([cand("p", NORAD_A, aos)], delay=0.2)
    rig.control.arm()
    rig.ex.enable()

    step = asyncio.create_task(rig.ex.step(aos - timedelta(seconds=60)))
    await asyncio.sleep(0.05)
    await rig.control.track(NORAD_B)                 # operator
    await step
    stops = rig.client.commands.count(("stop",))
    await rig.ex.disable()
    assert rig.client.commands.count(("stop",)) == stops
    assert rig.control.state().mode == "track"
    assert rig.control.state().target_norad == NORAD_B


@pytest.mark.asyncio
async def test_goto_does_not_overwrite_the_mode_of_a_track_started_during_it(make_rig):
    """goto used to set mode="manual" after its await, so a track started
    while the goto was in flight ran on with control reporting "manual"."""
    rig = make_rig([], delay=0.2)
    rig.control.arm()
    goto = asyncio.create_task(rig.control.goto(10.0, 10.0))
    await asyncio.sleep(0.05)
    await rig.control.track(NORAD_B)
    await goto
    st = rig.control.state()
    assert st.mode == "track" and st.target_norad == NORAD_B


@pytest.mark.asyncio
async def test_an_operator_command_between_two_autopilot_commands_in_one_step(make_rig):
    """Review C: one step issues the LOS stop for one pass, then the goto for
    the next. An operator track landing during the stop's await was then
    overridden by the goto in the same step."""
    a = cand("a", NORAD_A, NOW - timedelta(minutes=8), minutes=8)        # just ended
    b = cand("b", NORAD_B, NOW + timedelta(seconds=40), aos_az=200.0)    # inside lead
    rig = make_rig([a, b], delay=0.2)
    rig.control.arm()
    rig.ex.enable()
    # Autopilot owns a's track.
    rig.planner.plan = Plan(built_at=NOW, horizon_h=24, planned=[cand(
        "a", NORAD_A, NOW - timedelta(minutes=7), minutes=8), b])
    await rig.ex.step(NOW - timedelta(seconds=30))
    await asyncio.sleep(0.3)
    assert rig.control.state().mode == "track"

    step = asyncio.create_task(rig.ex.step(NOW + timedelta(minutes=2)))   # past a's LOS
    await asyncio.sleep(0.05)                        # autopilot's stop in flight
    await rig.control.track(NORAD_A)                 # operator
    await step
    assert not rig.ex.state.enabled
    assert ("set_pos", 200.0, 0.0) not in rig.client.moves(), "no goto over the operator"
    assert rig.control.state().mode == "track"


@pytest.mark.asyncio
async def test_autopilots_own_failed_stop_is_not_read_as_intervention(make_rig):
    """Review E: stop journals before its write; when autopilot's LOS stop
    failed, autopilot never recorded the entry and then disengaged blaming
    "autopilot took control"."""
    p = cand("p", NORAD_A, NOW - timedelta(minutes=8), minutes=8)
    rig = make_rig([p])
    rig.control.arm()
    rig.ex.enable()
    rig.planner.plan = Plan(built_at=NOW, horizon_h=24,
                            planned=[cand("p", NORAD_A, NOW - timedelta(minutes=7), minutes=8)])
    await rig.ex.step(NOW - timedelta(seconds=30))
    await asyncio.sleep(0.05)

    rig.client.fail_stop = True
    await rig.ex.step(NOW + timedelta(minutes=2))    # LOS stop fails
    await rig.ex.step(NOW + timedelta(minutes=2, seconds=1))
    assert "autopilot took control" not in rig.ex.state.disengaged_because


# --------------------------------------------------------------------------
# 2. failed writes leave an honest mode
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_failed_goto_during_a_track_leaves_mode_idle_not_track(make_rig):
    rig = make_rig([])
    rig.control.arm()
    await rig.control.track(NORAD_A)
    await asyncio.sleep(0.05)
    rig.client.fail_set = True
    with pytest.raises(RotctldError):
        await rig.control.goto(10.0, 10.0)
    st = rig.control.state()
    assert st.mode == "idle", "the track was cancelled and the goto never happened"
    assert rig.control._track_task is None or rig.control._track_task.done()


# --------------------------------------------------------------------------
# 3. release stops what is moving
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_release_stops_autopilots_pre_position_slew(make_rig):
    aos = NOW + timedelta(minutes=2)
    rig = make_rig([cand("p", NORAD_A, aos)])
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(aos - timedelta(seconds=60))
    assert rig.control.state().mode == "manual"
    await rig.control.release()
    assert rig.client.commands[-1] == ("stop",)
    await rig.ex.step(aos - timedelta(seconds=59))
    assert not rig.ex.state.enabled


@pytest.mark.asyncio
async def test_release_stops_an_operators_track(make_rig):
    rig = make_rig([])
    rig.control.arm()
    await rig.control.track(NORAD_A)
    await asyncio.sleep(0.05)
    await rig.control.release()
    await asyncio.sleep(0.05)
    assert rig.client.commands[-1] == ("stop",)
    assert rig.control.state().mode == "idle"


@pytest.mark.asyncio
async def test_release_while_idle_sends_nothing(make_rig):
    """If we were not driving, whatever is moving the rotator is not us."""
    rig = make_rig([])
    rig.control.arm()
    await rig.control.release()
    assert rig.client.commands == []


# --------------------------------------------------------------------------
# 4. NaN never reaches the rotator
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_goto_refuses_non_finite_positions(make_rig):
    rig = make_rig([])
    rig.control.arm()
    for az, el in ((math.nan, 10.0), (10.0, math.nan), (math.inf, 0.0)):
        with pytest.raises(ControlRefused, match="finite"):
            await rig.control.goto(az, el)
    assert rig.client.commands == []


@pytest.mark.asyncio
async def test_a_nan_prediction_ends_the_track_instead_of_commanding_it(make_rig):
    """Decayed elements propagate to NaN, which passes `el < 0` and every
    clamp. It used to reach rotctld as `set_pos nan nan`."""
    rig = make_rig([])
    rig.control.arm()
    rig.pred.az = math.nan
    await rig.control.track(NORAD_A)
    await asyncio.sleep(0.05)
    assert rig.client.moves() == []
    assert "non-finite" in rig.control.track_end_reason


@pytest.mark.asyncio
async def test_rotctld_client_refuses_nan_as_a_last_line():
    client = RotctldClient("127.0.0.1", 1)
    client.caps = Caps(model=901, name="x", min_az=-180, max_az=540,
                       min_el=-20, max_el=210, is_rotator=True)
    with pytest.raises(RotctldError, match="non-finite"):
        await client.set_position(math.nan, 0.0)


# --------------------------------------------------------------------------
# 5. the guard needs a rotator someone has identified
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_track_loop_stops_writing_when_the_link_goes_down(make_rig):
    """The track loop checks "can we" once, on entry. If the poll loop marks
    the link down mid-track, its next set_pos used to reconnect to rotctld on
    its own and command an unverified rotator."""
    rig = make_rig([])
    rig.control.arm()
    await rig.control.track(NORAD_A)
    await asyncio.sleep(0.05)
    before = len(rig.client.moves())
    rig.rot.last = RotatorSample(az_raw=0.0, el=0.0, az_rose=0.0,
                                 source="mock", link="down")
    rig.pred.az = None
    rig.rot.last.__dict__  # noqa: B018 — sample replaced; next iterations see it
    await asyncio.sleep(0.1)
    assert len(rig.client.moves()) == before


@pytest.mark.asyncio
async def test_the_guard_refuses_an_unidentified_rotator(make_rig):
    rig = make_rig([])
    rig.control.arm()
    rig.rot.verified = False
    with pytest.raises(ControlRefused):
        rig.control._guard()


# --------------------------------------------------------------------------
# 6. a pass is a pass, whatever its key is today
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_rebuilt_key_for_the_same_pass_does_not_stop_it(make_rig):
    """The same pass flips between two keys across rebuilds (AOS refined to
    half a second from a moving search start). Matching on key read autopilot's
    own pass as dropped and stopped the antenna mid-pass."""
    aos = NOW - timedelta(seconds=30)
    rig = make_rig([cand("67683-1000", NORAD_A, aos, minutes=10)])
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(NOW)
    await asyncio.sleep(0.05)
    tid = rig.control.track_id

    flipped = Candidate(**{**rig.planner.plan.planned[0].__dict__,
                           "key": "67683-1001",
                           "aos": aos + timedelta(seconds=1)})
    rig.planner.plan = Plan(built_at=NOW, horizon_h=24, planned=[flipped])
    await rig.ex.step(NOW + timedelta(seconds=1))
    assert ("stop",) not in rig.client.commands
    assert rig.control.track_id == tid, "same track, not stopped and restarted"


# --------------------------------------------------------------------------
# 7. planner: the antenna is where it is
# --------------------------------------------------------------------------

SLEW = SlewModel(az_rate_deg_s=1.5, el_rate_deg_s=1.5, setup_s=30.0,
                 min_az=-90.0, max_az=450.0)


def pc(key, start_s, minutes, score, aos_az, sweep=0.0, norad=1):
    aos = NOW + timedelta(seconds=start_s)
    return Candidate(key=key, norad=norad, name=key, aos=aos,
                     tca=aos + timedelta(minutes=minutes / 2),
                     los=aos + timedelta(minutes=minutes),
                     max_el=45.0, aos_az=aos_az, los_az=(aos_az + sweep) % 360,
                     priority=1.0, score=score, az_sweep=sweep)


def test_a_pass_the_antenna_already_points_at_stays_planned_in_its_last_minute():
    """Review planner#0. Positioned exactly where the pass rises, a rebuild 45 s
    before AOS used to drop it: the first leg was charged the 60 s turnaround
    floor and setup, though it follows nothing."""
    p = pc("p", 45, 10, 5.0, aos_az=120.0, sweep=60.0)
    plan = select([p], slew=SLEW, now=NOW, origin_az=120.0)
    assert [c.key for c in plan.planned] == ["p"]


def test_reaching_the_first_pass_still_needs_the_travel_time():
    """Travel-only does not mean free: 180° at 1.5°/s is 120 s."""
    p = pc("p", 60, 10, 5.0, aos_az=180.0, sweep=40.0)
    plan = select([p], slew=SLEW, now=NOW, origin_az=0.0)
    assert plan.planned == []


def test_a_pass_in_progress_is_planned_on_the_branch_the_antenna_is_on():
    """Review planner#1. C1 is under way and the antenna is following it on
    the 20° branch. The plan used to put it on 380 so that C2, reachable only
    from 440, looked feasible — but the track stays on the branch nearest the
    antenna, ends at 80, and C2 is then 360° away."""
    c1 = pc("C1", -120, 6, 1.0, aos_az=20.0, sweep=60.0)
    c2 = pc("C2", 4 * 60 + 120, 6, 1.0, aos_az=80.0, sweep=-200.0)
    plan = select([c1, c2], slew=SLEW, now=NOW, origin_az=40.0)
    planned = {c.key: c for c in plan.planned}
    assert planned["C1"].start_bearing == pytest.approx(20.0)
    assert "C2" not in planned


def _oracle(cands, slew, now, origin):
    """Exhaustive search under the same rules the planner claims to follow:
    first pass reachable from origin by travel time (any branch if not yet
    started, only the antenna's own branch if under way); consecutive passes
    separated by the full turnaround; a pass whose LOS has gone is over."""
    from itertools import combinations, product

    from app.services.planner import _live_branches, reach_s, sweep_of, turn_s

    best = 0.0
    ordered = sorted((c for c in cands if c.los > now), key=lambda c: c.aos)
    for r in range(1, len(ordered) + 1):
        for subset in combinations(ordered, r):
            if any(a.los >= b.aos for a, b in zip(subset, subset[1:])):
                continue
            opts = [_live_branches(c, slew, now, origin) for c in subset]
            if not all(opts):
                continue
            for assign in product(*opts):
                first, b0 = subset[0], assign[0]
                if origin is not None and first.aos > now:
                    if now + timedelta(seconds=reach_s(slew, origin, b0)) > first.aos:
                        continue
                if all((b.aos - a.los).total_seconds() >= turn_s(slew, ba + sweep_of(a), bb)
                       for (a, ba), (b, bb) in zip(zip(subset, assign),
                                                   zip(subset[1:], assign[1:]))):
                    best = max(best, sum(c.score for c in subset))
                    break
    return best


@pytest.mark.parametrize("seed", range(40))
def test_select_is_optimal_from_a_real_origin(seed):
    """The first oracle planned with no origin. This one fixes the antenna
    somewhere, includes passes already under way, and uses the station's real
    -90..450 limits — the cases the review's fuzzer caught out."""
    import random

    rng = random.Random(1000 + seed)
    origin = rng.uniform(-90, 450)
    cands = []
    for k in range(7):
        start = rng.uniform(-300, 3600)
        sweep = rng.choice([0.0, rng.uniform(-200, 200)])
        cands.append(pc(f"p{k}", start, rng.uniform(3, 12), round(rng.uniform(0.2, 2.0), 3),
                        aos_az=rng.uniform(0, 360), sweep=sweep))
    plan = select(cands, slew=SLEW, now=NOW, origin_az=origin)
    assert plan.total_score == pytest.approx(_oracle(cands, SLEW, NOW, origin), abs=1e-3)


def test_a_rival_that_scores_less_is_not_described_as_outscoring():
    """Review planner#2: the loser of an overlap can outscore the winner on
    its own, when the winner belongs to a better combination."""
    slew = SlewModel(az_rate_deg_s=1000.0, el_rate_deg_s=1000.0, setup_s=0.0,
                     accel_margin_s=0.0, min_turnaround_s=0.0)
    big = pc("big", 600, 20, 1.5, aos_az=0.0)
    x = pc("x", 600, 5, 1.0, aos_az=0.0)
    y = pc("y", 600 + 6 * 60, 5, 1.0, aos_az=0.0)
    plan = select([big, x, y], slew=slew, now=NOW)
    loser = next(d for d in plan.decisions if d.candidate.key == "big")
    assert loser.status == "conflict"
    assert "scores more in total" in loser.reason


# --------------------------------------------------------------------------
# 8. SatNOGS: an unexpected body is not an empty schedule
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_non_list_jobs_body_keeps_the_last_schedule_unstamped():
    from app.services.satnogs import SatnogsService

    class Resp:
        def __init__(self, d):
            self._d, self.headers = d, {}

        def raise_for_status(self):
            pass

        def json(self):
            return self._d

    class Client:
        def __init__(self, d):
            self.d = d

        async def get(self, url, params=None):
            return Resp(self.d)

    svc = SatnogsService(Settings(station_id=5024))
    start = datetime.now(timezone.utc) + timedelta(seconds=120)
    job = {"id": 1, "norad_cat_id": NORAD_A,
           "start": start.isoformat(), "end": (start + timedelta(minutes=8)).isoformat()}
    await svc.refresh_jobs(Client([job]))
    before = svc.seconds_to_next_job()
    await asyncio.sleep(0.02)
    await svc.refresh_jobs(Client({"count": 1, "results": [job]}))   # an envelope
    assert svc.jobs and svc.seconds_to_next_job() == pytest.approx(before, abs=1.0)
    assert svc.jobs_age_s >= 0.02, "an unexpected body must not stamp freshness"


# --------------------------------------------------------------------------
# 9. rotctld: a cancelled command must not leave its reply behind
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_command_cancelled_mid_read_drops_the_connection():
    """The track task is cancelled while its set_pos waits for a ~300 ms SPID
    reply. The reply used to stay on the socket, and the next command — the
    operator's STOP — read the set_pos's "RPRT 0" as its own."""
    replies = asyncio.Queue()

    async def handle(reader, writer):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                cmd = line.decode().strip()
                if "set_pos" in cmd:
                    await asyncio.sleep(0.3)
                    writer.write(b"set_pos:\nRPRT 0\n")
                elif "stop" in cmd:
                    writer.write(b"stop:\nRPRT -5\n")
                await writer.drain()
                await replies.put(cmd)
        except (ConnectionError, OSError):
            return              # the client dropped the socket mid-reply
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = RotctldClient("127.0.0.1", port)
    try:
        client.caps = Caps(model=901, name="x", min_az=-180, max_az=540,
                           min_el=-20, max_el=210, is_rotator=True)
        task = asyncio.create_task(client.set_position(10.0, 10.0))
        await asyncio.sleep(0.1)                 # written, reply not yet read
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The STOP must get *its own* answer — RPRT -5 — not the stale RPRT 0.
        with pytest.raises(RotctldError):
            await client.stop()
    finally:
        # Close our end first: since 3.12, wait_closed() waits for every
        # connection the server accepted, and an open client holds it forever.
        await client.close()
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=5)


# --------------------------------------------------------------------------
# 10. a wall display reconnecting must learn the executor's real state
# --------------------------------------------------------------------------

def _autopilot_frames(conn):
    out = []
    while not conn.queue.empty():
        f = conn.queue.get_nowait()
        if f.type == "autopilot":
            out.append(f)
    return out


@pytest.mark.asyncio
async def test_a_fresh_executor_publishes_its_state_at_start(make_rig):
    """After a deploy or a crash the new process starts disengaged, but it only
    published on a change, so the hub's snapshot had no autopilot frame and a
    reconnecting display went on showing the old process's "engaged"."""
    from app.hub import hub

    rig = make_rig([])
    conn = hub.register()
    try:
        task = asyncio.create_task(rig.ex.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        frames = _autopilot_frames(conn)
        assert frames and frames[0].data["enabled"] is False
        assert any(f["type"] == "autopilot" for f in hub.snapshot().data["frames"])
    finally:
        hub.unregister(conn)


def test_a_rekeyed_pass_is_published_though_phase_and_detail_are_unchanged(make_rig):
    """A rebuild can re-key the pass being tracked without changing "tracking"
    or its "until LOS" detail; the panel then lost which pass was autopilot's."""
    from app.hub import hub

    rig = make_rig([])
    rig.ex.state.enabled = True
    rig.ex.state.current = "67683-1000"
    rig.ex._set("tracking", "KNACKSAT-2 until 10:00:00Z")
    conn = hub.register()
    try:
        rig.ex.state.current = "67683-1001"
        rig.ex._set("tracking", "KNACKSAT-2 until 10:00:00Z")
        frames = _autopilot_frames(conn)
        assert frames and frames[-1].data["current"] == "67683-1001"
        # Nothing changed: nothing sent.
        rig.ex._set("tracking", "KNACKSAT-2 until 10:00:00Z")
        assert _autopilot_frames(conn) == []
    finally:
        hub.unregister(conn)
