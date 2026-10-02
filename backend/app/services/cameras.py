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
#
# Station 5024 has ONE camera, a Hikvision DS-2CD1023G2-LIUF/SL reporting
# itself as INSTED-GS_1, and these are its two encoder channels: 101 at
# 1920x1080 and 102 at 640x360, both H.264. They were labelled "Camera 1" and
# "Camera 2" while that was still an open question; calling two views of one
# camera two cameras tells the operator the wrong thing when one tile fails.
LABELS = {
    "cam_main": "main · 1080p",
    "cam_sub": "sub · 360p",
}


def _explain(url: str, exc: Exception) -> str:
    """Why the bridge could not be reached, in the operator's terms.

    The commonest cause by a distance is running the backend outside Docker
    with the default `GS_GO2RTC_URL`. That default is `http://video:1984` —
    `video` is the compose service name, so outside the compose network it does
    not resolve at all, and the failure is a DNS error rather than anything to
    do with a camera. An operator seeing CAMERA DOWN on a laptop is almost
    always seeing that, and it is worth saying outright instead of making them
    deduce it.
    """
    authority = url.split("//", 1)[-1].split("/", 1)[0]
    # The name on its own for the DNS case — "the host video:1984 does not
    # resolve" is not true of a port, and the whole point of this sentence is
    # to be precise about which half is wrong.
    host = authority.rsplit(":", 1)[0] if ":" in authority else authority
    if isinstance(exc, httpx.ConnectError) and "getaddrinfo" in str(exc).lower():
        return (f"the video bridge host “{host}” does not resolve — "
                f"GS_GO2RTC_URL points at a Docker service name, so this is "
                f"the backend running outside compose rather than a camera fault")
    if isinstance(exc, httpx.ConnectError):
        return f"nothing is listening at {authority} — go2rtc is not running"
    if isinstance(exc, httpx.TimeoutException):
        return f"the video bridge at {authority} did not answer in time"
    return f"the video bridge at {authority} could not be reached"


class CameraService:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    async def inventory(self) -> tuple[list[dict], dict]:
        """The streams, and the state of the bridge they come through.

        The reason the bridge is unreachable used to be logged here and nowhere
        else, so the tile could only ever say CAMERA DOWN — which reads as "the
        camera is broken" when nine times in ten it means "go2rtc is not
        running". Those are different problems with different fixes, and the
        person standing in front of the wall is not the person reading the
        journal. So the reason is published.
        """
        streams: dict[str, dict] = {}
        reason = ""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.s.go2rtc_url}/api/streams")
            if resp.status_code == 200:
                streams = resp.json() or {}
            else:
                reason = f"the video bridge answered HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            reason = _explain(self.s.go2rtc_url, exc)
            log.warning("go2rtc unreachable: %s", exc)

        if not streams:
            # Report the expected streams as offline rather than an empty panel,
            # so the operator sees "camera down" instead of "no cameras".
            streams = {name: {} for name in LABELS}
            online = False
            reason = reason or "the video bridge is running but has no streams"
        else:
            online = True
            reason = ""

        items = [
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
        return items, {"reachable": online, "url": self.s.go2rtc_url, "detail": reason}

    async def snapshot(self, stream: str) -> tuple[int, bytes, str]:
        url = f"{self.s.go2rtc_url}/api/frame.jpeg"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, params={"src": stream})
            if resp.status_code == 200 and not resp.content:
                # go2rtc answers 200 with an empty body when the stream is
                # configured but its producer never connected. On station 5024
                # that is what a wrong camera password looks like: the bridge
                # is healthy, RTSP is open, and every frame request comes back
                # successful and empty. Passing it through hands the browser a
                # zero-byte JPEG, which is a success the tile has to discover
                # is a failure. 502 is what it actually is — an upstream that
                # gave us nothing — and it puts the tile straight into its
                # backoff instead of polling a broken image once a second.
                log.warning("snapshot for %s: the bridge returned an empty "
                            "frame — the camera behind it is not producing "
                            "(check CAM_USER/CAM_PASS)", stream)
                return 502, b"", "text/plain"
            return (
                resp.status_code,
                resp.content,
                resp.headers.get("content-type", "image/jpeg"),
            )
        except httpx.HTTPError as exc:
            log.warning("snapshot failed for %s: %s", stream, exc)
            return 502, b"", "text/plain"
