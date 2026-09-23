"""Asking other people's stations to record KNACKSAT-2, without wearing out
our welcome on network.satnogs.org.

Two separate things are pinned here, and they fail in opposite directions.

**The read budget.** SatNOGS throttles its list endpoints and publishes the
rates in its own settings: 60 observation-list reads an hour anonymously, 240
with a token, 256 station-list reads an hour. Reading one station's calendar
costs one of those, so a campaign over the whole catalogue runs out of budget
long before it runs out of stations. Getting this wrong does not produce an
error - it produces a 429, and enough of those is the end of the campaign. So
the rules are: obey `Retry-After` to the second, give up rather than retry in
a loop, and stop *before* asking when the budget is already spent, because a
request we know will be refused is the one request we should never send.

**What a throttle means to the caller.** `build_campaign` treats a station
whose calendar it cannot read as free, which is a reasonable bet for one
flaky station and a disaster for a rate limit: the budget is spent for every
station still to come, so carrying on would read the entire rest of the
catalogue as having an empty calendar and book on top of whatever is really
there. A rate limit therefore has to be a distinguishable thing, not just
another exception - which is what `RateLimitedError` is for.

The visibility tests alongside them use the real Skyfield propagator and a
frozen element set, because "this station is filtered out" is only worth
anything if the thing doing the filtering is the thing that ships.

Nothing here touches the network. `test_telemetry.py` stubs httpx with
`httpx.MockTransport`; this package is built on `requests`, which has no
equivalent, so the session is the seam instead - and what the fake session
hands back is a real `requests.Response`, so `Retry-After` and the cursor
`Link` header are read by the real parsing rather than by a dict pretending to
be headers. The clock is faked too: a test that pins a backoff must not pay
for it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from app.vendor.autoscheduler import campaign as camp
from app.vendor.autoscheduler.campaign import WINDOW_HARD_END_MIN, build_campaign
from app.vendor.autoscheduler.db_client import Tle, Transmitter
from app.vendor.autoscheduler.http import SatnogsHTTPError, paginate
from app.vendor.autoscheduler.http import request as http_request
from app.vendor.autoscheduler.network_client import (
    MAX_RETRY_AFTER_WAIT_S,
    MAX_THROTTLE_RETRIES,
    OBSERVATION_LIST_PER_HOUR_ANON,
    THROTTLE_BACKOFF_BASE_S,
    THROTTLE_WINDOW_S,
    NetworkClient,
    RateLimitedError,
    RateLimitedSession,
    Station,
)
from app.vendor.autoscheduler.predictor import Pass

MISSION = 67683
OBSERVATIONS = "https://network.satnogs.org/api/observations/"
STATIONS = "https://network.satnogs.org/api/stations/"

# The same frozen element set `test_predictor.py` works from, so these tests
# do not depend on the network or on what the satellite is doing today.
KNACKSAT2_TLE1 = "1 67683U 98067XZ  26255.31192122  .00056149  00000+0  48789-3 0  9994"
KNACKSAT2_TLE2 = "2 67683  51.6258 213.5681 0007959 152.6345 207.5073 15.68476422 33916"
# Two days after that epoch, so the propagator never calls the TLE stale
# however long from now these tests are run.
NOW = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# stand-ins
# --------------------------------------------------------------------------

def response(status: int, *, body=None, headers=None) -> requests.Response:
    """A real `requests.Response`, so header lookup is the real thing."""
    resp = requests.Response()
    resp.status_code = status
    resp.headers.update(headers or {})
    resp._content = json.dumps([] if body is None else body).encode()
    resp.encoding = "utf-8"
    return resp


class FakeSession:
    """The seam. Never opens a socket; records what it was asked for."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        self.calls.append((method, url))
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


def gate(handler, *, authenticated: bool = False):
    clock = FakeClock()
    session = FakeSession(handler)
    limited = RateLimitedSession(
        session, authenticated=authenticated, sleep=clock.sleep, clock=clock
    )
    return limited, session, clock


def always(status: int, **kwargs):
    return lambda method, url, n: response(status, **kwargs)


def ok_after(failures: int, status: int, **kwargs):
    """`failures` refusals, then a 200."""
    def handler(method, url, n):
        return response(status, **kwargs) if n <= failures else response(200)
    return handler


# --------------------------------------------------------------------------
# a 429 is an instruction, not a suggestion
# --------------------------------------------------------------------------

def test_a_429_is_waited_out_for_exactly_as_long_as_the_server_asked():
    """The server names its own wait. Guessing a shorter one earns another."""
    limited, session, clock = gate(
        ok_after(1, 429, headers={"Retry-After": "7"})
    )

    assert limited.request("GET", OBSERVATIONS).status_code == 200
    assert clock.slept == [7.0]
    assert len(session.calls) == 2


def test_a_429_without_a_usable_retry_after_still_backs_off():
    """No header, or one carrying an HTTP-date we cannot read, must not be
    taken as permission to retry immediately."""
    limited, session, clock = gate(ok_after(2, 429))

    limited.request("GET", OBSERVATIONS)

    assert clock.slept == [THROTTLE_BACKOFF_BASE_S, THROTTLE_BACKOFF_BASE_S * 2]
    assert len(session.calls) == 3


def test_an_http_date_retry_after_is_not_read_as_no_wait_at_all():
    limited, _session, clock = gate(
        ok_after(1, 429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    )

    limited.request("GET", OBSERVATIONS)

    assert clock.slept == [THROTTLE_BACKOFF_BASE_S]


def test_an_enormous_retry_after_is_capped_rather_than_blocked_on():
    """A very long wait is a signal to stop for now, not to hold a thread
    open for it."""
    limited, _session, clock = gate(always(429, headers={"Retry-After": "99999"}))

    with pytest.raises(RateLimitedError):
        limited.request("GET", OBSERVATIONS)

    assert clock.slept == [MAX_RETRY_AFTER_WAIT_S] * MAX_THROTTLE_RETRIES


def test_a_standing_429_gives_up_instead_of_storming():
    """The whole point. However long it keeps refusing, the number of times we
    ask is bounded and small."""
    limited, session, _clock = gate(always(429, headers={"Retry-After": "1"}))

    with pytest.raises(RateLimitedError):
        limited.request("GET", OBSERVATIONS)

    assert len(session.calls) == MAX_THROTTLE_RETRIES + 1
    # Pinned as a number as well as against the constant. That a bound exists
    # is not the claim - a bound of fifty is still a retry storm to whoever is
    # being asked fifty times.
    assert len(session.calls) <= 4


def test_a_400_is_not_a_rate_limit_and_is_not_retried():
    """A 4xx that is not a throttle still means what it always meant: the
    request was wrong, and repeating it wastes everyone's time."""
    limited, session, _clock = gate(always(400))

    with pytest.raises(SatnogsHTTPError) as caught:
        http_request(limited, "GET", OBSERVATIONS)

    assert not isinstance(caught.value, RateLimitedError)
    assert len(session.calls) == 1


# --------------------------------------------------------------------------
# spending the budget, and knowing when it is gone
# --------------------------------------------------------------------------

def spend_the_observation_budget(limited, session) -> None:
    for _ in range(OBSERVATION_LIST_PER_HOUR_ANON):
        limited.request("GET", OBSERVATIONS)
    assert len(session.calls) == OBSERVATION_LIST_PER_HOUR_ANON


def test_a_spent_budget_stops_us_before_the_server_has_to():
    """The request we know will be refused is the one we must not send: it
    buys nothing and it is what a ban is made of."""
    limited, session, _clock = gate(always(200))
    spend_the_observation_budget(limited, session)

    with pytest.raises(RateLimitedError):
        limited.request("GET", OBSERVATIONS)

    assert len(session.calls) == OBSERVATION_LIST_PER_HOUR_ANON


def test_a_token_buys_the_larger_budget():
    """60/hour anonymous against 240/hour authenticated - the same code has to
    know which one it is working inside."""
    limited, session, _clock = gate(always(200), authenticated=True)

    for _ in range(OBSERVATION_LIST_PER_HOUR_ANON + 1):
        limited.request("GET", OBSERVATIONS)

    assert len(session.calls) == OBSERVATION_LIST_PER_HOUR_ANON + 1


def test_the_two_endpoints_have_separate_budgets():
    """Station reads are throttled on their own, far more generous, scope.
    Spending the observation budget must not close the station feed."""
    limited, session, _clock = gate(always(200))
    spend_the_observation_budget(limited, session)

    assert limited.request("GET", STATIONS).status_code == 200


def test_booking_is_never_held_up_by_the_read_budget():
    """The server exempts POST from these throttles, and so must we: the one
    write this project makes cannot be refused over reads it already did."""
    limited, session, _clock = gate(always(200))
    spend_the_observation_budget(limited, session)

    assert limited.request("POST", OBSERVATIONS).status_code == 200
    assert ("POST", OBSERVATIONS) in session.calls


def test_every_page_of_a_crawl_spends_budget_not_just_the_first():
    """Why the session is the seam and not `http.request`: `paginate()` walks
    later pages by calling the session itself, so a gate hooked anywhere
    higher up would pace one page of a crawl and none of the rest."""
    pages = 3

    def handler(method, url, n):
        headers = ({"Link": f'<{OBSERVATIONS}?cursor=p{n}>; rel="next"'}
                   if n < pages else {})
        return response(200, body=[{"id": n}], headers=headers)

    limited, session, _clock = gate(handler)
    walked = list(paginate(limited, OBSERVATIONS))
    assert len(walked) == pages

    # The crawl is charged for every page it fetched, so only the remainder of
    # the hour's budget is left.
    for _ in range(OBSERVATION_LIST_PER_HOUR_ANON - pages):
        limited.request("GET", OBSERVATIONS)
    with pytest.raises(RateLimitedError):
        limited.request("GET", OBSERVATIONS)


def test_a_slot_that_has_aged_out_of_the_hour_comes_back():
    """The server counts over a sliding hour, so a budget that never refilled
    would lock us out of a campaign we were entitled to run."""
    limited, session, clock = gate(always(200))
    spend_the_observation_budget(limited, session)

    clock.t += THROTTLE_WINDOW_S + 1

    assert limited.request("GET", OBSERVATIONS).status_code == 200


def test_a_slot_about_to_free_up_is_waited_for_rather_than_refused():
    """Short waits are worth taking; it is only the long ones that are better
    reported than slept through."""
    limited, session, clock = gate(always(200))
    spend_the_observation_budget(limited, session)

    clock.t += THROTTLE_WINDOW_S - 10

    assert limited.request("GET", OBSERVATIONS).status_code == 200
    assert clock.slept == [10.0]


def test_the_client_wires_the_gate_in_for_every_read():
    """A gate nothing goes through is no gate at all."""
    settings = type("S", (), {"network_token": "", "network_base_url": "x"})()
    client = NetworkClient(settings, cache=object())

    assert isinstance(client.session, RateLimitedSession)


# --------------------------------------------------------------------------
# what a throttle means to the campaign
# --------------------------------------------------------------------------

def station(sid: int, *, lat: float = 13.7, lng: float = 100.5,
            min_horizon: float = 0.0, min_culmination: float = 0.0,
            status: str = "Online", connected: bool = True) -> Station:
    return Station(
        id=sid, name=f"Station {sid}", lat=lat, lng=lng, altitude_m=10.0,
        min_horizon=min_horizon, min_culmination=min_culmination,
        status=status, is_connected=connected,
    )


class StubDb:
    """The mission TLE, and a transmitter every station can hear."""

    def tles(self) -> dict[int, Tle]:
        return {MISSION: Tle(norad_cat_id=MISSION, name="KNACKSAT-2",
                             line1=KNACKSAT2_TLE1, line2=KNACKSAT2_TLE2,
                             updated="", source="frozen")}

    def transmitters_for_station(self, segments):
        return {MISSION: [Transmitter(
            uuid="tx-uuid", norad_cat_id=MISSION, description="UHF Telemetry",
            mode="FSK", baud=9600.0, downlink_hz=436_000_000, type="Transmitter",
            status="active", service="Amateur",
        )]}


class StubNetwork:
    """Stations, their calendars, and whatever each one raises instead."""

    def __init__(self, stations, raises=None) -> None:
        self.stations = stations
        self.raises = raises or {}
        self.asked: list[int] = []

    def all_stations(self):
        return list(self.stations)

    def future_bookings(self, station_id: int, now=None):
        self.asked.append(station_id)
        if station_id in self.raises:
            raise self.raises[station_id]
        return []


def synthetic_passes(monkeypatch, *windows):
    """Replace the propagator with passes the test chose itself.

    Used only where the point is what happens *after* prediction; the
    visibility tests below keep the real Skyfield one, because that is the
    whole claim they are making.
    """
    class StubPredictor:
        def __init__(self, lat, lng, altitude_m) -> None:
            pass

        def load_tles(self, tles, now=None) -> dict:
            return {}

        def passes_for(self, norad, start, end, min_horizon):
            return [
                Pass(norad_cat_id=MISSION, name="KNACKSAT-2", aos=aos, tca=aos,
                     los=los, max_el=45.0, aos_az=0.0, los_az=180.0)
                for aos, los in windows
            ]

    monkeypatch.setattr(camp, "Predictor", StubPredictor)


def run(network, **overrides):
    kwargs = dict(
        mission_norad=MISSION, transmitter_uuid=None, now=NOW,
        exclude_station_id=None, max_per_station=2, max_total=50,
    )
    kwargs.update(overrides)
    return build_campaign(network, StubDb(), **kwargs)


def test_a_rate_limit_stops_the_run_instead_of_treating_the_rest_as_free(monkeypatch):
    """The failure this whole arrangement exists to prevent.

    `build_campaign` reads a station it cannot reach as having an empty
    calendar. That is a fair bet for one unreachable station and a very bad
    one for a throttle, because the budget is spent for every station after it
    too - so the run would book on top of real observations across the entire
    rest of the catalogue. It stops instead.
    """
    synthetic_passes(monkeypatch,
                     (NOW + timedelta(hours=2), NOW + timedelta(hours=2, minutes=10)))
    network = StubNetwork(
        [station(1), station(2), station(3)],
        raises={2: RateLimitedError("429, budget spent", status=429)},
    )

    preview = run(network, max_per_station=1)

    assert [item.station_id for item in preview.items] == [1]
    assert network.asked == [1, 2]          # station 3 was never even asked
    assert any("rate-limit" in row["reason"] for row in preview.skipped)


def test_one_unreachable_station_does_not_stop_the_run(monkeypatch):
    """The pre-existing bet, still taken: a single station timing out is not a
    reason to abandon everyone else."""
    synthetic_passes(monkeypatch,
                     (NOW + timedelta(hours=2), NOW + timedelta(hours=2, minutes=10)))
    network = StubNetwork(
        [station(1), station(2), station(3)],
        raises={2: RuntimeError("station unreachable")},
    )

    preview = run(network, max_per_station=1)

    assert [item.station_id for item in preview.items] == [1, 2, 3]
    assert network.asked == [1, 2, 3]


# --------------------------------------------------------------------------
# only asking stations that can actually see it
# --------------------------------------------------------------------------

def test_a_station_that_can_never_see_the_satellite_is_skipped():
    """KNACKSAT-2 is in a 51.6 degree inclination orbit, so it does not rise
    from the high Arctic at all. Asking that station to record it would spend
    a booking on a guaranteed empty waterfall.

    This runs the real propagator: the filter is only worth anything if the
    thing being tested is the thing that ships.
    """
    network = StubNetwork([station(1, lat=85.0, lng=20.0)])

    preview = run(network)

    assert preview.items == []
    assert preview.skipped[0]["reason"] == "no qualifying pass in the campaign window"


def test_a_station_whose_horizon_the_satellite_never_clears_is_skipped():
    """`min_horizon` is the station owner's own published figure, and a pass
    that never gets above it is one that station cannot record."""
    reachable = StubNetwork([station(1, min_horizon=0.0)])
    walled_in = StubNetwork([station(1, min_horizon=89.0)])

    assert run(reachable).items != []
    assert run(walled_in).items == []


def test_a_pass_below_the_stations_culmination_floor_is_skipped():
    """The other published figure: a station that will not take a pass under
    N degrees will not take ours either."""
    assert run(StubNetwork([station(1, min_culmination=89.0)])).items == []


def test_only_stations_the_network_calls_schedulable_are_ever_offered_one():
    """`all_stations()` filters on this before `build_campaign` sees anything.
    An Offline station will never record what it accepts, and a Testing one
    refuses everybody but its owner."""
    assert station(1).schedulable
    assert not station(1, status="Offline").schedulable
    assert not station(1, status="Testing").schedulable
    assert not station(1, connected=False).schedulable


def test_all_stations_hands_back_only_the_schedulable_ones():
    rows = [
        {"id": 1, "name": "online", "lat": 13.7, "lng": 100.5, "altitude": 10,
         "status": "Online", "is_connected": True, "antenna": []},
        {"id": 2, "name": "offline", "lat": 13.7, "lng": 100.5, "altitude": 10,
         "status": "Offline", "is_connected": True, "antenna": []},
        {"id": 3, "name": "testing", "lat": 13.7, "lng": 100.5, "altitude": 10,
         "status": "Testing", "is_connected": True, "antenna": []},
    ]
    limited, _session, _clock = gate(always(200, body=rows))

    class PassThroughCache:
        def get_or_fetch(self, key, ttl_s, fetch):
            return fetch()

    settings = type("S", (), {"network_token": "", "network_base_url": "x"})()
    client = NetworkClient(settings, PassThroughCache())
    client.session = limited

    assert [s.id for s in client.all_stations()] == [1]


# --------------------------------------------------------------------------
# the server's own edge on how far ahead a booking may reach
# --------------------------------------------------------------------------

def test_a_pass_running_past_the_servers_edge_is_trimmed_to_it(monkeypatch):
    """`check_end_datetime()` refuses anything ending more than 2900 minutes
    from now, and it is the *end* it looks at. A pass rising just inside our
    own 2880-minute horizon can still set half an hour later, which is past
    that edge - so the recording is trimmed rather than submitted as a
    booking the server is bound to reject.
    """
    aos = NOW + timedelta(minutes=2890)
    synthetic_passes(monkeypatch, (aos, NOW + timedelta(minutes=2920)))

    preview = run(StubNetwork([station(1)]))

    (item,) = preview.items
    assert item.start == aos
    assert item.end == NOW + timedelta(minutes=WINDOW_HARD_END_MIN)
    assert item.end < NOW + timedelta(minutes=2900)


def test_a_pass_comfortably_inside_the_window_is_left_alone(monkeypatch):
    """Trimming is for the edge case only; an ordinary pass is submitted whole."""
    aos = NOW + timedelta(hours=5)
    los = aos + timedelta(minutes=11)
    synthetic_passes(monkeypatch, (aos, los))

    (item,) = run(StubNetwork([station(1)])).items

    assert (item.start, item.end) == (aos, los)


def test_a_sliver_left_after_trimming_is_not_worth_a_booking(monkeypatch):
    """Trim a pass down to a handful of seconds and what is left is not an
    observation - it is a station's per-run cap spent on nothing."""
    synthetic_passes(monkeypatch, (NOW + timedelta(minutes=2898, seconds=30),
                                   NOW + timedelta(minutes=2960)))

    preview = run(StubNetwork([station(1)]))

    assert preview.items == []
    assert preview.skipped[0]["reason"] == "no qualifying pass in the campaign window"
