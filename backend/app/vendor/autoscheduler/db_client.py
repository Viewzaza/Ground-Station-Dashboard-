"""Client for db.satnogs.org - transmitters, satellites and TLEs.

Everything this module reads is public; the DB token is optional and only
matters for /telemetry/ and /artifacts/, which we never touch.

All three endpoints return the whole catalogue in a single response, so there
is no pagination to walk here - only the Network API needs that.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .cache import Cache
from .config import SATELLITE_TTL_S, TLE_TTL_S, TRANSMITTER_TTL_S, Settings
from .http import make_session, request

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Transmitter:
    uuid: str
    norad_cat_id: int
    description: str
    mode: str
    baud: float | None
    downlink_hz: int
    type: str
    status: str
    service: str

    @property
    def downlink_mhz(self) -> float:
        return self.downlink_hz / 1e6


@dataclass(frozen=True)
class Tle:
    norad_cat_id: int
    name: str
    line1: str
    line2: str
    updated: str
    source: str


def frequency_is_covered(hz: float, segments: list[tuple[float, float]]) -> bool:
    """Is this downlink inside any one of the station's antenna segments?

    Station 5024 publishes three separate UHF ranges - 380.000-436.220,
    436.600-436.980 and 437.380-490.000 MHz - with real gaps between them. A
    naive min/max test would accept 436.4 MHz, which the station cannot hear,
    and we would schedule an observation that records noise. Each segment has
    to be tested on its own.
    """
    return any(low <= hz <= high for low, high in segments)


class DbClient:
    def __init__(self, settings: Settings, cache: Cache) -> None:
        self.s = settings
        self.cache = cache
        self.session = make_session(settings.db_token)

    def _get_list(self, path: str, params: dict | None = None) -> list[dict]:
        url = f"{self.s.db_base_url}/{path}"
        resp = request(self.session, "GET", url, params=params)
        payload = resp.json()
        if not isinstance(payload, list):
            raise RuntimeError(f"expected a list from {url}, got {type(payload).__name__}")
        return payload

    # -- raw catalogues, cached ------------------------------------------------

    def raw_transmitters(self) -> list[dict]:
        return self.cache.get_or_fetch(
            "db-transmitters", TRANSMITTER_TTL_S,
            lambda: self._get_list("transmitters/", {"alive": "true", "format": "json"}),
        )

    def raw_satellites(self) -> list[dict]:
        return self.cache.get_or_fetch(
            "db-satellites", SATELLITE_TTL_S,
            lambda: self._get_list("satellites/", {"format": "json"}),
        )

    def raw_tles(self) -> list[dict]:
        return self.cache.get_or_fetch(
            "db-tle", TLE_TTL_S,
            lambda: self._get_list("tle/", {"format": "json"}),
        )

    # -- shaped views ---------------------------------------------------------

    def satellites_in_orbit(self, skip_frequency_violators: bool = True) -> dict[int, dict]:
        """NORAD id -> satellite record, for satellites that are still up there.

        The catalogue carries about a thousand re-entered objects and a few
        dozen not yet launched. Both still have transmitters and, for the
        re-entered ones, stale TLEs that propagate to plausible-looking
        nonsense. Dropping them here is the cheapest filter we have.

        Satellites the DB flags as transmitting outside their coordinated
        allocation are skipped too. SatNOGS can refuse to schedule them, so
        including them buys nothing but permission errors.
        """
        out: dict[int, dict] = {}
        for sat in self.raw_satellites():
            norad = sat.get("norad_cat_id")
            if norad is None or sat.get("status") != "in orbit":
                continue
            if skip_frequency_violators and sat.get("is_frequency_violator"):
                continue
            out[int(norad)] = sat
        return out

    def transmitters_for_station(
        self,
        segments: list[tuple[float, float]],
        modes: set[str] | None = None,
        services: set[str] | None = None,
        always_include: set[int] | None = None,
    ) -> dict[int, list[Transmitter]]:
        """NORAD id -> the active transmitters this station can actually hear.

        ``services`` filters on the DB's service classification - "Amateur",
        "Space Operation", "Earth Exploration" and so on. It is a blunt
        instrument, because most records say "Unknown" (620 of the 802
        satellites station 5024 can hear), but ``--service Amateur`` is still
        the quickest way to cut a whole-catalogue plan down to the amateur
        birds most operators actually want.

        ``always_include`` exempts satellites from the mode and service
        filters. The mission satellite belongs there: KNACKSAT-2 downlinks on
        400.630 MHz and is not classified "Amateur", so ``--service Amateur``
        would otherwise quietly drop the one satellite that must never be
        dropped. The frequency segments are not waived for anyone - if the
        antenna cannot reach it, no amount of priority changes that.
        """
        always_include = always_include or set()
        by_norad: dict[int, list[Transmitter]] = {}
        for raw in self.raw_transmitters():
            if raw.get("status") != "active" or not raw.get("alive"):
                continue
            downlink = raw.get("downlink_low")
            norad = raw.get("norad_cat_id")
            uuid = raw.get("uuid")
            if not downlink or norad is None or not uuid:
                continue
            if not frequency_is_covered(float(downlink), segments):
                continue
            mode = (raw.get("mode") or "").strip()
            exempt = int(norad) in always_include
            if modes and not exempt and mode.upper() not in modes:
                continue
            if (services and not exempt
                    and (raw.get("service") or "").strip().lower() not in services):
                continue
            by_norad.setdefault(int(norad), []).append(
                Transmitter(
                    uuid=uuid,
                    norad_cat_id=int(norad),
                    description=(raw.get("description") or "").strip(),
                    mode=mode,
                    baud=raw.get("baud"),
                    downlink_hz=int(downlink),
                    type=raw.get("type") or "",
                    status=raw.get("status") or "",
                    service=raw.get("service") or "",
                )
            )
        return by_norad

    def transmitters_by_uuid(self) -> dict[str, dict]:
        """Every alive transmitter keyed by UUID, unfiltered.

        Validation needs the whole set, not the station-matched subset: to say
        "that UUID belongs to a different satellite" you have to be able to
        find it in the first place.
        """
        return {t["uuid"]: t for t in self.raw_transmitters() if t.get("uuid")}

    def satellites_by_norad(self) -> dict[int, dict]:
        """Every satellite keyed by NORAD id, whatever its status.

        Unlike satellites_in_orbit this keeps re-entered and not-yet-launched
        objects, so validation can tell "decayed last year" apart from "never
        existed".
        """
        return {
            int(s["norad_cat_id"]): s
            for s in self.raw_satellites()
            if s.get("norad_cat_id") is not None
        }

    def tles(self) -> dict[int, Tle]:
        """NORAD id -> newest TLE we have for it."""
        out: dict[int, Tle] = {}
        for raw in self.raw_tles():
            norad = raw.get("norad_cat_id")
            line1, line2 = raw.get("tle1"), raw.get("tle2")
            if norad is None or not line1 or not line2:
                continue
            name = (raw.get("tle0") or "").strip()
            # TLE line 0 conventionally carries a "0 " prefix in 3LE form.
            if name.startswith("0 "):
                name = name[2:].strip()
            out[int(norad)] = Tle(
                norad_cat_id=int(norad),
                name=name or str(norad),
                line1=line1,
                line2=line2,
                updated=raw.get("updated") or "",
                source=raw.get("tle_source") or "",
            )
        return out


def pick_transmitter(
    candidates: list[Transmitter], preferred_uuid: str | None = None
) -> Transmitter | None:
    """Choose which transmitter to record for a satellite.

    A priority file may pin an exact UUID, in which case we honour it. Failing
    that we prefer a plain beacon over a transponder - a transponder's downlink
    is a passband, not a carrier, so its nominal frequency is the least useful
    thing to point a receiver at - and then the fastest baud rate, which is
    where the telemetry usually is.
    """
    if not candidates:
        return None
    if preferred_uuid:
        for tx in candidates:
            if tx.uuid == preferred_uuid:
                return tx
        # Loud on purpose. The official scheduler drops a pinned transmitter it
        # cannot find without a word at any log level, which is how a one
        # character typo in a priority file removed a satellite from a station's
        # schedule unnoticed. Say something.
        log.warning(
            "priority file pins transmitter %s for NORAD %d, but this station has no "
            "such transmitter - falling back to its best available one. Run "
            "'autoscheduler validate' to check the file.",
            preferred_uuid, candidates[0].norad_cat_id,
        )

    def rank(tx: Transmitter) -> tuple[int, float]:
        is_transponder = 1 if tx.type.lower() == "transponder" else 0
        return (is_transponder, -(tx.baud or 0.0))

    return sorted(candidates, key=rank)[0]
