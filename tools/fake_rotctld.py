"""A fake Hamlib daemon that speaks the real wire protocol.

`GS_MOCK=1` swaps the rotator for an in-process simulator, which exercises the
UI but not a single line of the parser that will face the real hardware. This
does the opposite: it is a genuine TCP server speaking rotctld's protocol, so
`GS_MOCK=0 GS_ROTCTLD_HOST=127.0.0.1` puts the production code path under test
on a machine with no rotator attached.

    python tools/fake_rotctld.py                      # rotctld on 4533, SPID ROT2PROG
    python tools/fake_rotctld.py --model 903          # MD-01/02
    python tools/fake_rotctld.py --kind rig --port 4532
    python tools/fake_rotctld.py --fault-after 30     # start returning RPRT -5
    python tools/fake_rotctld.py --split-frames       # reply one byte at a time

--kind rig is the important one. rigctld's default port is 4532 and rotctld's
is 4533, and the station's documentation says "4532" while describing a
rotator. Pointing the dashboard at a radio must fail loudly rather than
silently reporting nonsense as an antenna position, and this is how that gets
tested.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import time

# Values taken from Hamlib's spid.c so the client sees realistic limits.
SPID_MODELS = {
    901: ("SPID Rot2Prog", -180.0, 540.0, -20.0, 210.0),
    902: ("SPID Rot1Prog", -180.0, 540.0, 0.0, 0.0),
    903: ("SPID MD-01/02", -180.0, 540.0, -20.0, 210.0),
}


class FakeRotator:
    """Sweeps a plausible az/el so the client sees movement, including a pass
    that winds the rotator past north into the overlap range."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()

    def position(self) -> tuple[float, float]:
        t = time.monotonic() - self.t0
        # 6-minute cycle: azimuth sweeps through and past 360, elevation arcs.
        az = 300.0 + 40.0 * t / 6.0
        el = max(0.0, 45.0 * math.sin(math.pi * (t % 360.0) / 360.0))
        return round(az, 1), round(el, 1)


class Server:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rotator = FakeRotator()
        self.started = time.monotonic()
        self.clients = 0

    @property
    def faulting(self) -> bool:
        return (
            self.args.fault_after > 0
            and time.monotonic() - self.started > self.args.fault_after
        )

    async def handle(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        self.clients += 1
        peer = writer.get_extra_info("peername")
        print(f"[fake] connect from {peer} (now {self.clients} client(s))")
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                await self.dispatch(raw.decode(errors="replace").strip(), writer)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.clients -= 1
            print(f"[fake] disconnect {peer} (now {self.clients} client(s))")
            writer.close()

    async def dispatch(self, line: str, writer: asyncio.StreamWriter) -> None:
        if not line:
            return

        # Extended Response Protocol: a leading punctuation character asks for
        # an echoed command name and a terminating RPRT on every reply.
        extended = line[0] in "+;|,"
        sep = "\n" if line[0] == "+" else line[0]
        if extended:
            line = line[1:]
        cmd, *rest = line.split()
        cmd = cmd.lstrip("\\")

        if self.args.kind == "rig":
            await self.reply(writer, self.rig_response(cmd, extended, sep))
            return

        if cmd in ("p", "get_pos"):
            if self.faulting:
                # RIG_ETIMEOUT: the serial link to the controller is dead.
                await self.reply(writer, "RPRT -5\n")
                return
            az, el = self.rotator.position()
            if extended:
                body = sep.join([
                    "get_pos:",
                    f"Azimuth: {az:.6f}",
                    f"Elevation: {el:.6f}",
                    "RPRT 0",
                ])
                await self.reply(writer, body + "\n")
            else:
                await self.reply(writer, f"{az:.2f}\n{el:.2f}\n")

        elif cmd in ("1", "dump_caps"):
            await self.reply(writer, self.caps(extended, sep))

        elif cmd == "dump_state":
            await self.reply(writer, self.state())

        elif cmd in ("_", "get_info"):
            name = SPID_MODELS[self.args.model][0]
            await self.reply(writer, f"Info: {name}\nRPRT 0\n" if extended else f"{name}\n")

        elif cmd in ("P", "set_pos", "S", "stop", "K", "park", "M", "move", "R", "reset"):
            print(f"[fake] !! received a command that moves the rotator: {cmd} {rest}")
            await self.reply(writer, "RPRT 0\n")

        else:
            # RIG_EINVAL — an unrecognised command.
            await self.reply(writer, "RPRT -1\n")

    def rig_response(self, cmd: str, extended: bool, sep: str) -> str:
        """rigctld answers a different vocabulary. get_pos does not exist."""
        if cmd in ("f", "get_freq"):
            return "145800000\n" if not extended else f"get_freq:{sep}Frequency: 145800000{sep}RPRT 0\n"
        if cmd in ("1", "dump_caps"):
            return (
                "Caps dump for model: 2\n"
                "Model name:\tNET rigctl\n"
                "Rig type:\tOther\n"
                "RX freq ranges:\n"
                "\t30 kHz - 30 MHz\n"
                "Tuning steps:\n"
                "\t1 Hz\n"
                "RPRT 0\n"
            )
        return "RPRT -1\n"

    def caps(self, extended: bool, sep: str) -> str:
        name, min_az, max_az, min_el, max_el = SPID_MODELS[self.args.model]
        return (
            f"Caps dump for model: {self.args.model}\n"
            f"Model name:\t{name}\n"
            f"Mfg name:\tSPID\n"
            "Backend version:\t20231127.0\n"
            "Rot type:\tAzEl\n"
            f"Minimum Azimuth:\t{min_az:.2f}\n"
            f"Maximum Azimuth:\t{max_az:.2f}\n"
            f"Minimum Elevation:\t{min_el:.2f}\n"
            f"Maximum Elevation:\t{max_el:.2f}\n"
            "RPRT 0\n"
        )

    def state(self) -> str:
        _, min_az, max_az, min_el, max_el = SPID_MODELS[self.args.model]
        return (
            "1\n"
            f"{self.args.model}\n"
            f"min_az={min_az:.6f}\n"
            f"max_az={max_az:.6f}\n"
            f"min_el={min_el:.6f}\n"
            f"max_el={max_el:.6f}\n"
            "south_zero=0\n"
            "rot_type=AzEl\n"
            "done\n"
        )

    async def reply(self, writer: asyncio.StreamWriter, text: str) -> None:
        data = text.encode()
        if self.args.split_frames:
            # Deliver one byte per write, so a client that assumes a reply
            # arrives in a single read is caught immediately.
            for i in range(len(data)):
                writer.write(data[i : i + 1])
                await writer.drain()
                await asyncio.sleep(0.002)
        else:
            if self.args.latency_ms:
                await asyncio.sleep(self.args.latency_ms / 1000)
            writer.write(data)
            await writer.drain()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=0,
                    help="default: 4533 for a rotator, 4532 for a rig")
    ap.add_argument("--kind", choices=["rot", "rig"], default="rot")
    ap.add_argument("--model", type=int, choices=sorted(SPID_MODELS), default=901)
    ap.add_argument("--fault-after", type=float, default=0,
                    help="seconds before get_pos starts returning RPRT -5")
    ap.add_argument("--latency-ms", type=float, default=300,
                    help="SPID controllers default to a 300 ms post-write delay")
    ap.add_argument("--split-frames", action="store_true",
                    help="write replies one byte at a time")
    args = ap.parse_args()

    if not args.port:
        args.port = 4532 if args.kind == "rig" else 4533

    server = Server(args)
    listener = await asyncio.start_server(server.handle, args.host, args.port)
    kind = "rigctld (a RADIO)" if args.kind == "rig" else \
        f"rotctld, model {args.model} {SPID_MODELS[args.model][0]}"
    print(f"[fake] {kind} listening on {args.host}:{args.port}")
    if args.fault_after:
        print(f"[fake] will start returning RPRT -5 after {args.fault_after}s")
    async with listener:
        await listener.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
