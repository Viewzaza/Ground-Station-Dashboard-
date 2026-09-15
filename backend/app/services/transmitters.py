"""Transmitter data from SatNOGS DB.

What an operator wants from this panel during a pass is not a catalogue entry:
it is the number to tune the radio to *now*. So the useful output is the
downlink corrected for Doppler, and everything here exists to produce that.

The frequencies also replace a hard-coded constant. `predictor.DOWNLINK_HZ`
carried KNACKSAT-2's 400.630 MHz as a literal, which is right — SatNOGS DB
agrees to the Hz — but it only stayed right by luck, and it meant the Doppler
readout silently did nothing for every other satellite in the selector.

Two things about this API worth knowing:

**Here `satellite__norad_cat_id` is the correct filter**, which is the opposite
of the Network API, where that parameter is silently ignored and `norad_cat_id`
is the one that works. The two SatNOGS services do not share a convention.

**A satellite usually has several transmitters and they are not
interchangeable.** KNACKSAT-2 publishes a 145.825 MHz V/V digipeater and a
400.630 MHz UHF telemetry downlink; station 5024 is a UHF station and records
the latter. Picking "the first one" would tune the dashboard to a band the
antenna cannot hear, so the choice is explicit — see `primary_downlink`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 25.0
USER_AGENT = "knacksat2-ground-station-dashboard/0.1 (+github.com/Viewzaza)"


class TransmitterStore:
    """Per-satellite transmitter lists, cached on disk.

    Transmitters change on the scale of months, so this is cached hard and
    refreshed lazily. A ground station that cannot reach the internet still
    needs to know what to tune to, which is what the disk cache is for.
    """

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._by_norad: dict[int, list[dict]] = {}
        self._fetched_at: dict[int, datetime] = {}
        self._lock = asyncio.Lock()
        self._load_cache()

    # --- cache -------------------------------------------------------------
    @property
    def _path(self):
        return self.s.data_dir / "transmitters.json"

    def _load_cache(self) -> None:
        if not self._path.exists():
            return
        try:
            blob = json.loads(self._path.read_text(encoding="utf-8"))
            self._by_norad = {int(k): v for k, v in blob.get("sats", {}).items()}
            self._fetched_at = {
                int(k): datetime.fromisoformat(v)
                for k, v in blob.get("fetched_at", {}).items()
            }
            log.info("loaded transmitters for %d satellites from cache",
                     len(self._by_norad))
        except Exception as exc:                  # a corrupt cache is not fatal
            log.warning("ignoring unreadable transmitter cache: %s", exc)

    def _save_cache(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({
                "sats": {str(k): v for k, v in self._by_norad.items()},
                "fetched_at": {str(k): v.isoformat()
                               for k, v in self._fetched_at.items()},
            }, indent=1),
            encoding="utf-8",
        )

    def age_s(self, norad: int) -> float:
        stamp = self._fetched_at.get(norad)
        if stamp is None:
            return float("inf")
        return (datetime.now(timezone.utc) - stamp).total_seconds()

    def is_fresh(self, norad: int) -> bool:
        return self.age_s(norad) < self.s.transmitter_ttl_s

    # --- fetching ----------------------------------------------------------
    async def refresh(self, norad: int, force: bool = False) -> bool:
        if self.is_fresh(norad) and not force:
            return False
        if self.s.offline:
            return False

        headers = {"User-Agent": USER_AGENT}
        if self.s.satnogs_db_token:
            headers["Authorization"] = f"Token {self.s.satnogs_db_token}"

        async with self._lock:
            try:
                async with httpx.AsyncClient(
                    timeout=REQUEST_TIMEOUT_S, headers=headers
                ) as client:
                    resp = await client.get(
                        f"{self.s.satnogs_db}/transmitters/",
                        # Correct here; the Network API wants `norad_cat_id`.
                        params={"satellite__norad_cat_id": norad,
                                "format": "json"},
                    )
                if resp.status_code != 200:
                    log.warning("SatNOGS DB transmitters returned %s for %s",
                                resp.status_code, norad)
                    return False
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("transmitter fetch failed for %s: %s", norad, exc)
                return False

            if not isinstance(payload, list):
                return False

            self._by_norad[norad] = [_summarise(t) for t in payload]
            self._fetched_at[norad] = datetime.now(timezone.utc)
            self._save_cache()
            log.info("transmitters: %d for NORAD %s",
                     len(self._by_norad[norad]), norad)
            return True

    # --- access ------------------------------------------------------------
    def get(self, norad: int) -> list[dict]:
        return self._by_norad.get(norad, [])

    def primary_downlink(self, norad: int) -> dict | None:
        """The transmitter this station is most likely actually listening to.

        Station 5024's antennas are UHF (roughly 380-490 MHz), so a satellite's
        VHF downlink is not a candidate however prominently it is listed.
        Preference: alive and in the station's band, then alive at all, then
        whatever has a downlink. Ties break to the lowest frequency so the
        choice is at least deterministic across restarts.
        """
        candidates = [t for t in self.get(norad) if t.get("downlink_hz")]
        if not candidates:
            return None

        def rank(t: dict) -> tuple:
            in_band = UHF_LOW_HZ <= t["downlink_hz"] <= UHF_HIGH_HZ
            return (not (t.get("alive") and in_band), not t.get("alive"),
                    t["downlink_hz"])

        return sorted(candidates, key=rank)[0]

    def downlink_hz(self, norad: int) -> float | None:
        primary = self.primary_downlink(norad)
        return primary["downlink_hz"] if primary else None


# Station 5024's three Yagis span 380-490 MHz. Used only to prefer one
# transmitter over another, never to hide one from the operator.
UHF_LOW_HZ = 380_000_000.0
UHF_HIGH_HZ = 490_000_000.0


def _summarise(tx: dict) -> dict:
    """Only what the panel draws. The raw records carry ITU notification
    blobs and citation URLs that would otherwise go over the WebSocket."""
    downlink = tx.get("downlink_low") or tx.get("downlink_high")
    return {
        "uuid": tx.get("uuid"),
        "description": tx.get("description") or "",
        "type": tx.get("type") or "",
        "downlink_hz": float(downlink) if downlink else None,
        "uplink_hz": float(tx["uplink_low"]) if tx.get("uplink_low") else None,
        "mode": tx.get("mode") or "",
        "baud": tx.get("baud"),
        "service": tx.get("service") or "",
        "status": tx.get("status") or "",
        "alive": bool(tx.get("alive")),
        "invert": bool(tx.get("invert")),
        "iaru": tx.get("iaru_coordination") or "",
    }


C_M_S = 299_792_458.0


def doppler_shift_hz(downlink_hz: float, range_rate_km_s: float) -> float:
    """Shift seen at the ground for a given line-of-sight rate.

    Negative range rate means closing, which raises the observed frequency —
    hence the sign. This is the classical one-way approximation; at LEO speeds
    the relativistic correction is far below the tuning resolution of any radio
    this station owns.
    """
    return -downlink_hz * (range_rate_km_s * 1000.0) / C_M_S
