"""Station calendars come from /api/jobs/, anonymously, and fall back cleanly.

Why this read moved. A campaign over every station that can hear KNACKSAT-2
reads ~222 calendars. From the observation feed each one costs ~1.2-1.33 pages
of the token's 240/hour list budget - more than the whole budget for one
build - and every looped commit round used to read them all again.
/api/jobs/?ground_station=N answers the same question ("what on this station
has not started yet?") in one unpaginated response from a view with no
throttle at all, and it is how the official auto-scheduler reads calendars.

Two things about it are easy to get wrong, and both are pinned here:

* It must be read WITHOUT the token. With the owner's token, a /jobs/ read of
  the owner's own station is a heartbeat to the server (last_seen=now), not a
  read - so reading 5024's calendar would mark it alive whether it is or not.
* When it fails, the answer is the observation feed exactly as before, never
  an empty calendar - an empty calendar reads every pass as free.

Nothing here touches the network: the seam is the session (a real
`requests.Response` comes back), or for the header check a transport adapter
mounted on the real `requests.Session`, so the headers asserted on are the ones
requests would really put on the wire.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
import requests
from requests.adapters import BaseAdapter

from app.vendor.autoscheduler import http as http_mod
from app.vendor.autoscheduler import network_client as nc
from app.vendor.autoscheduler.http import MAX_RETRIES, SatnogsHTTPError
from app.vendor.autoscheduler.network_client import (
    JOBS_BYPASS_AFTER_FAILURES,
    JOBS_LIST_PER_HOUR,
    JOBS_TIMEOUT_S,
    OBSERVATION_LIST_PER_HOUR_AUTH,
    THROTTLE_WINDOW_S,
    NetworkClient,
    RateLimitedError,
    RateLimitedSession,
)

BASE = "https://network.example/api"
JOBS = f"{BASE}/jobs/"
OBSERVATIONS = f"{BASE}/observations/"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
MISSION = 67683


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    # http.request backs off between retries with time.sleep; never wait here.
    monkeypatch.setattr(http_mod.time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def fresh_gates(monkeypatch):
    # The gates are process-wide on purpose, which is exactly why a test must
    # not inherit another's spent budget or Retry-After deadline.
    monkeypatch.setattr(nc, "_GATES", {})


# --------------------------------------------------------------------------
# stand-ins
# --------------------------------------------------------------------------

def response(status: int, body=None, *, headers=None, raw: bytes | None = None) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp.headers.update(headers or {})
    resp._content = raw if raw is not None else json.dumps([] if body is None else body).encode()
    resp.encoding = "utf-8"
    return resp


class FakeSession:
    """Answers through `handler(method, url, n)`; records every request."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.headers: dict[str, str] = {}
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.handler(method, url, len(self.calls))


class FakeClock:
    """Time only moves when something sleeps, and sleeping is free."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def settings(token: str = "t0k3n-for-tests"):
    return type("S", (), {"network_token": token, "network_base_url": BASE})()


def always(status, body=None, **kw):
    return lambda method, url, n: response(status, body, **kw)


def never_called(method, url, n):
    raise AssertionError(f"{method} {url} should not have been requested")


def client_with(jobs=never_called, observations=never_called, token="t0k3n-for-tests"):
    """A real NetworkClient whose two sessions answer from fakes.

    The RateLimitedSession wrappers are kept, so scope routing and budgets are
    the real ones; only the socket underneath is replaced.
    """
    client = NetworkClient(settings(token), cache=object())
    client.jobs_session._session = FakeSession(jobs)
    client.session._session = FakeSession(observations)
    client.jobs_session._sleep = client.session._sleep = lambda _s: None
    return client


def iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def job(oid: int, start: datetime, *, station: int | None = 7, minutes: int = 10,
        norad: int = MISSION, **extra) -> dict:
    """A /jobs/ row, shaped like JobSerializer's: no status field."""
    row = {"id": oid, "start": iso(start), "end": iso(start + timedelta(minutes=minutes)),
           "norad_cat_id": norad, "transmitter": "UatCXtfDnoBPeVBGHgj4Bc",
           "tle0": "KNACKSAT-2", **extra}
    if station is not None:
        row["ground_station"] = station
    return row


def observation(oid: int, start: datetime, *, status: str = "future") -> dict:
    """An /observations/ row: the feed is newest first and carries a status."""
    return {"id": oid, "start": iso(start), "end": iso(start + timedelta(minutes=10)),
            "norad_cat_id": MISSION, "ground_station": 7, "status": status}


OBSERVATION_FEED = [observation(501, NOW + timedelta(hours=3)),
                    observation(500, NOW - timedelta(hours=1), status="good")]


# --------------------------------------------------------------------------
# the jobs read
# --------------------------------------------------------------------------

def test_a_calendar_is_read_from_jobs_and_spends_nothing_of_the_observation_budget():
    rows = [job(11, NOW + timedelta(hours=5)), job(10, NOW + timedelta(hours=2))]
    client = client_with(jobs=always(200, rows))

    bookings = client.future_bookings(7, now=NOW)

    assert [(b.id, b.start, b.norad_cat_id) for b in bookings] == [
        (11, NOW + timedelta(hours=5), MISSION), (10, NOW + timedelta(hours=2), MISSION)]
    (call,) = client.jobs_session._session.calls
    assert (call["method"], call["url"]) == ("GET", JOBS)
    assert call["params"] == {"ground_station": 7, "format": "json"}
    assert call["timeout"] == JOBS_TIMEOUT_S
    assert client.session._session.calls == []
    # The point of the move: the token's observation budget is untouched, and
    # the read was charged to the jobs courtesy budget instead.
    assert len(client.session._spent["observations"]) == 0
    assert len(client.jobs_session._spent["jobs"]) == 1
    assert client.calendar_sources == {"jobs": 1}


class CaptureAdapter(BaseAdapter):
    """A transport under the real requests.Session: sees the final headers."""

    def __init__(self, body) -> None:
        super().__init__()
        self.body = body
        self.sent: list[requests.PreparedRequest] = []

    def send(self, request, **_kwargs):
        self.sent.append(request)
        resp = response(200, self.body)
        resp.request = request
        resp.url = request.url
        return resp

    def close(self) -> None:
        pass


def test_the_jobs_read_never_carries_the_token():
    """With the owner's token, /jobs/?ground_station=<own station> saves
    last_seen=now on the server - a fake heartbeat. Checked on the wire, and
    against the token session to prove the check can see a token at all."""
    client = NetworkClient(settings("owner-token-must-not-leak"), cache=object())
    jobs_wire = CaptureAdapter([job(1, NOW + timedelta(hours=1), station=5024)])
    obs_wire = CaptureAdapter([observation(2, NOW + timedelta(hours=1))])
    client.jobs_session._session.mount("https://", jobs_wire)
    client.session._session.mount("https://", obs_wire)

    client.future_bookings(5024, now=NOW)
    client.future_bookings(5024, now=NOW, source="observations")

    (jobs_request,) = jobs_wire.sent
    assert jobs_request.url.startswith(JOBS)
    assert "Authorization" not in jobs_request.headers
    assert "owner-token-must-not-leak" not in json.dumps(dict(jobs_request.headers))
    (obs_request,) = obs_wire.sent
    assert obs_request.headers["Authorization"] == "Token owner-token-must-not-leak"
    assert "Authorization" not in client.jobs_session.headers


def test_rows_on_another_station_or_already_started_are_dropped():
    """Filtered, not walked to a stop: nothing depends on the order /jobs/
    returns rows in. A row with no ground_station is taken to be ours - if the
    filter were ever ignored, over-counting blocks passes, it never frees one."""
    rows = [
        job(1, NOW + timedelta(hours=4)),                       # ours, future
        job(2, NOW + timedelta(hours=3), station=8),            # another station
        job(3, NOW - timedelta(minutes=1)),                     # started a minute ago
        job(4, NOW, station=None),                              # starts exactly now, no station
        job(5, NOW + timedelta(hours=1), status="scheduled"),   # carries a status
        job(6, NOW + timedelta(hours=2), station="7"),          # station as a string
    ]
    client = client_with(jobs=always(200, rows))

    bookings = client.future_bookings(7, now=NOW)

    assert [b.id for b in bookings] == [1, 4, 5, 6]
    status = {b.id: b.status for b in bookings}
    assert status[1] == "future", "a /jobs/ row has no status; everything on it is future"
    assert status[5] == "scheduled"
    assert bookings[0].end == NOW + timedelta(hours=4, minutes=10)


# --------------------------------------------------------------------------
# falling back
# --------------------------------------------------------------------------

@pytest.mark.parametrize("jobs_answer", [
    always(500),
    always(404, {"detail": "Not found."}),
    always(200, {"detail": "not a list"}),
    always(200, raw=b"<html>maintenance</html>"),
    always(200, [{"id": 1, "end": "2026-09-25T13:00:00Z", "ground_station": 7}]),
    always(200, [job(1, NOW + timedelta(hours=1)) | {"start": "tomorrow-ish"}]),
    always(200, ["not a row"]),
    always(200, [job(1, NOW + timedelta(hours=1)) | {"start": "2026-09-25T13:00:00"}]),
    always(200, [job(1, NOW + timedelta(hours=1)) | {"start": None}]),
    always(429, headers={"Retry-After": "3600"}),
], ids=["5xx", "404", "non-list", "not-json", "missing-start", "bad-date",
        "non-dict-row", "naive-timestamp", "null-start", "429"])
def test_any_failed_jobs_read_falls_back_to_the_observation_feed(jobs_answer, caplog):
    client = client_with(jobs=jobs_answer, observations=always(200, OBSERVATION_FEED))

    with caplog.at_level(logging.WARNING, logger=nc.__name__):
        bookings = client.future_bookings(7, now=NOW)

    assert [b.id for b in bookings] == [501], "the observation feed's answer, walked as before"
    assert bookings[0].status == "future"
    assert client.calendar_sources == {"observations": 1}
    assert len(client.session._session.calls) == 1
    assert any("/jobs/" in rec.getMessage() and "station 7" in rec.getMessage()
               for rec in caplog.records), "a fallback must not be silent"


def test_source_observations_never_touches_jobs():
    client = client_with(observations=always(200, OBSERVATION_FEED))

    bookings = client.future_bookings(7, now=NOW, source="observations")

    assert [b.id for b in bookings] == [501]
    assert client.jobs_session._session.calls == []
    assert client.calendar_sources == {"observations": 1}


def test_source_jobs_lets_the_failure_escape_and_reads_nothing_else():
    client = client_with(jobs=always(404))

    with pytest.raises(SatnogsHTTPError) as info:
        client.future_bookings(7, now=NOW, source="jobs")

    assert info.value.status == 404
    assert client.session._session.calls == []
    assert client.calendar_sources == {}


def test_an_unknown_source_is_refused():
    with pytest.raises(ValueError):
        client_with().future_bookings(7, now=NOW, source="guess")


def test_a_throttled_fallback_still_raises_rate_limited():
    """build_campaign stops reading on RateLimitedError rather than carrying on
    with every later station's calendar unread. The fallback must not turn
    that signal into something else."""
    client = client_with(jobs=always(503),
                         observations=always(429, headers={"Retry-After": "3600"}))

    with pytest.raises(RateLimitedError):
        client.future_bookings(7, now=NOW)

    assert client.calendar_sources == {}


def test_calendar_sources_count_which_source_answered_each_call():
    def jobs(method, url, n):
        # First two stations answer; the third one's read fails every retry.
        return response(200, [job(n, NOW + timedelta(hours=1), station=n)]) if n <= 2 else response(502)

    client = client_with(jobs=jobs, observations=always(200, OBSERVATION_FEED))

    client.future_bookings(1, now=NOW)
    client.future_bookings(2, now=NOW)
    client.future_bookings(7, now=NOW)
    client.future_bookings(7, now=NOW, source="observations")

    assert client.calendar_sources == {"jobs": 2, "observations": 2}


def test_repeated_jobs_failures_stop_this_client_trying_jobs():
    """Each failed jobs read costs every retry and its backoff before the
    fallback starts. After a few in a row, go straight to the feed."""
    client = client_with(jobs=always(500), observations=always(200, OBSERVATION_FEED))

    for station_id in range(1, 7):
        client.future_bookings(station_id, now=NOW)

    assert len(client.jobs_session._session.calls) == JOBS_BYPASS_AFTER_FAILURES * MAX_RETRIES
    assert client.calendar_sources == {"observations": 6}

    # Forcing the source still asks, bypass or not.
    with pytest.raises(SatnogsHTTPError):
        client.future_bookings(7, now=NOW, source="jobs")


def test_a_jobs_success_resets_the_failure_count():
    """Only failures IN A ROW bypass /jobs/; an occasional 500 among good
    answers (4 in 1147 were seen live) must not."""
    # Two failed reads then a good one, three times over: never
    # JOBS_BYPASS_AFTER_FAILURES in a row, though twice that many in all.
    pattern = ["fail", "fail", "ok"] * 3
    assert pattern.count("fail") >= JOBS_BYPASS_AFTER_FAILURES
    # A failed read is MAX_RETRIES requests (5xx is retried), a good one is one.
    answers = []
    for outcome in pattern:
        answers += [outcome] * (MAX_RETRIES if outcome == "fail" else 1)

    def jobs(method, url, n):
        return response(200, []) if answers[n - 1] == "ok" else response(500)

    client = client_with(jobs=jobs, observations=always(200, OBSERVATION_FEED))

    for station_id in range(len(pattern)):
        client.future_bookings(station_id, now=NOW)

    assert client.calendar_sources == {"jobs": 3, "observations": 6}
    assert len(client.jobs_session._session.calls) == len(answers), "every read tried /jobs/ first"


# --------------------------------------------------------------------------
# the jobs courtesy budget
# --------------------------------------------------------------------------

def gate(handler, *, authenticated=True):
    clock = FakeClock()
    session = FakeSession(handler)
    return RateLimitedSession(session, authenticated=authenticated,
                              sleep=clock.sleep, clock=clock), session, clock


def test_jobs_reads_are_their_own_scope():
    scope = RateLimitedSession._scope
    assert scope("GET", f"{JOBS}?ground_station=7&format=json") == "jobs"
    assert scope("HEAD", JOBS) == "jobs"
    # Checked before the observation feed, so it is never charged to that budget.
    assert scope("GET", f"{JOBS}?cursor=/observations/") == "jobs"
    assert scope("GET", OBSERVATIONS) == "observations"
    assert scope("POST", JOBS) is None


def test_the_jobs_budget_is_a_ceiling_of_its_own():
    """No server throttle exists on /jobs/, so this is our own courtesy
    ceiling - and it neither spends nor is spent by the observation budget."""
    limited, session, _clock = gate(always(200))
    for _ in range(OBSERVATION_LIST_PER_HOUR_AUTH):
        limited.request("GET", OBSERVATIONS)
    for _ in range(JOBS_LIST_PER_HOUR):
        limited.request("GET", JOBS)
    sent = len(session.calls)

    with pytest.raises(RateLimitedError, match="jobs read budget"):
        limited.request("GET", JOBS)
    with pytest.raises(RateLimitedError, match="observations read budget"):
        limited.request("GET", OBSERVATIONS)
    assert len(session.calls) == sent, "a read the budget had refused was sent anyway"


def test_a_jobs_slot_about_to_free_up_is_waited_for():
    limited, _session, clock = gate(always(200))
    for _ in range(JOBS_LIST_PER_HOUR):
        limited.request("GET", JOBS)
    clock.t += THROTTLE_WINDOW_S - 5

    assert limited.request("GET", JOBS).status_code == 200
    assert clock.slept == [5.0]


def test_a_short_retry_after_on_jobs_is_slept_out_and_retried():
    limited, session, clock = gate(
        lambda m, u, n: response(429, headers={"Retry-After": "7"}) if n == 1 else response(200))

    assert limited.request("GET", JOBS).status_code == 200
    assert clock.slept == [7.0]
    assert len(session.calls) == 2


def test_a_long_retry_after_on_jobs_blocks_jobs_for_every_client_whatever_its_token():
    """The jobs gate key carries no token, so one process-wide budget covers
    the token client and the anonymous one alike; the observation budget of
    each token stays its own."""
    a = client_with(jobs=always(429, headers={"Retry-After": "3600"}), token="token-a")
    b = client_with(jobs=always(200, []), observations=always(200, []), token="token-b")
    c = client_with(jobs=always(200, []), token="")

    with pytest.raises(RateLimitedError):
        a.jobs_session.request("GET", JOBS)
    for other in (b, c):
        with pytest.raises(RateLimitedError, match="no jobs reads"):
            other.jobs_session.request("GET", JOBS)
        assert other.jobs_session._session.calls == []
    # Not a stop for the observation feed, on any credential.
    assert b.session.request("GET", OBSERVATIONS).status_code == 200
