"""Rotator control, and the interlock that stands in front of it.

This is the only code in the project that can move the antenna. Everything
about it is written on the assumption that the *dangerous* failure is not
refusing a legitimate move — it is allowing one while something else is already
driving, or while the station is about to record a pass. So every decision here
fails closed.

Four gates, all of which must pass, on every command and on every iteration of
a track:

  kill switch        GS_ROTATOR_CONTROL_ENABLED=1
  satnogs idle       station 5024 reports is_connected = false
  no imminent pass   nothing scheduled within GS_GATE_GUARD_S
  armed              an operator arm, expiring after GS_CONTROL_LEASE_S

Two consequences worth stating, because both look like bugs until you know:

**Unknown is not permission.** If SatNOGS has not answered yet, or its answer
is older than GS_GATE_MAX_STALE_S, the satnogs and pass gates fail. A cached
all-clear from four minutes ago is not evidence that satnogs-client is idle
now, and this is exactly the window in which it would pick up a job.

**The gates are re-checked mid-track, not just at the start.** A pass gets
scheduled, or the lease runs out, and the track must stop on its own. Checking
only on entry would leave a loop driving the antenna against a gate that has
since closed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ..config import Settings
from ..hub import hub
from ..schemas import ControlState
from .rotctld_client import RotctldError

log = logging.getLogger(__name__)

TRACK_STEP_S = 1.0
# Below the horizon there is nothing to point at, and most rotators will not
# accept a negative elevation anyway.
TRACK_MIN_EL_DEG = 0.0


class ControlRefused(RuntimeError):
    """A command was rejected by the interlock. Carries the failing gates."""

    def __init__(self, blocked_by: list[str], detail: str = "") -> None:
        super().__init__(detail or f"refused: {', '.join(blocked_by) or 'unknown'}")
        self.blocked_by = blocked_by


class ControlService:
    def __init__(self, settings: Settings, rotator, satnogs, predictor,
                 on_state=None) -> None:
        self.s = settings
        self.rotator = rotator
        self.satnogs = satnogs
        self.predictor = predictor
        self.on_state = on_state or (lambda component, state, detail="": None)

        self._lease_expires: datetime | None = None
        self._mode: str = "idle"
        self._target_norad: int | None = None
        self._track_task: asyncio.Task | None = None
        self._last_command: str = ""

    # --- lease -------------------------------------------------------------
    @property
    def armed(self) -> bool:
        if self._lease_expires is None:
            return False
        return datetime.now(timezone.utc) < self._lease_expires

    def arm(self) -> ControlState:
        """Take a control lease. Deliberately does not check the other gates.

        An operator must be able to arm and then read *why* they still cannot
        move; refusing the arm itself would leave them with no way to see the
        remaining blockers.
        """
        if not self.s.rotator_control_enabled:
            raise ControlRefused(["kill_switch"],
                                 "rotator control is disabled in configuration")
        self._lease_expires = datetime.now(timezone.utc) + timedelta(
            seconds=self.s.control_lease_s
        )
        log.info("control armed until %s", self._lease_expires.isoformat())
        return self.publish()

    def release(self) -> ControlState:
        self._lease_expires = None
        self._stop_track()
        self._mode = "idle"
        log.info("control released")
        return self.publish()

    # --- gates -------------------------------------------------------------
    def gates(self) -> dict[str, bool]:
        max_stale = float(self.s.gate_max_stale_s)

        station_age = self.satnogs.station_age_s
        station_fresh = station_age is not None and station_age <= max_stale
        # is_connected None means "no answer yet", which is not an all-clear.
        satnogs_idle = bool(station_fresh and self.satnogs.is_connected is False)

        jobs_age = self.satnogs.jobs_age_s
        jobs_fresh = jobs_age is not None and jobs_age <= max_stale
        seconds_to_job = self.satnogs.seconds_to_next_job()
        no_imminent_pass = bool(
            jobs_fresh
            and (seconds_to_job is None or seconds_to_job > self.s.gate_guard_s)
        )

        return {
            "kill_switch": bool(self.s.rotator_control_enabled),
            "satnogs_idle": satnogs_idle,
            "no_imminent_pass": no_imminent_pass,
            "armed": self.armed,
        }

    def blocked_by(self) -> list[str]:
        return [name for name, ok in self.gates().items() if not ok]

    def _require_clear(self) -> None:
        blocked = self.blocked_by()
        if blocked:
            raise ControlRefused(blocked)
        # Not one of the four safety gates: the interlock is about whether we
        # are *allowed* to move, this is about whether we *can*. Separating them
        # keeps "SatNOGS is using the antenna" from reading like a cable fault.
        if not self.rotator.verified:
            raise ControlRefused([], "rotator is not identified; refusing to command it")
        if self.rotator.last is None or self.rotator.last.link != "up":
            raise ControlRefused([], "rotator link is down")

    # --- state -------------------------------------------------------------
    def state(self) -> ControlState:
        gates = self.gates()
        return ControlState(
            enabled=self.s.rotator_control_enabled,
            armed=self.armed,
            lease_expires_at=self._lease_expires,
            gates=gates,
            blocked_by=[n for n, ok in gates.items() if not ok],
            mode=self._mode,  # type: ignore[arg-type]
            target_norad=self._target_norad,
        )

    def publish(self) -> ControlState:
        state = self.state()
        payload = state.model_dump(mode="json")
        payload["last_command"] = self._last_command
        payload["can_park"] = bool(
            self.rotator.client.caps and self.rotator.client.caps.can_park
        )
        hub.publish("control", payload)
        return state

    # --- commands ----------------------------------------------------------
    async def goto(self, az: float, el: float) -> ControlState:
        self._require_clear()
        self._stop_track()
        await self._write(lambda: self.rotator.client.set_position(az, el),
                          f"goto az={az:.1f} el={el:.1f}")
        self._mode = "manual"
        return self.publish()

    async def stop(self) -> ControlState:
        """Halt motion.

        Stop is *not* gated. If the antenna is moving and an operator wants it
        to stop, an expired lease is not a reason to keep driving. It is also
        harmless: the worst case is stopping a rotator that was already still.
        """
        self._stop_track()
        if self.rotator.verified:
            await self._write(self.rotator.client.stop, "stop")
        self._mode = "idle"
        return self.publish()

    async def park(self) -> ControlState:
        """Send the antenna to the park position.

        Station 5024's SPID reports `Can Park: N`, so there is no park command
        to send — park is an ordinary absolute move to the configured park
        coordinates. Where a backend does implement park natively we still use
        set_pos, so the resting position is the one in the configuration rather
        than whatever the controller's firmware happens to prefer.
        """
        self._require_clear()
        self._stop_track()
        await self._write(
            lambda: self.rotator.client.set_position(self.s.park_az, self.s.park_el),
            f"park az={self.s.park_az:.1f} el={self.s.park_el:.1f}",
        )
        self._mode = "manual"
        return self.publish()

    async def track(self, norad: int | None = None) -> ControlState:
        self._require_clear()
        norad = norad or self.s.default_norad
        if self.predictor.satellite(norad) is None:
            raise ControlRefused([], f"no elements for {norad}")
        self._stop_track()
        self._target_norad = norad
        self._mode = "track"
        self._track_task = asyncio.create_task(self._track_loop(norad))
        log.info("tracking %s", norad)
        return self.publish()

    async def _write(self, fn, what: str) -> None:
        try:
            await fn()
        except RotctldError as exc:
            self._last_command = f"{what} -> {exc}"
            self.on_state("control", "degraded", str(exc))
            raise
        self._last_command = what
        log.info("rotator command: %s", what)

    # --- tracking ----------------------------------------------------------
    def _stop_track(self) -> None:
        if self._track_task is not None and not self._track_task.done():
            self._track_task.cancel()
        self._track_task = None

    async def _track_loop(self, norad: int) -> None:
        """Follow the satellite until it sets, a gate closes, or we are stopped.

        Commands are only issued once the antenna is further than the deadband
        from where it should be. SatNOGS itself uses 4 degrees; below about 5
        the rotator is commanded continuously and a 600-baud SPID spends the
        whole pass acknowledging writes instead of moving.
        """
        try:
            while True:
                blocked = self.blocked_by()
                if blocked:
                    log.warning("track stopping, gate closed: %s", blocked)
                    self.on_state("control", "degraded", f"track stopped: {blocked}")
                    break

                pos = self.predictor.position(norad)
                if pos is None:
                    break
                if pos.el < TRACK_MIN_EL_DEG:
                    # Below the horizon: hold position rather than chase a
                    # target that is not there. The pass will bring it back.
                    await asyncio.sleep(TRACK_STEP_S)
                    continue

                sample = self.rotator.last
                if sample is not None:
                    az_err = abs(((sample.az_rose - pos.az + 540.0) % 360.0) - 180.0)
                    el_err = abs(sample.el - pos.el)
                    if max(az_err, el_err) < self.s.track_deadband_deg:
                        await asyncio.sleep(TRACK_STEP_S)
                        continue

                target_az = self._unwrap(pos.az, sample.az_raw if sample else pos.az)
                try:
                    await self._write(
                        lambda: self.rotator.client.set_position(target_az, pos.el),
                        f"track {norad} az={target_az:.1f} el={pos.el:.1f}",
                    )
                except RotctldError:
                    # A single refused write during a pass is not worth
                    # abandoning the track; the poll loop reports the link.
                    pass

                await asyncio.sleep(TRACK_STEP_S)
        except asyncio.CancelledError:
            raise
        finally:
            if self._mode == "track":
                self._mode = "idle"
                self._target_norad = None
                self.publish()

    def _unwrap(self, target_az: float, current_az: float) -> float:
        """Choose the representation of the target nearest the current reading.

        A SPID's range is -180..540, so north can be reached as 0 or as 360.
        Commanding 0 when the rotator sits at 359 sends it the long way round —
        a full rotation during a pass, and a cable wrap at the end of it.
        """
        best = target_az
        while best - current_az > 180.0:
            best -= 360.0
        while best - current_az < -180.0:
            best += 360.0
        caps = self.rotator.client.caps
        if caps is not None and not (caps.min_az <= best <= caps.max_az):
            return target_az
        return best
