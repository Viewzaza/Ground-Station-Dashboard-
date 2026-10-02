"""Planner tests.

The selection tests are built around the specific ways a naive scheduler goes
wrong: taking one excellent pass that blocks two good ones, ignoring how long
the antenna needs to get from one pass to the next, and planning over a pass
SatNOGS has already claimed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import combinations

import pytest

from app.services.planner import (
    Candidate,
    Reservation,
    ScoreWeights,
    SlewModel,
    link_gain_db,
    score_pass,
    select,
    slant_range_km,
)

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
FAST = SlewModel(az_rate_deg_s=1000.0, el_rate_deg_s=1000.0, setup_s=0.0)


def cand(key: str, start_min: float, dur_min: float, score: float, *,
         norad: int = 1, aos_az: float = 0.0, los_az: float = 0.0,
         max_el: float = 45.0, priority: float = 1.0) -> Candidate:
    aos = T0 + timedelta(minutes=start_min)
    los = aos + timedelta(minutes=dur_min)
    return Candidate(
        key=key, norad=norad, name=key,
        aos=aos, tca=aos + (los - aos) / 2, los=los,
        max_el=max_el, aos_az=aos_az, los_az=los_az,
        priority=priority, score=score,
    )


def planned_keys(plan) -> list[str]:
    return [c.key for c in plan.planned]


def brute_force_best(cands, slew) -> float:
    """Check every subset. Only feasible for tiny inputs — which is the point:
    it is an independent oracle for the DAG."""
    from app.services.planner import _compatible

    best = 0.0
    ordered = sorted(cands, key=lambda c: c.aos)
    for r in range(1, len(ordered) + 1):
        for subset in combinations(ordered, r):
            if all(_compatible(a, b, slew) for a, b in zip(subset, subset[1:])):
                best = max(best, sum(c.score for c in subset))
    return best


# --------------------------------------------------------------------------
# optimality
# --------------------------------------------------------------------------

def test_two_good_passes_beat_one_excellent_pass_that_blocks_both():
    """The failure that makes greedy-by-score wrong."""
    cands = [
        cand("excellent", 0, 30, 1.5),
        cand("good-a", 0, 12, 1.0),
        cand("good-b", 15, 12, 1.0),
    ]
    plan = select(cands, slew=FAST, now=T0 - timedelta(hours=1))
    assert set(planned_keys(plan)) == {"good-a", "good-b"}
    assert plan.total_score == pytest.approx(2.0)


def test_one_excellent_pass_wins_when_it_really_is_worth_more():
    cands = [
        cand("excellent", 0, 30, 2.5),
        cand("good-a", 0, 12, 1.0),
        cand("good-b", 15, 12, 1.0),
    ]
    plan = select(cands, slew=FAST, now=T0 - timedelta(hours=1))
    assert planned_keys(plan) == ["excellent"]


@pytest.mark.parametrize("seed", range(12))
def test_matches_brute_force_on_random_schedules(seed):
    """The DAG answer must equal the exhaustive optimum, including when the
    slew time between passes makes some adjacent pairs incompatible."""
    import random

    rng = random.Random(seed)
    slew = SlewModel(az_rate_deg_s=2.0, el_rate_deg_s=2.0, setup_s=30.0)
    cands = []
    for k in range(9):
        start = rng.uniform(0, 240)
        cands.append(cand(
            f"p{k}", start, rng.uniform(4, 14), round(rng.uniform(0.1, 2.0), 3),
            aos_az=rng.uniform(0, 360), los_az=rng.uniform(0, 360),
        ))
    plan = select(cands, slew=slew, now=T0 - timedelta(hours=1))
    assert plan.total_score == pytest.approx(brute_force_best(cands, slew), abs=1e-3)


def test_planned_passes_never_overlap():
    import random

    rng = random.Random(99)
    cands = [cand(f"p{k}", rng.uniform(0, 600), rng.uniform(4, 15),
                  rng.uniform(0.1, 2.0)) for k in range(60)]
    plan = select(cands, slew=SlewModel(), now=T0 - timedelta(hours=1))
    for a, b in zip(plan.planned, plan.planned[1:]):
        assert a.los <= b.aos


# --------------------------------------------------------------------------
# turnaround
# --------------------------------------------------------------------------

def test_a_pass_that_cannot_be_reached_in_time_is_marked_infeasible():
    """Ends at 0°, the next rises at 180° only 20 s later: at 2°/s that is a
    90-second slew. Overlap is not the only way two passes conflict."""
    slew = SlewModel(az_rate_deg_s=2.0, el_rate_deg_s=2.0, setup_s=0.0)
    cands = [
        cand("first", 0, 10, 1.0, los_az=0.0),
        cand("second", 10 + 20 / 60, 10, 0.9, aos_az=180.0),
    ]
    plan = select(cands, slew=slew, now=T0 - timedelta(hours=1))
    assert planned_keys(plan) == ["first"]
    second = next(d for d in plan.decisions if d.candidate.key == "second")
    assert second.status == "infeasible"
    assert "slew" in second.reason


def test_the_same_gap_is_fine_when_the_next_pass_rises_where_the_last_set():
    slew = SlewModel(az_rate_deg_s=2.0, el_rate_deg_s=2.0, setup_s=0.0)
    cands = [
        cand("first", 0, 10, 1.0, los_az=170.0),
        cand("second", 10 + 20 / 60, 10, 0.9, aos_az=180.0),
    ]
    plan = select(cands, slew=slew, now=T0 - timedelta(hours=1))
    assert planned_keys(plan) == ["first", "second"]


def test_setup_time_is_respected_even_with_no_slew():
    slew = SlewModel(az_rate_deg_s=1000.0, el_rate_deg_s=1000.0, setup_s=60.0)
    cands = [cand("a", 0, 10, 1.0), cand("b", 10.5, 10, 1.0)]   # 30 s gap
    plan = select(cands, slew=slew, now=T0 - timedelta(hours=1))
    assert len(plan.planned) == 1


# --------------------------------------------------------------------------
# slew model
# --------------------------------------------------------------------------

def test_axes_move_together_so_the_slower_axis_decides():
    slew = SlewModel(az_rate_deg_s=2.0, el_rate_deg_s=1.0)
    # 90° of az (45 s) and 60° of el (60 s) -> 60 s, not 105 s.
    assert slew.seconds(0, 0, 90, 60) == pytest.approx(60.0)


def test_the_short_way_round_through_north_is_used():
    slew = SlewModel()
    # 350 -> 10 is 20° through north (via 370), not 340° the long way.
    assert slew.az_travel(350.0, 10.0) == pytest.approx(20.0)


def test_the_end_stop_forces_the_long_way_round():
    """At +530 the short way to 10° would need +550, past the 540 limit; the
    rotator has to unwind to 370 instead. Assuming the short way here
    under-estimates the slew by most of a turn."""
    slew = SlewModel(min_az=-180.0, max_az=540.0)
    assert slew.az_travel(530.0, 10.0) == pytest.approx(160.0)


def test_a_rotator_without_overlap_cannot_cut_through_north():
    slew = SlewModel(min_az=0.0, max_az=360.0)
    assert slew.az_travel(350.0, 10.0) == pytest.approx(340.0)


# --------------------------------------------------------------------------
# SatNOGS reservations
# --------------------------------------------------------------------------

def test_a_pass_satnogs_already_scheduled_is_left_to_satnogs():
    c = cand("ks2", 30, 10, 2.0, norad=67683)
    r = Reservation(start=c.aos, end=c.los, norad=67683, job_id=42)
    plan = select([c], slew=FAST, reservations=[r], now=T0)
    assert plan.planned == []
    assert plan.decisions[0].status == "satnogs"


def test_a_satnogs_job_for_another_satellite_blocks_the_window():
    c = cand("ours", 30, 10, 2.0, norad=67683)
    r = Reservation(start=c.aos + timedelta(minutes=3),
                    end=c.los + timedelta(minutes=5), norad=25544, job_id=7)
    plan = select([c], slew=FAST, reservations=[r], now=T0)
    assert plan.decisions[0].status == "reserved"
    assert "25544" in plan.decisions[0].reason


def test_the_guard_band_around_a_reservation_is_honoured():
    c = cand("ours", 30, 10, 2.0)
    r = Reservation(start=c.los + timedelta(seconds=60),
                    end=c.los + timedelta(minutes=10), norad=2)
    clear = select([c], slew=FAST, reservations=[r], guard_s=0, now=T0)
    guarded = select([c], slew=FAST, reservations=[r], guard_s=300, now=T0)
    assert clear.decisions[0].status == "planned"
    assert guarded.decisions[0].status == "reserved"


# --------------------------------------------------------------------------
# filtering and explanations
# --------------------------------------------------------------------------

def test_past_and_low_passes_are_explained_not_dropped():
    cands = [
        cand("gone", -60, 10, 2.0),
        cand("low", 30, 6, 2.0, max_el=4.0),
        cand("ok", 90, 10, 1.0, max_el=40.0),
    ]
    plan = select(cands, slew=FAST, floor_el=10.0, now=T0)
    status = {d.candidate.key: d.status for d in plan.decisions}
    assert status == {"gone": "past", "low": "low", "ok": "planned"}


def test_every_candidate_gets_exactly_one_decision():
    import random

    rng = random.Random(5)
    cands = [cand(f"p{k}", rng.uniform(-30, 600), rng.uniform(4, 15),
                  rng.uniform(0.1, 2.0), max_el=rng.uniform(2, 85)) for k in range(40)]
    plan = select(cands, slew=SlewModel(), floor_el=10.0, now=T0)
    assert sorted(d.candidate.key for d in plan.decisions) == sorted(c.key for c in cands)


def test_a_lost_conflict_names_the_pass_that_won():
    cands = [cand("winner", 0, 10, 2.0), cand("loser", 5, 10, 1.0)]
    plan = select(cands, slew=FAST, now=T0 - timedelta(hours=1))
    loser = next(d for d in plan.decisions if d.candidate.key == "loser")
    assert loser.status == "conflict"
    assert loser.blocked_by == "winner"


def test_empty_input_gives_an_empty_plan():
    plan = select([], slew=FAST, now=T0)
    assert plan.planned == [] and plan.decisions == [] and plan.total_score == 0


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def test_slant_range_at_zenith_is_the_altitude():
    assert slant_range_km(90.0, 420.0) == pytest.approx(420.0, rel=1e-6)


def test_a_high_pass_is_about_ten_db_better_than_a_low_one():
    gain = link_gain_db(90.0, floor_el=10.0, alt_km=420.0)
    assert 9.0 < gain < 11.5


def test_higher_passes_score_higher():
    low, _ = score_pass(max_el=15, duration_s=400, priority=1, hours_since_heard=0, floor_el=10)
    high, _ = score_pass(max_el=75, duration_s=400, priority=1, hours_since_heard=0, floor_el=10)
    assert high > low


def test_priority_multiplies_rather_than_adds():
    """A perfect pass of an unimportant satellite must not outscore a middling
    pass of the one this station exists for."""
    ours, _ = score_pass(max_el=30, duration_s=400, priority=10,
                         hours_since_heard=0, floor_el=10)
    theirs, _ = score_pass(max_el=89, duration_s=800, priority=1,
                           hours_since_heard=24, floor_el=10)
    assert ours > theirs


def test_neglect_raises_the_score():
    fresh, _ = score_pass(max_el=40, duration_s=400, priority=1,
                          hours_since_heard=0, floor_el=10)
    stale, _ = score_pass(max_el=40, duration_s=400, priority=1,
                          hours_since_heard=48, floor_el=10)
    assert stale > fresh


def test_terms_are_normalised():
    _, breakdown = score_pass(max_el=90, duration_s=5000, priority=1,
                              hours_since_heard=1000, floor_el=10,
                              weights=ScoreWeights())
    terms = dict(breakdown)
    for name in ("elevation", "duration", "freshness"):
        assert 0.0 <= terms[name] <= 1.0
