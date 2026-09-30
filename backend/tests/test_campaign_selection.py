"""Which of a station's passes the campaign asks for.

Every station in the campaign gets asked for at most `max_per_station` passes —
two, by default, and that number is a courtesy limit rather than a tuning knob.
So the question that decides how good the campaign is is not how many passes it
books but *which two*, and that choice used to be made by clock time.

A station seeing five qualifying passes over the 48-hour window at 12, 75, 20, 8
and 45 degrees would be asked for whichever two came first. The best pass over
that station was a coin toss, decided by what time of day it happened to fall
at. Elevation is the difference between a recording and a decode — it sets the
range, the slant path through the atmosphere, and how long the spacecraft is up
— so that is the one property the choice should not be blind to.

These tests pin the replacement: the highest pass first, then the pass furthest
in elevation from everything already picked. Nothing here changes how MANY
bookings a station is asked for.
"""

from __future__ import annotations

from app.vendor.autoscheduler.campaign import _by_elevation_spread


class FakePass:
    """Only the attribute the ordering reads."""

    def __init__(self, max_el: float) -> None:
        self.max_el = max_el

    def __repr__(self) -> str:                       # readable assertion failures
        return f"<pass {self.max_el}deg>"


def order(elevations: list[float]) -> list[float]:
    gated = [(FakePass(e), None) for e in elevations]
    return [p.max_el for p, _ in _by_elevation_spread(gated)]


# --------------------------------------------------------------------------
# the best pass is not optional
# --------------------------------------------------------------------------

def test_the_highest_pass_is_taken_first():
    """If a station is only going to be asked for two things, one of them has
    to be the best it can do. Chronological order got this right only by
    luck."""
    assert order([12, 75, 20, 8, 45])[0] == 75


def test_a_high_pass_is_never_lost_to_an_earlier_low_one():
    """The regression this exists for. In clock order these five are taken as
    12 and 75, or 12 and 20, depending only on what time they fall at — and the
    second case silently throws away the best pass over that station."""
    assert 75 in order([12, 75, 20, 8, 45])[:2]


# --------------------------------------------------------------------------
# covering the range, not the top of it
# --------------------------------------------------------------------------

def test_two_picks_span_the_range_rather_than_taking_the_top_two():
    """Five passes all near 70 degrees say the same thing about the link. 75
    and 8 say where it starts to fail, which is the question an operator
    characterising a downlink is actually asking."""
    assert order([12, 75, 20, 8, 45])[:2] == [75, 8]


def test_the_third_pick_fills_the_widest_remaining_gap():
    """Farthest-point sampling, not "next highest": after 75 and 8 the useful
    third sample is the middle of the range."""
    assert order([12, 75, 20, 8, 45])[:3] == [75, 8, 45]


def test_a_cluster_of_similar_passes_still_spreads_as_far_as_it_can():
    """When nothing is far from anything, the order must still be the widest
    span available rather than an arbitrary one."""
    assert order([70, 72, 71, 69])[:2] == [72, 69]


def test_every_pass_is_kept_not_just_the_ones_that_fit():
    """The ordering re-ranks; it never drops. The per-station cap and the
    calendar conflict check decide what is actually booked, and a pass that
    conflicts must be able to fall through to the next one."""
    assert sorted(order([12, 75, 20, 8, 45])) == [8, 12, 20, 45, 75]


# --------------------------------------------------------------------------
# it runs on every station in the catalogue, so it may not raise
# --------------------------------------------------------------------------

def test_a_station_with_one_qualifying_pass_is_not_an_error():
    assert order([33]) == [33]


def test_a_station_with_no_qualifying_pass_is_not_an_error():
    """Reached for any station the satellite never usefully clears, which is
    most of the catalogue on any given run."""
    assert order([]) == []


def test_passes_at_identical_elevations_do_not_collapse_or_raise():
    """Two stations can see the same geometry, and a zero distance must not be
    read as "already covered" and drop the pass."""
    assert sorted(order([40, 40, 40])) == [40, 40, 40]


def test_the_order_is_the_same_on_every_run():
    """A booking set that changed between two runs over the same window would
    make the recent-attempts exclusion list useless, and the campaign would
    re-offer passes it had already submitted."""
    once = order([12, 75, 20, 8, 45, 61, 3])
    assert all(order([12, 75, 20, 8, 45, 61, 3]) == once for _ in range(3))
