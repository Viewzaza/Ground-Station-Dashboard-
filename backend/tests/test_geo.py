"""Geometry tests.

These exist because every one of them corresponds to a way the map has
historically been drawn wrong: a track streaking across the world, a footprint
that vanishes over the pole, a terminator on the wrong side of the planet.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from app.util.geo import (
    EARTH_RADIUS_KM,
    contains_pole,
    destination_point,
    footprint_half_angle_deg,
    footprint_radius_km,
    footprint_ring,
    split_antimeridian,
    subsolar_point,
    wrap_lon,
)


# --------------------------------------------------------------------------
# footprint
# --------------------------------------------------------------------------

def test_footprint_half_angle_matches_hand_calculation():
    # acos(Re / (Re + h)) for a 420 km orbit
    expected = math.degrees(math.acos(EARTH_RADIUS_KM / (EARTH_RADIUS_KM + 420.0)))
    assert footprint_half_angle_deg(420.0) == pytest.approx(expected, abs=1e-9)


def test_footprint_grows_with_altitude():
    assert footprint_radius_km(300) < footprint_radius_km(700) < footprint_radius_km(35786)


def test_footprint_shrinks_with_an_elevation_mask():
    """A 5-degree mask must give a smaller circle than the geometric horizon."""
    assert footprint_half_angle_deg(420, 5.0) < footprint_half_angle_deg(420, 0.0)


def test_footprint_of_a_surface_object_is_zero():
    assert footprint_half_angle_deg(0.0) == 0.0
    assert footprint_ring(0, 0, 0.0) == []


def test_footprint_ring_is_closed_and_equidistant():
    lat, lon, alt = 13.82, 100.51, 420.0
    ring = footprint_ring(lat, lon, alt, points=36)
    assert len(ring) == 37
    assert ring[0] == pytest.approx(ring[-1], abs=1e-9)

    half = footprint_half_angle_deg(alt)
    for plat, plon in ring:
        # Every point must sit exactly one half-angle from the sub-satellite point.
        cos_d = (
            math.sin(math.radians(lat)) * math.sin(math.radians(plat))
            + math.cos(math.radians(lat)) * math.cos(math.radians(plat))
            * math.cos(math.radians(plon - lon))
        )
        assert math.degrees(math.acos(max(-1.0, min(1.0, cos_d)))) == pytest.approx(half, abs=1e-6)


# --------------------------------------------------------------------------
# pole containment
# --------------------------------------------------------------------------

def test_pole_containment_detected():
    # A polar satellite high enough to see over the pole.
    assert contains_pole(85.0, 800.0) == 1
    assert contains_pole(-85.0, 800.0) == -1


def test_no_pole_containment_at_the_equator():
    assert contains_pole(0.0, 420.0) == 0


# --------------------------------------------------------------------------
# antimeridian
# --------------------------------------------------------------------------

def test_path_not_crossing_is_left_alone():
    path = [(0.0, 10.0), (1.0, 20.0), (2.0, 30.0)]
    assert split_antimeridian(path) == [path]


def test_eastbound_crossing_splits_into_two_segments():
    path = [(10.0, 170.0), (12.0, -170.0)]
    segments = split_antimeridian(path)

    assert len(segments) == 2
    # First segment must end exactly on the +180 edge, second start on -180.
    assert segments[0][-1][1] == pytest.approx(180.0)
    assert segments[1][0][1] == pytest.approx(-180.0)
    # The crossing latitude is shared, and lies between the two endpoints.
    assert segments[0][-1][0] == pytest.approx(segments[1][0][0])
    assert 10.0 <= segments[0][-1][0] <= 12.0


def test_westbound_crossing_splits_the_other_way():
    segments = split_antimeridian([(5.0, -175.0), (6.0, 175.0)])
    assert len(segments) == 2
    assert segments[0][-1][1] == pytest.approx(-180.0)
    assert segments[1][0][1] == pytest.approx(180.0)


def test_crossing_latitude_is_interpolated_not_copied():
    """The seam must be met at the interpolated latitude, not at an endpoint —
    otherwise the track visibly kinks at the edge of the map."""
    segments = split_antimeridian([(0.0, 175.0), (10.0, -175.0)])
    crossing_lat = segments[0][-1][0]
    assert crossing_lat == pytest.approx(5.0, abs=1e-9)


def test_multiple_crossings_produce_multiple_segments():
    path = [(0.0, 170.0), (0.0, -170.0), (0.0, 170.0), (0.0, -170.0)]
    assert len(split_antimeridian(path)) == 4


def test_degenerate_paths_do_not_raise():
    assert split_antimeridian([]) == []
    assert split_antimeridian([(1.0, 2.0)]) == [[(1.0, 2.0)]]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def test_wrap_lon_normalises_to_the_half_open_range():
    assert wrap_lon(190.0) == pytest.approx(-170.0)
    assert wrap_lon(-190.0) == pytest.approx(170.0)
    assert wrap_lon(180.0) == pytest.approx(-180.0)
    assert wrap_lon(0.0) == 0.0


def test_destination_point_due_north():
    lat, lon = destination_point(0.0, 0.0, 0.0, 10.0)
    assert lat == pytest.approx(10.0, abs=1e-9)
    assert lon == pytest.approx(0.0, abs=1e-9)


def test_destination_point_due_east_on_the_equator():
    lat, lon = destination_point(0.0, 0.0, 90.0, 10.0)
    assert lat == pytest.approx(0.0, abs=1e-9)
    assert lon == pytest.approx(10.0, abs=1e-9)


# --------------------------------------------------------------------------
# sub-solar point
# --------------------------------------------------------------------------

def test_subsolar_longitude_tracks_utc():
    """At 12:00 UTC the sun is near the prime meridian; at 00:00 near ±180.
    The equation of time keeps this within a couple of degrees."""
    noon = datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc)
    _, lon = subsolar_point(noon)
    assert abs(wrap_lon(lon)) < 5.0

    midnight = datetime(2026, 3, 20, 0, 0, tzinfo=timezone.utc)
    _, lon = subsolar_point(midnight)
    assert abs(abs(wrap_lon(lon)) - 180.0) < 5.0


def test_subsolar_declination_follows_the_seasons():
    _, _ = subsolar_point()
    june, _ = subsolar_point(datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc))
    december, _ = subsolar_point(datetime(2026, 12, 21, 12, 0, tzinfo=timezone.utc))
    equinox, _ = subsolar_point(datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc))

    assert june == pytest.approx(23.4, abs=0.5)       # northern solstice
    assert december == pytest.approx(-23.4, abs=0.5)  # southern solstice
    assert abs(equinox) < 1.0                          # equinox
