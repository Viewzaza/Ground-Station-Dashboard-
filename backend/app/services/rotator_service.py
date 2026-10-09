"""Rotator polling.

Owns the single connection, the 1 Hz poll and the backoff. Publishes a
`rotator` frame to the hub, plus a `pointing` frame with the error against the
satellite the antenna is working (see `focus` below).

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
import time

from ..config import Settings
from ..hub import hub
from ..schemas import RotatorSample
from .antenna import beam_error_deg, focus_look, is_finite_look
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
            self.client = RotctldClient(
                settings.rotctld_host, settings.rotctld_port,
                min_az=settings.rot_limit_min_az, max_az=settings.rot_limit_max_az,
                min_el=settings.rot_limit_min_el, max_el=settings.rot_limit_max_el,
            )
            self.source = "rotctld"

        self.last: RotatorSample | None = None
        self.verified = False
        self._fatal: str | None = None      # wrong peer: stop, do not retry
        # What the pointing error is measured against: a callable returning
        # (norad, override), where override is an EarthSatellite built from a
        # SatNOGS job's own elements, or None to use the catalogue. The
        # scheduler points this at the antenna's focus once the services that
        # decide it exist. Built alone — in tests and tools — it is the default
        # satellite, which is all it ever used to be. It moves only the ERR
        # readout: ControlService's track loop computes its own error, and the
        # mock rotator and the rig service stay on the default satellite.
        self.focus = lambda: (settings.default_norad, None)

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
        self._last_ok_mono = time.monotonic()
        self.on_state("rotctld", "ok")
        hub.publish("rotator", sample.model_dump(mode="json"))
        try:
            self._publish_pointing(sample)
        except Exception:
            # A readout, and nothing more. Raised from here it would reach the
            # poll loop as a failed read: the link marked down, and the
            # interlock refusing every command, over a display bug.
            log.exception("pointing readout failed")

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
        """Error against the satellite the antenna is working, while it is up.

        Two measures, because they answer different questions. Δaz and Δel are
        what each axis would have to turn; the beam error is how far off the
        beam actually points, and near zenith the two part company — at 85°
        elevation a 40° azimuth difference is a 3.5° beam error, which the old
        sqrt(Δaz² + Δel²) reported as 40. total_error_deg is that old number,
        kept for anything still reading it.

        An invalid frame says why — no elements, below the horizon — because a
        bare "—" cannot tell a satellite that has set from one nobody can see.
        """
        try:
            norad, override = self.focus()
        except Exception:
            log.exception("pointing focus failed; measuring against the default")
            norad, override = self.s.default_norad, None
        look = focus_look(self.predictor, norad, override)
        if look is None:
            hub.publish("pointing", {"valid": False, "norad": norad,
                                     "reason": f"no elements for #{norad}"})
            return
        name, sat_az, sat_el = look
        label = name or f"#{norad}"
        if not is_finite_look(look):
            hub.publish("pointing", {"valid": False, "norad": norad, "name": name,
                                     "reason": f"no usable position for {label}"})
            return
        if sat_el <= 0:
            hub.publish("pointing", {"valid": False, "norad": norad, "name": name,
                                     "reason": f"{label} is below the horizon"})
            return

        az_error = abs(((sample.az_rose - sat_az + 540.0) % 360.0) - 180.0)
        el_error = abs(sample.el - sat_el)
        hub.publish("pointing", {
            "valid": True,
            "norad": norad,
            "name": name,
            "az_error_deg": round(az_error, 2),
            "el_error_deg": round(el_error, 2),
            "total_error_deg": round((az_error ** 2 + el_error ** 2) ** 0.5, 2),
            "beam_error_deg": round(
                beam_error_deg(sample.az_rose, sample.el, sat_az, sat_el), 2),
        })

    def _down(self, detail: str) -> None:
        """Mark the link down — in `last`, not only on the wire.

        The down copy used to be published and dropped, so `last` went on
        saying "up" through the outage, and everything that reads it believed
        that: the interlock's "rotator link is down" check, the track loop's
        under-lock guard, the planner's origin. The one remaining protection
        was `verified`, and a reconnect sets that again as soon as dump_caps
        answers — which rotctld does from compiled-in capabilities with the
        SPID controller powered off. For the 2.8-4.6 s a failing get_pos then
        takes, a command passed both checks. Assigning it here means a refusal
        until the next position actually read, which is the only evidence that
        the rotator is there.

        `stale_s` is how long since that last good read; the position itself
        is kept, so a reader can still say where the antenna was.
        """
        if self.last is not None:
            ok_at = getattr(self, "_last_ok_mono", None)
            stale_s = round(time.monotonic() - ok_at, 1) if ok_at is not None else 0.0
            self.last = self.last.model_copy(update={"link": "down", "stale_s": stale_s})
            hub.publish("rotator", self.last.model_dump(mode="json"))
        self.on_state("rotctld", "down", detail)
