"""Polls the station's rigctld and compares it with our own Doppler.

The point of this service is the comparison, not the readout. Both numbers are
"the downlink, Doppler-corrected", but they are produced by two independent
chains: satnogs-client's propagator and elements on one side, Skyfield and the
TLE cache on this box on the other. Agreeing to a few hundred Hz means both are
tracking the same object with fresh elements. Diverging by tens of kHz means one
of them is wrong, and until now nothing on this station could have noticed.

Polling is slow on purpose. Between passes the dummy rig sits at whatever it was
last set to and there is nothing to learn; satnogs-client only writes to it while
it is running an observation. So this polls at 1 Hz while the tracked satellite
is up and backs off to a crawl when it is not.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import Settings
from ..hub import hub
from .rig_client import RigClient, RigError
from .transmitters import doppler_shift_hz

log = logging.getLogger(__name__)

POLL_UP_S = 1.0
POLL_IDLE_S = 20.0
BACKOFF_S = 30.0
# satnogs-client and this backend both correct for Doppler but not from the same
# elements. A few hundred Hz is two propagators disagreeing in the last decimal;
# tens of kHz is one of them tracking something else, and at 9k6 FSK that is the
# difference between decoding the pass and missing it entirely.
AGREE_HZ = 2000.0


class RigService:
    def __init__(self, settings: Settings, predictor, transmitters,
                 on_state=None) -> None:
        self.s = settings
        self.predictor = predictor
        self.transmitters = transmitters
        self.on_state = on_state or (lambda component, state, detail="": None)
        self.client = RigClient(settings.rigctld_host, settings.rigctld_port)
        self.last: dict | None = None

    async def stop(self) -> None:
        await self.client.close()

    async def run(self) -> None:
        if not self.s.rigctld_enabled:
            return
        while True:
            try:
                await self._poll_once()
                up = bool(self.last and self.last.get("satellite_up"))
                await asyncio.sleep(POLL_UP_S if up else POLL_IDLE_S)
            except asyncio.CancelledError:
                raise
            except (RigError, ConnectionError, OSError, asyncio.TimeoutError) as exc:
                # A missing rigctld is a normal deployment, not a fault: plenty
                # of stations drive the SDR entirely inside the flowgraph. Say
                # so once, degraded rather than down, and keep trying quietly.
                self.on_state("rig", "degraded", str(exc))
                self.last = None
                await self.client.close()
                await asyncio.sleep(BACKOFF_S)

    async def _poll_once(self) -> None:
        state = await self.client.get_state()
        norad = self.s.default_norad
        pos = self.predictor.position(norad)
        # Without a transmitter table there is no nominal downlink to correct,
        # so the rig's reading is still shown — it is just not comparable yet.
        nominal = (self.transmitters.downlink_hz(norad)
                   if self.transmitters is not None else None)

        ours = None
        if nominal and pos is not None:
            ours = nominal + doppler_shift_hz(nominal, pos.range_rate_km_s)

        theirs = state["freq_hz"]

        # The comparison is only meaningful while the satellite is up. Between
        # passes satnogs-client is not writing to this rig at all and the dummy
        # simply holds whatever it was last set to — 145 MHz idle against a
        # 400 MHz downlink is a 255 MHz "disagreement" that means nothing. A
        # wall display that sits permanently on a red alarm teaches operators to
        # ignore the alarm, which costs more than never having shown it.
        up = bool(pos and pos.el > 0)
        comparable = bool(up and ours)
        delta = (theirs - ours) if comparable else None

        sample = {
            "freq_hz": theirs,
            "mode": state["mode"],
            "passband_hz": state["passband_hz"],
            "latency_ms": state["latency_ms"],
            "our_freq_hz": round(ours, 1) if ours else None,
            "nominal_hz": nominal,
            "delta_hz": round(delta, 1) if delta is not None else None,
            # None, not False: "we cannot tell yet" and "they disagree" are
            # different answers and the panel renders them differently.
            "agrees": (abs(delta) <= AGREE_HZ) if delta is not None else None,
            "tracking": comparable,
            "satellite_up": up,
            "el": round(pos.el, 2) if pos else None,
            "source": f"rigctld {self.s.rigctld_host}:{self.s.rigctld_port}",
        }
        self.last = sample
        self.on_state("rig", "ok")
        hub.publish("rig", sample)

        if delta is not None and abs(delta) > AGREE_HZ:
            log.warning(
                "rig disagrees with our Doppler by %.0f Hz (rig %.0f, ours %.0f)",
                delta, theirs, ours,
            )
