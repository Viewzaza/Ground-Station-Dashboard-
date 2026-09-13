"""Simulated rotator, for development on a machine with no station LAN.

It is driven by the real predictor rather than a sine wave, so what you watch
on the polar plot is an actual KNACKSAT-2 pass. It also reproduces the three
behaviours that a naive implementation gets wrong, so they are exercised long
before anyone plugs in a real antenna:

  * azimuth winds past 360 into the SPID overlap range rather than wrapping,
  * the link drops periodically with RPRT -5,
  * replies take about 300 ms, which is the SPID post-write delay.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from ..config import Settings
from .predictor import Predictor
from .rotctld_client import RPRT_ETIMEOUT, Caps, RotctldError

SLEW_RATE_DEG_S = 4.0
PARK_AZ_TRACKING_MARGIN = 2.0


class MockRotator:
    """Same surface as RotctldClient, so nothing downstream can tell them apart."""

    def __init__(self, settings: Settings, predictor: Predictor) -> None:
        self.s = settings
        self.predictor = predictor
        self.host = "mock"
        self.port = 0
        self.connected = True
        self.caps = Caps(
            model=901,
            name="SPID Rot2Prog (simulated)",
            min_az=-180.0, max_az=540.0,
            min_el=-20.0, max_el=210.0,
            is_rotator=True,
        )
        self._az = float(settings.park_az)
        self._el = float(settings.park_el)
        self._last = time.monotonic()
        self._started = time.monotonic()

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        self.connected = False

    async def dump_caps(self) -> Caps:
        return self.caps

    async def verify_is_rotator(self) -> Caps:
        return self.caps

    async def get_position(self) -> tuple[float, float, float]:
        elapsed = time.monotonic() - self._started
        period = self.s.mock_fault_period_s
        # A 20-second outage every fault period, so the link-down banner and
        # the backoff get exercised without anyone unplugging anything.
        if period > 0 and (elapsed % period) > (period - 20.0):
            raise RotctldError(RPRT_ETIMEOUT, "simulated serial timeout")

        now = time.monotonic()
        dt = min(2.0, now - self._last)
        self._last = now

        target_az, target_el = self._target()
        self._az = _slew(self._az, target_az, dt)
        self._el = _slew(self._el, max(0.0, target_el), dt)

        # A SPID reports to its configured step, typically half a degree.
        az = round(self._az * 2) / 2
        el = round(self._el * 2) / 2
        return az, el, 300.0

    def _target(self) -> tuple[float, float]:
        """Follow the default satellite when it is up, else stay parked."""
        pos = self.predictor.position(
            self.s.default_norad, datetime.now(timezone.utc)
        )
        if pos is None or pos.el < -PARK_AZ_TRACKING_MARGIN:
            return float(self.s.park_az), float(self.s.park_el)

        # Unwrap the target into the rotator's own continuous range, so the
        # simulated antenna winds past north instead of spinning back round.
        target = pos.az
        while target - self._az > 180.0:
            target -= 360.0
        while target - self._az < -180.0:
            target += 360.0
        return max(self.caps.min_az, min(self.caps.max_az, target)), pos.el


def _slew(current: float, target: float, dt: float) -> float:
    delta = target - current
    step = SLEW_RATE_DEG_S * dt
    if abs(delta) <= step:
        return target
    return current + step * (1.0 if delta > 0 else -1.0)
