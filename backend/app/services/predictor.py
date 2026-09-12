"""Orbit propagation and pass prediction.

Skyfield is the single source of truth for AOS/TCA/LOS. The browser runs its own
SGP4 (satellite.js) for the smooth 1 Hz "where is it now" render, but it never
computes a schedule — if it did, the antenna and the display would drift apart.

Skyfield's timescale is loaded with builtin data, so nothing here reaches the
network and the container works air-gapped.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from skyfield.api import EarthSatellite, load, wgs84

from ..config import Settings
from ..schemas import Pass, SatPos
from ..util.geo import footprint_radius_km
from .tle_store import TleStore

log = logging.getLogger(__name__)

C_KM_S = 299792.458

# Known downlinks, for the Doppler readout. KNACKSAT-2's UHF telemetry is the
# transmitter station 5024 actually schedules.
DOWNLINK_HZ: dict[int, float] = {
    67683: 400_630_000.0,      # KNACKSAT-2 UHF TLM, FSK 9600
}


class Predictor:
    def __init__(self, settings: Settings, tles: TleStore) -> None:
        self.s = settings
        self.tles = tles
        self.ts = load.timescale()          # builtin=True: no download
        self.site = wgs84.latlon(
            settings.station_lat, settings.station_lon,
            elevation_m=settings.station_alt_m,
        )
        self._cache: dict[int, tuple[str, EarthSatellite]] = {}

    # --- satellites --------------------------------------------------------
    def satellite(self, norad: int) -> EarthSatellite | None:
        tle = self.tles.get(norad)
        if tle is None:
            return None
        cached = self._cache.get(norad)
        if cached and cached[0] == tle.tle1:
            return cached[1]
        sat = EarthSatellite(tle.tle1, tle.tle2, tle.name, self.ts)
        self._cache[norad] = (tle.tle1, sat)
        return sat

    # --- instantaneous state ----------------------------------------------
    def position(self, norad: int, when: datetime | None = None) -> SatPos | None:
        sat = self.satellite(norad)
        if sat is None:
            return None
        when = when or datetime.now(timezone.utc)
        t = self.ts.from_datetime(when)

        geo = sat.at(t)
        sub = wgs84.subpoint(geo)
        alt_km = sub.elevation.km

        topo = (sat - self.site).at(t)
        el, az, dist = topo.altaz()

        r = topo.position.km
        v = topo.velocity.km_per_s
        r_mag = math.sqrt(sum(c * c for c in r))
        # Skyfield's topocentric difference already carries the observer's own
        # rotational velocity, so this is the true line-of-sight rate.
        range_rate = sum(a * b for a, b in zip(r, v)) / r_mag if r_mag else 0.0

        speed = math.sqrt(sum(c * c for c in geo.velocity.km_per_s))

        downlink = DOWNLINK_HZ.get(norad)
        doppler = -downlink * range_rate / C_KM_S if downlink else None

        tle = self.tles.get(norad)
        return SatPos(
            norad=norad,
            name=sat.name or "",
            lat=sub.latitude.degrees,
            lon=sub.longitude.degrees,
            alt_km=alt_km,
            vel_km_s=speed,
            az=az.degrees,
            el=el.degrees,
            range_km=dist.km,
            range_rate_km_s=range_rate,
            doppler_hz=doppler,
            footprint_km=footprint_radius_km(alt_km),
            tle_age_days=tle.age_days if tle else None,
        )

    # --- passes ------------------------------------------------------------
    def passes(self, norad: int, hours: float = 24.0,
               min_el: float | None = None) -> list[Pass]:
        sat = self.satellite(norad)
        if sat is None:
            return []
        min_el = self.s.min_elevation_deg if min_el is None else min_el

        now = datetime.now(timezone.utc)
        t0 = self.ts.from_datetime(now - timedelta(minutes=20))   # catch one in progress
        t1 = self.ts.from_datetime(now + timedelta(hours=hours))

        try:
            times, events = sat.find_events(
                self.site, t0, t1, altitude_degrees=min_el
            )
        except Exception as exc:                    # decayed or nonsense elements
            log.warning("find_events failed for %s: %s", norad, exc)
            return []

        out: list[Pass] = []
        rise = culm = None
        for t, event in zip(times, events):
            if event == 0:
                rise, culm = t, None
            elif event == 1:
                culm = t
            elif event == 2 and rise is not None:
                # `culm if culm is not None` rather than `culm or rise`: a
                # Skyfield Time raises on truth-testing.
                peak = culm if culm is not None else rise
                out.append(self._build_pass(sat, norad, rise, peak, t, now))
                rise = culm = None
        return out

    def _build_pass(self, sat: EarthSatellite, norad: int,
                    rise, culm, set_, now: datetime) -> Pass:
        aos = rise.utc_datetime()
        tca = culm.utc_datetime()
        los = set_.utc_datetime()

        def look(t):
            el, az, _ = (sat - self.site).at(t).altaz()
            return az.degrees, el.degrees

        aos_az, _ = look(rise)
        los_az, _ = look(set_)
        _, max_el = look(culm)

        if los <= now:
            state = "complete"
        elif aos <= now < los:
            state = "in_progress"
        else:
            state = "upcoming"

        return Pass(
            pass_id=f"{norad}-{int(aos.timestamp())}",
            norad=norad,
            name=sat.name or "",
            aos=aos, tca=tca, los=los,
            duration_s=(los - aos).total_seconds(),
            max_el=max_el,
            aos_az=aos_az,
            los_az=los_az,
            state=state,
            seconds_to_aos=(aos - now).total_seconds(),
        )

    def next_pass(self, norad: int, hours: float = 24.0) -> Pass | None:
        for p in self.passes(norad, hours=hours):
            if p.state in ("in_progress", "upcoming"):
                return p
        return None

    def track(self, norad: int, start: datetime, end: datetime,
              step_s: float = 10.0) -> list[dict]:
        """az/el samples across a pass, for the polar plot overlay."""
        sat = self.satellite(norad)
        if sat is None:
            return []
        total = (end - start).total_seconds()
        if total <= 0:
            return []
        n = max(2, int(total / step_s) + 1)
        samples = []
        for i in range(n):
            when = start + timedelta(seconds=total * i / (n - 1))
            t = self.ts.from_datetime(when)
            el, az, dist = (sat - self.site).at(t).altaz()
            samples.append({
                "t": when.isoformat(),
                "az": float(az.degrees),
                "el": float(el.degrees),
                "range_km": float(dist.km),
            })
        return samples

    def ground_track(self, norad: int, minutes_before: float = 45.0,
                     minutes_after: float = 45.0, step_s: float = 30.0) -> list[dict]:
        """Sub-satellite points either side of now, for the 2D map."""
        sat = self.satellite(norad)
        if sat is None:
            return []
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=minutes_before)
        total_s = (minutes_before + minutes_after) * 60.0
        n = int(total_s / step_s) + 1
        out = []
        for i in range(n):
            when = start + timedelta(seconds=i * step_s)
            sub = wgs84.subpoint(sat.at(self.ts.from_datetime(when)))
            out.append({
                "t": when.isoformat(),
                "lat": float(sub.latitude.degrees),
                "lon": float(sub.longitude.degrees),
                "alt_km": float(sub.elevation.km),
            })
        return out
