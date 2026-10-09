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

from skyfield.api import EarthSatellite

from .config import Settings
from .hub import hub
from .services.antenna import TleCache, antenna_state
from .services.antenna import fingerprint as antenna_fingerprint
from .services.control import ControlService
from .services.planner_service import PlanExecutor, PlannerService
from .services.predictor import Predictor
from .services.rig_service import RigService
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
        self.rig = RigService(
            settings, predictor,
            getattr(predictor, "transmitters", None),
            on_state=self.set_state,
        )
        self.control = ControlService(
            settings, self.rotator, self.satnogs, predictor, on_state=self.set_state
        )
        # The planner only reads; the executor acts, and only through
        # ControlService, so the interlock covers autopilot exactly as it
        # covers an operator's hand on the panel.
        self.planner = PlannerService(
            settings, predictor, self.satnogs, getattr(predictor, 'tles', tles)
        )
        self.planner.rotator = self.rotator
        self.executor = PlanExecutor(settings, self.planner, self.control, self.rotator)

        # Who has the antenna, for the display. Read from everything above and
        # fed back into none of it: the gates, the track loop and the executor
        # never see it (services/antenna.py says why).
        self.antenna_tles = TleCache()
        self.antenna_last: dict | None = None
        self._antenna_key: tuple | None = None
        self._antenna_los: dict = {}
        self._job_sat: tuple | None = None      # (job_id, EarthSatellite | None)
        # The ERR readout measures against the satellite the antenna is
        # working, not the one this process happens to default to.
        self.rotator.focus = self._focus

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
        self._spawn("planner", self.planner.run)
        self._spawn("autopilot", self.executor.run)

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
        """Publish the state and next pass of the satellite the antenna is on.

        The browser propagates its own position for the smooth 1 Hz render, so
        this exists for clients that do not (and to keep every consumer working
        from the same schedule the antenna does).

        Which satellite is the antenna's focus when the catalogue has it, and
        the default satellite otherwise: a job's own elements are enough for a
        pointing error, but not for a pass card the browser could not also
        draw an orbit for.
        """
        while True:
            norad = self._display_norad()
            pos = self.predictor.position(norad)
            if pos is not None:
                hub.publish("satpos", pos.model_dump(mode="json"))
                self.set_state("predictor", "ok")

            nxt = self.predictor.next_pass(norad)
            # Always say which satellite this is about, "no pass" included.
            # Every browser shows whichever satellite its operator picked, and
            # each applies this only when it is about that one. An unlabelled
            # frame used to be applied by all of them, so an operator who chose
            # ISS saw the next-pass card and the header's AOS snap back to
            # KNACKSAT-2 within five seconds, under a map still showing ISS.
            hub.publish("pass_next",
                        nxt.model_dump(mode="json") if nxt else {"norad": norad})

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
            # Before reading the state, so a lapse that stops a slew is in the
            # state published this tick.
            try:
                await self.control.check_lease()
            except Exception:
                log.exception("lease check failed")
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
            # After the interlock, from the same state, and never the other
            # way round: who has the antenna is worked out from the gates,
            # never fed into them.
            self._publish_antenna(state)
            await asyncio.sleep(1.0)

    # --- antenna ownership (display only) -----------------------------------
    def compute_antenna(self, control_state=None) -> dict:
        """Who has the antenna and which satellite it is working, now.

        Pure reads, no publish — which is what lets GET /api/antenna answer
        before the control loop's first tick without becoming a second writer.
        """
        return antenna_state(
            control_state=control_state if control_state is not None
            else self.control.state(),
            control=self.control,
            executor_state=self.executor.state,
            satnogs=self.satnogs,
            plan=self.planner.plan,
            settings=self.s,
            predictor=self.predictor,
            tlecache=self.antenna_tles,
            now=datetime.now(timezone.utc),
            los_cache=self._antenna_los,
        )

    def _publish_antenna(self, control_state=None) -> dict | None:
        """Recompute, and publish only when something other than the clock
        moved — at 1 Hz an unchanging owner would otherwise be a frame a
        second to every screen, which is what the interlock's own publish
        above avoids too."""
        try:
            self.antenna_tles.update(self.satnogs.commitments)
            current = self.compute_antenna(control_state)
        except Exception:
            # A display line failing must never take the control loop with it:
            # the lease check above is the one thing here that stops motion.
            log.exception("antenna state failed")
            return None
        key = antenna_fingerprint(current)
        if key != self._antenna_key:
            hub.publish("antenna", current)
            self._antenna_key = key
        self.antenna_last = current
        return current

    def _focus(self) -> tuple[int, EarthSatellite | None]:
        """(norad, override) for the pointing readout.

        The override is an EarthSatellite built from a SatNOGS job's own
        elements, and only when the catalogue lacks the satellite — a job under
        a temporary catalogue number. It is built once per job, not once a
        second.
        """
        state = self.antenna_last
        if not state or state.get("focus_norad") is None:
            return self.s.default_norad, None
        norad = int(state["focus_norad"])
        if state.get("focus_source") != "job_tle":
            return norad, None
        job_id = state.get("focus_job_id")
        if self._job_sat is None or self._job_sat[0] != job_id:
            entry = self.antenna_tles.get(job_id)
            sat = None
            if entry is not None:
                try:
                    sat = EarthSatellite(entry.tle1, entry.tle2,
                                         entry.tle0 or f"#{norad}", self.predictor.ts)
                except Exception:
                    log.warning("job %s carries elements that do not parse", job_id)
            self._job_sat = (job_id, sat)
        return norad, self._job_sat[1]

    def _display_norad(self) -> int:
        """The focus, when the catalogue can draw it; the default otherwise."""
        state = self.antenna_last
        norad = state.get("focus_norad") if state else None
        if norad is not None and state.get("focus_source") == "catalogue":
            return int(norad)
        return self.s.default_norad
