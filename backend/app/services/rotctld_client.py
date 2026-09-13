"""Hamlib rotctld client.

Three rules, all of them learned from how rotctld actually behaves:

**One socket, process-wide.** rotctld spawns a thread per connection and shares
a single rotator handle between them with no mutex. Two clients talking at once
can interleave writes mid-frame on a 600-baud serial link and desynchronise the
controller's response parser — which, during a pass, means the tracking client
gets a read error and may abandon the move. So N browser tabs must produce
exactly one connection, and every command here is serialised behind a lock.

**Use the extended protocol.** `+\\get_pos` always terminates its reply with
`RPRT <n>`, which gives an unambiguous frame boundary on a persistent stream.
Bare `p` returns two bare numbers on success and one `RPRT` line on failure, so
a reader has to guess how much to wait for.

**Probe before polling.** rigctld's default port is 4532 and rotctld's is 4533,
and a station's notes may well name the wrong one. `\\dump_caps` is answered
from the backend's compiled-in capability struct without touching the serial
line — safe to send mid-pass — and its reply says unambiguously whether the
peer is a rotator or a radio.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 5.0
COMMAND_TIMEOUT_S = 5.0

# Hamlib error codes, negated on the wire (rig.h). Only the ones worth naming.
RPRT_OK = 0
RPRT_EINVAL = -1        # unrecognised command
RPRT_ETIMEOUT = -5      # the controller is not answering on the serial line
RPRT_EIO = -6           # port gone


class RotctldError(RuntimeError):
    def __init__(self, code: int, detail: str = "") -> None:
        super().__init__(f"RPRT {code}{(': ' + detail) if detail else ''}")
        self.code = code


class NotARotator(RuntimeError):
    """The peer answered, but it is a radio (rigctld), not a rotator."""


@dataclass
class Caps:
    model: int | None
    name: str
    min_az: float
    max_az: float
    min_el: float
    max_el: float
    is_rotator: bool


class RotctldClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self.caps: Caps | None = None

    # --- connection --------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        if self.connected:
            return
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), CONNECT_TIMEOUT_S
        )
        log.info("rotctld connected: %s:%s", self.host, self.port)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        self._reader = self._writer = None

    # --- protocol ----------------------------------------------------------
    async def _command(self, command: str) -> tuple[list[str], int]:
        """Send one extended-protocol command; return its records and RPRT code.

        Reads until the terminating `RPRT <n>` line, so a reply split across
        packets — or arriving one byte at a time — is reassembled correctly.
        """
        async with self._lock:
            await self.connect()
            assert self._reader and self._writer

            self._writer.write(f"+\\{command}\n".encode())
            await self._writer.drain()

            records: list[str] = []
            while True:
                raw = await asyncio.wait_for(
                    self._reader.readline(), COMMAND_TIMEOUT_S
                )
                if not raw:
                    raise ConnectionError("rotctld closed the connection")
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                if line.startswith("RPRT"):
                    try:
                        return records, int(line.split()[1])
                    except (IndexError, ValueError):
                        raise RotctldError(RPRT_EINVAL, f"malformed terminator: {line!r}")
                records.append(line)

    @staticmethod
    def _fields(records: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for rec in records:
            if ":" in rec:
                key, _, value = rec.partition(":")
                out[key.strip().lower()] = value.strip()
        return out

    # --- commands ----------------------------------------------------------
    async def dump_caps(self) -> Caps:
        """Identify the peer. Answered from compiled capabilities, so this does
        not touch the serial line and is safe to send during a pass."""
        records, code = await self._command("dump_caps")
        if code != RPRT_OK:
            raise RotctldError(code, "dump_caps")

        fields = self._fields(records)
        blob = "\n".join(records).lower()

        # rotctld says "Rot type: AzEl"; rigctld talks about frequency ranges.
        is_rotator = "rot type" in fields or "rot type" in blob
        looks_like_rig = "rx freq ranges" in blob or "tuning steps" in blob

        model = None
        for rec in records:
            if rec.lower().startswith("caps dump for model"):
                try:
                    model = int(rec.split(":")[1].strip())
                except (IndexError, ValueError):
                    pass

        caps = Caps(
            model=model,
            name=fields.get("model name", ""),
            min_az=_as_float(fields.get("minimum azimuth"), -180.0),
            max_az=_as_float(fields.get("maximum azimuth"), 540.0),
            min_el=_as_float(fields.get("minimum elevation"), -20.0),
            max_el=_as_float(fields.get("maximum elevation"), 210.0),
            is_rotator=is_rotator and not looks_like_rig,
        )
        self.caps = caps
        return caps

    async def verify_is_rotator(self) -> Caps:
        caps = await self.dump_caps()
        if not caps.is_rotator:
            raise NotARotator(
                f"{self.host}:{self.port} is a radio (rigctld), not a rotator — "
                f"rotctld normally listens on 4533"
            )
        return caps

    async def get_position(self) -> tuple[float, float, float]:
        """Returns (azimuth, elevation, round-trip latency in ms).

        The azimuth is returned exactly as reported. A SPID legitimately reads
        outside 0-360 (its range is -180..540) and a value of 412 means the
        rotator is wound past north, which matters for cable wrap. Callers that
        want a compass bearing take the modulus themselves.
        """
        started = time.perf_counter()
        records, code = await self._command("get_pos")
        latency_ms = (time.perf_counter() - started) * 1000

        if code != RPRT_OK:
            raise RotctldError(code, "get_pos")

        fields = self._fields(records)
        try:
            az = float(fields["azimuth"])
            el = float(fields["elevation"])
        except (KeyError, ValueError):
            raise RotctldError(RPRT_EINVAL, f"unparsable position: {records!r}")
        return az, el, latency_ms


def _as_float(value: str | None, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
