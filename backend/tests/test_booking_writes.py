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


# --- NetworkClient.schedule: a 409 costs one station, not the whole batch ------
#
# satnogs-network validates every item of a POST before it saves any, and the
# overlap check (create_new_observation) refuses the batch with HTTP 409 naming
# the FIRST station, in item order, whose item overlaps its calendar. So a 409
# created nothing, and says where to look. The old rule answered any refusal
# by sending every item on its own: the 2026-09-22 run had 37 of 150 refused,
# all such 409s, and at 600 items one stale slot meant ~600 sequential POSTs.

def overlap_409(station) -> requests.Response:
    # DRF's Response(str(error), status=409): the body is a JSON string.
    return response(409, f"One or more observations of station {station} overlap "
                         "with the already scheduled ones.")


class CalendarServer:
    """Refuses overlaps the way satnogs-network does, and records every POST.

    `taken` holds (station, start) pairs that overlap something already on
    that station. The whole batch is checked before anything is saved; the
    first item that hits one fails it with a 409 naming its station, and
    nothing is created. `blame` can replace that choice, to model a server
    that names some other station. `lost_reply(keys)` makes a POST create its
    rows and then lose the answer.
    """

    def __init__(self, taken=(), *, blame=None, lost_reply=lambda keys: False) -> None:
        self.taken = set(taken)
        self.blame = blame
        self.lost_reply = lost_reply
        self.headers: dict[str, str] = {}
        self.batches: list[list[tuple]] = []
        self.rows: list[tuple] = []

    def request(self, method, url, json=None, **_kwargs):
        keys = [(item["ground_station"], item["start"]) for item in json or []]
        self.batches.append(keys)
        clash = next((gs for gs, start in keys if (gs, start) in self.taken), None)
        if self.blame is not None and len(keys) > 1:
            clash = self.blame(keys)
        if clash is not None:
            return overlap_409(clash)
        self.rows.extend(keys)
        if self.lost_reply(keys):
            raise requests.exceptions.ReadTimeout("read timed out")
        return response(201, json)

    def sizes(self) -> list[int]:
        return [len(batch) for batch in self.batches]

    def duplicates(self) -> int:
        return len(self.rows) - len(set(self.rows))


def plan(per_station: dict[int, int]) -> list[dict]:
    """`per_station[s]` items on station s, one hour apart."""
    return [
        {"ground_station": station, "transmitter_uuid": "TX-U",
         "start": f"2026-09-26 0{n}:00:00", "end": f"2026-09-26 0{n}:10:00"}
        for station, count in per_station.items() for n in range(count)
    ]


def key(item: dict) -> tuple:
    return (item["ground_station"], item["start"])


def test_a_409_isolates_the_named_station_and_rebatches_the_rest():
    batch = plan({101: 2, 102: 2, 103: 2, 104: 2, 105: 2})
    stale = batch[2]                       # station 102's first pass
    server = CalendarServer(taken={key(stale)})

    result = client_on(server).schedule(batch, execute=True)

    # All ten, then station 102's two alone, then the other eight together.
    # The old rule: all ten, then every one of the ten alone.
    assert server.sizes() == [10, 1, 1, 8]
    assert server.batches[1:3] == [[key(batch[2])], [key(batch[3])]]
    assert result.submitted == 10
    assert result.accepted == 9
    assert result.accepted_items == [item for item in batch if item is not stale], (
        "the very dicts sent, in the order sent - CampaignService maps them back by identity"
    )
    (error,) = result.errors
    assert "station 102" in error and "HTTP 409" in error
    assert server.duplicates() == 0


def test_each_further_409_prunes_one_more_station():
    batch = plan({101: 1, 102: 2, 103: 1, 104: 2, 105: 1})
    server = CalendarServer(taken={key(batch[1]), key(batch[5])})   # on 102 and 104

    result = client_on(server).schedule(batch, execute=True)

    assert server.sizes() == [7, 1, 1, 5, 1, 1, 3]
    assert result.accepted == 5
    assert sorted(e.split()[1] for e in result.errors) == ["102", "104"]
    assert server.duplicates() == 0


def test_batch_attempts_are_bounded_by_the_number_of_stations():
    """A server that refuses every batch, blaming whoever is first in it, costs
    one batch per station - never an endless loop - and still books each item
    exactly once."""
    batch = plan({101: 2, 102: 2, 103: 2, 104: 2})
    server = CalendarServer(blame=lambda keys: keys[0][0])

    result = client_on(server).schedule(batch, execute=True)

    batch_attempts = [size for size in server.sizes() if size > 1]
    assert len(batch_attempts) <= 4 + 1
    assert result.accepted == 8
    assert sorted(server.rows) == sorted(key(item) for item in batch)


def test_a_409_naming_a_station_not_in_the_batch_is_sent_one_at_a_time():
    batch = plan({101: 1, 102: 1, 103: 1})
    server = CalendarServer(blame=lambda keys: 999)

    result = client_on(server).schedule(batch, execute=True)

    assert server.sizes() == [3, 1, 1, 1], "the pre-existing one-at-a-time fallback"
    assert result.accepted == 3


def test_a_409_that_keeps_naming_a_removed_station_cannot_loop():
    batch = plan({101: 1, 102: 2, 103: 1})
    server = CalendarServer(blame=lambda keys: 101)

    result = client_on(server).schedule(batch, execute=True)

    # 101 isolated once; the next 409 names a station no longer sent, so the
    # rest go one at a time rather than round again.
    assert server.sizes() == [4, 1, 3, 1, 1, 1]
    assert result.accepted == 4
    assert server.duplicates() == 0


@pytest.mark.parametrize("body", [
    {"non_field_errors": ["Observations of station 101 overlap"]},
    "Error in DB API connection. Please try again!",
], ids=["within-batch-overlap", "db-api"])
def test_a_400_is_still_sent_one_at_a_time_even_when_it_names_a_station(body):
    """Only the 409 prunes. The within-batch 400 reads much the same but means
    two of OUR items collide."""
    class Server(CalendarServer):
        def request(self, method, url, json=None, **kw):
            if len(json) > 1:
                self.batches.append([key(item) for item in json])
                return response(400, body)
            return super().request(method, url, json=json, **kw)

    server = Server()
    result = client_on(server).schedule(plan({101: 2, 102: 1, 103: 1}), execute=True)

    # Pruning would have shown as [4, 1, 1, 2, ...]: 101 alone, then 102+103.
    assert server.sizes() == [4, 1, 1, 1, 1]
    assert result.accepted == 4


def test_a_409_that_names_no_station_is_sent_one_at_a_time():
    """The scheduling-limit refusal is also a 409, with other words in it and
    no station to isolate."""
    class Server(CalendarServer):
        def request(self, method, url, json=None, **kw):
            if len(json) > 1:
                self.batches.append([key(item) for item in json])
                return response(409, "Scheduling limit reached for this satellite")
            return super().request(method, url, json=json, **kw)

    server = Server()
    result = client_on(server).schedule(plan({101: 2, 102: 1, 103: 1}), execute=True)

    assert server.sizes() == [4, 1, 1, 1, 1]
    assert result.accepted == 4


def test_an_unknown_single_during_a_prune_is_never_sent_again():
    batch = plan({101: 2, 102: 2, 103: 1})
    unsure = batch[1]                      # station 101's second pass
    server = CalendarServer(taken={key(batch[0])},
                            lost_reply=lambda keys: keys == [key(unsure)])

    result = client_on(server).schedule(batch, execute=True)

    assert server.sizes() == [5, 1, 1, 3]
    assert result.uncertain_items == [unsure]
    lost_at = server.batches.index([key(unsure)])
    assert not any(key(unsure) in sent for sent in server.batches[lost_at + 1:]), (
        "an item that may have landed was sent again"
    )
    assert result.accepted == 3
    assert server.duplicates() == 0


def test_an_unknown_rebatch_is_never_sent_again():
    batch = plan({101: 2, 102: 2, 103: 1})
    server = CalendarServer(taken={key(batch[0])},
                            lost_reply=lambda keys: len(keys) > 1)

    result = client_on(server).schedule(batch, execute=True)

    assert server.sizes() == [5, 1, 1, 3], "nothing after the lost re-batch"
    assert result.accepted_items == [batch[1]]
    assert result.uncertain_items == batch[2:]
    assert any("OUTCOME UNKNOWN for all 3" in e and "Do NOT resubmit" in e
               for e in result.errors), result.errors
    assert server.duplicates() == 0


# satnogs-network checks scheduling permission for the whole batch at once
# (NewObservationListSerializer.validate -> check_schedule_perms_per_station)
# and answers HTTP 400 {"non_field_errors": [...]} listing EVERY station in it
# the account may not book. The answer depends only on the account and the
# station, so resending a listed station's items alone can only be refused the
# same way. On 2026-10-02, with our own station offline, every station was
# listed: the one-at-a-time rule turned one refused POST of 100 into 101 POSTs.

def no_permission_400(stations) -> requests.Response:
    stations = sorted(stations)
    text = (f"No permission to schedule observations on station: {stations[0]}"
            if len(stations) == 1
            else f"No permission to schedule observations on stations: {stations}")
    return response(400, {"non_field_errors": [text]})


class PermissionServer(CalendarServer):
    """CalendarServer behind satnogs-network's permission check: a POST with
    any item on a station in `barred` is refused, listing every barred station
    it contains, before anything else is looked at."""

    def __init__(self, barred, **kwargs) -> None:
        super().__init__(**kwargs)
        self.barred = set(barred)

    def request(self, method, url, json=None, **kwargs):
        named = {item["ground_station"] for item in json or []} & self.barred
        if named:
            self.batches.append([key(item) for item in json])
            return no_permission_400(named)
        return super().request(method, url, json=json, **kwargs)


def test_a_batch_refused_for_permission_on_every_station_costs_one_post():
    batch = plan({101: 2, 102: 1, 103: 2})
    server = PermissionServer(barred={101, 102, 103})

    result = client_on(server).schedule(batch, execute=True)

    assert server.sizes() == [5], "a listed station's items were sent again on their own"
    assert result.submitted == 5 and result.accepted == 0 and not result.uncertain_items
    assert len(result.errors) == 5
    for item, error in zip(batch, result.errors):
        assert error.startswith(f"station {item['ground_station']} {item['start']} transmitter TX-U: HTTP 400 ")
        assert f"No permission to schedule observations on station: {item['ground_station']}" in error
    assert server.rows == []


def test_only_the_listed_stations_are_held_back_and_the_rest_rebatched():
    """One station flipped to unavailable between our read and the POST: its
    items are refused, everything else goes back as one batch."""
    batch = plan({101: 2, 102: 2, 103: 1})
    server = PermissionServer(barred={102})

    result = client_on(server).schedule(batch, execute=True)

    assert server.sizes() == [5, 3]
    assert result.accepted_items == [item for item in batch if item["ground_station"] != 102]
    assert [e.split()[1] for e in result.errors] == ["102", "102"]
    assert all("No permission to schedule observations on station: 102" in e
               for e in result.errors)
    assert server.duplicates() == 0


def test_the_singular_form_names_its_one_station():
    batch = plan({101: 3})
    server = PermissionServer(barred={101})

    result = client_on(server).schedule(batch, execute=True)

    assert server.sizes() == [3]
    assert result.accepted == 0 and len(result.errors) == 3


@pytest.mark.parametrize("body", [
    # http.py keeps 2000 characters of a body: a list cut off mid-number must
    # not be read as naming a station ("10" of "1011") that was never in it.
    {"non_field_errors": ["No permission to schedule observations on stations: [101, 10"]},
    "No permission to schedule observations on stations: 101 and others",
], ids=["truncated-list", "unrecognised-shape"])
def test_a_permission_refusal_that_cannot_be_read_in_full_goes_one_at_a_time(body):
    class Server(CalendarServer):
        def request(self, method, url, json=None, **kw):
            if len(json) > 1:
                self.batches.append([key(item) for item in json])
                return response(400, body)
            return super().request(method, url, json=json, **kw)

    server = Server()
    result = client_on(server).schedule(plan({101: 1, 10: 1, 102: 1}), execute=True)

    assert server.sizes() == [3, 1, 1, 1], "the pre-existing one-at-a-time fallback"
    assert result.accepted == 3


def test_a_permission_refusal_listing_no_station_we_sent_goes_one_at_a_time():
    class Server(CalendarServer):
        def request(self, method, url, json=None, **kw):
            if len(json) > 1:
                self.batches.append([key(item) for item in json])
                return no_permission_400({999})
            return super().request(method, url, json=json, **kw)

    server = Server()
    result = client_on(server).schedule(plan({101: 1, 102: 1}), execute=True)

    assert server.sizes() == [2, 1, 1]
    assert result.accepted == 2
