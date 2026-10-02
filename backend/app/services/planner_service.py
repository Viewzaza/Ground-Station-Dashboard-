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
  * An operator always wins. Pressing STOP, or driving the antenna by hand,
    disengages autopilot rather than being fought by it.
  * It only stops tracks it started. A track an operator began is theirs.

If SatNOGS reconnects, or one of its observations comes within the guard
window, the interlock closes and ControlService's own track loop stops the
antenna. The executor sees the closed gate, reports it, and waits.
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

class PlannerService:
    def __init__(self, settings: Settings, predictor, satnogs, tles=None) -> None:
        self.s = settings
        self.predictor = predictor
        self.satnogs = satnogs
        self.tles = tles
        self.plan: Plan | None = None
        self.weights = ScoreWeights()
        self._inputs: tuple = ()
        # Watched satellites the plan cannot see, and why. SatNOGS schedules
        # objects under temporary catalogue numbers that no public element
        # set carries; their windows are still reserved, but an operator
        # should be told the plan is blind to them rather than left to
        # wonder why they are missing.
        self.unplannable: list[dict] = []

    # --- model --------------------------------------------------------------
    @property
    def slew(self) -> SlewModel:
        caps = None
        rotator = getattr(self, "rotator", None)
        if rotator is not None:
            caps = getattr(rotator.client, "caps", None)
        return SlewModel(
            az_rate_deg_s=self.s.rotator_az_rate_deg_s,
            el_rate_deg_s=self.s.rotator_el_rate_deg_s,
            setup_s=float(self.s.planner_setup_s),
            # Use the limits the rotator itself reported, so the cable-wrap
            # arithmetic matches the hardware rather than a datasheet.
            min_az=caps.min_az if caps else -180.0,
            max_az=caps.max_az if caps else 540.0,
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
        for job in self.satnogs.jobs or []:
            norad = job.get("norad_cat_id")
            if isinstance(norad, int):
                out.setdefault(norad, self.s.planner_default_priority)
        if self.s.planner_include_catalog and self.tles is not None:
            for item in self.tles.catalog():
                out.setdefault(int(item["norad"]), self.s.planner_default_priority)
        return out

    def reservations(self) -> list[Reservation]:
        out = []
        for job in self.satnogs.jobs or []:
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
        for obs in self.satnogs.observations or []:
            norad = obs.get("norad")
            start = _parse_ts(obs.get("start"))
            if not isinstance(norad, int) or start is None or start > now:
                continue
            if obs.get("status") in HEARD or (obs.get("demoddata") or 0) > 0:
                if norad not in out or start > out[norad]:
                    out[norad] = start
        return out

    def candidates(self, now: datetime | None = None) -> list[Candidate]:
        now = now or datetime.now(timezone.utc)
        heard = self.last_heard()
        out: list[Candidate] = []
        blind: list[dict] = []
        has_elements = getattr(self.predictor, "satellite", None)
        for norad, priority in self.priorities().items():
            if has_elements is not None and has_elements(norad) is None:
                blind.append({"norad": norad, "reason": "no orbital elements — "
                              "not in the element sets this station fetches"})
                continue
            last = heard.get(norad)
            hours = None if last is None else (now - last).total_seconds() / 3600.0
            for p in self.predictor.passes(norad, hours=self.s.planner_horizon_h):
                score, breakdown = score_pass(
                    max_el=p.max_el, duration_s=p.duration_s, priority=priority,
                    hours_since_heard=hours, floor_el=self.s.min_culmination_deg,
                    weights=self.weights,
                )
                out.append(Candidate(
                    key=p.pass_id, norad=norad, name=p.name,
                    aos=p.aos, tca=p.tca, los=p.los,
                    max_el=p.max_el, aos_az=p.aos_az, los_az=p.los_az,
                    priority=priority, score=round(score, 4), breakdown=breakdown,
                ))
        self.unplannable = blind
        return out

    # --- build --------------------------------------------------------------
    def rebuild(self, now: datetime | None = None) -> Plan:
        now = now or datetime.now(timezone.utc)
        plan = select(
            self.candidates(now),
            slew=self.slew,
            reservations=self.reservations(),
            guard_s=float(self.s.gate_guard_s),
            floor_el=self.s.min_culmination_deg,
            now=now,
        )
        plan.horizon_h = self.s.planner_horizon_h
        self.plan = plan
        return plan

    def snapshot(self) -> dict:
        plan = self.plan
        if plan is None:
            return {"built_at": None, "planned": [], "decisions": [], "total_score": 0}
        return {
            "built_at": plan.built_at.isoformat(),
            "horizon_h": plan.horizon_h,
            "total_score": plan.total_score,
            "planned": [_candidate_json(c) for c in plan.planned],
            "decisions": [
                {**_candidate_json(d.candidate), "status": d.status,
                 "reason": d.reason, "blocked_by": d.blocked_by}
                for d in plan.decisions if d.status != "past"
            ],
            "counts": _counts(plan),
            "unplannable": self.unplannable,
            "slew": {
                "az_rate_deg_s": self.slew.az_rate_deg_s,
                "el_rate_deg_s": self.slew.el_rate_deg_s,
                "setup_s": self.slew.setup_s,
            },
        }

    def _fingerprint(self) -> tuple:
        """Everything a rebuild depends on, so an unchanged input is not
        re-planned every few seconds."""
        jobs = tuple(sorted((j.get("id"), j.get("start")) for j in self.satnogs.jobs or []))
        tle_at = None
        if self.tles is not None:
            tle_at = getattr(self.tles, "_fetched_at", None)
        return (jobs, tle_at, tuple(sorted(self.priorities().items())))

    async def run(self) -> None:
        last_build = 0.0
        loop = asyncio.get_running_loop()
        while True:
            inputs = self._fingerprint()
            stale = loop.time() - last_build >= self.s.planner_rebuild_s
            if stale or inputs != self._inputs:
                # Skyfield is CPU-bound; keep it off the event loop so the
                # rotator poll and the WebSocket do not stall during a rebuild.
                await asyncio.to_thread(self.rebuild)
                self._inputs = inputs
                last_build = loop.time()
                hub.publish("plan", self.snapshot())
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


class PlanExecutor:
    def __init__(self, settings: Settings, planner: PlannerService, control,
                 rotator) -> None:
        self.s = settings
        self.planner = planner
        self.control = control
        self.rotator = rotator
        self.state = ExecutorState()
        self._positioned_for: str | None = None
        self._owned_track: Candidate | None = None
        self._expect: str | None = None       # mode we last put control into

    # --- switching ----------------------------------------------------------
    def enable(self) -> ExecutorState:
        """Engage autopilot. Requires an operator to be holding a lease now."""
        if not self.s.rotator_control_enabled:
            raise AutopilotRefused("rotator control is disabled in configuration")
        if not self.control.armed:
            raise AutopilotRefused("arm control first — autopilot never takes a lease itself")
        self.state = ExecutorState(enabled=True, phase="waiting",
                                   detail="engaged")
        self._positioned_for = None
        self._expect = None
        log.warning("autopilot ENGAGED")
        self.publish()
        return self.state

    async def disable(self, because: str = "switched off by the operator") -> ExecutorState:
        was_tracking = self._owned_track is not None and self.control.state().mode == "track"
        self.state = ExecutorState(enabled=False, phase="off", detail=because,
                                   disengaged_because=because)
        self._positioned_for = None
        self._expect = None
        if was_tracking:
            # Stop what we started. A track the operator began is theirs.
            try:
                await self.control.stop()
            except Exception:
                log.exception("autopilot could not stop its own track")
        self._owned_track = None
        log.warning("autopilot disengaged: %s", because)
        self.publish()
        return self.state

    def publish(self) -> None:
        hub.publish("autopilot", {
            "enabled": self.state.enabled,
            "phase": self.state.phase,
            "detail": self.state.detail,
            "current": self.state.current,
            "disengaged_because": self.state.disengaged_because,
        })

    # --- loop ---------------------------------------------------------------
    async def run(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A crash must leave the antenna alone, not half-way through a
                # sequence it no longer understands.
                log.exception("autopilot step failed")
                await self.disable(f"internal error: {exc}")
            await asyncio.sleep(1.0)

    async def step(self, now: datetime | None = None) -> None:
        if not self.state.enabled:
            return
        now = now or datetime.now(timezone.utc)

        # 1. The lease is the operator's consent. Lose it and stand down.
        if not self.control.armed:
            await self.disable("control lease ended — re-arm and re-engage to continue")
            return
        if not self.s.rotator_control_enabled:
            await self.disable("rotator control was disabled")
            return

        # 2. Has an operator taken over?
        mode = self.control.state().mode
        if self._expect is not None and mode != self._expect:
            if mode == "idle" and not self.control.blocked_by():
                await self.disable("operator stopped the antenna")
                return
            if mode in ("manual", "track"):
                await self.disable("operator took manual control")
                return

        # 3. End a track we own once its pass is over.
        if self._owned_track is not None and now >= self._owned_track.los:
            if mode == "track":
                await self.control.stop()
            log.info("autopilot: pass %s complete", self._owned_track.key)
            self._owned_track = None
            self._expect = None

        plan = self.planner.plan
        upcoming = [c for c in (plan.planned if plan else []) if c.los > now]
        if not upcoming:
            self._set("idle", "nothing left in the plan")
            return
        nxt = upcoming[0]
        self.state.current = nxt.key

        # 4. The interlock decides, not us.
        blocked = self.control.blocked_by()
        if blocked:
            # ControlService ends its own track when a gate closes, so the
            # mode we put it in no longer holds. Forget it — otherwise, when
            # the gate reopens, "expected track, found idle" would read as an
            # operator pressing STOP and wrongly disengage autopilot.
            self._expect = None
            self._set("blocked", f"waiting for the interlock: {', '.join(blocked)}")
            return

        # 5. In the pass: track it.
        if nxt.aos <= now < nxt.los:
            state = self.control.state()
            if state.mode != "track" or state.target_norad != nxt.norad:
                await self.control.track(nxt.norad)
                self._owned_track = nxt
                self._expect = "track"
            self._set("tracking", f"{nxt.name or nxt.norad} until {nxt.los:%H:%M:%S}Z")
            return

        # 6. Before the pass: get to where it rises, in good time.
        lead = self._lead_s(nxt)
        if now >= nxt.aos - timedelta(seconds=lead):
            if self._positioned_for != nxt.key:
                await self.control.goto(self._nearest(nxt.aos_az), 0.0)
                self._positioned_for = nxt.key
                self._expect = "manual"
            self._set("positioning",
                      f"at {nxt.aos_az:.0f}° for {nxt.name or nxt.norad}, AOS {nxt.aos:%H:%M:%S}Z")
            return

        self._set("waiting", f"next: {nxt.name or nxt.norad} at {nxt.aos:%H:%M:%S}Z")

    # --- helpers ------------------------------------------------------------
    def _set(self, phase: str, detail: str) -> None:
        if (phase, detail) != (self.state.phase, self.state.detail):
            self.state.phase, self.state.detail = phase, detail
            self.publish()

    def _lead_s(self, nxt: Candidate) -> float:
        """How early to start moving: the configured lead, or longer if the
        antenna is far from where the pass rises."""
        sample = self.rotator.last
        need = 0.0
        if sample is not None:
            need = self.planner.slew.seconds(sample.az_raw, sample.el, nxt.aos_az, 0.0)
        return max(float(self.s.planner_lead_s), need + float(self.s.planner_setup_s))

    def _nearest(self, az: float) -> float:
        """The representation of `az` nearest the antenna, inside its limits —
        so pre-positioning does not wind the cable a full turn."""
        sample = self.rotator.last
        slew = self.planner.slew
        current = sample.az_raw if sample is not None else az
        options = [az + 360.0 * k for k in range(-2, 3)
                   if slew.min_az <= az + 360.0 * k <= slew.max_az]
        if not options:
            return az

        def margin(a: float) -> float:
            return min(a - slew.min_az, slew.max_az - a)

        # Nearest first; on a tie, the bearing furthest from either end stop.
        # From 0° a pass rising at 180° is as far one way as the other, and the
        # naive pick is -180 — the rotator's limit — which starts the pass with
        # no room at all to follow the satellite in one direction.
        return min(options, key=lambda a: (round(abs(a - current), 1), -margin(a)))
