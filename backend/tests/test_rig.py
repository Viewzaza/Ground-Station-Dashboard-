"""The rig read path.

satnogs-client owns the station's rig and drives it through a pass. This
dashboard only watches it. The first test is the one that matters: if this
module ever grows a way to set a frequency, two things will be commanding one
receiver during an observation.
"""

from __future__ import annotations

import ast
import asyncio
import inspect

import pytest

from app.services import rig_client as rig_module
from app.services.rig_client import RigClient, RigError

FREQ = "get_freq:\nFrequency: 400628765\nRPRT 0\n"
MODE = "get_mode:\nMode: FM\nPassband: 15000\nRPRT 0\n"


class ScriptedRig:
    """A rigctld that answers canned text and records what it was asked."""

    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies
        self.server = None
        self.port = 0
        self.commands: list[str] = []
        self._writers: list[asyncio.StreamWriter] = []

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        # Close our side first: a test that fails inside the block never reaches
        # its own close(), and on Python 3.12 wait_closed() then blocks forever.
        for w in self._writers:
            w.close()
        self._writers.clear()
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        self._writers.append(writer)
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                command = raw.decode().strip().lstrip("+\\")
                self.commands.append(command)
                verb = command.split()[0] if command else ""
                writer.write(self.replies.get(verb, "RPRT -1\n").encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()


# --------------------------------------------------------------------------
# the property that matters
# --------------------------------------------------------------------------

def test_the_client_has_no_way_to_set_a_frequency():
    """satnogs-client is mid-pass on this rig. A dashboard that could retune it
    would fight the thing actually running the observation, and the symptom
    would be a failed pass with no obvious cause.

    The check reads the AST rather than the raw text, because the module's own
    docstring explains at length why there is no set_freq — and a naive
    substring search fails on the very comment documenting the guarantee."""
    forbidden = ("set_freq", "set_mode", "set_vfo", "set_ptt", "set_powerstat")
    for name in forbidden:
        assert not hasattr(RigClient, name), f"RigClient grew a {name} method"

    tree = ast.parse(inspect.getsource(rig_module))

    # Every docstring in the module, so they can be excluded from the scan.
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert not node.name.startswith("set_"), \
                f"rig_client defines {node.name}"
        # A command reaches the wire as a string literal, so a set_* command
        # cannot be sent without one appearing here.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in docstrings:
                continue
            assert not any(f in node.value for f in forbidden), \
                f"a write command appears in code: {node.value!r}"


@pytest.mark.asyncio
async def test_reading_state_sends_only_reads():
    async with ScriptedRig({"get_freq": FREQ, "get_mode": MODE}) as srv:
        client = RigClient("127.0.0.1", srv.port)
        await client.get_state()
        await client.close()

    assert srv.commands == ["get_freq", "get_mode"]
    assert not any(c.startswith("set_") for c in srv.commands)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_frequency_and_mode_are_read():
    async with ScriptedRig({"get_freq": FREQ, "get_mode": MODE}) as srv:
        client = RigClient("127.0.0.1", srv.port)
        state = await client.get_state()
        await client.close()

    assert state["freq_hz"] == pytest.approx(400628765)
    assert state["mode"] == "FM"
    assert state["passband_hz"] == pytest.approx(15000)
    assert state["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_a_rig_that_will_not_report_mode_still_gives_the_frequency():
    """Mode is decoration; the frequency is the number being cross-checked."""
    async with ScriptedRig({"get_freq": FREQ}) as srv:   # get_mode -> RPRT -1
        client = RigClient("127.0.0.1", srv.port)
        state = await client.get_state()
        await client.close()

    assert state["freq_hz"] == pytest.approx(400628765)
    assert state["mode"] == ""


@pytest.mark.asyncio
async def test_an_error_reply_is_raised_with_its_code():
    async with ScriptedRig({"get_freq": "RPRT -5\n"}) as srv:
        client = RigClient("127.0.0.1", srv.port)
        with pytest.raises(RigError) as exc:
            await client.get_state()
        await client.close()
    assert exc.value.code == -5


@pytest.mark.asyncio
async def test_an_unparsable_frequency_does_not_return_garbage():
    async with ScriptedRig({"get_freq": "get_freq:\nFrequency: wide\nRPRT 0\n"}) as srv:
        client = RigClient("127.0.0.1", srv.port)
        with pytest.raises(RigError):
            await client.get_state()
        await client.close()


@pytest.mark.asyncio
async def test_consecutive_reads_reuse_one_connection():
    """rigctld shares one rig handle across connections, same as rotctld."""
    async with ScriptedRig({"get_freq": FREQ, "get_mode": MODE}) as srv:
        client = RigClient("127.0.0.1", srv.port)
        for _ in range(3):
            await client.get_state()
        await client.close()

    assert srv.commands.count("get_freq") == 3
