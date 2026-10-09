"""Spherical geometry shared by the map panels.

`frontend/js/lib/geo.js` is the JavaScript twin of this module; the unit tests
here are what keep the two honest.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

EARTH_RADIUS_KM = 6371.0088


def footprint_half_angle_deg(alt_km: float, min_elevation_deg: float = 0.0) -> float:
    """Earth-central half-angle of the area that can see a satellite.

        lambda = acos( (Re / (Re + h)) * cos(eps) ) - eps

    With eps = 0 this is the geometric horizon; raising it gives the smaller
    circle inside which the satellite clears a minimum elevation mask.
    """
    if alt_km <= 0:
        return 0.0
    eps = math.radians(min_elevation_deg)
    ratio = EARTH_RADIUS_KM / (EARTH_RADIUS_KM + alt_km)
    inner = max(-1.0, min(1.0, ratio * math.cos(eps)))
    return math.degrees(max(0.0, math.acos(inner) - eps))


def footprint_radius_km(alt_km: float, min_elevation_deg: float = 0.0) -> float:
    return EARTH_RADIUS_KM * math.radians(
        footprint_half_angle_deg(alt_km, min_elevation_deg)
    )


def destination_point(lat_deg: float, lon_deg: float, bearing_deg: float,
                      angular_distance_deg: float) -> tuple[float, float]:
    """Great-circle destination, all arguments and results in degrees."""
    lat1 = math.radians(lat_deg)
    lon1 = math.radians(lon_deg)
    brg = math.radians(bearing_deg)
    ang = math.radians(angular_distance_deg)

    sin_lat2 = math.sin(lat1) * math.cos(ang) + math.cos(lat1) * math.sin(ang) * math.cos(brg)
    sin_lat2 = max(-1.0, min(1.0, sin_lat2))
    lat2 = math.asin(sin_lat2)
    lon2 = lon1 + math.atan2(
        math.sin(brg) * math.sin(ang) * math.cos(lat1),
        math.cos(ang) - math.sin(lat1) * sin_lat2,
    )
    return math.degrees(lat2), wrap_lon(math.degrees(lon2))


def wrap_lon(lon_deg: float) -> float:
    """Normalise longitude to [-180, 180)."""
    return (lon_deg + 180.0) % 360.0 - 180.0


def footprint_ring(lat_deg: float, lon_deg: float, alt_km: float,
                   min_elevation_deg: float = 0.0,
                   points: int = 72) -> list[tuple[float, float]]:
    """The visibility circle as a closed ring of (lat, lon) points."""
    half = footprint_half_angle_deg(alt_km, min_elevation_deg)
    if half <= 0:
        return []
    return [
        destination_point(lat_deg, lon_deg, (360.0 * i) / points, half)
        for i in range(points + 1)
    ]


def contains_pole(lat_deg: float, alt_km: float,
                  min_elevation_deg: float = 0.0) -> int:
    """+1 if the footprint covers the north pole, -1 the south, else 0.

    A ring that encloses a pole does not close when projected onto an
    equirectangular map — the caller has to fill to the top or bottom edge
    instead of drawing a closed polygon.
    """
    half = footprint_half_angle_deg(alt_km, min_elevation_deg)
    if lat_deg + half >= 90.0:
        return 1
    if lat_deg - half <= -90.0:
        return -1
    return 0


def split_antimeridian(
    points: list[tuple[float, float]]
) -> list[list[tuple[float, float]]]:
    """Break a (lat, lon) path wherever it crosses +/-180.

    Without this a step from lon 179 to lon -179 is drawn as a line straight
    across the whole map. The crossing point is interpolated and added to both
    segments so they meet the map edge cleanly.
    """
    if len(points) < 2:
        return [list(points)] if points else []

    segments: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = [points[0]]

    for (lat_a, lon_a), (lat_b, lon_b) in zip(points, points[1:]):
        delta = lon_b - lon_a
        if abs(delta) > 180.0:
            # Unwrap b so the interpolation runs the short way round.
            unwrapped = lon_b - 360.0 * math.copysign(1.0, delta)
            edge = 180.0 * math.copysign(1.0, lon_a)
            span = unwrapped - lon_a
            f = (edge - lon_a) / span if span else 0.0
            lat_cross = lat_a + f * (lat_b - lat_a)

            current.append((lat_cross, edge))
            segments.append(current)
            current = [(lat_cross, -edge), (lat_b, lon_b)]
        else:
            current.append((lat_b, lon_b))

    if len(current) > 1:
        segments.append(current)
    return segments


def subsolar_point(when: datetime | None = None) -> tuple[float, float]:
    """Approximate sub-solar (lat, lon). Good to a few tenths of a degree,
    which is far below what a terminator line on a wall display resolves."""
    when = when or datetime.now(timezone.utc)
    when = when.astimezone(timezone.utc)

    # Days since J2000.0
    jd = when.timestamp() / 86400.0 + 2440587.5
    n = jd - 2451545.0

    mean_lon = (280.460 + 0.9856474 * n) % 360.0
    mean_anom = math.radians((357.528 + 0.9856003 * n) % 360.0)
    ecliptic_lon = math.radians(
        mean_lon + 1.915 * math.sin(mean_anom) + 0.020 * math.sin(2 * mean_anom)
    )
    obliquity = math.radians(23.439 - 0.0000004 * n)

    declination = math.degrees(math.asin(math.sin(obliquity) * math.sin(ecliptic_lon)))

    utc_hours = when.hour + when.minute / 60.0 + when.second / 3600.0
    eot_deg = math.degrees(
        math.atan2(
            math.cos(obliquity) * math.sin(ecliptic_lon),
            math.cos(ecliptic_lon),
        )
    ) - (mean_lon % 360.0)
    eot_deg = (eot_deg + 180.0) % 360.0 - 180.0

    lon = wrap_lon(-15.0 * (utc_hours - 12.0) + eot_deg)
    return declination, lon


def terminator_ring(when: datetime | None = None,
                    points: int = 180) -> list[tuple[float, float]]:
    """The day/night boundary: the 90-degree ring around the sub-solar point."""
    lat, lon = subsolar_point(when)
    return [
        destination_point(lat, lon, (360.0 * i) / points, 90.0)
        for i in range(points + 1)
    ]
