"""Pass prediction with Skyfield.

Cost is not a concern here, which is worth stating because it shapes the
design: propagating all 936 VHF/UHF satellites that have TLEs over a 24 hour
window takes about 2.5 seconds, and 48 hours about 5. There is no need for a
coarse pre-filter or a vectorised first pass - we just run ``find_events``
over the whole catalogue and filter afterwards.

``load.timescale(builtin=True)`` keeps this fully offline; Skyfield will not
reach out for a leap-second file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from skyfield.api import EarthSatellite, load, wgs84

from .db_client import Tle

log = logging.getLogger(__name__)

# A pass that is still open when the window ends would be reported without its
# LOS. We propagate a little past the window so those passes close properly,
# then drop anything that rises after the window.
WINDOW_OVERRUN = timedelta(minutes=30)

# TLEs this old propagate to plausible-looking nonsense. SatNOGS DB refreshes
# from Space-Track several times a day, so anything this stale means the object
# is no longer being tracked.
MAX_TLE_AGE_DAYS = 14.0

RISE, CULMINATE, SET = 0, 1, 2


@dataclass
class Pass:
    norad_cat_id: int
    name: str
    aos: datetime
    tca: datetime
    los: datetime
    max_el: float
    aos_az: float
    los_az: float

    @property
    def duration_s(self) -> float:
        return (self.los - self.aos).total_seconds()

    @property
    def duration_min(self) -> float:
        return self.duration_s / 60.0


class Predictor:
    def __init__(self, lat: float, lon: float, altitude_m: float) -> None:
        self.ts = load.timescale(builtin=True)
        self.site = wgs84.latlon(lat, lon, elevation_m=altitude_m)
        self._satellites: dict[int, EarthSatellite] = {}

    def load_tles(self, tles: dict[int, Tle], now: datetime | None = None) -> dict[int, str]:
        """Build EarthSatellites, skipping anything with a stale or bad TLE.

        Returns a NORAD id -> reason map of what was skipped, so the caller can
        report it rather than silently losing satellites.
        """
        now = now or datetime.now(timezone.utc)
        skipped: dict[int, str] = {}
        for norad, tle in tles.items():
            try:
                sat = EarthSatellite(tle.line1, tle.line2, tle.name, self.ts)
            except Exception as exc:             # malformed element set
                skipped[norad] = f"unparseable TLE ({exc})"
                continue
            age_days = (now - sat.epoch.utc_datetime()).total_seconds() / 86400.0
            if age_days > MAX_TLE_AGE_DAYS:
                skipped[norad] = f"TLE is {age_days:.0f} days old"
                continue
            self._satellites[norad] = sat
        if skipped:
            log.info("skipped %d satellite(s) on TLE quality", len(skipped))
        return skipped

    def passes_for(
        self,
        norad: int,
        start: datetime,
        end: datetime,
        min_horizon: float,
    ) -> list[Pass]:
        sat = self._satellites.get(norad)
        if sat is None:
            return []

        t0 = self.ts.from_datetime(start)
        t1 = self.ts.from_datetime(end + WINDOW_OVERRUN)
        try:
            times, events = sat.find_events(
                self.site, t0, t1, altitude_degrees=min_horizon
            )
        except Exception as exc:
            log.debug("find_events failed for %d: %s", norad, exc)
            return []

        topocentric = sat - self.site
        out: list[Pass] = []
        aos = tca = None
        max_el = 0.0
        aos_az = 0.0

        for t, event in zip(times, events):
            if event == RISE:
                aos, tca, max_el, aos_az = t, None, 0.0, self._az(topocentric, t)
            elif event == CULMINATE:
                # A pass already in progress at t0 has no rise event; without an
                # AOS we cannot report its duration, so we let it go.
                if aos is None:
                    continue
                tca = t
                max_el = self._el(topocentric, t)
            elif event == SET:
                if aos is None or tca is None:
                    aos = tca = None
                    continue
                los_dt = t.utc_datetime()
                aos_dt = aos.utc_datetime()
                # Passes that rise after the requested window are only here so
                # that window-edge passes close cleanly.
                if aos_dt <= end:
                    out.append(
                        Pass(
                            norad_cat_id=norad,
                            name=sat.name or str(norad),
                            aos=aos_dt,
                            tca=tca.utc_datetime(),
                            los=los_dt,
                            max_el=max_el,
                            aos_az=aos_az,
                            los_az=self._az(topocentric, t),
                        )
                    )
                aos = tca = None

        return out

    def all_passes(
        self,
        norads: list[int],
        start: datetime,
        end: datetime,
        min_horizon: float,
    ) -> list[Pass]:
        out: list[Pass] = []
        for norad in norads:
            out.extend(self.passes_for(norad, start, end, min_horizon))
        out.sort(key=lambda p: p.aos)
        return out

    @staticmethod
    def _el(topocentric, t) -> float:
        return float(topocentric.at(t).altaz()[0].degrees)

    @staticmethod
    def _az(topocentric, t) -> float:
        return float(topocentric.at(t).altaz()[1].degrees)
