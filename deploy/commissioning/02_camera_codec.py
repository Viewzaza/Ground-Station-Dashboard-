"""Say what the camera is, and whether WebRTC can carry it.

    python deploy/commissioning/02_camera_codec.py 10.90.36.130 admin

The password is read from deploy/secrets/camera_password.txt, or prompted for,
so it never reaches the shell history or this file.

Two questions this settles. **H.264 or H.265** — WebRTC cannot carry H.265 at
all, and a camera shipped in HEVC mode gives tiles that connect and then show
nothing. **One camera or two** — "two streams" is usually the main and sub
channels of a single device, which matters because they then fail together.

Station 5024 answered, on 2026-09-13: one Hikvision DS-2CD1023G2-LIUF/SL named
INSTED-GS_1, channel 101 at 1920x1080 and 102 at 640x360, both H.264, RTSP
advertising profile-level-id=420029 (Baseline 4.1) with packetization-mode=1.
"""

from __future__ import annotations

import getpass
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

try:
    import httpx
except ImportError:                                    # pragma: no cover
    sys.exit("pip install httpx, or run this inside backend/.venv")

SECRET = pathlib.Path(__file__).resolve().parents[1] / "secrets" / "camera_password.txt"
NS = {"h": "http://www.hikvision.com/ver20/XMLSchema"}


def password() -> str:
    if SECRET.exists():
        return SECRET.read_text(encoding="utf-8").strip()
    return getpass.getpass("camera password: ")


def text(node, path: str, default: str = "?") -> str:
    found = node.find(path, NS)
    return default if found is None or not found.text else found.text.strip()


def main() -> None:
    host = sys.argv[1] if len(sys.argv) > 1 else "10.90.36.130"
    user = sys.argv[2] if len(sys.argv) > 2 else "admin"
    auth = httpx.DigestAuth(user, password())

    with httpx.Client(auth=auth, timeout=20) as client:
        info = client.get(f"http://{host}/ISAPI/System/deviceInfo")
        if info.status_code == 401:
            sys.exit("authentication failed — check the user and password")
        info.raise_for_status()
        device = ET.fromstring(info.text)

        print(f"device   {text(device, 'h:deviceName')}  "
              f"{text(device, 'h:model')}")
        print(f"firmware {text(device, 'h:firmwareVersion')} "
              f"({text(device, 'h:firmwareReleasedDate')})")

        chans = client.get(f"http://{host}/ISAPI/Streaming/channels")
        chans.raise_for_status()
        root = ET.fromstring(chans.text)

    codecs: set[str] = set()
    names: set[str] = set()
    print("\nchannels:")
    for chan in root.findall("h:StreamingChannel", NS):
        video = chan.find("h:Video", NS)
        if video is None:
            continue
        codec = text(video, "h:videoCodecType")
        codecs.add(codec.upper())
        names.add(text(chan, "h:channelName"))
        print(f"  {text(chan, 'h:id'):>4}  {text(chan, 'h:channelName'):<16} "
              f"{codec:<6} {text(video, 'h:videoResolutionWidth')}x"
              f"{text(video, 'h:videoResolutionHeight')}  "
              f"{text(video, 'h:constantBitRate')} kbps")

    print()
    if any(c.startswith("H.265") or c == "HEVC" for c in codecs):
        print("  !! H.265 present. WebRTC cannot carry it — switch the encoder")
        print("     to H.264 before going further, or every tile will connect")
        print("     and then show nothing.")
    elif codecs == {"H.264"}:
        print("  H.264 throughout — WebRTC carries this with no transcode.")

    # Distinct channel names, not channel count: 101 and 102 are the main and
    # sub streams of one device and share a name.
    print(f"  {len(names)} physical camera(s): {', '.join(sorted(names))}")
    if len(names) == 1:
        print("  Both tiles are views of the SAME device — it failing blanks both.")


if __name__ == "__main__":
    main()
