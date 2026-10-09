"""Read-only Hamlib rigctld client, for what the receiver is tuned to.

Station 5024 runs `rigctld -m 1 -t 4534` — Hamlib's *Dummy* rig. That sounds
like a stub with nothing to say, and it is the opposite: satnogs-client is
configured to drive a rig, so during a pass it writes the Doppler-corrected
downlink frequency to this daemon once a second. The SDR itself is tuned inside
the gr-satnogs flowgraph and has no network interface, so this dummy is the
only place the receiver's intended frequency is visible from outside the box.

That makes it worth reading for one specific reason: it is an *independent*
answer. The dashboard computes Doppler from Skyfield and its own TLE; SatNOGS
computes it from its own propagator and its own elements. Showing both means a
disagreement is visible rather than silent — and a disagreement means one of
the two is tracking the wrong thing.

**There is deliberately no set_freq here.** The read path for the rotator was
built the same way and a test asserts it: a module that cannot express a write
cannot perform one by accident. satnogs-client owns this rig. If this dashboard
ever set a frequency it would fight the thing actually running the pass.
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 4.0
COMMAND_TIMEOUT_S = 4.0
RPRT_OK = 0


class RigError(RuntimeError):
    def __init__(self, code: int, detail: str = "") -> None:
        super().__init__(f"RPRT {code}{(': ' + detail) if detail else ''}")
        self.code = code


class RigClient:
    """One socket, reads only. Same framing rules as the rotctld client."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        if self.connected:
            return
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), CONNECT_TIMEOUT_S
        )

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        self._reader = self._writer = None

    async def _command(self, command: str) -> tuple[list[str], int]:
        """Extended protocol, so every reply ends in an unambiguous RPRT."""
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
                    raise ConnectionError("rigctld closed the connection")
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                if line.startswith("RPRT"):
                    try:
                        return records, int(line.split()[1])
                    except (IndexError, ValueError):
                        raise RigError(-1, f"malformed terminator: {line!r}")
                records.append(line)

    @staticmethod
    def _fields(records: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for rec in records:
            if ":" in rec:
                key, _, value = rec.partition(":")
                out[key.strip().lower()] = value.strip()
        return out

    # --- reads -------------------------------------------------------------
    async def get_state(self) -> dict:
        """Frequency, mode and passband, with the round-trip cost."""
        started = time.perf_counter()
        records, code = await self._command("get_freq")
        latency_ms = (time.perf_counter() - started) * 1000
        if code != RPRT_OK:
            raise RigError(code, "get_freq")

        fields = self._fields(records)
        try:
            freq = float(fields["frequency"])
        except (KeyError, ValueError):
            raise RigError(-1, f"unparsable frequency: {records!r}")

        # Mode is a bonus; a rig that will not report it should not cost us the
        # frequency, which is the number that matters.
        mode, passband = "", None
        try:
            mrecords, mcode = await self._command("get_mode")
            if mcode == RPRT_OK:
                mfields = self._fields(mrecords)
                mode = mfields.get("mode", "")
                try:
                    passband = float(mfields["passband"])
                except (KeyError, ValueError):
                    passband = None
        except (RigError, ConnectionError, OSError, asyncio.TimeoutError):
            pass

        return {
            "freq_hz": freq,
            "mode": mode,
            "passband_hz": passband,
            "latency_ms": round(latency_ms, 1),
        }
