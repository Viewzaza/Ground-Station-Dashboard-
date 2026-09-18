"""Scoring candidate passes and picking a conflict-free set.

Selection is greedy by score, which is what the official satnogs-auto-scheduler
does and what makes the "the mission satellite always wins" guarantee trivial:
mission passes form their own tier and are placed before anything else is even
considered, so no amount of scoring can bump them.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .db_client import Transmitter
from .predictor import Pass
from .priorities import PRIORITY_WEIGHT, SCARCITY_WEIGHT, Priority

log = logging.getLogger(__name__)

TIER_MISSION = 0
TIER_NORMAL = 1


@dataclass
class Candidate:
    pass_: Pass
    transmitter: Transmitter
    tier: int
    priority_weight: float
    scarcity: float
    observed_here: int

    @property
    def score(self) -> float:
        return PRIORITY_WEIGHT * self.priority_weight + SCARCITY_WEIGHT * self.scarcity

    @property
    def tiebreak(self) -> float:
        """Geometry only breaks ties - a higher, longer pass is a better one."""
        return self.pass_.max_el * self.pass_.duration_min

    @property
    def sort_key(self) -> tuple:
        return (self.tier, -self.score, -self.tiebreak, self.pass_.aos)


@dataclass
class Slot:
    """A pass we have decided to book, with its final recording window."""
    candidate: Candidate
    start: datetime
    end: datetime

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass
class Selection:
    slots: list[Slot] = field(default_factory=list)
    considered: int = 0
    rejected_conflict: int = 0
    rejected_capped: int = 0


def build_candidates(
    passes: list[Pass],
    transmitters: dict[int, list[Transmitter]],
    priorities: dict[int, Priority],
    scarcity: dict[int, float],
    history,
    mission_norad: int | None,
    pick_transmitter,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for p in passes:
        available = transmitters.get(p.norad_cat_id)
        if not available:
            continue
        priority = priorities.get(p.norad_cat_id)
        tx = pick_transmitter(available, priority.transmitter_uuid if priority else None)
        if tx is None:
            continue
        candidates.append(
            Candidate(
                pass_=p,
                transmitter=tx,
                tier=TIER_MISSION if p.norad_cat_id == mission_norad else TIER_NORMAL,
                priority_weight=priority.weight if priority else 0.0,
                scarcity=scarcity.get(p.norad_cat_id, 0.0),
                observed_here=history.get(p.norad_cat_id, 0),
            )
        )
    return candidates


def recording_window(
    p: Pass, max_duration_s: float
) -> tuple[datetime, datetime]:
    """The slice of the pass we will actually record.

    Most passes are recorded whole. A very long one gets centred on closest
    approach, because that is where the elevation - and so the link - is best.
    """
    if p.duration_s <= max_duration_s:
        return p.aos, p.los
    half = timedelta(seconds=max_duration_s / 2.0)
    start, end = p.tca - half, p.tca + half
    if start < p.aos:
        start, end = p.aos, p.aos + timedelta(seconds=max_duration_s)
    if end > p.los:
        start, end = p.los - timedelta(seconds=max_duration_s), p.los
    return start, end


class Calendar:
    """Occupied intervals, kept sorted and disjoint so overlap checks are cheap.

    Insertion merges anything that overlaps. That is not just tidiness: the
    cheap overlap test below looks only at the last interval starting before
    the query, which is correct precisely because the stored intervals never
    overlap each other. Bookings read back from the API are not guaranteed
    disjoint, so we enforce the invariant rather than assume it.
    """

    def __init__(self, buffer_s: float) -> None:
        self.buffer = timedelta(seconds=buffer_s)
        self._starts: list[datetime] = []
        self._ends: list[datetime] = []

    def conflicts(self, start: datetime, end: datetime) -> bool:
        lo = start - self.buffer
        hi = end + self.buffer
        # Intervals are disjoint and sorted, so ends are sorted too: the only
        # one that can reach past `lo` is the last one starting at or before
        # `hi`. Anything later starts after we would have finished.
        i = bisect.bisect_right(self._starts, hi)
        return i > 0 and self._ends[i - 1] > lo

    def add(self, start: datetime, end: datetime) -> None:
        i = bisect.bisect_left(self._starts, start)
        self._starts.insert(i, start)
        self._ends.insert(i, end)
        # Absorb any neighbour this one now touches, on either side.
        j = i
        while j > 0 and self._ends[j - 1] >= self._starts[j]:
            j -= 1
            self._ends[j] = max(self._ends[j], self._ends[j + 1])
            del self._starts[j + 1], self._ends[j + 1]
        while j + 1 < len(self._starts) and self._ends[j] >= self._starts[j + 1]:
            self._ends[j] = max(self._ends[j], self._ends[j + 1])
            del self._starts[j + 1], self._ends[j + 1]

    def __len__(self) -> int:
        return len(self._starts)


def select(
    candidates: list[Candidate],
    booked: list[tuple[datetime, datetime]],
    *,
    buffer_s: float,
    max_schedule: int,
    max_duration_s: float,
) -> Selection:
    """Greedy, conflict-free selection over the scored candidates."""
    calendar = Calendar(buffer_s)
    for start, end in booked:
        calendar.add(start, end)

    result = Selection(considered=len(candidates))
    for candidate in sorted(candidates, key=lambda c: c.sort_key):
        if len(result.slots) >= max_schedule:
            result.rejected_capped += 1
            continue
        start, end = recording_window(candidate.pass_, max_duration_s)
        if calendar.conflicts(start, end):
            result.rejected_conflict += 1
            continue
        calendar.add(start, end)
        result.slots.append(Slot(candidate=candidate, start=start, end=end))

    result.slots.sort(key=lambda s: s.start)
    return result
