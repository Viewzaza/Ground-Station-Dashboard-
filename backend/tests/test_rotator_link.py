"""A rotator link that is down has to *read* as down, to everything.

RotatorService._down() used to publish a link-down copy of the last sample and
then drop it, so `last` went on saying "up" through the outage. Everything that
reads `last` believed it: the interlock's "rotator link is down" refusal, the
track loop's under-lock guard, the planner's idea of where the antenna is.

The only thing left standing between a command and a dead rotator was then
`verified`, and a reconnect sets that again as soon as dump_caps answers —
which rotctld does from its compiled-in capabilities with the SPID controller
powered off, while every get_pos takes 2.8-4.6 s to fail. In that window a
command passed both checks. These tests pin the window shut.

Nothing here can move hardware: the rotctld client is a scripted fake.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.control import ControlRefused, ControlService
from app.services.planner_service import PlannerService
from app.services.rotator_service import RotatorService
from app.services.rotctld_client import Caps, RotctldError

# Helpers only, imported by name, so pytest does not collect test_control's
# tests a second time here.
from tests.test_control import FakePredictor, FakeSatnogs, settings


class ScriptedClient:
    """Stands where rotctld would. get_position follows a script — a tuple is
    a reading, an exception is raised, an Event is waited on first (a serial
    retry in progress) — and every write is recorded."""

    def __init__(self, *script) -> None:
        self.caps = Caps(model=901, name="SPID Rot2Prog",
                         min_az=-180.0, max_az=540.0, min_el=-20.0, max_el=210.0,
                         is_rotator=True, can_set_position=True, can_stop=True)
        self.script = list(script)
        self.reads = 0
        self.commands: list[tuple] = []

    def limits(self):
        return (-90.0, 450.0, 0.0, 100.0)

    async def verify_is_rotator(self) -> Caps:
        # What rotctld does with the controller off: answers from its
        # compiled-in capabilities, model and ranges and all.
        return self.caps

    async def get_position(self):
        self.reads += 1
        step = self.script.pop(0) if self.script else (10.0, 5.0)
        if isinstance(step, asyncio.Event):
            await step.wait()
            raise RotctldError(-5, "RPRT -5")
        if isinstance(step, BaseException):
            raise step
        return step[0], step[1], 300.0

    async def set_position(self, az, el, guard=None):
        if guard is not None:
            guard()
        self.commands.append(("set_pos", round(az, 1), round(el, 1)))

    async def stop(self):
        self.commands.append(("stop",))

    async def close(self):
        return None


def rotator(*script, **overrides) -> RotatorService:
    service = RotatorService(settings(**overrides), FakePredictor())
    service.client = ScriptedClient(*script)
    return service


def control_over(rot: RotatorService) -> ControlService:
    control = ControlService(rot.s, rot, FakeSatnogs(), FakePredictor())
    control.arm()
    return control


# --------------------------------------------------------------------------
# the sample
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_down_marks_the_last_sample_down():
    """Was: published as down, kept as up."""
    rot = rotator((10.0, 5.0), RotctldError(-5, "RPRT -5"))
    await rot._poll_once()
    assert rot.last.link == "up"

    rot._down("RPRT -5")

    assert rot.last.link == "down"
    assert rot.last.stale_s >= 0.0
    # The position is kept, so a reader can still say where the antenna was.
    assert rot.last.az_raw == 10.0


def test_down_before_any_reading_leaves_nothing_to_mark():
    rot = rotator()
    rot._down("connection refused")
    assert rot.last is None


@pytest.mark.asyncio
async def test_stale_s_counts_from_the_last_good_reading():
    rot = rotator((10.0, 5.0))
    await rot._poll_once()
    rot._last_ok_mono -= 7.0           # the good reading was seven seconds ago
    rot._down("RPRT -5")
    assert 6.9 <= rot.last.stale_s <= 8.0


# --------------------------------------------------------------------------
# the interlock
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commands_are_refused_once_the_link_goes_down():
    """The unsafe case: a goto written to a rotator the poll loop has just
    failed to read. Was: accepted, because `last` still said up."""
    rot = rotator((10.0, 5.0))
    rot.verified = True
    await rot._poll_once()
    control = control_over(rot)

    rot._down("RPRT -5")

    with pytest.raises(ControlRefused, match="rotator link is down"):
        await control.goto(10.0, 10.0)
    assert rot.client.commands == []


@pytest.mark.asyncio
async def test_a_reconnect_that_only_reads_capabilities_does_not_reopen_it():
    """The window. run() drops `verified` on a failure and sets it again the
    moment dump_caps answers — which proves nothing about the rotator. Until a
    position has actually been read, a command is still refused."""
    rot = rotator((10.0, 5.0))
    await rot._ensure_verified()
    await rot._poll_once()
    control = control_over(rot)

    rot._down("RPRT -5")
    rot.verified = False                    # what run() does next
    await rot._ensure_verified()            # dump_caps answers
    assert rot.verified

    with pytest.raises(ControlRefused, match="rotator link is down"):
        await control.goto(10.0, 10.0)
    assert rot.client.commands == []


@pytest.mark.asyncio
async def test_the_next_good_reading_reopens_it():
    rot = rotator((10.0, 5.0), (12.0, 6.0))
    rot.verified = True
    await rot._poll_once()
    control = control_over(rot)
    rot._down("RPRT -5")

    await rot._poll_once()

    assert rot.last.link == "up"
    assert rot.last.stale_s == 0.0
    await control.goto(10.0, 10.0)
    assert rot.client.commands == [("set_pos", 10.0, 10.0)]


@pytest.mark.asyncio
async def test_the_window_is_shut_in_the_real_poll_loop():
    """The same window, driven by run() itself: one good read, one failure,
    then a reconnect whose get_pos hangs in serial retries. While it hangs,
    `verified` is True again and a goto must still be refused."""
    retrying = asyncio.Event()
    rot = rotator((10.0, 5.0), RotctldError(-5, "RPRT -5"), retrying,
                  rotator_backoff_min_s=0.01, rotator_backoff_max_s=0.01,
                  rotator_poll_hz=100.0)
    control = control_over(rot)
    loop = asyncio.create_task(rot.run())
    try:
        for _ in range(200):
            if rot.client.reads >= 3 and rot.verified:
                break
            await asyncio.sleep(0.005)
        assert rot.client.reads == 3 and rot.verified, "never reached the retry"

        with pytest.raises(ControlRefused, match="rotator link is down"):
            await control.goto(10.0, 10.0)
        assert rot.client.commands == []
    finally:
        loop.cancel()
        retrying.set()
        await asyncio.gather(loop, return_exceptions=True)


@pytest.mark.asyncio
async def test_the_planner_does_not_plan_from_a_position_it_cannot_read():
    """PlannerService._origin_az already returns None — its 'unknown origin'
    path — for a link that is down. It only ever saw one once `last` said so."""
    rot = rotator((10.0, 5.0))
    await rot._poll_once()
    origin = PlannerService._origin_az
    holder = type("P", (), {"rotator": rot})()
    assert origin(holder) == 10.0
    rot._down("RPRT -5")
    assert origin(holder) is None



@pytest.mark.asyncio
async def test_a_failing_pointing_readout_does_not_take_the_link_down():
    """The pointing frame is a readout. When it raised, the exception left
    _poll_once as if the read had failed — the link marked down and the
    interlock refusing every command, over a display bug."""
    service = rotator((10.0, 5.0))

    class Broken:
        def position(self, norad, when=None):
            raise RuntimeError("elements unreadable")

        def satellite(self, norad):
            return None

    service.predictor = Broken()
    await service._poll_once()
    assert service.last is not None and service.last.link == "up"
