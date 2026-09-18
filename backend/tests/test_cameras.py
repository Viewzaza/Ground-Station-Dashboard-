"""Camera inventory, and saying why there is no picture.

The tile can only report what this service tells it. For a long time that was a
boolean, so every cause arrived on the wall as the same two words — CAMERA DOWN
— and the reason existed only in the journal, which is not where the person
looking at the wall is standing. The operator asked why the camera was down
three times in one afternoon; that is the bug these tests hold shut.

The distinction that matters is between **the bridge** and **the camera**. A
stopped go2rtc, a `GS_GO2RTC_URL` still pointing at a compose service name, and
an unplugged Hikvision are three different jobs — start a container, edit an
env file, walk to the mast — and telling them apart from the tile is the whole
point.

Nothing here touches the network.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.routes.cameras import cameras as cameras_endpoint
from app.services import cameras as cam
from app.services.cameras import CameraService, _explain

REAL_CLIENT = httpx.AsyncClient


def service(**overrides) -> CameraService:
    base = dict(go2rtc_url="http://video:1984")
    base.update(overrides)
    return CameraService(Settings(**base))


def answer_with(monkeypatch, handler) -> None:
    def factory(**kwargs):
        return REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(cam.httpx, "AsyncClient", factory)


def raises(exc):
    def handler(request):
        raise exc

    return handler


# --------------------------------------------------------------------------
# the bridge is reachable
# --------------------------------------------------------------------------

async def test_a_running_bridge_reports_no_fault_to_explain(monkeypatch):
    """A working camera must not carry an explanation. A sentence that stays on
    screen after the picture comes back is worse than no sentence."""
    answer_with(monkeypatch, lambda r: httpx.Response(
        200, json={"cam_main": {}, "cam_sub": {}}))

    items, bridge = await service().inventory()

    assert bridge["reachable"] is True
    assert bridge["detail"] == ""
    assert all(i["online"] for i in items)


async def test_the_expected_streams_are_listed_even_when_the_bridge_is_gone(
        monkeypatch):
    """An empty panel looks like a station with no cameras configured, which is
    a different and much more alarming thing than a bridge that is down."""
    answer_with(monkeypatch, raises(httpx.ConnectError("nope")))

    items, _ = await service().inventory()

    assert [i["id"] for i in items] == ["cam_main", "cam_sub"]
    assert not any(i["online"] for i in items)
    # One camera, two encoder channels — the labels must not imply redundancy
    # that does not exist.
    assert [i["label"] for i in items] == ["main · 1080p", "sub · 360p"]


# --------------------------------------------------------------------------
# why there is no picture
# --------------------------------------------------------------------------

async def test_an_unresolvable_bridge_host_is_named_as_a_config_fault(monkeypatch):
    """The commonest cause by a mile, and the one most often misread as a dead
    camera: the backend running outside compose, where the default
    `http://video:1984` is a service name with no DNS behind it. Nobody should
    walk to a mast over this."""
    answer_with(monkeypatch, raises(
        httpx.ConnectError("[Errno 11001] getaddrinfo failed")))

    _, bridge = await service().inventory()

    assert bridge["reachable"] is False
    assert "does not resolve" in bridge["detail"]
    assert "GS_GO2RTC_URL" in bridge["detail"]
    # The host, not the authority: a port does not resolve or fail to.
    assert "“video”" in bridge["detail"]


async def test_a_refused_connection_says_the_bridge_is_not_running(monkeypatch):
    """Resolving and refusing is a different fault from not resolving: the name
    is right and the process is absent."""
    answer_with(monkeypatch, raises(httpx.ConnectError("connection refused")))

    _, bridge = await service(go2rtc_url="http://127.0.0.1:1984").inventory()

    assert "go2rtc is not running" in bridge["detail"]
    assert "127.0.0.1:1984" in bridge["detail"]


async def test_a_timeout_is_not_reported_as_a_missing_bridge(monkeypatch):
    """A bridge that answers slowly is running. Saying it is not would send
    someone to start a container that is already up."""
    answer_with(monkeypatch, raises(httpx.ReadTimeout("slow")))

    _, bridge = await service(go2rtc_url="http://10.0.0.5:1984").inventory()

    assert "did not answer in time" in bridge["detail"]
    assert "not running" not in bridge["detail"]


async def test_an_http_error_from_the_bridge_is_distinguished_from_silence(
        monkeypatch):
    """Something is listening and it answered. That is the bridge's problem to
    explain, not the camera's, and the status code is the thing to go on."""
    answer_with(monkeypatch, lambda r: httpx.Response(503, text="nope"))

    _, bridge = await service().inventory()

    assert "503" in bridge["detail"]


async def test_a_bridge_with_no_streams_is_not_the_same_as_no_bridge(monkeypatch):
    """go2rtc up with an empty config: the fix is the config, not the process.
    This arrived as "camera down" too."""
    answer_with(monkeypatch, lambda r: httpx.Response(200, json={}))

    _, bridge = await service().inventory()

    assert bridge["reachable"] is False
    assert "no streams" in bridge["detail"]


# --------------------------------------------------------------------------
# what reaches the browser
# --------------------------------------------------------------------------

class FakeRequest:
    def __init__(self, state) -> None:
        self.app = type("App", (), {"state": state})


async def test_the_endpoint_carries_the_reason_to_the_browser(monkeypatch):
    """The reason used to stop at the log line. The tile cannot render what it
    is not sent, and this is the field it renders."""
    answer_with(monkeypatch, raises(httpx.ConnectError("refused")))
    settings = Settings(go2rtc_url="http://127.0.0.1:1984")
    state = type("State", (), {"settings": settings, "cameras": CameraService(settings)})

    body = await cameras_endpoint(FakeRequest(state))

    assert body["bridge"]["reachable"] is False
    assert body["bridge"]["detail"]
    assert body["bridge"]["url"] == "http://127.0.0.1:1984"
    assert len(body["items"]) == 2


@pytest.mark.parametrize("url", ["http://video:1984", "http://[::1]:1984",
                                 "http://video", "video:1984"])
def test_explaining_a_fault_never_raises_on_a_url_shape(url):
    """This runs on the failure path. A crash while composing the sentence that
    explains a failure would replace a bad picture with no panel at all."""
    detail = _explain(url, httpx.ConnectError("boom"))
    assert isinstance(detail, str) and detail
