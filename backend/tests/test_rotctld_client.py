"""Rotctld protocol tests.

The parser reads from a stream, not from discrete messages, so the tests that
matter are the ones that split a reply at awkward places. On a real 600-baud
serial-backed link, a reply genuinely does arrive in pieces.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.rotctld_client import (
    RPRT_ETIMEOUT,
    NotARotator,
    RotctldClient,
    RotctldError,
)

# Transcribed from what station 5024's rotctld actually returns. Hamlib prints
# "Min Azimuth", not "Minimum Azimuth" — a fixture using the long spelling
# passed while the parser silently fell back to its defaults.
ROT_CAPS = (
    "Caps dump for model:\t901\n"
    "Model name:\t\tRot2Prog\n"
    "Mfg name:\t\tSPID\n"
    "Rot type:\t\tAz-El\n"
    "Serial speed:\t\t600..600 bauds, 8N1, ctrl=NONE\n"
    "Post write delay:\t300ms\n"
    "Min Azimuth:\t\t-180.00\n"
    "Max Azimuth:\t\t540.00\n"
    "Min Elevation:\t\t-20.00\n"
    "Max Elevation:\t\t210.00\n"
    "Can set Position:\tY\n"
    "Can get Position:\tY\n"
    "Can Stop:\t\tY\n"
    "Can Park:\t\tN\n"
    "Can Move:\t\tN\n"
    "RPRT 0\n"
)

# A different rotator, to prove the limits are read rather than assumed.
OTHER_ROT_CAPS = (
    "Caps dump for model:\t903\n"
    "Model name:\t\tMD-01\n"
    "Rot type:\t\tAz-El\n"
    "Min Azimuth:\t\t0.00\n"
    "Max Azimuth:\t\t450.00\n"
    "Min Elevation:\t\t0.00\n"
    "Max Elevation:\t\t180.00\n"
    "Can set Position:\tY\n"
    "Can Stop:\t\tY\n"
    "Can Park:\t\tY\n"
    "RPRT 0\n"
)

RIG_CAPS = (
    "Caps dump for model: 2\n"
    "Model name:\tNET rigctl\n"
    "Rig type:\tOther\n"
    "RX freq ranges:\n"
    "\t30 kHz - 30 MHz\n"
    "Tuning steps:\n"
    "\t1 Hz\n"
    "RPRT 0\n"
)

GET_POS = "get_pos:\nAzimuth: 412.500000\nElevation: 41.700000\nRPRT 0\n"


class ScriptedServer:
    """A TCP server that replies with canned text, optionally byte by byte."""

    def __init__(self, replies: dict[str, str], chunk: int | None = None) -> None:
        self.replies = replies
        self.chunk = chunk
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.connections = 0
        self.commands: list[str] = []
        self._writers: list[asyncio.StreamWriter] = []

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        # Close our side of every accepted connection first. A test that fails
        # inside the `async with` never reaches its own client.close(), and
        # wait_closed() on 3.12 then blocks forever — turning one failing
        # assertion into a suite that hangs instead of reporting.
        for writer in self._writers:
            writer.close()
        self._writers.clear()
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        self.connections += 1
        self._writers.append(writer)
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                command = raw.decode().strip().lstrip("+\\")
                self.commands.append(command)
                # Replies are keyed by verb, arguments recorded separately:
                # `set_pos 450.00 180.00` is answered by a "set_pos" entry,
                # while self.commands keeps exactly what went on the wire.
                text = self.replies.get(command.split()[0] if command else "",
                                        self.replies.get(command, "RPRT -1\n"))
                data = text.encode()
                if self.chunk:
                    for i in range(0, len(data), self.chunk):
                        writer.write(data[i : i + self.chunk])
                        await writer.drain()
                        await asyncio.sleep(0.001)
                else:
                    writer.write(data)
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            # Python 3.12 changed Server.wait_closed() to block until every
            # accepted connection is closed, so a handler that returns without
            # closing its own side hangs __aexit__ forever rather than ending
            # the test. On 3.11 this was unnecessary, which is why it is easy
            # to leave out.
            writer.close()


# --------------------------------------------------------------------------
# position parsing
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reads_a_position():
    async with ScriptedServer({"get_pos": GET_POS}) as srv:
        client = RotctldClient("127.0.0.1", srv.port)
        az, el, latency = await client.get_position()
        await client.close()

    assert az == pytest.approx(412.5)
    assert el == pytest.approx(41.7)
    assert latency >= 0


@pytest.mark.asyncio
async def test_azimuth_past_360_is_not_clamped():
    """A SPID reads -180..540. 412 degrees means the rotator is wound past
    north, which the operator needs to see — normalising it destroys that."""
    async with ScriptedServer({"get_pos": GET_POS}) as srv:
        client = RotctldClient("127.0.0.1", srv.port)
        az, _, _ = await client.get_position()
        await client.close()
    assert az > 360.0


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 13])
@pytest.mark.asyncio
async def test_reply_split_across_packets_is_reassembled(chunk):
    """The reply arrives in pieces on a real link. Every split must parse."""
    async with ScriptedServer({"get_pos": GET_POS}, chunk=chunk) as srv:
        client = RotctldClient("127.0.0.1", srv.port)
        az, el, _ = await client.get_position()
        await client.close()
    assert (az, el) == (pytest.approx(412.5), pytest.approx(41.7))


@pytest.mark.asyncio
async def test_consecutive_reads_do_not_desynchronise():
    """Each reply must be consumed exactly up to its RPRT terminator, or the
    next read picks up the previous reply's leftovers."""
    async with ScriptedServer({"get_pos": GET_POS}) as srv:
        client = RotctldClient("127.0.0.1", srv.port)
        for _ in range(5):
            az, el, _ = await client.get_position()
            assert (az, el) == (pytest.approx(412.5), pytest.approx(41.7))
        await client.close()
        assert srv.connections == 1, "each poll must reuse the one connection"


# --------------------------------------------------------------------------
# error handling
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rprt_timeout_is_raised_with_its_code():
    """RPRT -5 is the controller not answering on the serial line."""
    async with ScriptedServer({"get_pos": "RPRT -5\n"}) as srv:
        client = RotctldClient("127.0.0.1", srv.port)
        with pytest.raises(RotctldError) as exc:
            await client.get_position()
        await client.close()
    assert exc.value.code == RPRT_ETIMEOUT


@pytest.mark.asyncio
async def test_unparsable_position_does_not_return_garbage():
    async with ScriptedServer({"get_pos": "get_pos:\nAzimuth: north\nRPRT 0\n"}) as srv:
        client = RotctldClient("127.0.0.1", srv.port)
        with pytest.raises(RotctldError):
            await client.get_position()
        await client.close()


# --------------------------------------------------------------------------
# identifying the peer
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rotator_is_identified_with_its_limits():
    async with ScriptedServer({"dump_caps": ROT_CAPS}) as srv:
        client = RotctldClient("127.0.0.1", srv.port)
        caps = await client.verify_is_rotator()
        await client.close()

    assert caps.is_rotator
    assert caps.model == 901
    assert "Rot2Prog" in caps.name
    assert (caps.min_az, caps.max_az) == (-180.0, 540.0)


@pytest.mark.asyncio
async def test_limits_are_read_from_the_peer_not_assumed():
    """The SPID 901's range coincides with the code's defaults, so a parser
    that silently fell back would look correct against that one rotator and
    clamp every other one wrongly."""
    async with ScriptedServer({"dump_caps": OTHER_ROT_CAPS}) as srv:
        client = RotctldClient("127.0.0.1", srv.port)
        caps = await client.verify_is_rotator()
        await client.close()

    assert caps.model == 903
    assert (caps.min_az, caps.max_az) == (0.0, 450.0)
    assert (caps.min_el, caps.max_el) == (0.0, 180.0)


@pytest.mark.asyncio
async def test_park_capability_is_read():
    """Station 5024's 901 answers `Can Park: N`, so park has to be a set_pos.
    Assuming the command exists means the antenna quietly does not move."""
    async with ScriptedServer({"dump_caps": ROT_CAPS}) as srv:
        client = RotctldClient("127.0.0.1", srv.port)
        caps = await client.verify_is_rotator()
        await client.close()

    assert caps.can_park is False
    assert caps.can_set_position is True
    assert caps.can_stop is True


@pytest.mark.asyncio
async def test_a_move_is_clamped_to_the_peers_limits():
    async with ScriptedServer({"dump_caps": OTHER_ROT_CAPS, "set_pos": "RPRT 0\n"}) as srv:
        client = RotctldClient("127.0.0.1", srv.port)
        await client.verify_is_rotator()
        await client.set_position(600.0, 200.0)      # past both maxima
        await client.close()

    assert srv.commands[-1] == "set_pos 450.00 180.00"


@pytest.mark.asyncio
async def test_a_move_before_dump_caps_is_refused():
    """Without capabilities there are no limits to clamp against, so the safe
    answer is to refuse rather than to guess a range."""
    async with ScriptedServer({"set_pos": "RPRT 0\n"}) as srv:
        client = RotctldClient("127.0.0.1", srv.port)
        with pytest.raises(RotctldError):
            await client.set_position(10.0, 10.0)
        await client.close()

    assert srv.commands == [], "nothing may be sent before the peer is identified"


@pytest.mark.asyncio
async def test_a_radio_on_the_port_is_refused():
    """rigctld listens on 4532 and rotctld on 4533, and station notes get this
    wrong. Polling a radio must fail loudly, not report nonsense as a heading."""
    async with ScriptedServer({"dump_caps": RIG_CAPS}) as srv:
        client = RotctldClient("127.0.0.1", srv.port)
        with pytest.raises(NotARotator, match="radio"):
            await client.verify_is_rotator()
        await client.close()


@pytest.mark.asyncio
async def test_dump_caps_is_the_only_command_used_to_probe():
    """dump_caps is answered from compiled capabilities and never reaches the
    serial line, so probing is safe during a pass. Nothing else may be sent."""
    async with ScriptedServer({"dump_caps": ROT_CAPS}) as srv:
        client = RotctldClient("127.0.0.1", srv.port)
        await client.verify_is_rotator()
        await client.close()
    assert srv.commands == ["dump_caps"]


@pytest.mark.asyncio
async def test_client_never_sends_a_command_that_moves_the_rotator():
    """The read path must be incapable of moving the antenna."""
    async with ScriptedServer({"dump_caps": ROT_CAPS, "get_pos": GET_POS}) as srv:
        client = RotctldClient("127.0.0.1", srv.port)
        await client.verify_is_rotator()
        await client.get_position()
        await client.close()

    forbidden = {"P", "set_pos", "S", "stop", "K", "park", "M", "move", "R", "reset", "w"}
    assert not forbidden.intersection(srv.commands)
