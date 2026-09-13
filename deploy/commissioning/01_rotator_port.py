"""Find the rotator, and say what it is.

    python deploy/commissioning/01_rotator_port.py 10.90.36.140

rigctld's default port is 4532 and rotctld's is 4533, and a station's notes are
quite often wrong about which is in use. Pointing the dashboard at a radio and
polling it produces plausible-looking numbers that are not a heading.

Only `dump_caps` and `get_pos` are ever sent. `dump_caps` is answered from the
backend's compiled-in capability struct without touching the serial line, so it
is safe to run during a pass. Nothing here can move the antenna.

Station 5024 answered, on 2026-09-13:

    4532  closed
    4533  SPID Rot2Prog, model 901, Az-El, az -180..540, el -20..210,
          600 baud, Can set Position: Y, Can Stop: Y, Can Park: N
"""

from __future__ import annotations

import socket
import sys
import time

PORTS = (4532, 4533)
TIMEOUT_S = 6.0

INTERESTING = (
    "caps dump for model", "model name", "mfg name", "rot type", "rig type",
    "serial speed", "post write delay", "minimum azimuth", "maximum azimuth",
    "minimum elevation", "maximum elevation",
    "can set position", "can get position", "can stop", "can park",
    "can move", "can reset",
)


def command(sock: socket.socket, cmd: str) -> tuple[list[str], int | None]:
    """Send one extended-protocol command and read to its RPRT terminator."""
    sock.sendall(f"+\\{cmd}\n".encode())
    buf = b""
    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
        text = buf.decode(errors="replace")
        for line in text.splitlines():
            if line.startswith("RPRT"):
                try:
                    return text.splitlines(), int(line.split()[1])
                except (IndexError, ValueError):
                    return text.splitlines(), None
    return buf.decode(errors="replace").splitlines(), None


def probe(host: str, port: int) -> None:
    print(f"\n--- {host}:{port} ---")
    try:
        sock = socket.create_connection((host, port), timeout=TIMEOUT_S)
    except OSError as exc:
        print(f"  closed ({exc.__class__.__name__})")
        return

    sock.settimeout(TIMEOUT_S)
    with sock:
        records, code = command(sock, "dump_caps")
        if code != 0:
            print(f"  answered, but dump_caps returned RPRT {code}")
            return

        blob = "\n".join(records).lower()
        for line in records:
            if any(line.lower().startswith(key) for key in INTERESTING):
                print("  " + " ".join(line.split()))

        if "rot type" in blob:
            print("  => ROTATOR (rotctld). This is the port to configure.")
        elif "rx freq ranges" in blob or "rig type" in blob:
            print("  => RADIO (rigctld). Do NOT point the rotator at this.")
        else:
            print("  => answered, but is neither clearly a rotator nor a radio")

        # Capabilities come from compiled-in data, so they are readable even
        # with the controller powered off. Only a position read proves the
        # serial side is alive.
        started = time.perf_counter()
        pos, pos_code = command(sock, "get_pos")
        elapsed = (time.perf_counter() - started) * 1000
        fields = {ln.split(":")[0].strip().lower(): ln.split(":", 1)[1].strip()
                  for ln in pos if ":" in ln and not ln.startswith("RPRT")}
        if pos_code == 0 and "azimuth" in fields:
            print(f"  position: az={fields['azimuth']} el={fields.get('elevation')} "
                  f"({elapsed:.0f} ms)")
        else:
            print(f"  position: RPRT {pos_code} after {elapsed:.0f} ms "
                  f"— rotctld is up but the controller is not answering "
                  f"on the serial line (powered off, or cabling)")


def main() -> None:
    host = sys.argv[1] if len(sys.argv) > 1 else "10.90.36.140"
    print(f"probing {host} — read-only, nothing here moves the antenna")
    for port in PORTS:
        probe(host, port)


if __name__ == "__main__":
    main()
