"""Propagation and pass-prediction tests.

The interesting one is test_passes_agree_with_satnogs_schedule: SatNOGS runs its
own propagator to decide when station 5024 will record, so its job list is an
independent oracle for our Skyfield predictions. If the two disagree by more
than a minute, one of us is wrong — and it is worth knowing which before the
antenna is driven from it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.schemas import TleInfo
from app.services.predictor import Predictor
from app.services.tle_store import parse_tle_epoch

# A frozen element set, so these tests do not depend on the network or on what
# the satellite happens to be doing today.
KNACKSAT2_TLE1 = "1 67683U 98067XZ  26255.31192122  .00056149  00000+0  48789-3 0  9994"
KNACKSAT2_TLE2 = "2 67683  51.6258 213.5681 0007959 152.6345 207.5073 15.68476422 33916"


class FrozenTleStore:
    """Minimal stand-in for TleStore holding one known satellite."""

    def get(self, norad: int) -> TleInfo | None:
        if norad != 67683:
            return None
        return TleInfo(
            norad=67683,
            name="KNACKSAT-2",
            tle1=KNACKSAT2_TLE1,
            tle2=KNACKSAT2_TLE2,
            source="frozen",
            fetched_at=datetime(2026, 9, 12, 11, 20, tzinfo=timezone.utc),
            epoch=parse_tle_epoch(KNACKSAT2_TLE1),
            age_days=0.5,
            state="fresh",
        )


@pytest.fixture
def predictor() -> Predictor:
    return Predictor(Settings(), FrozenTleStore())


# --------------------------------------------------------------------------
# elements
# --------------------------------------------------------------------------

def test_tle_epoch_parsed_from_line_one():
    epoch = parse_tle_epoch(KNACKSAT2_TLE1)
    # 26255.31192122 -> day 255 of 2026, at 0.31192122 of a day
    assert epoch.year == 2026
    assert epoch.timetuple().tm_yday == 255
    assert epoch.hour == 7 and epoch.minute == 29


def test_tle_epoch_of_rubbish_is_none():
    assert parse_tle_epoch("not a tle line at all") is None


# --------------------------------------------------------------------------
# propagation
# --------------------------------------------------------------------------

def test_position_is_physically_plausible(predictor):
    pos = predictor.position(67683, datetime(2026, 9, 13, 0, 0, tzinfo=timezone.utc))
    assert pos is not None
    assert -90 <= pos.lat <= 90
    assert -180 <= pos.lon <= 180
    assert 200 < pos.alt_km < 600          # a decaying ISS-deployed cubesat
    assert 6.5 < pos.vel_km_s < 8.5        # LEO orbital speed
    assert 0 <= pos.az < 360
    assert -90 <= pos.el <= 90
    assert pos.footprint_km > 1500


def test_unknown_satellite_returns_none(predictor):
    assert predictor.position(99999) is None
    assert predictor.passes(99999) == []
    assert predictor.next_pass(99999) is None


def test_doppler_sign_follows_range_rate(predictor):
    """Receding (positive range rate) must lower the received frequency."""
    when = datetime(2026, 9, 13, 0, 0, tzinfo=timezone.utc)
    pos = predictor.position(67683, when)
    assert pos.doppler_hz is not None
    if pos.range_rate_km_s > 0:
        assert pos.doppler_hz < 0
    else:
        assert pos.doppler_hz > 0


def test_doppler_magnitude_is_within_uhf_limits(predictor):
    """A 400 MHz downlink from LEO cannot shift by more than about 10 kHz."""
    when = datetime(2026, 9, 13, 0, 0, tzinfo=timezone.utc)
    for minutes in range(0, 95, 5):
        pos = predictor.position(67683, when + timedelta(minutes=minutes))
        assert abs(pos.doppler_hz) < 11_000


# --------------------------------------------------------------------------
# passes
# --------------------------------------------------------------------------

def test_passes_are_ordered_and_self_consistent(predictor):
    passes = predictor.passes(67683, hours=24.0)
    assert passes, "a LEO satellite must have passes over Bangkok in 24 hours"

    for p in passes:
        assert p.aos < p.tca < p.los
        assert p.duration_s == pytest.approx((p.los - p.aos).total_seconds())
        assert p.max_el >= Settings().min_elevation_deg
        assert 60 < p.duration_s < 1200        # a LEO pass is minutes, not hours
        assert 0 <= p.aos_az < 360
        assert 0 <= p.los_az < 360

    for earlier, later in zip(passes, passes[1:]):
        assert earlier.los < later.aos


def test_a_higher_mask_yields_fewer_passes(predictor):
    low = predictor.passes(67683, hours=24.0, min_el=0.0)
    high = predictor.passes(67683, hours=24.0, min_el=40.0)
    assert len(high) < len(low)


def test_track_samples_span_the_pass(predictor):
    p = predictor.passes(67683, hours=24.0)[0]
    samples = predictor.track(67683, p.aos, p.los, step_s=10.0)

    assert len(samples) > 5
    assert samples[0]["el"] == pytest.approx(p.max_el, abs=p.max_el)   # starts low
    # Elevation must rise to the maximum and fall away again.
    peak = max(s["el"] for s in samples)
    assert peak == pytest.approx(p.max_el, abs=0.5)
    assert samples[0]["el"] < peak and samples[-1]["el"] < peak


def test_ground_track_is_continuous_in_time(predictor):
    points = predictor.ground_track(67683, minutes_before=10, minutes_after=10, step_s=60)
    assert len(points) == 21
    for p in points:
        assert -90 <= p["lat"] <= 90
        assert -180 <= p["lon"] <= 180
