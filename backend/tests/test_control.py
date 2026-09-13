"""Rotator control interlock.

These are the tests that matter most in the project. Every other failure shows
the operator something wrong; a failure here moves a three-metre antenna while
something else is driving it, or during an observation the station was supposed
to record.

So the bias throughout is that *refusing* is the safe answer, and each test
below states the unsafe thing it exists to prevent.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.schemas import RotatorSample
from app.services.control import ControlRefused, ControlService
from app.services.rotctld_client import Caps, RotctldError


def settings(**overrides) -> Settings:
    base = dict(
        mock=True,
        rotator_control_enabled=True,
        control_lease_s=900,
        gate_guard_s=300,
        gate_max_stale_s=120,
        track_deadband_deg=2.0,
        park_az=0.0,
        park_el=0.0,
        default_norad=67683,
    )
    base.update(overrides)
    return Settings(**base)


class FakeClient:
    def __init__(self, can_park: bool = False) -> None:
        self.caps = Caps(
            model=901, name="SPID Rot2Prog",
            min_az=-180.0, max_az=540.0, min_el=-20.0, max_el=210.0,
            is_rotator=True, can_set_position=True, can_stop=True,
            can_park=can_park,
        )
        self.commands: list[tuple] = []
        self.fail_with: RotctldError | None = None

    async def set_position(self, az: float, el: float) -> None:
        if self.fail_with:
            raise self.fail_with
        self.commands.append(("set_pos", round(az, 2), round(el, 2)))

    async def stop(self) -> None:
        self.commands.append(("stop",))


class FakeRotator:
    def __init__(self, link: str = "up", verified: bool = True) -> None:
        self.client = FakeClient()
        self.verified = verified
        self.last = RotatorSample(
            az_raw=10.0, el=5.0, az_rose=10.0, source="mock", link=link,
        )


class FakeSatnogs:
    """Stands in for the poller. Ages are what the gates actually reason about."""

    def __init__(self, is_connected=False, station_age=1.0, jobs_age=1.0,
                 seconds_to_job=None) -> None:
        self.is_connected = is_connected
        self.station_age_s = station_age
        self.jobs_age_s = jobs_age
        self._seconds_to_job = seconds_to_job

    def seconds_to_next_job(self, now=None):
        return self._seconds_to_job


class FakePredictor:
    def __init__(self, el: float = 45.0, az: float = 180.0) -> None:
        self.el, self.az = el, az

    def satellite(self, norad):
        return object()

    def position(self, norad, when=None):
        class P:
            pass
        p = P()
        p.az, p.el = self.az, self.el
        return p


def build(sat=None, rot=None, pred=None, **overrides) -> ControlService:
    return ControlService(
        settings(**overrides),
        rot or FakeRotator(),
        sat or FakeSatnogs(),
        pred or FakePredictor(),
    )


# --------------------------------------------------------------------------
# the four gates
# --------------------------------------------------------------------------

def test_all_gates_open_allows_control():
    service = build()
    service.arm()
    assert service.blocked_by() == []


def test_kill_switch_off_blocks_everything():
    service = build(rotator_control_enabled=False)
    assert "kill_switch" in service.blocked_by()


def test_arm_is_refused_while_the_kill_switch_is_off():
    """Arming a disabled system would show an armed light on a dead control."""
    service = build(rotator_control_enabled=False)
    with pytest.raises(ControlRefused) as exc:
        service.arm()
    assert "kill_switch" in exc.value.blocked_by


def test_satnogs_connected_blocks_control():
    """The unsafe case: satnogs-client is live and may start a track at any
    moment. Two writers on one 600-baud link corrupt each other's frames."""
    service = build(sat=FakeSatnogs(is_connected=True))
    service.arm()
    assert "satnogs_idle" in service.blocked_by()


def test_an_imminent_job_blocks_control():
    service = build(sat=FakeSatnogs(seconds_to_job=120.0), gate_guard_s=300)
    service.arm()
    assert "no_imminent_pass" in service.blocked_by()


def test_a_distant_job_does_not_block_control():
    service = build(sat=FakeSatnogs(seconds_to_job=3600.0), gate_guard_s=300)
    service.arm()
    assert "no_imminent_pass" not in service.blocked_by()


def test_a_job_in_progress_blocks_control():
    """seconds_to_next_job returns 0.0 for a job that is running. Zero must not
    read as 'comfortably in the past' and open the gate."""
    service = build(sat=FakeSatnogs(seconds_to_job=0.0), gate_guard_s=300)
    service.arm()
    assert "no_imminent_pass" in service.blocked_by()


# --------------------------------------------------------------------------
# failing closed
# --------------------------------------------------------------------------

def test_unknown_satnogs_state_blocks_control():
    """No answer yet is not an all-clear."""
    service = build(sat=FakeSatnogs(is_connected=None, station_age=None,
                                    jobs_age=None))
    service.arm()
    blocked = service.blocked_by()
    assert "satnogs_idle" in blocked and "no_imminent_pass" in blocked


def test_stale_satnogs_state_blocks_control():
    """A four-minute-old all-clear is exactly the window in which
    satnogs-client would have picked up a job."""
    service = build(
        sat=FakeSatnogs(is_connected=False, station_age=240.0, jobs_age=240.0),
        gate_max_stale_s=120,
    )
    service.arm()
    blocked = service.blocked_by()
    assert "satnogs_idle" in blocked and "no_imminent_pass" in blocked


def test_stale_jobs_block_even_when_the_station_is_fresh():
    service = build(
        sat=FakeSatnogs(is_connected=False, station_age=1.0, jobs_age=600.0),
        gate_max_stale_s=120,
    )
    service.arm()
    assert service.blocked_by() == ["no_imminent_pass"]


# --------------------------------------------------------------------------
# the lease
# --------------------------------------------------------------------------

def test_control_starts_unarmed():
    service = build()
    assert not service.armed
    assert "armed" in service.blocked_by()


def test_the_lease_expires():
    service = build(control_lease_s=1)
    service.arm()
    assert service.armed
    service._lease_expires = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert not service.armed
    assert "armed" in service.blocked_by()


def test_release_disarms():
    service = build()
    service.arm()
    service.release()
    assert not service.armed


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_goto_moves_the_rotator_when_clear():
    service = build()
    service.arm()
    await service.goto(123.0, 45.0)
    assert service.rotator.client.commands == [("set_pos", 123.0, 45.0)]


@pytest.mark.asyncio
async def test_goto_is_refused_when_a_gate_is_shut():
    service = build(sat=FakeSatnogs(is_connected=True))
    service.arm()
    with pytest.raises(ControlRefused):
        await service.goto(123.0, 45.0)
    assert service.rotator.client.commands == [], "refused, so nothing may be sent"


@pytest.mark.asyncio
async def test_goto_is_refused_while_unarmed():
    service = build()
    with pytest.raises(ControlRefused) as exc:
        await service.goto(10.0, 10.0)
    assert "armed" in exc.value.blocked_by
    assert service.rotator.client.commands == []


@pytest.mark.asyncio
async def test_commands_are_refused_while_the_link_is_down():
    """Queueing a move at a rotator we cannot read is how an antenna ends up
    somewhere nobody expects once the link returns."""
    service = build(rot=FakeRotator(link="down"))
    service.arm()
    with pytest.raises(ControlRefused):
        await service.goto(10.0, 10.0)
    assert service.rotator.client.commands == []


@pytest.mark.asyncio
async def test_commands_are_refused_before_the_rotator_is_identified():
    service = build(rot=FakeRotator(verified=False))
    service.arm()
    with pytest.raises(ControlRefused):
        await service.goto(10.0, 10.0)
    assert service.rotator.client.commands == []


@pytest.mark.asyncio
async def test_park_uses_set_position_because_the_hardware_cannot_park():
    """Station 5024's SPID answers `Can Park: N`. Sending a park command would
    come back RPRT -11 and the antenna would simply stay where it was."""
    service = build(park_az=0.0, park_el=0.0)
    service.arm()
    await service.park()
    assert service.rotator.client.commands == [("set_pos", 0.0, 0.0)]


@pytest.mark.asyncio
async def test_stop_is_not_gated():
    """An expired lease is not a reason to keep driving an antenna."""
    service = build(sat=FakeSatnogs(is_connected=True))
    await service.stop()
    assert ("stop",) in service.rotator.client.commands


@pytest.mark.asyncio
async def test_stop_is_safe_when_the_rotator_was_never_identified():
    service = build(rot=FakeRotator(verified=False))
    await service.stop()
    assert service.rotator.client.commands == []


@pytest.mark.asyncio
async def test_a_failed_write_is_reported_not_swallowed():
    service = build()
    service.arm()
    service.rotator.client.fail_with = RotctldError(-5, "set_pos")
    with pytest.raises(RotctldError):
        await service.goto(10.0, 10.0)


# --------------------------------------------------------------------------
# tracking
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_track_commands_the_rotator_towards_the_satellite():
    service = build(pred=FakePredictor(az=180.0, el=45.0))
    service.arm()
    await service.track(67683)
    await asyncio.sleep(0.15)
    service._stop_track()
    assert service.rotator.client.commands, "track issued no command at all"
    kind, az, el = service.rotator.client.commands[0]
    assert kind == "set_pos"
    assert az == pytest.approx(180.0, abs=1.0)
    assert el == pytest.approx(45.0, abs=1.0)


@pytest.mark.asyncio
async def test_track_respects_the_deadband():
    """Inside the deadband a 600-baud SPID would spend the pass acknowledging
    writes instead of moving."""
    rot = FakeRotator()
    rot.last = RotatorSample(az_raw=180.0, el=45.0, az_rose=180.0,
                             source="mock", link="up")
    service = build(rot=rot, pred=FakePredictor(az=180.5, el=45.2),
                    track_deadband_deg=2.0)
    service.arm()
    await service.track(67683)
    await asyncio.sleep(0.15)
    service._stop_track()
    assert service.rotator.client.commands == []


@pytest.mark.asyncio
async def test_track_stops_when_a_gate_closes_mid_pass():
    """The gates are re-read every iteration; a track must abandon itself."""
    sat = FakeSatnogs(is_connected=False)
    service = build(sat=sat, pred=FakePredictor(az=180.0, el=45.0))
    service.arm()
    await service.track(67683)
    await asyncio.sleep(0.05)

    sat.is_connected = True          # satnogs-client comes back up mid-pass
    await asyncio.sleep(1.2)
    assert service._mode == "idle", "track kept driving against a closed gate"


@pytest.mark.asyncio
async def test_track_holds_position_below_the_horizon():
    service = build(pred=FakePredictor(az=180.0, el=-5.0))
    service.arm()
    await service.track(67683)
    await asyncio.sleep(0.15)
    service._stop_track()
    assert service.rotator.client.commands == []


@pytest.mark.asyncio
async def test_releasing_control_stops_a_track():
    service = build(pred=FakePredictor(az=180.0, el=45.0))
    service.arm()
    await service.track(67683)
    service.release()
    await asyncio.sleep(0.05)
    assert service._track_task is None


# --------------------------------------------------------------------------
# azimuth unwrapping
# --------------------------------------------------------------------------

def test_unwrap_takes_the_short_way_round():
    """Commanding 0 while the rotator reads 359 sends it a full turn the wrong
    way, and leaves the cable wound at the end of the pass."""
    service = build()
    assert service._unwrap(1.0, 359.0) == pytest.approx(361.0)


def test_unwrap_stays_inside_the_rotator_limits():
    service = build()
    # 540 is the SPID's maximum; an unwrap past it must fall back rather than
    # command a position the controller will reject.
    assert -180.0 <= service._unwrap(200.0, 530.0) <= 540.0
