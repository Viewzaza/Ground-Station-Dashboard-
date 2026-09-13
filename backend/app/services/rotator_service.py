"""Rotator polling.

Owns the single connection, the 1 Hz poll and the backoff. Publishes a
`rotator` frame to the hub, plus a `pointing` frame with the error against the
satellite the display is tracking.

1 Hz is deliberate, not lazy. A SPID ROT2PROG runs its serial link at 600 baud
with a 300 ms post-write delay, so one position read occupies the line for the
better part of a second — 2 Hz is not physically available. An MD-01 at 19200
could sustain it, but the antenna does not move fast enough for it to show, and
every extra poll is extra contention with whatever is actually tracking.
"""

from __future__ import annotations

import asyncio
import logging
import random

from ..config import Settings
from ..hub import hub
from ..schemas import RotatorSample
from .predictor import Predictor
from .rotator_mock import MockRotator
from .rotctld_client import NotARotator, RotctldClient, RotctldError

log = logging.getLogger(__name__)


class RotatorService:
    def __init__(self, settings: Settings, predictor: Predictor,
                 on_state=None) -> None:
        self.s = settings
        self.predictor = predictor
        self.on_state = on_state or (lambda component, state, detail="": None)

        self.client: RotctldClient | MockRotator
        if settings.mock:
            self.client = MockRotator(settings, predictor)
            self.source = "mock"
        else:
            self.client = RotctldClient(settings.rotctld_host, settings.rotctld_port)
            self.source = "rotctld"

        self.last: RotatorSample | None = None
        self.verified = False
        self._fatal: str | None = None      # wrong peer: stop, do not retry

    # --- lifecycle ---------------------------------------------------------
    async def run(self) -> None:
        backoff = self.s.rotator_backoff_min_s
        while True:
            if self._fatal:
                # Pointing at a radio is a configuration error, not a transient
                # fault. Retrying forever would just bury the message.
                await asyncio.sleep(60.0)
                continue
            try:
                await self._ensure_verified()
                await self._poll_once()
                backoff = self.s.rotator_backoff_min_s
                await asyncio.sleep(self.s.rotator_poll_interval_s)
            except asyncio.CancelledError:
                raise
            except NotARotator as exc:
                self._fatal = str(exc)
                self._down(str(exc))
                log.error("%s", exc)
            except (RotctldError, ConnectionError, OSError, asyncio.TimeoutError) as exc:
                self._down(str(exc))
                await self.client.close()
                self.verified = False
                jitter = random.uniform(0, backoff / 2)
                await asyncio.sleep(backoff + jitter)
                backoff = min(backoff * 2, self.s.rotator_backoff_max_s)

    async def stop(self) -> None:
        await self.client.close()

    async def _ensure_verified(self) -> None:
        if self.verified:
            return
        caps = await self.client.verify_is_rotator()
        self.verified = True
        log.info(
            "rotator identified: model %s %s (az %.0f..%.0f, el %.0f..%.0f)",
            caps.model, caps.name, caps.min_az, caps.max_az, caps.min_el, caps.max_el,
        )

    # --- polling -----------------------------------------------------------
    async def _poll_once(self) -> None:
        az_raw, el, latency_ms = await self.client.get_position()

        sample = RotatorSample(
            az_raw=az_raw,
            el=el,
            az_rose=az_raw % 360.0,
            source=self.source,
            link="up",
            rprt=0,
            latency_ms=round(latency_ms, 1),
            wrap=self._wrap_state(az_raw),
            stale_s=0.0,
        )
        self.last = sample
        self.on_state("rotctld", "ok")
        hub.publish("rotator", sample.model_dump(mode="json"))
        self._publish_pointing(sample)

    def _wrap_state(self, az_raw: float) -> str:
        """Whether the rotator is wound past a full turn, and which way.

        A SPID's range is -180..540, so this is real information about the
        cable, not a rendering artefact to be normalised away.
        """
        if az_raw > 360.0:
            return "cw"
        if az_raw < 0.0:
            return "ccw"
        return "none"

    def _publish_pointing(self, sample: RotatorSample) -> None:
        """Error against the satellite, but only while it is actually up."""
        pos = self.predictor.position(self.s.default_norad)
        if pos is None or pos.el <= 0:
            hub.publish("pointing", {"valid": False})
            return

        az_error = abs(((sample.az_rose - pos.az + 540.0) % 360.0) - 180.0)
        el_error = abs(sample.el - pos.el)
        hub.publish("pointing", {
            "valid": True,
            "az_error_deg": round(az_error, 2),
            "el_error_deg": round(el_error, 2),
            "total_error_deg": round((az_error ** 2 + el_error ** 2) ** 0.5, 2),
        })

    def _down(self, detail: str) -> None:
        if self.last is not None:
            stale = self.last.model_copy(update={"link": "down", "stale_s": 0.0})
            hub.publish("rotator", stale.model_dump(mode="json"))
        self.on_state("rotctld", "down", detail)
