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
    peak_pointing_error,
    score_pass,
    select,
    slant_range_km,
)

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
# Effectively instant, with no floors, so selection tests test selection.
FAST = SlewModel(az_rate_deg_s=1000.0, el_rate_deg_s=1000.0, setup_s=0.0,
                 accel_margin_s=0.0, min_turnaround_s=0.0)
# Pure slew arithmetic: real rates, but no margins or floors.
def bare(**kw):
    base = dict(az_rate_deg_s=2.0, el_rate_deg_s=2.0, setup_s=0.0,
                accel_margin_s=0.0, min_turnaround_s=0.0)
    return SlewModel(**{**base, **kw})



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
    slew = bare()
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
    slew = bare()
    cands = [
        cand("first", 0, 10, 1.0, los_az=170.0),
        cand("second", 10 + 20 / 60, 10, 0.9, aos_az=180.0),
    ]
    plan = select(cands, slew=slew, now=T0 - timedelta(hours=1))
    assert planned_keys(plan) == ["first", "second"]


def test_setup_time_is_respected_even_with_no_slew():
    slew = bare(az_rate_deg_s=1000.0, el_rate_deg_s=1000.0, setup_s=60.0)
    cands = [cand("a", 0, 10, 1.0), cand("b", 10.5, 10, 1.0)]   # 30 s gap
    plan = select(cands, slew=slew, now=T0 - timedelta(hours=1))
    assert len(plan.planned) == 1


# --------------------------------------------------------------------------
# slew model
# --------------------------------------------------------------------------

def test_axes_move_together_so_the_slower_axis_decides():
    slew = bare(az_rate_deg_s=2.0, el_rate_deg_s=1.0)
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


# --------------------------------------------------------------------------
# cable wrap: the whole pass has to fit
# --------------------------------------------------------------------------

def test_a_start_bearing_must_leave_room_for_the_whole_sweep():
    """At +500 the pass rising at 140° could be met at 500 — but it sweeps a
    further +100°, to 600, past the 540 limit. It has to be met at 140."""
    slew = bare(min_az=-180.0, max_az=540.0)
    start, _ = slew.best_start(500.0, 140.0, sweep=+100.0)
    assert start == pytest.approx(140.0)
    assert start + 100.0 <= 540.0


def test_without_a_sweep_the_near_branch_is_fine():
    slew = bare(min_az=-180.0, max_az=540.0)
    start, travel = slew.best_start(500.0, 140.0, sweep=0.0)
    assert start == pytest.approx(500.0)
    assert travel == pytest.approx(0.0)


def test_when_no_branch_fits_the_cost_is_a_full_unwind():
    """A rotator with a single turn of travel cannot hold a 300° sweep from
    anywhere; charge the honest cost rather than pretending it fits."""
    slew = bare(min_az=0.0, max_az=360.0)
    _, travel = slew.best_start(10.0, 20.0, sweep=+350.0)
    assert travel >= 360.0


def test_a_long_sweep_can_make_a_tight_turnaround_infeasible():
    """Same gap, same bearings — only the next pass's sweep differs. If the
    near branch leaves no room for that sweep, the far branch is a long slew."""
    slew = bare(min_az=-180.0, max_az=540.0)
    a = cand("a", 0, 10, 1.0, los_az=170.0)
    b_short = cand("b", 10 + 40 / 60, 10, 1.0, aos_az=530.0 - 360.0)   # 170°
    fits = select([a, b_short], slew=slew, now=T0 - timedelta(hours=1))
    assert len(fits.planned) == 2

    # Pass rising at 170° that sweeps -400°: from 170 that would reach -230,
    # past -180. Meeting it at 530 instead is a 360° slew — 3 minutes at 2°/s.
    b_long = Candidate(**{**b_short.__dict__, "key": "b2", "az_sweep": -400.0})
    blocked = select([a, b_long], slew=slew, now=T0 - timedelta(hours=1))
    assert len(blocked.planned) == 1


# --------------------------------------------------------------------------
# turnaround floors
# --------------------------------------------------------------------------

def test_the_community_minimum_turnaround_applies_even_with_no_slew():
    slew = SlewModel(az_rate_deg_s=1000.0, el_rate_deg_s=1000.0, setup_s=0.0,
                     accel_margin_s=0.0, min_turnaround_s=60.0)
    assert slew.turnaround(100, 0, 100, 0) == pytest.approx(60.0)


def test_acceleration_margin_is_only_charged_for_an_actual_move():
    slew = SlewModel(az_rate_deg_s=2.0, el_rate_deg_s=2.0, accel_margin_s=4.0)
    assert slew.seconds(100, 0, 100, 0) == pytest.approx(0.0)
    assert slew.seconds(100, 0, 120, 0) == pytest.approx(10.0 + 4.0)


def test_default_rates_are_the_conservative_ones():
    """The Rot2Prog controller does not say which SPID rotator it drives, and
    the rates vary 1.5-3 deg/s. Defaulting to the slowest keeps plans honest."""
    from app.config import Settings

    s = Settings()
    assert s.rotator_az_rate_deg_s <= 1.5
    assert s.rotator_el_rate_deg_s <= 1.5


# --------------------------------------------------------------------------
# the keyhole
# --------------------------------------------------------------------------

def _overhead_track(max_el: float, alt_km: float = 400.0, step_s: float = 1.0):
    """A straight-line pass over (or near) the station, sampled in az/el.

    Flat-earth geometry is plenty to exercise the follower: what matters is the
    azimuth whipping round near TCA, and that is reproduced exactly."""
    import math as _m
    v = 7.67                                   # km/s ground track speed
    offset = alt_km / _m.tan(_m.radians(max_el)) if max_el < 89.99 else 0.0
    out = []
    t = T0
    for k in range(-300, 301, int(step_s)):
        x = v * k                               # along-track, km
        rng = _m.hypot(x, offset)
        el = _m.degrees(_m.atan2(alt_km, rng))
        az = (_m.degrees(_m.atan2(x, offset if offset else 1e-6)) + 360.0) % 360.0
        out.append({"t": t + timedelta(seconds=k), "az": az, "el": el})
    return out


def test_a_rotator_keeps_up_with_a_moderate_pass():
    err = peak_pointing_error(_overhead_track(45.0), 1.5, 1.5)
    assert err < 1.0


def test_an_overhead_pass_outruns_the_rotator():
    """The research table: at 80°+ a ~2.5°/s rotator lags ~15° at TCA, more
    at lower rates. The exact figure depends on geometry; the shape must hold."""
    e60 = peak_pointing_error(_overhead_track(60.0), 1.5, 1.5)
    e80 = peak_pointing_error(_overhead_track(80.0), 1.5, 1.5)
    e89 = peak_pointing_error(_overhead_track(89.0), 1.5, 1.5)
    assert e60 < e80 < e89
    assert e80 > 5.0
    assert e89 > 20.0


def test_a_faster_rotator_suffers_less_in_the_keyhole():
    slow = peak_pointing_error(_overhead_track(85.0), 1.5, 1.5)
    fast = peak_pointing_error(_overhead_track(85.0), 6.0, 6.0)
    assert fast < slow


def test_keyhole_derate_is_three_db_at_half_the_beamwidth():
    from app.services.planner import keyhole_derate

    assert keyhole_derate(0.0, 40.0) == 1.0
    assert keyhole_derate(20.0, 40.0) == pytest.approx(10 ** (-0.3), rel=1e-6)


def test_on_this_hardware_an_overhead_pass_scores_below_a_seventy_degree_one():
    """The counter-intuitive result the research quantified: through the
    keyhole, the best part of an overhead pass is spent pointing behind it."""
    def score(max_el):
        err = peak_pointing_error(_overhead_track(max_el), 1.5, 1.5)
        total, _ = score_pass(max_el=max_el, duration_s=600, priority=1,
                              hours_since_heard=0, floor_el=10,
                              keyhole_error_deg=err)
        return total

    assert score(89.0) < score(70.0)


# --------------------------------------------------------------------------
# research-backed scoring terms
# --------------------------------------------------------------------------

def test_duration_is_scored_as_expected_beacons():
    """KNACKSAT-2 beacons once a minute: a 9m59s pass hears nine, not ten."""
    _, a = score_pass(max_el=40, duration_s=599, priority=1, hours_since_heard=0, floor_el=10)
    _, b = score_pass(max_el=40, duration_s=600, priority=1, hours_since_heard=0, floor_el=10)
    assert dict(a)["duration"] == pytest.approx(0.9)
    assert dict(b)["duration"] == pytest.approx(1.0)


def test_freshness_is_half_after_one_half_life():
    _, bd = score_pass(max_el=40, duration_s=600, priority=1,
                       hours_since_heard=12, floor_el=10)
    assert dict(bd)["freshness"] == pytest.approx(0.5, abs=1e-3)


# Independently computed by a separate simulation (400 km, rate-limited
# follower). Two implementations agreeing is the check; the 89° row differs by
# a few percent because this fixture uses flat-earth geometry.
@pytest.mark.parametrize("rate,max_el,expected", [
    (2.5, 70, 2.2), (2.5, 75, 7.5), (2.5, 80, 14.8), (2.5, 85, 25.5),
    (3.0, 75, 3.6), (3.0, 80, 9.9), (3.0, 85, 19.7),
    (5.0, 80, 1.4), (5.0, 85, 7.9),
])
def test_keyhole_lag_matches_an_independent_simulation(rate, max_el, expected):
    ours = peak_pointing_error(_overhead_track(max_el), rate, rate)
    assert ours == pytest.approx(expected, rel=0.06, abs=0.3)
