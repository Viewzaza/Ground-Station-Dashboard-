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
from .services.campaign_service import AUTO_CYCLE_BUSY, AUTO_CYCLE_RETRY, CampaignService
from .services.control import ControlService
from .services.predictor import Predictor
from .services.rig_service import RigService
from .services.rotator_service import RotatorService
from .services.satnogs import SatnogsService
from .services.schedule_service import ScheduleService, run_is_retryable
from .services.tle_store import TleStore

log = logging.getLogger(__name__)


class Scheduler:
    # --- retrying an auto-run slot's work ------------------------------------
    # 2026-10-04 11:00Z: SatNOGS answered HTTP 500 to one read in each half of
    # the slot - the station run's calendar download ("network_download") and
    # the chained campaign's station catalogue - and the whole slot was lost:
    # nothing retried, and the next slot was 12 hours away. SatNOGS was flaky,
    # not down (12 of our next 126 requests got a 500, the rest a 200), so the
    # same work ten minutes later would very likely have gone through.
    #
    # A retry only ever repeats work that sent NO booking POST: a chained
    # campaign whose preview failed transiently (run_chained_cycle "skipped"
    # with "retryable") and a station run the official tool itself stopped
    # before booking (run_is_retryable). Anything that reached a commit or a
    # submit is never repeated, whatever came of it. Retries run as background
    # tasks, at most one of each kind, so the slot loop never waits on them;
    # they never touch the slot mark, and as in-process tasks they die with
    # the process - a restart never fires one.
    CHAIN_RETRY_DELAY_S = 600.0
    CHAIN_MAX_RETRIES = 5
    STATION_RETRY_DELAY_S = 600.0
    STATION_MAX_RETRIES = 3
    # A retry this close to the next slot is left to that slot, which runs the
    # station and the chain afresh anyway; retrying as well would race it for
    # the same single-flight guards. A slot that has already fired but not yet
    # done the retry's half is covered too, by _slot_owes: next_auto_run()
    # has moved past it by then, so this guard alone no longer sees it.
    RETRY_NEXT_SLOT_GUARD_S = 900.0

    # Class-level defaults, so a Scheduler made without __init__ (the loop
    # tests' owners) has them too.
    _chain_retry_task: asyncio.Task | None = None
    _station_retry_task: asyncio.Task | None = None
    # How many slots have run the chain / the station run themselves, and the
    # count a pending retry stands for. A later slot that ran the same work
    # makes the retry stand down instead of repeating work that slot already
    # did - unless that slot needs a retry too, when it re-arms the pending
    # one rather than starting a second (_start_chain_retry).
    _chain_runs = 0
    _chain_retry_for = 0
    # When the failure a pending chain retry stands for happened; bookings
    # sent after it make the retry stand down (_campaign_booked_since).
    _chain_retry_since: datetime | None = None
    _station_runs = 0
    _station_retry_for = 0
    # The halves ("station", "chain") of a slot that has fired which it has
    # yet to run itself; a retry of either kind waking meanwhile leaves its
    # half to the slot. Without it, with interval auto-runs (5 min and up), a
    # chain retry woke while the next slot was still in its station run -
    # past the next-slot guard, since that slot was already marked, and past
    # the stand-down, since it had not chained yet - committed, and then that
    # slot's own chain committed again straight after. Two unattended commits
    # back to back can push a station over max_per_station while SatNOGS's
    # reads lag (campaign.py does not count recent_attempts toward the cap).
    _slot_owes: frozenset[str] = frozenset()

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
        self.campaign_service = CampaignService(
            settings, self.schedule_service, on_state=self.set_state,
            own_station=self._own_station,
        )

    def _own_station(self) -> dict | None:
        """Our own station as SatnogsService last polled it (every ~60 s), for
        CampaignService's own-station gate - SatNOGS refuses bookings on other
        people's stations while none of ours is Online. Reuses that poll
        rather than reading again: it is already the dashboard's one source
        for this station's status. None until the first poll lands (and
        always under GS_OFFLINE, where the poller does not run), which the
        gate reads as "unknown" and never blocks on."""
        station = self.satnogs.station
        if station is None:
            return None
        return {
            "id": station.get("id", self.s.station_id),
            "status": station.get("status"),
            "last_seen": station.get("last_seen"),
            "age_s": self.satnogs.station_age_s,
        }

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
        # The retry tasks are not supervised, but go the same way. One asleep
        # between attempts just ends; one mid-attempt releases its service's
        # single-flight guard on the way out, since run_plan, preview_campaign
        # and commit_campaign all clear it in a finally.
        retries = [task for task in (self._chain_retry_task, self._station_retry_task)
                   if task is not None]
        for task in [*self._tasks, *retries]:
            task.cancel()
        await asyncio.gather(*self._tasks, *retries, return_exceptions=True)
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

    # How long to back off when a slot comes due while a manual run is still
    # going. Without it the loop re-marks, is refused and restores as fast as
    # it can go - two config writes and two log lines per pass - for as long
    # as that run lasts, which on a cold cache is minutes.
    _AUTO_RUN_BUSY_RETRY_S = 30.0

    async def _schedule_loop(self) -> None:
        """Fire the station scheduler when the operator's auto-run says to.

        Two behaviours here are deliberate and are changes from how this used
        to work.

        **It no longer runs eagerly at boot.** This loop used to execute its
        body before its first sleep, so every restart triggered a run. That was
        harmless when a run only ever planned; now that every run BOOKS, a
        crash loop would book repeatedly. The catch-up grace window in `next_fire`
        covers the legitimate case - the backend being down across a slot -
        without turning restarts into bookings.

        **It waits on a config change, not just a clock.** `save_config()` sets
        an event this wait watches, so editing the auto-run times in the
        browser takes effect immediately instead of at the end of a multi-hour
        sleep. That is what makes a restart API unnecessary; the supervisor has
        none.

        After a slot that ran, `_after_auto_run` may chain the worldwide
        Network Campaign (its own opt-in toggle). It is awaited in line, so
        the next slot is computed only once that finishes - minutes, against
        slots hours apart. Retries of either half after a transient SatNOGS
        failure are background tasks (`_station_retry_loop`,
        `_chain_retry_loop`) that this loop never waits for.
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

            # Owed from before the mark, not after it: mark_auto_run() puts
            # the mark in memory - moving next_auto_run() past this slot -
            # before it has finished writing it, and a retry waking in that
            # gap must already see this slot.
            self._slot_owes = frozenset({"station", "chain"})
            try:
                # Marked BEFORE the run, not after. A run can take minutes, and
                # if the process dies mid-run an unmarked slot would fire again
                # on restart - against a station that may already have the
                # bookings.
                previous = await self.schedule_service.mark_auto_run()
                log.info("auto-run firing (BOOKING FOR REAL)")
                result = await self.schedule_service.run_plan(trigger="auto")
                busy = result.get("status") == "running"
                if busy:
                    # run_plan refused: a manual run was already in flight.
                    # Nothing was planned and nothing was booked, so the slot
                    # has not happened - put the mark back or it is consumed
                    # silently and the scheduled booking never occurs at all.
                    # A cold-cache manual run easily spans a slot boundary, so
                    # this is not a rare race.
                    await self.schedule_service.restore_auto_run_mark(previous)
                    log.warning(
                        "auto-run slot skipped: a run was already in progress. The "
                        "slot has been left unfired and will be retried in %.0fs.",
                        self._AUTO_RUN_BUSY_RETRY_S,
                    )
                else:
                    # The station half is done, so it is no longer owed: if
                    # this run hit network_download too, the pending station
                    # retry is re-armed for it just below and must be free to
                    # run while this slot is still chaining. Only a slot that
                    # really ran gets the chain - the busy path above has not
                    # happened yet and will come round again.
                    self._slot_owes = frozenset({"chain"})
                    self._station_runs += 1
                    if run_is_retryable(result):
                        self._start_station_retry(result)
                    await self._after_auto_run(result)
            finally:
                self._slot_owes = frozenset()
            if busy:
                # Outside the slot: restored, it is simply due again, which
                # the next-slot guard already covers.
                await asyncio.sleep(self._AUTO_RUN_BUSY_RETRY_S)

    async def _after_auto_run(self, result: dict) -> None:
        """After a Station Schedule auto-run slot: if the operator has
        auto_run_chain_campaign on, book KNACKSAT-2 on community stations
        worldwide too (CampaignService.run_chained_cycle).

        Runs whatever the station run's own outcome was: 5024's run failing
        says nothing about the community stations, and when 5024 is Offline
        the campaign's own-station gate turns the commit into "blocked"
        anyway. It never touches the slot mark - the slot is already marked
        and fired, so it is never re-fired, and nothing new happens at
        startup. A preview that failed on a transient SatNOGS error (nothing
        sent) is retried in the background by _chain_retry_loop; every other
        outcome, commits of any status included, is final for this slot. And
        it never raises: a campaign failure must not take the station's own
        booking loop down with it.
        """
        try:
            if not self.schedule_service.auto_run_chain_campaign():
                return
            log.info("auto-run: chaining the worldwide KNACKSAT-2 campaign "
                     "(station run status %s)", result.get("status"))
            outcome = await self.campaign_service.run_chained_cycle()
        except Exception:
            log.exception("auto-run: the chained campaign failed; the station "
                          "schedule is unaffected and the slot is not retried")
            return
        status = outcome.get("status")
        if status != "running":
            # This slot ran the chain itself, so a retry still pending from an
            # earlier slot stands down - unless it is re-armed just below.
            self._chain_runs += 1
        if status == "running":
            log.info("auto-run: a campaign preview/commit/cross-check was already "
                     "running, so this slot's chain was skipped")
        elif status == "skipped":
            log.info("auto-run: chained campaign skipped: %s", outcome.get("reason"))
            if outcome.get("retryable"):
                self._start_chain_retry(outcome.get("reason") or "")
        else:
            log.info("auto-run: chained campaign %s - %s booked; %s", status,
                     outcome.get("accepted", 0), outcome.get("stopped_reason") or "")

    @staticmethod
    def _retry_pending(task: asyncio.Task | None) -> bool:
        return task is not None and not task.done()

    def _retry_blocker(self, half: str, switched_off: str | None,
                       superseded: bool) -> str | None:
        """Why a retry of `half` ("station" or "chain") that is due must not
        run after all, or None if it may. Checked after each wait, so it
        reflects the config as it is now."""
        if switched_off:
            return f"{switched_off} has been switched off"
        if superseded:
            return "a later slot has run it since"
        if half in self._slot_owes:
            return "a slot is in progress and will run it itself"
        due = self.schedule_service.next_auto_run()
        if due is not None and ((due - datetime.now(timezone.utc)).total_seconds()
                                <= self.RETRY_NEXT_SLOT_GUARD_S):
            return f"the next slot ({due.isoformat()}) is close enough to do it instead"
        return None

    def _campaign_booked_since(self, since: datetime | None) -> datetime | None:
        """When a campaign booking POST went out after `since`, or None.

        A manual click or the campaign timer committing between a failed
        chain and its retry has already topped the stations up, from a fresh
        read. The retry would plan again from calendars that need not show
        those bookings yet - the observation feed has been measured lagging
        writes by over an hour, and /jobs/ lag is unmeasured - so it could push
        stations past the per-station cap with nothing at SatNOGS to stop it
        (the passes differ, so no 409). Standing down costs nothing: the
        top-up the retry was for has been made. A "blocked" commit sends
        nothing and records no submit, so it does not count."""
        if since is None:
            return None
        try:
            last = self.campaign_service.last_submit_at()
        except Exception:  # noqa: BLE001 - an unreadable record must not stop the retry
            return None
        return last if last is not None and last > since else None

    def _start_chain_retry(self, reason: str) -> None:
        self._chain_retry_for = self._chain_runs
        # Re-armed by a later failing slot too: only bookings after the most
        # recent failure say the work is done.
        self._chain_retry_since = datetime.now(timezone.utc)
        if self._retry_pending(self._chain_retry_task):
            log.info("auto-run: a chained campaign retry is already pending; it now "
                     "stands for this slot as well")
            return
        log.warning("auto-run: the chained campaign failed before sending anything (%s); "
                    "retrying in %.0f s, up to %d times", reason,
                    self.CHAIN_RETRY_DELAY_S, self.CHAIN_MAX_RETRIES)
        self._chain_retry_task = asyncio.create_task(
            self._chain_retry_loop(reason), name="chain-retry")

    async def _chain_retry_loop(self, reason: str) -> None:
        """Run a slot's chained campaign again after its preview failed on a
        transient SatNOGS error.

        Each attempt is a whole run_chained_cycle(): a fresh preview, then a
        commit of it. Only another transient preview failure goes round
        again. "running" stops it (a manual or timer operation has the
        campaign in hand), and so does any commit result, whatever its status:
        a commit may have sent bookings, and is never repeated.

        It stops early if the chain or the auto-run itself is switched off
        (the chain only exists as part of the auto-run, so switching that off
        is the operator's stop for it too), if a later slot has run the chain
        itself or has fired and is still to chain, or if the next slot is
        within RETRY_NEXT_SLOT_GUARD_S. It never raises.
        """
        try:
            for attempt in range(1, self.CHAIN_MAX_RETRIES + 1):
                await asyncio.sleep(self.CHAIN_RETRY_DELAY_S)
                on = (self.schedule_service.auto_run_chain_campaign()
                      and self.schedule_service.auto_run_enabled())
                blocker = self._retry_blocker(
                    "chain",
                    None if on else "the auto-run or its campaign chain",
                    self._chain_retry_for != self._chain_runs,
                )
                if blocker:
                    log.info("auto-run: chained campaign retry %d/%d not run: %s",
                             attempt, self.CHAIN_MAX_RETRIES, blocker)
                    return
                booked_at = self._campaign_booked_since(self._chain_retry_since)
                if booked_at is not None:
                    log.info("auto-run: chained campaign retry %d/%d not run: a campaign "
                             "booking went out at %s, after this slot's chain failed, "
                             "and already topped the stations up", attempt,
                             self.CHAIN_MAX_RETRIES, booked_at.isoformat())
                    return
                log.info("auto-run: chained campaign retry %d/%d (the last attempt "
                         "failed: %s)", attempt, self.CHAIN_MAX_RETRIES, reason)
                outcome = await self.campaign_service.run_chained_cycle()
                status = outcome.get("status")
                if status == "skipped" and outcome.get("retryable"):
                    reason = outcome.get("reason") or ""
                    continue
                if status == "running":
                    log.info("auto-run: chained campaign retry %d/%d found another campaign "
                             "operation running; leaving it to that", attempt,
                             self.CHAIN_MAX_RETRIES)
                elif status == "skipped":
                    log.info("auto-run: chained campaign retry %d/%d skipped: %s", attempt,
                             self.CHAIN_MAX_RETRIES, outcome.get("reason"))
                else:
                    log.info("auto-run: chained campaign retry %d/%d %s - %s booked; %s",
                             attempt, self.CHAIN_MAX_RETRIES, status,
                             outcome.get("accepted", 0), outcome.get("stopped_reason") or "")
                return
            log.warning("auto-run: chained campaign still failing after %d retries (%s); "
                        "the next slot will try again", self.CHAIN_MAX_RETRIES, reason)
        except Exception:
            log.exception("auto-run: a chained campaign retry failed; no more retries "
                          "for this slot")

    def _start_station_retry(self, result: dict) -> None:
        self._station_retry_for = self._station_runs
        if self._retry_pending(self._station_retry_task):
            log.info("auto-run: a station run retry is already pending; it now stands "
                     "for this slot as well")
            return
        log.warning("auto-run: the station run stopped before booking (%s); retrying in "
                    "%.0f s, up to %d times", result.get("error"),
                    self.STATION_RETRY_DELAY_S, self.STATION_MAX_RETRIES)
        self._station_retry_task = asyncio.create_task(
            self._station_retry_loop(), name="station-retry")

    async def _station_retry_loop(self) -> None:
        """Run a slot's station run again after it failed on network_download.

        Safe to repeat because nothing can have been booked: the official
        auto-scheduler downloads the station's existing schedule before it
        plans anything and exits rather than book without it - that refusal
        is exactly network_download - and run_is_retryable also demands
        booked == 0 and booked_state "failed" (never reached its booking
        step).

        It does not run the chain again: the chain already ran for this slot
        and has its own retry. It stops early if the auto-run is switched
        off, if a later slot has run the station itself or has fired and is
        still to, if the next slot is within RETRY_NEXT_SLOT_GUARD_S, on
        "running" (a manual run is in flight and covers it), and on any
        result run_is_retryable refuses. It never raises.
        """
        try:
            for attempt in range(1, self.STATION_MAX_RETRIES + 1):
                await asyncio.sleep(self.STATION_RETRY_DELAY_S)
                blocker = self._retry_blocker(
                    "station",
                    None if self.schedule_service.auto_run_enabled() else "the auto-run",
                    self._station_retry_for != self._station_runs,
                )
                if blocker:
                    log.info("auto-run: station run retry %d/%d not run: %s",
                             attempt, self.STATION_MAX_RETRIES, blocker)
                    return
                log.info("auto-run: station run retry %d/%d (BOOKING FOR REAL; the last "
                         "run could not read the station's schedule from SatNOGS Network)",
                         attempt, self.STATION_MAX_RETRIES)
                result = await self.schedule_service.run_plan(trigger="auto")
                if run_is_retryable(result):
                    continue
                if result.get("status") == "running":
                    log.info("auto-run: station run retry %d/%d found a run already in "
                             "progress; leaving it to that", attempt, self.STATION_MAX_RETRIES)
                else:
                    log.info("auto-run: station run retry %d/%d finished: %s, %s booked",
                             attempt, self.STATION_MAX_RETRIES, result.get("status"),
                             result.get("booked", 0))
                return
            log.warning("auto-run: the station run still could not read its schedule after "
                        "%d retries; the next slot will try again", self.STATION_MAX_RETRIES)
        except Exception:
            log.exception("auto-run: a station run retry failed; no more retries for this slot")

    # The campaign timer's answer to a busy guard, the counterpart of
    # _AUTO_RUN_BUSY_RETRY_S. Since the cross-check went behind the same
    # single-flight guard as preview and commit, a CROSS-CHECK clicked shortly
    # before the timer is due holds it for minutes (one serial calendar read
    # per station, ~1.7 s each: ~6 min for ~220 stations), and a cycle that
    # simply gave up then lost the whole day - the loop went straight back to
    # sleep for campaign_poll_s (24 h), auto-commit or not. Bounded, so a
    # guard that never frees (a 90-minute looped commit is the longest real
    # case) costs this cycle, not a retry loop forever: 30 x 60 s = 30 min.
    _CAMPAIGN_BUSY_RETRY_S = 60.0
    _CAMPAIGN_BUSY_MAX_RETRIES = 30

    # Its answer to a preview that failed on a transient SatNOGS error
    # (AUTO_CYCLE_RETRY), which used to cost the day's cycle the same way:
    # a failed preview slept a whole campaign_poll_s. Counted apart from the
    # busy retries, and neither resets the other, so a cycle that keeps
    # alternating between the two still ends: at most
    # _CAMPAIGN_BUSY_MAX_RETRIES + CAMPAIGN_MAX_RETRIES + 1 attempts.
    CAMPAIGN_RETRY_DELAY_S = 600.0
    CAMPAIGN_MAX_RETRIES = 6

    async def _campaign_loop(self) -> None:
        """Keep the ~48h network-campaign booking window full on a timer.

        Always previews; only actually books if the operator has explicitly
        turned campaign_auto_commit_enabled on (default off) - see
        CampaignService.run_auto_cycle()'s own docstring for why the timer
        path is allowed to auto-commit at all while the UI's manual trigger
        never does.

        It does not fire at startup if its own last cycle was within the last
        campaign_poll_s: it sleeps out the remainder first. Running eagerly
        meant every restart - including each uvicorn --reload after an edit
        under backend/app - fired a full real preview, and with auto-commit
        on, real bookings. See CampaignService.auto_cycle_delay_s().

        A cycle refused because another campaign operation holds the guard
        is retried every _CAMPAIGN_BUSY_RETRY_S (at most
        _CAMPAIGN_BUSY_MAX_RETRIES times) instead of waiting a whole period,
        and one whose preview failed on a transient SatNOGS error every
        CAMPAIGN_RETRY_DELAY_S (at most CAMPAIGN_MAX_RETRIES times). These
        retries are in-process only: a restart in between waits out the
        period from the latest attempt, as above.
        """
        delay_s = self.campaign_service.auto_cycle_delay_s()
        if delay_s > 0:
            log.info("campaign timer: its last cycle is recent; first cycle in %.0f s", delay_s)
            await asyncio.sleep(delay_s)
        busy_retries = 0
        error_retries = 0
        while True:
            outcome = await self.campaign_service.run_auto_cycle()
            if outcome == AUTO_CYCLE_BUSY:
                if busy_retries < self._CAMPAIGN_BUSY_MAX_RETRIES:
                    busy_retries += 1
                    log.info("campaign timer: another campaign operation is running; "
                             "retrying in %.0f s (%d/%d)", self._CAMPAIGN_BUSY_RETRY_S,
                             busy_retries, self._CAMPAIGN_BUSY_MAX_RETRIES)
                    await asyncio.sleep(self._CAMPAIGN_BUSY_RETRY_S)
                    continue
                log.warning("campaign timer: still busy after %d retries; this cycle is "
                            "skipped, the next is in %.0f s", busy_retries, self.s.campaign_poll_s)
            elif outcome == AUTO_CYCLE_RETRY:
                if error_retries < self.CAMPAIGN_MAX_RETRIES:
                    error_retries += 1
                    log.warning("campaign timer: the preview failed on a transient SatNOGS "
                                "error; retrying in %.0f s (%d/%d)", self.CAMPAIGN_RETRY_DELAY_S,
                                error_retries, self.CAMPAIGN_MAX_RETRIES)
                    await asyncio.sleep(self.CAMPAIGN_RETRY_DELAY_S)
                    continue
                log.warning("campaign timer: the preview still failed after %d retries; this "
                            "cycle is skipped, the next is in %.0f s", error_retries,
                            self.s.campaign_poll_s)
            # The cycle is over. Both counters reset here and only here.
            busy_retries = 0
            error_retries = 0
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
