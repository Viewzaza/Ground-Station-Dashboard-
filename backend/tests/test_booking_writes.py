"""The booking POST must never be sent twice when the first one may have landed.

These tests use a fake server that does what a real one can: create the rows,
and then fail to deliver the answer. That is the case the old retry policy got
wrong - it could not tell "the request never arrived" from "the reply never
came back", treated both as "try again", and re-created every observation that
had already been booked. A probe against exactly this server turned 3 intended
bookings into 12 POSTs and 18 rows, with the run reporting accepted 0.

The property being protected, stated once: after any single submit, the number
of distinct observations the server holds equals the number we meant to book,
and no observation exists twice.
"""

from __future__ import annotations

import json

import pytest
import requests

from app.vendor.autoscheduler import http as http_mod
from app.vendor.autoscheduler.http import (
    MAX_RETRIES,
    SatnogsHTTPError,
    SatnogsOutcomeUnknown,
    request,
)
from app.vendor.autoscheduler.network_client import NetworkClient

URL = "https://network.example/api/observations/"


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    # Retries back off with time.sleep; none of these tests should wait.
    monkeypatch.setattr(http_mod.time, "sleep", lambda _s: None)


def response(status: int, body=None) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps([] if body is None else body).encode()
    resp.encoding = "utf-8"
    return resp


class PersistingServer:
    """Creates every row it is sent, THEN decides how to answer.

    `answer` is what happens after the rows exist: "ok", "read_timeout" (the
    reply is lost), "gateway_504" (a proxy fails after the app committed),
    "reject_400" (the batch is refused and nothing is created), or
    "connect_timeout" (the connection never completes, so nothing arrives).
    """

    def __init__(self, answer: str = "ok", batch_answer: str | None = None) -> None:
        self.answer = answer
        self.batch_answer = batch_answer or answer
        self.headers: dict[str, str] = {}
        self.posts = 0
        self.rows: list[tuple] = []

    def request(self, method: str, url: str, json=None, **_kwargs) -> requests.Response:
        items = json or []
        mode = self.batch_answer if len(items) > 1 else self.answer
        if mode == "connect_timeout":
            raise requests.exceptions.ConnectTimeout("connect timed out")
        self.posts += 1
        if mode == "reject_400":
            return response(400, {"detail": "one entry overlaps an existing booking"})
        for item in items:
            self.rows.append((item["ground_station"], item["start"], item["transmitter_uuid"]))
        if mode == "read_timeout":
            raise requests.exceptions.ReadTimeout("read timed out")
        if mode == "gateway_504":
            return response(504)
        return response(201, items)

    def duplicates(self) -> int:
        return len(self.rows) - len(set(self.rows))


def items(n: int) -> list[dict]:
    return [
        {"ground_station": 100 + i, "transmitter_uuid": "TX-U",
         "start": f"2026-09-24T0{i}:00:00Z", "end": f"2026-09-24T0{i}:10:00Z"}
        for i in range(n)
    ]


def client_on(server: PersistingServer) -> NetworkClient:
    settings = type("S", (), {"network_token": "t0k3n", "network_base_url": "https://network.example/api"})()
    client = NetworkClient(settings, cache=object())
    # POST is exempt from the read-rate gate, so talking to the fake directly
    # changes nothing about the write path under test.
    client.session = server
    return client


# --- http.request: the transport rule ------------------------------------------

def test_a_write_whose_reply_is_lost_is_sent_exactly_once():
    server = PersistingServer("read_timeout")
    with pytest.raises(SatnogsOutcomeUnknown):
        request(server, "POST", URL, json_body=items(3))
    assert server.posts == 1, "the POST was repeated after it may already have landed"
    assert server.duplicates() == 0


def test_a_write_that_gets_a_5xx_is_not_repeated_either():
    """A 504 from a gateway does not mean the application behind it did nothing."""
    server = PersistingServer("gateway_504")
    with pytest.raises(SatnogsOutcomeUnknown) as info:
        request(server, "POST", URL, json_body=items(2))
    assert server.posts == 1
    assert info.value.status == 504


def test_a_write_that_never_connected_is_retried():
    """ConnectTimeout is the one failure that proves nothing arrived."""
    server = PersistingServer("connect_timeout")
    with pytest.raises(SatnogsHTTPError) as info:
        request(server, "POST", URL, json_body=items(1))
    assert not isinstance(info.value, SatnogsOutcomeUnknown), (
        "a request that never connected cannot have been applied - calling its "
        "outcome unknown would send the operator to check for bookings that "
        "cannot exist"
    )
    assert server.rows == []


def test_reads_are_still_retried_on_a_lost_reply():
    """The fix is for writes. A GET is safe to repeat and must stay resilient."""
    attempts = {"n": 0}

    class Flaky:
        headers: dict = {}

        def request(self, method, url, **_kw):
            attempts["n"] += 1
            if attempts["n"] < MAX_RETRIES:
                raise requests.exceptions.ReadTimeout("slow feed")
            return response(200, [{"ok": True}])

    assert request(Flaky(), "GET", URL).status_code == 200
    assert attempts["n"] == MAX_RETRIES


def test_a_4xx_is_still_a_definite_rejection_for_any_method():
    server = PersistingServer("reject_400")
    with pytest.raises(SatnogsHTTPError) as info:
        request(server, "POST", URL, json_body=items(2))
    assert not isinstance(info.value, SatnogsOutcomeUnknown)
    assert info.value.status == 400
    assert server.rows == [], "a rejected batch creates nothing"


# --- NetworkClient.schedule: the booking rule ----------------------------------

def test_the_regression_a_lost_reply_never_duplicates_a_booking():
    """The probe that found this, made permanent. Before the fix: 3 bookings
    became 12 POSTs and 18 rows. After: one POST, three rows, no duplicates,
    and all three honestly reported as unconfirmed rather than as failed."""
    server = PersistingServer("read_timeout")
    result = client_on(server).schedule(items(3), execute=True)

    assert server.posts == 1
    assert len(server.rows) == 3
    assert server.duplicates() == 0
    assert result.accepted == 0
    assert len(result.uncertain_items) == 3, (
        "items that may be booked must be kept for the cross-check to read back"
    )
    assert any("OUTCOME UNKNOWN" in e and "Do NOT resubmit" in e for e in result.errors)


def test_an_unknown_batch_does_not_fall_back_to_item_by_item():
    """The item-by-item fallback exists for a batch the server REJECTED. Taking
    it after an unknown outcome is what re-created every landed observation."""
    server = PersistingServer("gateway_504")
    client_on(server).schedule(items(5), execute=True)
    assert server.posts == 1


def test_a_rejected_batch_still_falls_back_and_names_the_station():
    """The pre-existing behaviour, kept: one bad entry must not cost the rest.
    A 400 creates nothing, so retrying each item alone is safe."""
    server = PersistingServer(answer="ok", batch_answer="reject_400")
    result = client_on(server).schedule(items(3), execute=True)

    assert result.accepted == 3
    assert server.duplicates() == 0
    assert len(server.rows) == 3


def test_a_rejected_item_error_says_which_station():
    """With 77 stations in a run, "HTTP 400" alone did not say which refused."""
    server = PersistingServer(answer="reject_400", batch_answer="reject_400")
    result = client_on(server).schedule(items(2), execute=True)
    assert result.accepted == 0
    assert all("station 10" in e for e in result.errors), result.errors


def test_an_unknown_item_inside_the_fallback_is_not_retried():
    server = PersistingServer(answer="read_timeout", batch_answer="reject_400")
    result = client_on(server).schedule(items(3), execute=True)
    assert server.posts == 1 + 3, "one batch attempt, then each item exactly once"
    assert server.duplicates() == 0
    assert len(result.uncertain_items) == 3


def test_an_unreachable_server_books_nothing_and_says_so():
    """No connection on any attempt means nothing arrived; retrying each item
    alone would only fail the same way len(items) more times."""
    server = PersistingServer("connect_timeout")
    result = client_on(server).schedule(items(4), execute=True)
    assert server.rows == []
    assert result.uncertain_items == []
    assert any("Nothing was booked" in e for e in result.errors)
