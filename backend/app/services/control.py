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
import math
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

        # The command journal. Every command this service *accepts* bumps
        # command_seq and records who issued it; nothing else does. That is
        # what lets autopilot tell, without guessing, whether anyone else has
        # touched the antenna since it last did. Inferring it from the mode
        # string cannot work: a track ended by a closing gate and one ended by
        # an operator's STOP both leave mode "idle", and an operator parking
        # after autopilot pre-positioned leaves it "manual" either way.
        self.command_seq: int = 0
        self.last_origin: str = "none"
        self._origins: dict[int, str] = {}
        # Incremented per track started, so a track can be named rather than
        # inferred from "mode == track".
        self.track_id: int = 0
        self.track_end_reason: str = ""
        # The expiry time of the last lease whose lapse check_lease() acted on,
        # so each lapse is acted on once.
        self._lapse_handled: datetime | None = None

    def _journal(self, origin: str) -> int:
        """Record an accepted command. Always called synchronously at the
        moment of acceptance, before any await — so the entry belongs to the
        caller even if other commands land while this one's write is in
        flight, and a caller can know its own entry is `seq_before + 1`."""
        self.command_seq += 1
        self.last_origin = origin
        self._origins[self.command_seq] = origin
        if len(self._origins) > 64:
            for old in sorted(self._origins)[:-64]:
                self._origins.pop(old, None)
        return self.command_seq

    def origin_of(self, seq: int) -> str | None:
        """Who issued journal entry `seq`, if it is still remembered."""
        return self._origins.get(seq)

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

    async def release(self, origin: str = "operator") -> ControlState:
        """Give up the lease, and stop anything moving on the strength of it.

        Release revokes consent exactly as expiry does, so it stops motion the
        same way: a lease that lapses mid-slew stops the antenna (check_lease),
        and one that is handed back mid-slew must not leave it running. Only if
        this service was driving — mode track or manual — because if it was
        idle, whatever is moving the rotator is not us, and a stop could fight
        it; and not while SatNOGS may have the antenna (satnogs_may_drive).

        Journaled synchronously, so anything running on the old lease notices
        even if a new arm follows before it next looks.
        """
        was_driving = self._mode in ("track", "manual")
        self._lease_expires = None
        self._stop_track()
        self._mode = "idle"
        self._journal(origin)
        log.info("control released")
        if was_driving and self.rotator.verified and not self.satnogs_may_drive():
            try:
                await self._write(self.rotator.client.stop, "stop (lease released)")
            except Exception:
                log.exception("could not stop the antenna on release")
        return self.publish()

    async def check_lease(self) -> None:
        """Act on a lease that has run out. Called once a second by the scheduler.

        The track loop stops its own track when consent lapses, but nothing
        did the same for an absolute move: a goto or park issued under a lease
        kept slewing after the lease ran out — at 1.5°/s, for minutes — though
        release() already promised that a lapsed lease stops the antenna.

        `mode == "manual"` only says the last command was an absolute move, not
        that the antenna is still moving, so the stop may land on a rotator
        that has already arrived. That is harmless, unless SatNOGS has the
        antenna, and then it is skipped (see satnogs_may_drive). The stop is
        journaled under its own origin, so autopilot, finding its command no
        longer the latest, does not send a second one.
        """
        expires = self._lease_expires
        if expires is None or self.armed or self._lapse_handled == expires:
            return
        self._lapse_handled = expires
        if self._mode != "manual":
            return          # track: its own loop sees the gate; idle: nothing to stop
        self._mode = "idle"
        if self.satnogs_may_drive() or not self.rotator.verified:
            self.publish()
            return
        self._journal("lease")
        try:
            await self._write(self.rotator.client.stop, "stop (lease expired)")
        except Exception:
            log.exception("could not stop the antenna when the lease expired")
        self.publish()

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

    def satnogs_may_drive(self) -> bool:
        """Whether SatNOGS could be commanding the antenna right now.

        True whenever either SatNOGS gate is shut: the client is connected, its
        status is unknown or stale, or a job is within the guard window. This
        decides every stop nobody pressed — a lease lapsing, a release,
        autopilot standing down. Each of those stops our own motion, and our
        last move is bounded and was consented to when it was made; a STOP
        written while satnogs-client is tracking halts its recording instead.
        So when SatNOGS may have the antenna, those stops are not sent. An
        operator's STOP is never subject to this: that is their call.
        """
        gates = self.gates()
        return not (gates["satnogs_idle"] and gates["no_imminent_pass"])

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
        payload["command_seq"] = self.command_seq
        payload["last_origin"] = self.last_origin
        payload["track_id"] = self.track_id
        payload["track_end_reason"] = self.track_end_reason
        payload["can_park"] = bool(
            self.rotator.client.caps and self.rotator.client.caps.can_park
        )
        hub.publish("control", payload)
        return state

    # --- commands ----------------------------------------------------------
    def _guard(self) -> None:
        """Re-check the interlock at the last possible moment.

        Passed into the client and run *after* it takes the rotctld lock, just
        before the command is written. A gate checked before queueing for that
        lock can close during the wait — behind a slow poll that is seconds —
        and this is the only check that cannot be stale when the bytes go out.
        """
        blocked = self.blocked_by()
        if blocked:
            raise ControlRefused(blocked, "gate closed while the command was queued")
        # The same "can we" checks _require_clear makes, made again here. The
        # track loop writes for minutes after its one entry check, and if the
        # poll loop marks the link down — or the rotator unidentified — while
        # it runs, its next set_pos would reconnect to rotctld on its own and
        # command a rotator nobody has verified since.
        if not self.rotator.verified:
            raise ControlRefused([], "rotator is not identified; refusing to command it")
        if self.rotator.last is None or self.rotator.last.link != "up":
            raise ControlRefused([], "rotator link is down")

    async def goto(self, az: float, el: float,
                   origin: str = "operator") -> ControlState:
        # NaN survives every comparison and min/max clamp unchanged, and would
        # reach rotctld as `set_pos nan nan`. Refuse it here, once, for every
        # caller — HTTP, WebSocket and autopilot alike.
        if not (math.isfinite(az) and math.isfinite(el)):
            raise ControlRefused([], f"position must be finite, got az={az!r} el={el!r}")
        self._require_clear()
        self._stop_track()
        return await self._absolute(az, el, f"goto az={az:.1f} el={el:.1f}", origin)

    async def _absolute(self, az: float, el: float, what: str,
                        origin: str) -> ControlState:
        """An accepted absolute move: journal and set the mode *now*, then write.

        Both used to happen after the awaited write. While the write waited on
        the rotctld lock, an operator's STOP or track could be journaled, and
        this move's entry then landed on top of it — so autopilot read the
        operator's command as its own, and the mode was overwritten to
        "manual" with the operator's track still running.

        If the write fails and nothing newer has been accepted since, nothing
        is commanded: the track (if any) was already cancelled, so the mode is
        idle — not "track" with no task, and not "manual" for a move never made.
        """
        self._mode = "manual"
        seq = self._journal(origin)
        try:
            await self._write(
                lambda: self.rotator.client.set_position(az, el, guard=self._guard),
                what,
            )
        except BaseException:
            if self.command_seq == seq:
                self._mode = "idle"
                self.publish()
            raise
        return self.publish()

    async def stop(self, origin: str = "operator") -> ControlState:
        """Halt motion.

        Stop is *not* gated. If the antenna is moving and an operator wants it
        to stop, an expired lease is not a reason to keep driving. It is also
        harmless: the worst case is stopping a rotator that was already still.
        """
        self._stop_track()
        self._mode = "idle"
        self._journal(origin)
        if self.rotator.verified:
            await self._write(self.rotator.client.stop, "stop")
        return self.publish()

    async def park(self, origin: str = "operator") -> ControlState:
        """Send the antenna to the park position.

        Station 5024's SPID reports `Can Park: N`, so there is no park command
        to send — park is an ordinary absolute move to the configured park
        coordinates. Where a backend does implement park natively we still use
        set_pos, so the resting position is the one in the configuration rather
        than whatever the controller's firmware happens to prefer.
        """
        self._require_clear()
        self._stop_track()
        return await self._absolute(
            self.s.park_az, self.s.park_el,
            f"park az={self.s.park_az:.1f} el={self.s.park_el:.1f}", origin,
        )

    async def track(self, norad: int | None = None,
                    origin: str = "operator") -> ControlState:
        self._require_clear()
        norad = norad or self.s.default_norad
        if self.predictor.satellite(norad) is None:
            raise ControlRefused([], f"no elements for {norad}")
        self._stop_track()
        self.track_id += 1
        self.track_end_reason = ""
        self._target_norad = norad
        self._mode = "track"
        self._track_task = asyncio.create_task(self._track_loop(norad, self.track_id))
        self._journal(origin)
        log.info("tracking %s (track %d, %s)", norad, self.track_id, origin)
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

    async def _track_loop(self, norad: int, track_id: int = 0) -> None:
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
                    self.track_end_reason = f"gate closed: {', '.join(blocked)}"
                    # Consent gone — lease lapsed, or control switched off —
                    # means the antenna should not keep finishing a move this
                    # loop commanded, so stop it. Not if a SatNOGS gate is
                    # shut too: there SatNOGS may be the one about to drive,
                    # and a stop from us could fight it.
                    if (("armed" in blocked or "kill_switch" in blocked)
                            and not self.satnogs_may_drive()):
                        try:
                            await self.rotator.client.stop()
                        except Exception:
                            log.exception("could not stop after consent lapsed")
                    break

                pos = self.predictor.position(norad)
                if pos is None:
                    self.track_end_reason = f"no position for {norad}"
                    break
                if not (math.isfinite(pos.az) and math.isfinite(pos.el)):
                    # Decayed or corrupt elements propagate to NaN, and NaN
                    # fails `el < 0` as surely as it passes every clamp: it
                    # would go to rotctld as `set_pos nan nan`. End the track.
                    self.track_end_reason = f"non-finite position for {norad}"
                    log.warning("track %s: non-finite position %r/%r — stopping",
                                norad, pos.az, pos.el)
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
                        lambda: self.rotator.client.set_position(
                            target_az, pos.el, guard=self._guard),
                        f"track {norad} az={target_az:.1f} el={pos.el:.1f}",
                    )
                except ControlRefused:
                    # The gate closed while this write was queued; the next
                    # iteration sees it and ends the track properly.
                    pass
                except (RotctldError, ConnectionError, OSError, asyncio.TimeoutError):
                    # A failed write during a pass is not worth abandoning the
                    # track for — the poll loop reports the link — and letting
                    # a socket error kill this task would end the track with no
                    # reason recorded.
                    pass

                await asyncio.sleep(TRACK_STEP_S)
        except asyncio.CancelledError:
            raise
        finally:
            # Only the *current* track may reset the mode. track() cancels the
            # old task without awaiting it, so the old task's finally runs after
            # the new track has already set mode = "track" — and used to reset
            # it to idle while the new task carried on driving the antenna: a
            # track nobody could see, still moving the rotator.
            if self._track_task is asyncio.current_task() and self._mode == "track":
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
        limits = getattr(self.rotator.client, "limits", None)
        if callable(limits):
            min_az, max_az = limits()[:2]
        else:
            caps = self.rotator.client.caps
            if caps is None:
                return best
            min_az, max_az = caps.min_az, caps.max_az
        # The limits in force, not dump_caps's compiled range: on station 5024
        # those are -90..450, and choosing a branch the clamp then moves
        # silently undoes the unwrap.
        if not (min_az <= best <= max_az):
            for candidate in (best + 360.0, best - 360.0):
                if min_az <= candidate <= max_az:
                    return candidate
            return target_az
        return best
