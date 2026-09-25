"""The transport rules that decide whether SatNOGS gets asked again.

Two families. Reads: one budget per credential for the whole process, however
many clients are built, and a long Retry-After obeyed as a stop. Writes: never
re-sent unless the request provably never left, never redirected, and a server
that goes away mid-run is not asked about every remaining item.
"""

from __future__ import annotations

import socket

import pytest
import requests
from urllib3.exceptions import MaxRetryError, NameResolutionError, NewConnectionError

from app.vendor.autoscheduler import http as http_mod
from app.vendor.autoscheduler import network_client as nc
from app.vendor.autoscheduler.http import (
    MAX_RETRIES, SatnogsHTTPError, SatnogsOutcomeUnknown, request,
)
from app.vendor.autoscheduler.network_client import (
    OBSERVATION_LIST_PER_HOUR_AUTH, NetworkClient, RateLimitedError, gate_key,
)

URL = "https://network.example/api/observations/"


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    monkeypatch.setattr(http_mod.time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def fresh_gates(monkeypatch):
    # The gate is process-wide on purpose, which is exactly why a test must not
    # inherit another's spent budget or Retry-After deadline.
    monkeypatch.setattr(nc, "_GATES", {})


def resp(status, headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = b"[]"
    r.encoding = "utf-8"
    r.headers.update(headers or {})
    return r


def refused():
    return requests.exceptions.ConnectionError(
        MaxRetryError(None, URL, reason=NewConnectionError(None, "Connection refused")))


def unresolvable():
    return requests.exceptions.ConnectionError(
        MaxRetryError(None, URL, reason=NameResolutionError(
            "network.example", None, socket.gaierror("Name or service not known"))))


class Recorder:
    """Raises or answers as told, and records what it was asked."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, kwargs.get("allow_redirects", True)))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


# --- writes -------------------------------------------------------------------

@pytest.mark.parametrize("make", [refused, unresolvable], ids=["refused", "dns"])
def test_a_write_that_never_opened_a_connection_is_retried_not_reported_unknown(make):
    """Refused and unresolvable happen only while connecting, so nothing was
    sent. Calling that OUTCOME UNKNOWN sent operators hunting for bookings that
    could not exist."""
    session = Recorder(make())
    with pytest.raises(SatnogsHTTPError) as info:
        request(session, "POST", URL, json_body=[{}])
    assert not isinstance(info.value, SatnogsOutcomeUnknown)
    assert info.value.status is None
    assert len(session.calls) == MAX_RETRIES


def test_a_proxy_error_on_a_write_stays_unknown():
    """A proxy may have forwarded the request before failing - not provable."""
    session = Recorder(requests.exceptions.ProxyError("proxy dropped it"))
    with pytest.raises(SatnogsOutcomeUnknown):
        request(session, "POST", URL, json_body=[{}])
    assert len(session.calls) == 1


def test_a_write_is_never_redirected():
    """Following a redirect breaks the never-sent reasoning: a POST delivered,
    answered 307, then connect-timed-out on the target looks like 'never
    connected' and is sent again."""
    session = Recorder(resp(307, {"Location": "https://elsewhere.example/"}))
    with pytest.raises(SatnogsOutcomeUnknown) as info:
        request(session, "POST", URL, json_body=[{}])
    assert session.calls == [("POST", False)]
    assert info.value.status == 307


def test_reads_still_follow_redirects():
    session = Recorder(resp(200))
    request(session, "GET", URL)
    assert session.calls == [("GET", True)]


def items(n):
    return [{"ground_station": 100 + i, "transmitter_uuid": "TX",
             "start": f"2026-09-24 0{i}:00:00", "end": f"2026-09-24 0{i}:10:00"} for i in range(n)]


def test_a_server_lost_mid_fallback_is_not_asked_about_every_item():
    """Batch rejected, then SatNOGS becomes unreachable. The old loop tried
    each remaining item three times and logged 'HTTP None' for every one."""
    class Server:
        headers = {}
        posts = 0

        def request(self, method, url, json=None, **_kw):
            Server.posts += 1
            if len(json) > 1:
                return resp(400)
            raise refused()

    settings = type("S", (), {"network_token": "t", "network_base_url": "https://network.example/api"})()
    client = NetworkClient(settings, cache=object())
    client.session = Server()
    result = client.schedule(items(5), execute=True)
    assert Server.posts == 1 + MAX_RETRIES, "only the first item should be tried"
    assert result.accepted == 0
    assert any("last 5 item(s)" in e and "not sent" in e for e in result.errors), result.errors
    assert not any("HTTP None" in e for e in result.errors)


# --- reads: one budget per credential -----------------------------------------

def settings(token, base="https://network.example/api"):
    return type("S", (), {"network_token": token, "network_base_url": base})()


def test_clients_on_the_same_credential_share_one_budget(monkeypatch):
    """CampaignService builds a client per operation. Each used to count from
    zero while the server did not, so the next operation in the same hour ran
    straight into real 429s."""
    token = "shared-budget-test-token"
    a = NetworkClient(settings(token), cache=object())
    b = NetworkClient(settings(token), cache=object())
    for sess in (a.session, b.session):
        sess._session = Recorder(resp(200))
        sess._sleep = lambda _s: None
    for _ in range(OBSERVATION_LIST_PER_HOUR_AUTH):
        a.session.request("GET", URL)
    with pytest.raises(RateLimitedError):
        b.session.request("GET", URL)
    assert b.session._session.calls == [], "b sent a read the shared budget had already spent"


def test_a_different_credential_has_its_own_budget():
    assert gate_key("https://x/api", "one") != gate_key("https://x/api", "two")
    assert gate_key("https://x/api", "") == "https://x/api|anonymous"
    assert "one" not in gate_key("https://x/api", "one"), "the token must not be stored"


def test_a_long_retry_after_blocks_every_client_on_that_credential():
    token = "blocked-until-test-token"
    a = NetworkClient(settings(token), cache=object())
    b = NetworkClient(settings(token), cache=object())
    a.session._session = Recorder(resp(429, {"Retry-After": "3600"}))
    b.session._session = Recorder(resp(200))
    # Stubbed so that a regression FAILS here rather than hanging: the old code
    # really did sleep 120 s twice waiting out this Retry-After.
    a.session._sleep = b.session._sleep = lambda _s: None
    with pytest.raises(RateLimitedError):
        a.session.request("GET", URL)
    with pytest.raises(RateLimitedError):
        b.session.request("GET", URL)
    assert b.session._session.calls == []
