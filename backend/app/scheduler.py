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
from .services.predictor import Predictor
from .services.tle_store import TleStore

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, settings: Settings, tles: TleStore, predictor: Predictor) -> None:
        self.s = settings
        self.tles = tles
        self.predictor = predictor
        self._tasks: list[asyncio.Task] = []
        self.components: dict[str, str] = {}

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        # One eager refresh so the first page load has elements to work with.
        await self._safe_refresh_tles()
        self._spawn("tle", self._tle_loop)

    async def stop(self) -> None:
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

    async def _tle_loop(self) -> None:
        # Re-check on the TTL boundary. refresh() itself refuses to hit the
        # network early, so this cadence is safe even if it is generous.
        while True:
            await asyncio.sleep(self.s.tle_ttl_s / 4)
            await self._safe_refresh_tles()
