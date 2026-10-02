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
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class SlewModel:
    """How fast the antenna can move, and how long a station needs to get ready.

    Both axes move at once on a SPID, so the time for a move is the slower of
    the two axes, not their sum. Rates are deliberately conservative: a plan
    that assumes the rotator is faster than it is schedules passes that start
    with the antenna still swinging into place, which loses the first minute of
    every pass — usually the low, Doppler-heavy part that is worst already.
    """
    az_rate_deg_s: float = 2.0
    el_rate_deg_s: float = 2.0
    setup_s: float = 30.0            # retune, start the recorder, settle
    min_az: float = -180.0
    max_az: float = 540.0

    def seconds(self, from_az: float, from_el: float,
                to_az: float, to_el: float) -> float:
        az_travel = self.az_travel(from_az, to_az)
        el_travel = abs(to_el - from_el)
        return max(az_travel / self.az_rate_deg_s, el_travel / self.el_rate_deg_s)

    def az_travel(self, from_az: float, to_az: float) -> float:
        """Degrees of azimuth the rotator must actually turn.

        The short way round is usually available — a SPID's -180..540 range
        holds every bearing at least twice — but not always: near either end
        stop, the short way would drive past the limit and the rotator has to
        go round the long way instead. Getting this wrong under-estimates the
        slew by up to 180 degrees, which at 2 deg/s is a minute and a half.
        """
        best: float | None = None
        for k in range(-2, 3):
            target = to_az + 360.0 * k
            if self.min_az <= target <= self.max_az:
                travel = abs(target - from_az)
                if best is None or travel < best:
                    best = travel
        # No representation within the limits (a misconfigured range): assume
        # the worst rather than the best.
        return best if best is not None else 360.0


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoreWeights:
    elevation: float = 0.55
    duration: float = 0.30
    freshness: float = 0.15
    # Hours without a planned or recorded pass after which a satellite counts
    # as fully neglected. Past this, its freshness term saturates.
    freshness_horizon_h: float = 24.0


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


def score_pass(*, max_el: float, duration_s: float, priority: float,
               hours_since_heard: float | None, floor_el: float,
               weights: ScoreWeights = ScoreWeights()
               ) -> tuple[float, tuple[tuple[str, float], ...]]:
    """A pass's value. Higher is better; priority multiplies, it does not add.

    Each term is normalised to 0..1 before weighting, so the weights mean what
    they say. Priority multiplies the whole thing because "our own satellite"
    should beat "a slightly higher pass of someone else's", and an additive
    priority would let a perfect pass of an unimportant satellite outscore a
    middling pass of the one this station exists for.
    """
    best_gain = link_gain_db(90.0, floor_el)
    elevation = link_gain_db(max_el, floor_el) / best_gain if best_gain > 0 else 0.0
    elevation = max(0.0, min(1.0, elevation))

    # 15 minutes is about the longest pass a LEO satellite gives this station.
    duration = max(0.0, min(1.0, duration_s / 900.0))

    if hours_since_heard is None:
        freshness = 1.0             # never heard: as neglected as it gets
    else:
        freshness = max(0.0, min(1.0, hours_since_heard / weights.freshness_horizon_h))

    base = (
        weights.elevation * elevation
        + weights.duration * duration
        + weights.freshness * freshness
    )
    total = base * max(0.0, priority)
    return total, (
        ("elevation", round(elevation, 3)),
        ("duration", round(duration, 3)),
        ("freshness", round(freshness, 3)),
        ("priority", round(priority, 3)),
    )


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

def _overlaps(c: Candidate, r: Reservation, guard_s: float) -> bool:
    guard = timedelta(seconds=guard_s)
    return c.aos < r.end + guard and c.los > r.start - guard


def select(candidates: Iterable[Candidate], *, slew: SlewModel,
           reservations: Iterable[Reservation] = (), guard_s: float = 0.0,
           floor_el: float = 0.0, now: datetime | None = None) -> Plan:
    """The highest-scoring set of passes that the antenna can physically work.

    Exact, not heuristic: a longest path through the DAG of compatible passes.
    See the module docstring for why the textbook O(n log n) method does not
    apply when the turnaround depends on the pair.
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
        pool.append(c)

    # A pass already under way must start from wherever the antenna is, not be
    # judged on whether it could be reached by its AOS; treat it as reachable.
    n = len(pool)
    best = [0.0] * n            # best total of a plan whose last pass is j
    prev: list[int | None] = [None] * n

    for j, cj in enumerate(pool):
        best[j] = cj.score
        for i in range(j):
            ci = pool[i]
            if not _compatible(ci, cj, slew):
                continue
            if best[i] + cj.score > best[j]:
                best[j] = best[i] + cj.score
                prev[j] = i

    chosen: list[int] = []
    if n:
        j: int | None = max(range(n), key=lambda k: (best[k], -k))
        while j is not None:
            chosen.append(j)
            j = prev[j]
    chosen.reverse()
    chosen_set = set(chosen)
    planned = [pool[k] for k in chosen]

    for k, c in enumerate(pool):
        if k in chosen_set:
            decisions[c.key] = Decision(c, "planned", _why_planned(c))
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
        neighbour = next(
            (p for p in planned
             if not _compatible(p, c, slew) or not _compatible(c, p, slew)),
            None,
        )
        if neighbour is not None:
            decisions[c.key] = Decision(
                c, "infeasible",
                f"not enough time to slew between this and "
                f"{neighbour.name or neighbour.norad} at {neighbour.aos:%H:%M}Z",
                blocked_by=neighbour.key,
            )
        else:
            decisions[c.key] = Decision(c, "conflict", "a better combination excludes it")

    ordered = sorted(decisions.values(), key=lambda d: (d.candidate.aos, d.candidate.key))
    return Plan(
        built_at=now,
        horizon_h=0.0,
        planned=planned,
        decisions=ordered,
        total_score=round(sum(p.score for p in planned), 4),
    )


def _time_overlap(a: Candidate, b: Candidate) -> bool:
    return a.aos < b.los and b.aos < a.los


def _compatible(a: Candidate, b: Candidate, slew: SlewModel) -> bool:
    """Can the antenna finish pass a, then be ready for pass b at its AOS?

    The move is from where a sets to where b rises, both on the horizon.
    """
    if b.aos <= a.los:
        return False
    gap = (b.aos - a.los).total_seconds()
    need = slew.seconds(a.los_az, 0.0, b.aos_az, 0.0) + slew.setup_s
    return gap >= need


def _why_planned(c: Candidate) -> str:
    return (
        f"peaks {c.max_el:.0f}°, {c.duration_s / 60:.0f} min, "
        f"priority {c.priority:g}, score {c.score:.2f}"
    )
