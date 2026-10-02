"""Observation planning: which passes this station should work, and in what order.

One rotator and one radio means at most one observation at a time, so passes
genuinely compete. Choosing among them is weighted interval scheduling with a
twist that matters: the time the antenna needs between two passes depends on
*which* two passes they are. Leaving one pass in the north-east and catching the
next rising in the south-west is a long slew; two passes that set and rise in
the same part of the sky need almost none.

That sequence dependence is why this does not use the textbook algorithm —
sort by finish time, binary-search the last compatible job. That relies on
compatibility being a property of time alone, and here it is not. Instead the
plan is a longest path through a DAG: one node per candidate pass, an edge
i -> j wherever the antenna can get from the end of i to the start of j in time,
node weight = the pass's score. Because the turnaround cost depends only on the
immediately preceding pass, that graph captures the problem exactly, and the
optimum is found in O(n^2). A day of passes for the whole amateur catalogue is a
few hundred candidates, which is nothing.

Greedy-by-score is the obvious alternative and it is wrong in a specific,
common way: it takes one excellent pass that blocks two good ones whose total is
higher. Greedy-by-earliest-finish is optimal only when every pass is worth the
same, which is exactly the assumption a priority scheme exists to break.

What this module does not do is move anything. It produces a plan and the
reasons behind it. Executing the plan is PlanExecutor's job, and that goes
through ControlService, so every interlock gate still applies to every command.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal

from ..config import Settings

log = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0

Status = Literal[
    "planned",          # chosen; this station will work it
    "satnogs",          # SatNOGS already has it scheduled; nothing for us to do
    "reserved",         # overlaps a SatNOGS job for a different satellite
    "conflict",         # lost to a better-scoring overlapping pass
    "infeasible",       # cannot slew there in time from the pass before it
    "low",              # peaks below the station's culmination threshold
    "past",             # already over
]


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    key: str
    norad: int
    name: str
    aos: datetime
    tca: datetime
    los: datetime
    max_el: float
    aos_az: float
    los_az: float
    priority: float
    score: float = 0.0
    breakdown: tuple[tuple[str, float], ...] = ()
    # Signed azimuth the pass sweeps from AOS to LOS, unwrapped (so an
    # overhead pass from 10° to 190° through east is +180, through west -180).
    # Needed to know whether a starting bearing leaves room to finish the pass.
    az_sweep: float = 0.0
    # Worst pointing error a rate-limited rotator would suffer chasing this
    # pass through the keyhole near zenith. 0 for anything below ~60°.
    keyhole_error_deg: float = 0.0
    # For a planned pass: the exact, unwrapped bearing to meet it at — the one
    # the plan's slew times were costed on. Autopilot drives here, so the
    # executor and the plan cannot disagree about where the antenna will be.
    start_bearing: float | None = None

    @property
    def duration_s(self) -> float:
        return (self.los - self.aos).total_seconds()


@dataclass
class Decision:
    candidate: Candidate
    status: Status
    reason: str
    # For an infeasible or conflicting pass, which planned pass caused it.
    blocked_by: str | None = None


@dataclass
class Reservation:
    """A window the planner must leave alone — a SatNOGS observation."""
    start: datetime
    end: datetime
    norad: int | None
    job_id: int | None = None


@dataclass
class Plan:
    built_at: datetime
    horizon_h: float
    planned: list[Candidate] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    total_score: float = 0.0
    # Whether SatNOGS's schedule had loaded when this plan was built. A plan
    # built from an empty job list *before the first poll* has not been checked
    # against SatNOGS at all — it is not a plan that happens to find no
    # conflicts — and autopilot must not act on it.
    schedule_known: bool = True
    schedule_age_s: float | None = None


@dataclass(frozen=True)
class SlewModel:
    """How fast the antenna can move, and how long a station needs to get ready.

    Both axes move at once on a SPID, so the time for a move is the slower of
    the two axes, not their sum. Rates are deliberately conservative: a plan
    that assumes the rotator is faster than it is schedules passes that start
    with the antenna still swinging into place, which loses the first minute of
    every pass — usually the low, Doppler-heavy part that is worst already.
    """
    az_rate_deg_s: float = 1.5
    el_rate_deg_s: float = 1.5
    setup_s: float = 30.0            # retune, start the recorder, settle
    min_az: float = -180.0
    max_az: float = 540.0
    # SPID motors are not instantaneous; this covers start/stop ramps.
    # An engineering margin, not a datasheet figure.
    accel_margin_s: float = 4.0
    # The community floor for a rotator station between observations
    # (satnogs-auto-scheduler's recommended -w 60). Below this the antenna has
    # no time to settle even when it barely has to move.
    min_turnaround_s: float = 60.0

    def seconds(self, from_az: float, from_el: float,
                to_az: float, to_el: float, sweep: float = 0.0) -> float:
        _, az_travel = self.best_start(from_az, to_az, sweep)
        el_travel = abs(to_el - from_el)
        moving = max(az_travel, el_travel) > 0.05
        return (max(az_travel / self.az_rate_deg_s, el_travel / self.el_rate_deg_s)
                + (self.accel_margin_s if moving else 0.0))

    def turnaround(self, from_az: float, from_el: float,
                   to_az: float, to_el: float, sweep: float = 0.0) -> float:
        """Total time needed between one pass's LOS and the next one's AOS."""
        return max(self.min_turnaround_s,
                   self.seconds(from_az, from_el, to_az, to_el, sweep) + self.setup_s)

    def az_travel(self, from_az: float, to_az: float, sweep: float = 0.0) -> float:
        return self.best_start(from_az, to_az, sweep)[1]

    def best_start(self, from_az: float, to_az: float,
                   sweep: float = 0.0) -> tuple[float, float]:
        """Where to meet a pass that rises at `to_az`, and how far that is.

        A SPID's -180..540 range holds every bearing at least twice, so there
        is a choice. Two things constrain it:

        * The whole pass has to fit. A starting bearing is only usable if the
          antenna can follow the pass's full azimuth sweep from it without
          hitting an end stop — a pass sweeps up to ~180°, so a start that is
          fine on its own can still be unusable. Checking only the AOS point is
          the mistake that leaves a track pinned against a limit at TCA.
        * Nearest wins, and a tie goes to the bearing with the most room to
          spare. From 0°, a pass rising at 180° is as far one way as the other;
          the naive pick is -180, the end stop itself.

        If no bearing fits the sweep, fall back to one that at least fits the
        AOS point, charged as a full unwind — the honest cost of having to swing
        round to the other branch first.
        """
        def fits(a: float) -> bool:
            return (self.min_az <= a <= self.max_az
                    and self.min_az <= a + sweep <= self.max_az)

        def margin(a: float) -> float:
            lo, hi = min(a, a + sweep), max(a, a + sweep)
            return min(lo - self.min_az, self.max_az - hi)

        reps = [to_az + 360.0 * k for k in range(-3, 4)]
        valid = [a for a in reps if fits(a)]
        if valid:
            best = min(valid, key=lambda a: (round(abs(a - from_az), 1), -margin(a)))
            return best, abs(best - from_az)

        # Nothing fits the sweep. Charging a notional "full unwind" here would
        # make an unfollowable pass look merely slow; it is not reachable at
        # all, and saying so lets the plan mark it infeasible.
        return to_az, math.inf


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoreWeights:
    """Weights for the three terms, each normalised to 0..1 before weighting.

    Path loss gets the most authority because it has ~11 dB of dynamic range
    across the passes worth working and dominates whether frames decode at all.
    Duration earns little because it saturates early — most passes above 20°
    are long enough. Freshness needs enough weight to pull a long-unheard
    satellite ahead of a marginally better pass of one heard an hour ago, but
    not enough to overturn a 10x priority difference.
    """
    elevation: float = 0.55
    duration: float = 0.15
    freshness: float = 0.30
    # Duration is scored as expected beacons: KNACKSAT-2 beacons once a
    # minute, and ten beacons is a well-heard pass.
    beacon_interval_s: float = 60.0
    beacons_for_full_marks: float = 10.0
    # Freshness rises as 1 - 2^(-t / half-life): half credit after 12 hours
    # unheard, three-quarters after a day.
    freshness_half_life_h: float = 12.0
    # The antenna's full 3 dB beamwidth, for the keyhole derate. A typical
    # SatNOGS UHF Yagi is 30-50°.
    beamwidth_deg: float = 40.0


def slant_range_km(el_deg: float, alt_km: float) -> float:
    """Distance from the station to a satellite at a given elevation.

        r = sqrt((Re + h)^2 - (Re cos el)^2) - Re sin el
    """
    el = math.radians(max(0.0, el_deg))
    re = EARTH_RADIUS_KM
    return math.sqrt((re + alt_km) ** 2 - (re * math.cos(el)) ** 2) - re * math.sin(el)


def link_gain_db(max_el: float, floor_el: float, alt_km: float = 420.0) -> float:
    """How much closer this pass gets than the worst pass worth working.

    Free-space path loss goes as 20·log10(range), so the difference between two
    passes is 20·log10(r_floor / r_peak) dB. At 420 km a 10-degree pass peaks at
    roughly 1 400 km and an overhead pass at 420 km — about 10 dB, which for a
    9600-baud FSK downlink is the difference between frames and noise.
    """
    r_floor = slant_range_km(floor_el, alt_km)
    r_peak = slant_range_km(max_el, alt_km)
    return 20.0 * math.log10(r_floor / r_peak) if r_peak > 0 else 0.0


def keyhole_derate(pointing_error_deg: float, beamwidth_deg: float) -> float:
    """Fraction of signal left when the antenna lags the satellite.

    An az/el mount's required azimuth rate near zenith goes as 1/cos(el): for a
    400 km pass peaking at 80° it is about 6°/s, at 89° about 60°/s. A SPID
    turning at 1.5-3°/s cannot keep up, so through TCA — the best part of the
    pass — the beam points well behind the satellite. That is why an overhead
    pass is genuinely worse than a 60-75° one on this hardware.

    Gaussian beam loss: G = 12·(ε/θ3dB)² dB, which is 3 dB at half the
    beamwidth. Returned as a linear factor.
    """
    if pointing_error_deg <= 0 or beamwidth_deg <= 0:
        return 1.0
    loss_db = 12.0 * (pointing_error_deg / beamwidth_deg) ** 2
    return 10.0 ** (-loss_db / 10.0)


def score_pass(*, max_el: float, duration_s: float, priority: float,
               hours_since_heard: float | None, floor_el: float,
               keyhole_error_deg: float = 0.0,
               weights: ScoreWeights = ScoreWeights()
               ) -> tuple[float, tuple[tuple[str, float], ...]]:
    """A pass's value. Higher is better; priority multiplies, it does not add.

    Each term is normalised to 0..1 before weighting, so the weights mean what
    they say. Priority multiplies the whole thing because "our own satellite"
    should beat "a slightly higher pass of someone else's", and an additive
    priority would let a perfect pass of an unimportant satellite outscore a
    middling pass of the one this station exists for.

    Freshness depends only on when this station last *actually* heard the
    satellite, never on what else is in the plan. Letting an earlier planned
    pass lower a later one's freshness would make a pass's value depend on its
    predecessors, and the plan would stop being an exact optimum.
    """
    best_gain = link_gain_db(90.0, floor_el)
    elevation = link_gain_db(max_el, floor_el) / best_gain if best_gain > 0 else 0.0
    elevation = max(0.0, min(1.0, elevation))

    beacons = math.floor(max(0.0, duration_s) / weights.beacon_interval_s)
    duration = max(0.0, min(1.0, beacons / weights.beacons_for_full_marks))

    if hours_since_heard is None:
        freshness = 1.0             # never heard: as neglected as it gets
    else:
        freshness = 1.0 - 2.0 ** (-max(0.0, hours_since_heard)
                                  / weights.freshness_half_life_h)

    keyhole = keyhole_derate(keyhole_error_deg, weights.beamwidth_deg)

    base = (
        weights.elevation * elevation
        + weights.duration * duration
        + weights.freshness * freshness
    )
    total = base * keyhole * max(0.0, priority)
    return total, (
        ("elevation", round(elevation, 3)),
        ("duration", round(duration, 3)),
        ("freshness", round(freshness, 3)),
        ("keyhole", round(keyhole, 3)),
        ("priority", round(priority, 3)),
    )


def peak_pointing_error(samples: list[dict], az_rate_deg_s: float,
                        el_rate_deg_s: float) -> float:
    """Simulate a rate-limited rotator chasing a pass; return its worst error.

    `samples` are {"t", "az", "el"} along the pass. The rotator starts on the
    target and each step moves toward it no faster than its rates allow. The
    error is the great-circle angle between where it points and where the
    satellite is — what the beam actually cares about, which is the azimuth lag
    scaled down by cos(el).
    """
    if len(samples) < 2:
        return 0.0

    def ts(s):
        t = s["t"]
        return t.timestamp() if isinstance(t, datetime) else datetime.fromisoformat(t).timestamp()

    # Unwrap the target azimuth so the follower never "chases" a 360° jump.
    unwrapped = [samples[0]["az"]]
    for s in samples[1:]:
        prev = unwrapped[-1]
        a = s["az"]
        while a - prev > 180.0:
            a -= 360.0
        while a - prev < -180.0:
            a += 360.0
        unwrapped.append(a)

    az_r, el_r = unwrapped[0], samples[0]["el"]
    worst = 0.0
    t_prev = ts(samples[0])
    for s, az_t in zip(samples[1:], unwrapped[1:]):
        t = ts(s)
        dt = max(0.0, t - t_prev)
        t_prev = t
        el_t = s["el"]
        az_r += max(-az_rate_deg_s * dt, min(az_rate_deg_s * dt, az_t - az_r))
        el_r += max(-el_rate_deg_s * dt, min(el_rate_deg_s * dt, el_t - el_r))
        worst = max(worst, _separation(az_r, el_r, az_t, el_t))
    return worst


def _separation(az1: float, el1: float, az2: float, el2: float) -> float:
    a1, e1, a2, e2 = map(math.radians, (az1, el1, az2, el2))
    cos_d = math.sin(e1) * math.sin(e2) + math.cos(e1) * math.cos(e2) * math.cos(a1 - a2)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_d))))


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

def _overlaps(c: Candidate, r: Reservation, guard_s: float) -> bool:
    guard = timedelta(seconds=guard_s)
    return c.aos < r.end + guard and c.los > r.start - guard


def sweep_of(c: Candidate) -> float:
    """The pass's unwrapped AOS-to-LOS azimuth change.

    Measured by sampling the track when that was possible. When it was not —
    no track available, or sampling failed — fall back to the short way from
    AOS to LOS rather than to zero: a zero sweep claims the antenna finishes
    the pass exactly where it started, and every turnaround after it is then
    costed from the wrong side of the sky.
    """
    if c.az_sweep:
        return c.az_sweep
    return (c.los_az - c.aos_az + 180.0) % 360.0 - 180.0


def branches(c: Candidate, slew: SlewModel) -> list[float]:
    """Every unwrapped bearing from which this pass can be followed to LOS.

    A SPID holds each compass bearing more than once, and which one the antenna
    meets a pass at decides both where it ends up and how far the next slew
    is. Only branches that keep the pass's whole sweep inside the limits count.
    """
    sweep = sweep_of(c)
    out = []
    for k in range(-3, 4):
        a = c.aos_az + 360.0 * k
        if (slew.min_az <= a <= slew.max_az
                and slew.min_az <= a + sweep <= slew.max_az):
            out.append(a)
    return out


def turn_s(slew: SlewModel, from_bearing: float, to_bearing: float) -> float:
    """Time from one explicit unwrapped bearing to another, both on the horizon,
    plus setup — with the turnaround floor. No branch choice is made here; the
    DAG already made it."""
    travel = abs(to_bearing - from_bearing)
    moving = travel > 0.05
    move = travel / slew.az_rate_deg_s + (slew.accel_margin_s if moving else 0.0)
    return max(slew.min_turnaround_s, move + slew.setup_s)


def _margin(a: float, sweep: float, slew: SlewModel) -> float:
    lo, hi = min(a, a + sweep), max(a, a + sweep)
    return min(lo - slew.min_az, slew.max_az - hi)


def select(candidates: Iterable[Candidate], *, slew: SlewModel,
           reservations: Iterable[Reservation] = (), guard_s: float = 0.0,
           floor_el: float = 0.0, now: datetime | None = None,
           origin_az: float | None = None) -> Plan:
    """The highest-scoring set of passes that the antenna can physically work.

    Exact, not heuristic: a longest path through a DAG whose nodes are
    (pass, wrap branch) pairs. A node is a decision about *where* to meet the
    pass as well as whether to work it, because on a rotator with overlap the
    two are not separable — the branch chosen for one pass is where the
    antenna is when the next one is due, after the pass's own sweep.

    Planning over passes alone, and costing each turnaround from the compass
    LOS bearing, under-estimates a slew by up to 320° after a wrapped pass:
    the antenna is not at 170° but at 530°. That is a plan the rotator cannot
    actually fly.

    `origin_az` is where the antenna is now, unwrapped. If known, the first
    pass must be reachable from it; if not, any branch may start the plan.

    The objective is lexicographic: total score first, then least total slew,
    then the branch with most room to spare from the end stops. The last two
    only ever decide between plans of equal score.
    """
    now = now or datetime.now(timezone.utc)
    reservations = list(reservations)
    decisions: dict[str, Decision] = {}

    pool: list[Candidate] = []
    for c in sorted(candidates, key=lambda c: (c.aos, c.key)):
        if c.los <= now:
            decisions[c.key] = Decision(c, "past", "already over")
            continue
        if c.max_el < floor_el:
            decisions[c.key] = Decision(
                c, "low", f"peaks at {c.max_el:.1f}°, below the {floor_el:.0f}° threshold"
            )
            continue
        if c.score <= 0:
            # A zero-priority satellite is one an operator has said not to
            # work. Left in the pool it would still be planned whenever it is
            # the only pass on offer, because a plan of one beats a plan of none.
            decisions[c.key] = Decision(
                c, "low", "scores 0 — its priority is 0, so it is not worth working"
            )
            continue

        clash = next((r for r in reservations if _overlaps(c, r, guard_s)), None)
        if clash is not None:
            if clash.norad == c.norad:
                decisions[c.key] = Decision(
                    c, "satnogs", "SatNOGS has this pass scheduled already"
                )
            else:
                decisions[c.key] = Decision(
                    c, "reserved",
                    f"overlaps SatNOGS job {clash.job_id} for NORAD {clash.norad}",
                )
            continue
        if not branches(c, slew):
            decisions[c.key] = Decision(
                c, "infeasible",
                f"its {abs(sweep_of(c)):.0f}° azimuth sweep does not fit the "
                f"rotator's {slew.min_az:.0f}..{slew.max_az:.0f}° travel from any "
                f"starting bearing",
            )
            continue
        pool.append(c)

    # --- nodes -----------------------------------------------------------------
    nodes: list[tuple[int, float]] = []
    for k, c in enumerate(pool):
        for b in branches(c, slew):
            nodes.append((k, b))
    nodes.sort(key=lambda v: (pool[v[0]].aos, v[0], v[1]))

    NEG = (-math.inf, 0.0, 0.0)
    value: list[tuple[float, float, float]] = [NEG] * len(nodes)
    prev: list[int | None] = [None] * len(nodes)

    for vi, (k, b) in enumerate(nodes):
        c = pool[k]
        margin = _margin(b, sweep_of(c), slew)

        # Starting the plan here.
        if origin_az is None:
            value[vi] = (c.score, 0.0, margin)
        elif c.aos <= now:
            # Already under way: the antenna has to catch up wherever it is,
            # and the cost of doing so is the slew.
            value[vi] = (c.score, -abs(b - origin_az), margin)
        elif (now + timedelta(seconds=turn_s(slew, origin_az, b))) <= c.aos:
            value[vi] = (c.score, -abs(b - origin_az), margin)

        # Following an earlier node.
        for ui in range(vi):
            uk, ub = nodes[ui]
            if value[ui][0] == -math.inf:
                continue
            u = pool[uk]
            if uk == k or u.los >= c.aos:
                continue
            gap = (c.aos - u.los).total_seconds()
            end_bearing = ub + sweep_of(u)
            if gap < turn_s(slew, end_bearing, b):
                continue
            cand = (value[ui][0] + c.score,
                    value[ui][1] - abs(b - end_bearing),
                    margin)
            if cand > value[vi]:
                value[vi] = cand
                prev[vi] = ui

    chosen_nodes: list[int] = []
    reachable = [vi for vi in range(len(nodes)) if value[vi][0] > -math.inf]
    if reachable:
        vi: int | None = max(reachable, key=lambda v: (value[v], -v))
        while vi is not None:
            chosen_nodes.append(vi)
            vi = prev[vi]
    chosen_nodes.reverse()

    planned = [replace(pool[nodes[vi][0]], start_bearing=nodes[vi][1])
               for vi in chosen_nodes]
    chosen_keys = {c.key for c in planned}

    for k, c in enumerate(pool):
        if c.key in chosen_keys:
            continue
        # Explain the loss in terms of the plan, so an operator can see what a
        # pass was traded for rather than just that it was dropped.
        rival = next((p for p in planned if _time_overlap(c, p)), None)
        if rival is not None:
            decisions[c.key] = Decision(
                c, "conflict",
                f"overlaps {rival.name or rival.norad} at {rival.aos:%H:%M}Z, "
                f"which scores {rival.score:.2f} to this pass's {c.score:.2f}",
                blocked_by=rival.key,
            )
            continue
        before = next((p for p in reversed(planned) if p.los <= c.aos), None)
        after = next((p for p in planned if p.aos >= c.los), None)
        if not _fits_between(c, before, after, slew, now, origin_az):
            neighbour = before or after
            if neighbour is not None:
                decisions[c.key] = Decision(
                    c, "infeasible",
                    f"not enough time to slew between this and "
                    f"{neighbour.name or neighbour.norad} at {neighbour.aos:%H:%M}Z",
                    blocked_by=neighbour.key,
                )
            else:
                decisions[c.key] = Decision(
                    c, "infeasible",
                    "the antenna cannot reach where it rises before it rises",
                )
        else:
            decisions[c.key] = Decision(c, "conflict", "a better combination excludes it")

    for p in planned:
        decisions[p.key] = Decision(p, "planned", _why_planned(p))

    ordered = sorted(decisions.values(), key=lambda d: (d.candidate.aos, d.candidate.key))
    return Plan(
        built_at=now,
        horizon_h=0.0,
        planned=planned,
        decisions=ordered,
        total_score=round(sum(p.score for p in planned), 4),
    )


def _fits_between(c: Candidate, before: Candidate | None, after: Candidate | None,
                  slew: SlewModel, now: datetime, origin_az: float | None) -> bool:
    """Could c be slotted between its planned neighbours, on any branch?"""
    for b in branches(c, slew):
        if before is not None:
            end = (before.start_bearing if before.start_bearing is not None
                   else before.aos_az) + sweep_of(before)
            if (c.aos - before.los).total_seconds() < turn_s(slew, end, b):
                continue
        elif origin_az is not None and c.aos > now:
            if now + timedelta(seconds=turn_s(slew, origin_az, b)) > c.aos:
                continue
        if after is not None:
            nb = after.start_bearing if after.start_bearing is not None else after.aos_az
            if (after.aos - c.los).total_seconds() < turn_s(slew, b + sweep_of(c), nb):
                continue
        return True
    return False


def _time_overlap(a: Candidate, b: Candidate) -> bool:
    return a.aos < b.los and b.aos < a.los


def _why_planned(c: Candidate) -> str:
    return (
        f"peaks {c.max_el:.0f}°, {c.duration_s / 60:.0f} min, "
        f"priority {c.priority:g}, score {c.score:.2f}"
    )
