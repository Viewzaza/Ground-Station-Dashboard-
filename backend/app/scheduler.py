"""Background task supervision.

Every long-lived loop is owned here so there is exactly one place that knows
what is running. A crashed loop is restarted with backoff, and the state change
is logged once rather than once per attempt.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .config import Settings
from .hub import hub
from .services.campaign_service import CampaignService
from .services.control import ControlService
from .services.predictor import Predictor
from .services.rig_service import RigService
from .services.rotator_service import RotatorService
from .services.satnogs import SatnogsService
from .services.schedule_service import ScheduleService
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
        self.rig = RigService(
            settings, predictor,
            getattr(predictor, "transmitters", None),
            on_state=self.set_state,
        )
        self.control = ControlService(
            settings, self.rotator, self.satnogs, predictor, on_state=self.set_state
        )
        self.schedule_service = ScheduleService(settings, on_state=self.set_state)
        self.campaign_service = CampaignService(settings, self.schedule_service, on_state=self.set_state)

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
        self._spawn("rig", self.rig.run)
        self._spawn("schedule", self._schedule_loop)
        self._spawn("campaign", self._campaign_loop)

    async def stop(self) -> None:
        await self.rotator.stop()
        await self.rig.stop()
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

    # Never sleep longer than this in one go, however far away the next run
    # is. It bounds how stale our idea of "now" can get after an NTP step or a
    # suspend, and it is also the upper bound on how long a config change can
    # sit unnoticed if the wake-up event is ever missed.
    _AUTO_RUN_MAX_SLEEP_S = 300.0

    async def _schedule_loop(self) -> None:
        """Fire the station scheduler when the operator's auto-run says to.

        Two behaviours here are deliberate and are changes from how this used
        to work.

        **It no longer runs eagerly at boot.** This loop used to execute its
        body before its first sleep, so every restart triggered a run. That was
        harmless when a run only ever planned; now that a run can BOOK, a crash
        loop would book repeatedly. The catch-up grace window in `next_fire`
        covers the legitimate case - the backend being down across a slot -
        without turning restarts into bookings.

        **It waits on a config change, not just a clock.** `save_config()` sets
        an event this wait watches, so editing the auto-run times in the
        browser takes effect immediately instead of at the end of a multi-hour
        sleep. That is what makes a restart API unnecessary; the supervisor has
        none.
        """
        while True:
            due = self.schedule_service.next_auto_run()
            if due is None:
                # Auto-run is off, or is on with no times set. Sleep on the
                # config event so turning it on is noticed at once.
                await self.schedule_service.wait_for_config_change(
                    self._AUTO_RUN_MAX_SLEEP_S
                )
                continue

            now = datetime.now(timezone.utc)
            wait_s = (due - now).total_seconds()
            if wait_s > 0:
                changed = await self.schedule_service.wait_for_config_change(
                    min(wait_s, self._AUTO_RUN_MAX_SLEEP_S)
                )
                if changed:
                    continue  # recompute against the new settings
                if (due - datetime.now(timezone.utc)).total_seconds() > 0:
                    continue  # capped sleep; go round again
                # fall through: the slot has arrived

            dry_run = self.schedule_service.auto_run_dry_run()
            # Marked BEFORE the run, not after. A run can take minutes, and if
            # the process dies mid-run an unmarked slot would fire again on
            # restart - against a station that may already have the bookings.
            previous = await self.schedule_service.mark_auto_run()
            log.info(
                "auto-run firing (%s)", "dry run" if dry_run else "BOOKING FOR REAL"
            )
            result = await self.schedule_service.run_plan(
                dry_run=dry_run, trigger="auto"
            )
            if result.get("status") == "running":
                # run_plan refused: a manual run was already in flight. Nothing
                # was planned and nothing was booked, so the slot has not
                # happened - put the mark back or it is consumed silently and
                # the scheduled booking never occurs at all. A cold-cache
                # manual run easily spans a slot boundary, so this is not a
                # rare race.
                await self.schedule_service.restore_auto_run_mark(previous)
                log.warning(
                    "auto-run slot skipped: a run was already in progress. The "
                    "slot has been left unfired and will be retried."
                )

    async def _campaign_loop(self) -> None:
        """Keep the ~48h network-campaign booking window full on a timer.

        Always previews; only actually books if the operator has explicitly
        turned campaign_auto_commit_enabled on (default off) - see
        CampaignService.run_auto_cycle()'s own docstring for why the timer
        path is allowed to auto-commit at all while the UI's manual trigger
        never does.
        """
        while True:
            await self.campaign_service.run_auto_cycle()
            await asyncio.sleep(self.s.campaign_poll_s)

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
