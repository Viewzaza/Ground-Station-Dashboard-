"""Camera inventory and snapshot proxy.

The frontend asks this service which streams exist and gets back go2rtc stream
names. Those names are identical in go2rtc.yaml and go2rtc.mock.yaml, which is
what makes development on a machine with no camera indistinguishable from
production to every other line of code.

Snapshots are proxied rather than linked because the camera speaks plain HTTP
with Digest auth: a browser on an HTTPS page would refuse the mixed content,
and the credentials would be visible in page source.
"""

from __future__ import annotations

import logging

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

# Friendly labels for the stream names defined in deploy/go2rtc/*.yaml.
LABELS = {
    "cam_main": "Camera 1 · main",
    "cam_sub": "Camera 2 · sub",
}


class CameraService:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    async def inventory(self) -> list[dict]:
        streams: dict[str, dict] = {}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.s.go2rtc_url}/api/streams")
            if resp.status_code == 200:
                streams = resp.json() or {}
        except httpx.HTTPError as exc:
            log.warning("go2rtc unreachable: %s", exc)

        if not streams:
            # Report the expected streams as offline rather than an empty panel,
            # so the operator sees "camera down" instead of "no cameras".
            streams = {name: {} for name in LABELS}
            online = False
        else:
            online = True

        return [
            {
                "id": name,
                "label": LABELS.get(name, name),
                "stream": name,
                "ws_url": f"/video/api/ws?src={name}",
                "snapshot_url": f"/api/cameras/{name}/snapshot.jpg",
                "online": online,
            }
            for name in streams
        ]

    async def snapshot(self, stream: str) -> tuple[int, bytes, str]:
        url = f"{self.s.go2rtc_url}/api/frame.jpeg"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, params={"src": stream})
            return (
                resp.status_code,
                resp.content,
                resp.headers.get("content-type", "image/jpeg"),
            )
        except httpx.HTTPError as exc:
            log.warning("snapshot failed for %s: %s", stream, exc)
            return 502, b"", "text/plain"
