"""Background task supervision.

Every long-lived loop is owned here so there is exactly one place that knows
what is running. A crashed loop is restarted with backoff, and the state change
is logged once rather than once per attempt.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable

from .config import Settings
from .hub import hub
from .services.control import ControlService
from .services.predictor import Predictor
from .services.rotator_service import RotatorService
from .services.satnogs import SatnogsService
from .services.tle_store import TleStore

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, settings: Settings, tles: TleStore, predictor: Predictor) -> None:
        self.s = settings
        self.tles = tles
        self.predictor = predictor
        self._tasks: list[asyncio.Task] = []
        self.components: dict[str, str] = {}
        self.rotator = RotatorService(settings, predictor, on_state=self.set_state)
        self.satnogs = SatnogsService(settings, on_state=self.set_state)
        self.control = ControlService(
            settings, self.rotator, self.satnogs, predictor, on_state=self.set_state
        )

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        # One eager refresh so the first page load has elements to work with.
        await self._safe_refresh_tles()
        # Same reasoning for the tracked satellite's transmitters: without them
        # the Doppler readout is blank through the first pass after a restart.
        await self._safe_refresh_transmitters()
        self._spawn("tle", self._tle_loop)
        self._spawn("rotctld", self.rotator.run)
        self._spawn("satpos", self._satpos_loop)
        self._spawn("satnogs", self.satnogs.run)
        self._spawn("control", self._control_loop)

    async def stop(self) -> None:
        await self.rotator.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def _spawn(self, name: str, coro: Callable[[], Awaitable[None]]) -> None:
        self._tasks.append(asyncio.create_task(self._supervise(name, coro), name=name))

    async def _supervise(self, name: str, coro: Callable[[], Awaitable[None]]) -> None:
        delay = 1.0
        while True:
            try:
                await coro()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.set_state(name, "down", str(exc))
                log.exception("task %s crashed; restarting in %.0fs", name, delay)
                await asyncio.sleep(delay + random.uniform(0, delay / 2))
                delay = min(delay * 2, 60.0)

    # --- component health --------------------------------------------------
    def set_state(self, component: str, state: str, detail: str = "") -> None:
        if self.components.get(component) == state:
            return
        self.components[component] = state
        hub.publish("status", {"component": component, "state": state, "detail": detail})
        log.info("component %s -> %s %s", component, state, detail)

    # --- loops -------------------------------------------------------------
    async def _safe_refresh_tles(self) -> None:
        try:
            await self.tles.refresh()
            if len(self.tles):
                self.set_state("tle", "ok", f"{len(self.tles)} satellites")
            else:
                self.set_state("tle", "down", "no elements available")
        except Exception as exc:
            self.set_state("tle", "degraded", str(exc))
            log.exception("TLE refresh failed")

    async def _safe_refresh_transmitters(self) -> None:
        store = getattr(self.predictor, "transmitters", None)
        if store is None:
            return
        try:
            await store.refresh(self.s.default_norad)
        except Exception:
            # Never fatal: a missing downlink costs the Doppler readout, not
            # the pass schedule, and the disk cache usually covers it.
            log.warning("transmitter refresh failed", exc_info=True)

    async def _tle_loop(self) -> None:
        # Re-check on the TTL boundary. refresh() itself refuses to hit the
        # network early, so this cadence is safe even if it is generous.
        while True:
            await asyncio.sleep(self.s.tle_ttl_s / 4)
            await self._safe_refresh_tles()

    async def _satpos_loop(self) -> None:
        """Publish the tracked satellite's state and its next pass.

        The browser propagates its own position for the smooth 1 Hz render, so
        this exists for clients that do not (and to keep every consumer working
        from the same schedule the antenna does).
        """
        while True:
            norad = self.s.default_norad
            pos = self.predictor.position(norad)
            if pos is not None:
                hub.publish("satpos", pos.model_dump(mode="json"))
                self.set_state("predictor", "ok")

            nxt = self.predictor.next_pass(norad)
            hub.publish("pass_next", nxt.model_dump(mode="json") if nxt else {})

            # 1 Hz while the satellite is up, otherwise every 5 s.
            fast = pos is not None and pos.el > -2.0
            await asyncio.sleep(1.0 if fast else 5.0)

    async def _control_loop(self) -> None:
        """Publish the interlock whenever it changes.

        The gates move on their own: a lease expires, SatNOGS picks up a job,
        the station reconnects. Without this the operator's panel would keep
        showing whatever was true when they last pressed something, which for a
        safety interlock is the wrong way round — it must go red by itself.

        Only changes are published. At 1 Hz an unchanging interlock would
        otherwise be by far the noisiest thing on the WebSocket.
        """
        previous: tuple | None = None
        while True:
            state = self.control.state()
            fingerprint = (
                state.armed,
                tuple(sorted(state.gates.items())),
                state.mode,
                state.target_norad,
            )
            if fingerprint != previous:
                self.control.publish()
                previous = fingerprint
            await asyncio.sleep(1.0)
