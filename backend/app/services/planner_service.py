"""The planner running live, and the executor that can work its plan.

PlannerService turns predictions into a plan: it gathers every pass of every
watched satellite, treats SatNOGS's scheduled observations as reservations it
must leave alone, scores what is left and asks `planner.select` for the optimum.
It rebuilds on a timer and whenever its inputs change, and publishes the result
with a reason attached to every pass — planned, or why not.

PlanExecutor is the only part that acts on a plan, and it is deliberately
timid. It cannot do anything an operator could not do from the control panel,
because every command it issues goes through ControlService and so through all
four interlock gates. On top of that:

  * It never arms or extends a lease. Autopilot can only be switched on while
    an operator holds one, and the moment that lease lapses — expiry or
    release — autopilot switches itself off. Re-engaging is a human decision.
  * An operator always wins. Any command an operator issues — STOP, a goto,
    a park, a track, a release — disengages autopilot and is left to stand.
    That is read from ControlService's command journal, not guessed from the
    mode: a track ended by a closing gate leaves the same mode behind as one
    ended by a human, and only the journal can tell them apart.
  * It stops only motion it started, named by track id — never by "the mode
    says track".

If SatNOGS reconnects, or one of its observations comes within the guard
window, the interlock closes and ControlService's own track loop stops the
antenna. The executor sees the closed gate, reports it, and resumes when it
reopens, within the same lease.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Settings
from ..hub import hub
from .planner import (
    Candidate,
    Plan,
    Reservation,
    ScoreWeights,
    SlewModel,
    peak_pointing_error,
    score_pass,
    select,
)

log = logging.getLogger(__name__)

# Observation statuses that count as "this station heard the satellite".
HEARD = {"good"}


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def parse_priorities(raw: str) -> dict[int, float]:
    """`67683:10,25544:2` -> {67683: 10.0, 25544: 2.0}. Bad entries are skipped
    loudly rather than silently, because a typo here quietly drops a satellite
    from the plan."""
    out: dict[int, float] = {}
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        norad_s, _, weight_s = part.partition(":")
        try:
            out[int(norad_s)] = float(weight_s) if weight_s else 1.0
        except ValueError:
            log.warning("ignoring malformed planner priority %r", part)
    return out


# --------------------------------------------------------------------------
# planner
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanInputs:
    """Everything a rebuild reads from the live system, captured in one place.

    Gathered on the event loop, then handed to a worker thread. The thread
    touches only this and the predictor, never SatNOGS's or the rotator's
    mutable state — which the event loop is iterating and pruning at the same
    time.
    """
    now: datetime
    priorities: dict
    reservations: tuple
    heard: dict
    slew: SlewModel
    origin_az: float | None
    schedule_known: bool
    schedule_age_s: float | None


class PlannerService:
    # Below this peak elevation a 1.5°/s rotator keeps up with the satellite
    # comfortably, so the follower simulation is skipped.
    KEYHOLE_FROM_EL = 55.0

    def __init__(self, settings: Settings, predictor, satnogs, tles=None) -> None:
        self.s = settings
        self.predictor = predictor
        self.satnogs = satnogs
        self.tles = tles
        self.rotator = None
        self.plan: Plan | None = None
        self.weights = ScoreWeights()
        self._inputs: tuple = ()
        self._lock = asyncio.Lock()
        # Watched satellites the plan cannot see, and why. SatNOGS schedules
        # objects under temporary catalogue numbers that no public element
        # set carries; their windows are still reserved, but an operator
        # should be told the plan is blind to them rather than left to
        # wonder why they are missing.
        self.unplannable: list[dict] = []

    # --- model --------------------------------------------------------------
    @property
    def slew(self) -> SlewModel:
        min_az, max_az = -180.0, 540.0
        client = getattr(self.rotator, "client", None)
        limits = getattr(client, "limits", None)
        if callable(limits):
            try:
                min_az, max_az = limits()[:2]
            except Exception:
                log.exception("could not read rotator limits; using defaults")
        return SlewModel(
            az_rate_deg_s=self.s.rotator_az_rate_deg_s,
            el_rate_deg_s=self.s.rotator_el_rate_deg_s,
            setup_s=float(self.s.planner_setup_s),
            # The limits actually in force — the configured station limits
            # intersected with the backend's — not dump_caps's compiled range.
            # On station 5024 those differ (-90..450 against -180..540), and a
            # start bearing the clamp then moves undoes the cable-wrap logic.
            min_az=min_az,
            max_az=max_az,
        )

    def priorities(self) -> dict[int, float]:
        """What to plan for, and how much each satellite matters.

        The configured priorities, plus — at the default weight — any satellite
        SatNOGS has scheduled here, so the plan shows what SatNOGS is about to
        do rather than leaving those windows unexplained. Optionally the whole
        catalogue too.
        """
        out = parse_priorities(self.s.planner_priorities)
        out.setdefault(self.s.default_norad, max(out.values(), default=1.0))
        for job in list(self.satnogs.jobs or []):
            norad = job.get("norad_cat_id")
            if isinstance(norad, int):
                out.setdefault(norad, self.s.planner_default_priority)
        if self.s.planner_include_catalog and self.tles is not None:
            for item in list(self.tles.catalog()):
                out.setdefault(int(item["norad"]), self.s.planner_default_priority)
        return out

    def reservations(self) -> list[Reservation]:
        out = []
        # commitments, not jobs: /api/jobs/ drops an observation the moment
        # it starts, so planning from jobs alone could plan straight over a
        # recording SatNOGS is making right now.
        source = getattr(self.satnogs, "commitments", None)
        if source is None:
            source = list(self.satnogs.jobs or [])
        for job in source:
            start, end = _parse_ts(job.get("start")), _parse_ts(job.get("end"))
            if start is None or end is None:
                # An unparsable job cannot be planned around, so it cannot be
                # ignored either: reserve the rest of the horizon rather than
                # risk planning on top of it.
                log.warning("unparsable SatNOGS job %r — reserving defensively",
                            job.get("id"))
                start = datetime.now(timezone.utc)
                end = start + timedelta(hours=self.s.planner_horizon_h)
            out.append(Reservation(start=start, end=end,
                                   norad=job.get("norad_cat_id"),
                                   job_id=job.get("id")))
        return out

    def last_heard(self) -> dict[int, datetime]:
        """When this station last decoded something from each satellite."""
        out: dict[int, datetime] = {}
        now = datetime.now(timezone.utc)
        for obs in list(self.satnogs.observations or []):
            norad = obs.get("norad")
            start = _parse_ts(obs.get("start"))
            if not isinstance(norad, int) or start is None or start > now:
                continue
            if obs.get("status") in HEARD or (obs.get("demoddata") or 0) > 0:
                if norad not in out or start > out[norad]:
                    out[norad] = start
        return out

    def _origin_az(self) -> float | None:
        """Where the antenna is, unwrapped — if the link says it is real."""
        last = getattr(self.rotator, "last", None)
        if last is None or getattr(last, "link", "up") != "up":
            return None
        return float(last.az_raw)

    # --- build --------------------------------------------------------------
    def gather(self, now: datetime | None = None) -> PlanInputs:
        """Read the live system. Call on the event loop."""
        now = now or datetime.now(timezone.utc)
        age = getattr(self.satnogs, "jobs_age_s", None)
        if not isinstance(age, (int, float)):
            # Fakes and older services without freshness tracking: treat an
            # attribute that is simply absent as "known", None as "not yet".
            age = 0.0 if not hasattr(self.satnogs, "jobs_age_s") else None
        return PlanInputs(
            now=now,
            priorities=dict(self.priorities()),
            reservations=tuple(self.reservations()),
            heard=dict(self.last_heard()),
            slew=self.slew,
            origin_az=self._origin_az(),
            schedule_known=age is not None,
            schedule_age_s=age,
        )

    def compute(self, inputs: PlanInputs) -> tuple[Plan, list[dict]]:
        """Build a plan from captured inputs. Safe to run in a worker thread:
        it reads only `inputs` and the predictor."""
        cands, blind = self._candidates(inputs)
        plan = select(
            cands,
            slew=inputs.slew,
            reservations=inputs.reservations,
            guard_s=float(self.s.gate_guard_s),
            floor_el=self.s.min_culmination_deg,
            now=inputs.now,
            origin_az=inputs.origin_az,
        )
        plan.horizon_h = self.s.planner_horizon_h
        plan.schedule_known = inputs.schedule_known
        plan.schedule_age_s = inputs.schedule_age_s
        return plan, blind

    def rebuild(self, now: datetime | None = None) -> Plan:
        """Synchronous rebuild, on the calling thread. For tests and tools —
        the running service uses rebuild_async, which keeps Skyfield off the
        event loop."""
        plan, blind = self.compute(self.gather(now))
        self.plan, self.unplannable = plan, blind
        return plan

    async def rebuild_async(self) -> Plan:
        """Gather on the loop, compute in a thread, one rebuild at a time.

        The lock is what stops two rebuilds — the timer and an operator's
        "rebuild" button — racing, with the older one landing last and
        silently replacing the newer plan.
        """
        async with self._lock:
            inputs = self.gather()
            plan, blind = await asyncio.to_thread(self.compute, inputs)
            self.plan, self.unplannable = plan, blind
            hub.publish("plan", self.snapshot())
            return plan

    def candidates(self, now: datetime | None = None) -> list[Candidate]:
        cands, blind = self._candidates(self.gather(now))
        self.unplannable = blind
        return cands

    def _candidates(self, inputs: PlanInputs) -> tuple[list[Candidate], list[dict]]:
        now = inputs.now
        out: list[Candidate] = []
        blind: list[dict] = []
        has_elements = getattr(self.predictor, "satellite", None)
        for norad, priority in inputs.priorities.items():
            if has_elements is not None and has_elements(norad) is None:
                blind.append({"norad": norad, "reason": "no orbital elements — "
                              "not in the element sets this station fetches"})
                continue
            last = inputs.heard.get(norad)
            hours = None if last is None else (now - last).total_seconds() / 3600.0
            for p in self.predictor.passes(norad, hours=self.s.planner_horizon_h):
                sweep, keyhole = self._geometry(norad, p)
                score, breakdown = score_pass(
                    max_el=p.max_el, duration_s=p.duration_s, priority=priority,
                    hours_since_heard=hours, floor_el=self.s.min_culmination_deg,
                    keyhole_error_deg=keyhole, weights=self.weights,
                )
                out.append(Candidate(
                    key=p.pass_id, norad=norad, name=p.name,
                    aos=p.aos, tca=p.tca, los=p.los,
                    max_el=p.max_el, aos_az=p.aos_az, los_az=p.los_az,
                    priority=priority, score=round(score, 4), breakdown=breakdown,
                    az_sweep=round(sweep, 1), keyhole_error_deg=round(keyhole, 1),
                ))
        return out, blind

    def _geometry(self, norad: int, p) -> tuple[float, float]:
        """The pass's unwrapped azimuth sweep, and its worst keyhole lag.

        Both come from sampling the predicted track. The sweep decides which
        starting bearing leaves room for the whole pass; the lag is what a
        rate-limited rotator loses chasing the satellite through zenith.
        """
        track = getattr(self.predictor, "track", None)
        if track is None:
            return 0.0, 0.0
        step = 2.0 if p.max_el >= self.KEYHOLE_FROM_EL else 15.0
        try:
            samples = track(norad, p.aos, p.los, step_s=step)
        except Exception:
            log.exception("could not sample pass %s", p.pass_id)
            return 0.0, 0.0
        if len(samples) < 2:
            return 0.0, 0.0

        sweep = 0.0
        for a, b in zip(samples, samples[1:]):
            d = b["az"] - a["az"]
            d = (d + 180.0) % 360.0 - 180.0      # the short way between samples
            sweep += d

        keyhole = 0.0
        if p.max_el >= self.KEYHOLE_FROM_EL:
            keyhole = peak_pointing_error(
                samples, self.s.rotator_az_rate_deg_s, self.s.rotator_el_rate_deg_s
            )
        return sweep, keyhole

    def snapshot(self) -> dict:
        plan = self.plan
        if plan is None:
            return {"built_at": None, "planned": [], "decisions": [], "total_score": 0,
                    "unplannable": [], "schedule_known": False}
        slew = self.slew
        return {
            "built_at": plan.built_at.isoformat(),
            "horizon_h": plan.horizon_h,
            "total_score": plan.total_score,
            "schedule_known": plan.schedule_known,
            "schedule_age_s": plan.schedule_age_s,
            "planned": [_candidate_json(c) for c in plan.planned],
            "decisions": [
                {**_candidate_json(d.candidate), "status": d.status,
                 "reason": d.reason, "blocked_by": d.blocked_by}
                for d in plan.decisions if d.status != "past"
            ],
            "counts": _counts(plan),
            "unplannable": self.unplannable,
            "slew": {
                "az_rate_deg_s": slew.az_rate_deg_s,
                "el_rate_deg_s": slew.el_rate_deg_s,
                "setup_s": slew.setup_s,
                "min_az": slew.min_az,
                "max_az": slew.max_az,
            },
        }

    def _fingerprint(self) -> tuple:
        """Everything a rebuild depends on, so an unchanged input is not
        re-planned every few seconds — including recordings in progress, so a
        job that starts is planned around at once rather than at the next
        timed rebuild."""
        source = getattr(self.satnogs, "commitments", None)
        if source is None:
            source = list(self.satnogs.jobs or [])
        jobs = tuple(sorted((str(j.get("id")), str(j.get("start"))) for j in source))
        tle_at = getattr(self.tles, "_fetched_at", None) if self.tles is not None else None
        known = getattr(self.satnogs, "jobs_age_s", 0.0) is not None
        slew = self.slew
        return (jobs, tle_at, known, (slew.min_az, slew.max_az),
                tuple(sorted(self.priorities().items())))

    async def run(self) -> None:
        last_build = 0.0
        loop = asyncio.get_running_loop()
        while True:
            inputs = self._fingerprint()
            stale = loop.time() - last_build >= self.s.planner_rebuild_s
            if stale or inputs != self._inputs:
                # Skyfield is CPU-bound; rebuild_async keeps it off the event
                # loop so the rotator poll and the WebSocket do not stall.
                await self.rebuild_async()
                self._inputs = inputs
                last_build = loop.time()
                log.info("plan rebuilt: %d planned of %d candidates",
                         len(self.plan.planned), len(self.plan.decisions))
            await asyncio.sleep(15.0)


def _candidate_json(c: Candidate) -> dict:
    return {
        "key": c.key, "norad": c.norad, "name": c.name,
        "aos": c.aos.isoformat(), "tca": c.tca.isoformat(), "los": c.los.isoformat(),
        "max_el": round(c.max_el, 1), "aos_az": round(c.aos_az, 1),
        "los_az": round(c.los_az, 1), "duration_s": round(c.duration_s),
        "priority": c.priority, "score": c.score, "breakdown": dict(c.breakdown),
        "az_sweep": c.az_sweep, "keyhole_error_deg": c.keyhole_error_deg,
        "start_bearing": c.start_bearing,
    }


def _counts(plan: Plan) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in plan.decisions:
        counts[d.status] = counts.get(d.status, 0) + 1
    return counts


# --------------------------------------------------------------------------
# executor
# --------------------------------------------------------------------------

@dataclass
class ExecutorState:
    enabled: bool = False
    phase: str = "off"         # off | waiting | positioning | tracking | blocked | idle
    detail: str = ""
    current: str | None = None
    disengaged_because: str = ""


class AutopilotRefused(RuntimeError):
    pass


# What a refused or failed command looks like from here. None of these is an
# internal error: the interlock said no, or the link is down, and the right
# response is to report it and wait — not to disengage.
_TRANSIENT = (ConnectionError, OSError, asyncio.TimeoutError)


class PlanExecutor:
    """Works the plan through ControlService. See the module docstring.

    Operator intervention is read from ControlService's command journal, never
    inferred from the mode string. The journal increments on every command
    ControlService *accepts*, and on nothing else — so "has anyone else
    commanded the antenna since I last did?" is exactly `command_seq != mine`.
    A track ended by a closing gate, a lapsing position or a failed write does
    not move the journal, so it can never be mistaken for a human.
    """

    def __init__(self, settings: Settings, planner: PlannerService, control,
                 rotator) -> None:
        self.s = settings
        self.planner = planner
        self.control = control
        self.rotator = rotator
        self.state = ExecutorState()
        self._seq: int | None = None            # journal position after our last command
        self._owned: Candidate | None = None    # the pass whose track we started
        self._owned_track_id: int | None = None
        self._positioned_for: Candidate | None = None
        self._lock = asyncio.Lock()

    # --- journal ------------------------------------------------------------
    def _journal(self) -> int:
        return int(getattr(self.control, "command_seq", 0))

    def _ours_is_latest(self) -> bool:
        return self._seq is not None and self._journal() == self._seq

    async def _command(self, fn, *args) -> bool:
        """Issue one command as autopilot. Returns False if it was refused.

        A refusal is not a crash: the interlock closed under the rotctld lock,
        or the rotator link is down. Report it and try again next step.

        Which journal entry is ours is decided by position, never by reading
        the journal after the await. ControlService journals every command
        synchronously at the moment it accepts it, before any await, so if
        this command was accepted its entry is exactly `before + 1` — nothing
        can run between this call starting and that entry being written.
        Reading `command_seq` after the await instead credited autopilot with
        whatever an operator did while the write was in flight: a STOP pressed
        during a pre-position slew became autopilot's own entry, and autopilot
        carried on to track the pass.

        An accepted command whose write then failed still journaled, and that
        entry is still ours — or the next step would read autopilot's own
        failed stop as someone else taking control.
        """
        from .control import ControlRefused
        from .rotctld_client import RotctldError

        # Re-check for intervention before *every* command, not only at the
        # top of a step. One step can issue two — the LOS stop of one pass,
        # then the goto for the next — and an operator's command can land
        # during the first one's await. Checking only at step start let the
        # second command override it within the same second.
        if self._seq is not None and self._journal() != self._seq:
            who = getattr(self.control, "last_origin", "someone")
            what = getattr(self.control, "_last_command", "") or "a command"
            await self._disable(f"{who} took control ({what})", stop_motion=False)
            return False

        before = self._journal()
        problem: tuple[str, str] | None = None
        try:
            await fn(*args, origin="autopilot")
        except ControlRefused as exc:
            problem = ("blocked", f"refused: {exc}")
        except (RotctldError, *_TRANSIENT) as exc:
            problem = ("blocked", f"rotator write failed: {exc}")

        origin_of = getattr(self.control, "origin_of", None)
        if callable(origin_of):
            if origin_of(before + 1) == "autopilot":
                self._seq = before + 1
        elif problem is None:
            self._seq = self._journal()        # a control with no origin record

        if problem is not None:
            self._set(*problem)
            return False
        return True

    @staticmethod
    def _same_pass(a: Candidate | None, b: Candidate | None) -> bool:
        """The same physical pass: same satellite, overlapping in time.

        Not the key. A pass's key is built from its AOS to the second, and a
        rebuild's search refines AOS by half a second from a moving start —
        so the same pass flips between two keys across rebuilds. Matching on
        the key made autopilot read its own pass as "dropped from the plan"
        and stop the antenna mid-pass, every second or third rebuild.
        """
        return (a is not None and b is not None and a.norad == b.norad
                and a.aos < b.los and b.aos < a.los)

    # --- switching ----------------------------------------------------------
    def enable(self) -> ExecutorState:
        """Engage autopilot. Requires an operator to be holding a lease now.

        Idempotent: engaging an engaged autopilot changes nothing. Re-engaging
        used to reset what it believed it had last done, so a second click —
        or a second browser — made the operator's next STOP invisible to it.

        Engaging is a handover. From this moment the antenna is autopilot's to
        drive, including away from a track the operator had running.
        """
        if self.state.enabled:
            return self.state
        if not self.s.rotator_control_enabled:
            raise AutopilotRefused("rotator control is disabled in configuration")
        if not self.control.armed:
            raise AutopilotRefused("arm control first — autopilot never takes a lease itself")
        self.state = ExecutorState(enabled=True, phase="waiting", detail="engaged")
        # Everything already in the journal is history; only what happens from
        # here on can count as someone else taking over.
        self._seq = self._journal()
        self._owned = None
        self._owned_track_id = None
        self._positioned_for = None
        log.warning("autopilot ENGAGED")
        self._publish()
        return self.state

    async def disable(self, because: str = "switched off by the operator") -> ExecutorState:
        async with self._lock:
            await self._disable(because)
        return self.state

    async def _disable(self, because: str, *, stop_motion: bool = True) -> None:
        """Stand down. State first, so nothing below can leave autopilot on.

        If autopilot's command is still the latest in the journal, whatever the
        antenna is doing — tracking, or a minutes-long pre-position slew at
        1.5°/s — it is doing because autopilot said so, and it is stopped. If
        anyone else has commanded it since, their command stands.
        """
        mine = self._ours_is_latest()
        self.state = ExecutorState(enabled=False, phase="off", detail=because,
                                   disengaged_because=because)
        self._seq = None
        self._owned = None
        self._owned_track_id = None
        self._positioned_for = None
        log.warning("autopilot disengaged: %s", because)

        if stop_motion and mine:
            try:
                mode = self.control.state().mode
            except Exception:
                mode = "unknown"
            if mode in ("track", "manual", "unknown"):
                try:
                    await self.control.stop(origin="autopilot")
                except Exception:
                    log.exception("autopilot could not stop its own motion")
        self._publish()

    def _publish(self) -> None:
        try:
            hub.publish("autopilot", {
                "enabled": self.state.enabled,
                "phase": self.state.phase,
                "detail": self.state.detail,
                "current": self.state.current,
                "disengaged_because": self.state.disengaged_because,
            })
        except Exception:
            log.exception("could not publish autopilot state")

    # --- loop ---------------------------------------------------------------
    async def run(self) -> None:
        while True:
            try:
                async with self._lock:
                    await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A crash must leave the antenna alone, not half-way through a
                # sequence it no longer understands. Disengage *first*, by
                # assignment, so that even a failure inside the stop below
                # cannot leave autopilot engaged.
                log.exception("autopilot step failed")
                mine = self._ours_is_latest()
                self.state = ExecutorState(
                    enabled=False, phase="off",
                    detail=f"internal error: {exc}",
                    disengaged_because=f"internal error: {exc}",
                )
                self._seq = None
                self._owned = None
                self._owned_track_id = None
                if mine:
                    try:
                        await self.control.stop(origin="autopilot")
                    except Exception:
                        log.exception("autopilot could not stop after an error")
                self._publish()
            await asyncio.sleep(1.0)

    async def step(self, now: datetime | None = None) -> None:
        if not self.state.enabled:
            return
        now = now or datetime.now(timezone.utc)
        control = self.control

        # 1. Consent. The lease is the operator's; lose it and stand down.
        if not self.s.rotator_control_enabled:
            await self._disable("rotator control was disabled")
            return
        if not control.armed:
            await self._disable("control lease ended — re-arm and re-engage to continue")
            return

        # 2. Has anyone else commanded the antenna since we last did? Their
        #    command stands — do not stop it, do not fight it.
        if self._seq is not None and self._journal() != self._seq:
            who = getattr(control, "last_origin", "someone")
            what = getattr(control, "_last_command", "") or "a command"
            await self._disable(f"{who} took control ({what})", stop_motion=False)
            return

        plan = self.planner.plan

        # 3. A track we own ends at its LOS — or as soon as the plan no longer
        #    contains it, because a rebuild found a reason not to work it.
        #    "Contains it" means the same physical pass, not the same key.
        if self._owned is not None:
            match = next((c for c in (plan.planned if plan else [])
                          if self._same_pass(c, self._owned)), None)
            if match is not None:
                self._owned = match        # keep LOS current across rebuilds
            over = now >= self._owned.los
            dropped = match is None
            if over or dropped:
                st = control.state()
                if st.mode == "track" and getattr(control, "track_id", None) == self._owned_track_id:
                    if not await self._command(control.stop):
                        return
                log.info("autopilot: pass %s %s", self._owned.key,
                         "complete" if over else "dropped from the plan")
                self._owned = None
                self._owned_track_id = None

        # 4. The interlock decides, not us — checked before anything else
        #    about the plan, so an empty plan cannot skip it.
        blocked = control.blocked_by()
        if blocked:
            self._set("blocked", f"waiting for the interlock: {', '.join(blocked)}")
            return

        # 5. Is the plan fit to act on?
        if plan is None:
            self._set("idle", "no plan built yet")
            return
        if not plan.schedule_known:
            self._set("blocked", "the plan was built before SatNOGS's schedule "
                                 "loaded — waiting for a rebuild")
            return

        upcoming = [c for c in plan.planned if c.los > now]
        if not upcoming:
            self._set("idle", "nothing left in the plan")
            return
        nxt = upcoming[0]
        self.state.current = nxt.key

        # 6. In the pass: track it, unless the track running is already ours
        #    for exactly this pass. After a gate closed and reopened, the track
        #    ControlService ended is no longer running, so this resumes it.
        if nxt.aos <= now < nxt.los:
            st = control.state()
            ours = (st.mode == "track"
                    and getattr(control, "track_id", None) == self._owned_track_id
                    and st.target_norad == nxt.norad
                    and self._same_pass(self._owned, nxt))
            if not ours:
                if not await self._command(control.track, nxt.norad):
                    return
                self._owned = nxt
                self._owned_track_id = getattr(control, "track_id", None)
            self._set("tracking", f"{nxt.name or nxt.norad} until {nxt.los:%H:%M:%S}Z")
            return

        # 7. Before the pass: get to where it rises, in good time.
        if now >= nxt.aos - timedelta(seconds=self._lead_s(nxt)):
            target = self._start_bearing(nxt)
            done = self._positioned_for
            already = (self._same_pass(done, nxt) and done is not None
                       and abs(self._start_bearing(done) - target) < 0.5)
            if not already:
                if not await self._command(control.goto, target, 0.0):
                    return
                self._positioned_for = nxt
                # A goto ends any track; nothing is ours to stop any more.
                self._owned = None
                self._owned_track_id = None
            self._set("positioning",
                      f"at {self._start_bearing(nxt):.0f}° for {nxt.name or nxt.norad}, "
                      f"AOS {nxt.aos:%H:%M:%S}Z")
            return

        self._set("waiting", f"next: {nxt.name or nxt.norad} at {nxt.aos:%H:%M:%S}Z")

    # --- helpers ------------------------------------------------------------
    def _set(self, phase: str, detail: str) -> None:
        if (phase, detail) != (self.state.phase, self.state.detail):
            self.state.phase, self.state.detail = phase, detail
            self._publish()

    def _start_bearing(self, nxt: Candidate) -> float:
        """Where to meet the pass: the bearing the plan was costed on.

        Planned passes carry it, chosen by the DAG with the whole sequence in
        view — so the bearing autopilot drives to is exactly the one whose
        slew time made the plan feasible. Only a pass without one (built by
        hand, say) falls back to choosing from where the antenna is.
        """
        if nxt.start_bearing is not None:
            return nxt.start_bearing
        sample = self.rotator.last
        current = sample.az_raw if sample is not None else nxt.aos_az
        bearing, _ = self.planner.slew.best_start(current, nxt.aos_az, nxt.az_sweep)
        return bearing

    def _lead_s(self, nxt: Candidate) -> float:
        """How early to start moving: the configured lead, or longer if the
        antenna is far from where the pass will be met."""
        sample = self.rotator.last
        if sample is None:
            return float(self.s.planner_lead_s)
        slew = self.planner.slew
        target = self._start_bearing(nxt)
        az_s = abs(target - sample.az_raw) / slew.az_rate_deg_s
        el_s = abs(sample.el) / slew.el_rate_deg_s
        need = max(az_s, el_s) + slew.accel_margin_s + float(self.s.planner_setup_s)
        return max(float(self.s.planner_lead_s), need)
