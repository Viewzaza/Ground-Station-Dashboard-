"""How many STATIONS one campaign run reaches.

The operator's question is "how many stations does one click cover", and the
answer was being decided by a constant that does not mention stations.
`max_total` is documented and presented as a cap on observations, but its break
ended the station walk, so with the shipped defaults - 150 items, 2 per station
- a run stopped after 75 stations out of the 200-300 that can hear the
spacecraft. It bound long before the read budget did, and the knob an operator
would reach for to cover more made it worse: 3 per station meant 50 stations.

The second half is the walk order. `all_stations()` preserves the API's
id-ascending order and is cached for an hour, so every run that was cut short
covered the same prefix. Clicking again re-read the stations already done and
stopped in the same place - the tail was unreachable by any number of clicks,
which is a different problem from being slow to cover.

These tests hold both down. Nothing here raises how much any one station is
asked for: `max_per_station` is the courtesy limit and is untouched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.vendor.autoscheduler import campaign as cam
from app.vendor.autoscheduler.campaign import build_campaign

# Within a day of the element set below. `build_campaign` refuses a TLE older
# than a fortnight, and a stale one skips every station before its calendar is
# ever read - which makes each assertion here pass against zero work done.
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
KNACKSAT2 = 67683


class Seg:
    def __init__(self) -> None:
        self.lower = 400_000_000.0
        self.upper = 440_000_000.0


class Stn:
    def __init__(self, sid: int) -> None:
        self.id = sid
        self.name = f"station-{sid}"
        self.lat = 13.8
        self.lng = 100.5
        self.altitude_m = 60.0
        self.min_horizon = 0.0
        self.min_culmination = 0.0
        self.segments = [Seg()]
        self.future_observations = 0


class Tx:
    """The fields `pick_transmitter` ranks on, and the ones the item carries."""

    uuid = "tx-uuid"
    norad_cat_id = KNACKSAT2
    description = "UHF Telemetry"
    mode = "FSK"
    baud = 9600.0
    downlink_hz = 400_630_000
    type = "Transmitter"
    status = "active"
    service = "Amateur"


class Tle:
    """A real element set, so Skyfield parses it. Which orbit it is does not
    matter here — `passes_for` is stubbed — but it has to be well-formed, and
    a bare object() gets skipped as an unparseable TLE before the station is
    ever read, which silently makes every assertion below vacuous."""

    name = "TEST SAT"
    line1 = "1 69015U 26100AM  26259.49075841  .00006570  00000-0  28250-3 0  9990"
    line2 = "2 69015  97.3861 155.8586 0009983  83.9560 276.2814 15.22937493 20710"


class StubDb:
    def tles(self):
        return {KNACKSAT2: Tle()}

    def transmitters_for_station(self, segments):
        return {KNACKSAT2: [Tx()]}


class StubNetwork:
    """Counts which stations actually had their calendar read."""

    def __init__(self, stations) -> None:
        self._stations = stations
        self.read = []

    def all_stations(self):
        return list(self._stations)

    def future_bookings(self, station_id, now=None):
        self.read.append(station_id)
        return []


class FakePass:
    def __init__(self, aos, los, max_el) -> None:
        self.aos, self.los, self.max_el = aos, los, max_el


def synthetic_passes(monkeypatch, per_station=2):
    """Every station sees the same passes, so station count is the only variable."""
    def passes_for(self, norad, start, end, min_horizon):
        out = []
        for i in range(per_station):
            aos = NOW + timedelta(hours=2 + i * 4)
            out.append(FakePass(aos, aos + timedelta(minutes=10), 40.0 + i * 10))
        return out

    monkeypatch.setattr(cam.Predictor, "passes_for", passes_for, raising=True)


def run(network, *, max_per_station=2, max_total=150, start_offset=0):
    return build_campaign(
        network, StubDb(), mission_norad=KNACKSAT2, transmitter_uuid=None,
        now=NOW, exclude_station_id=None, max_per_station=max_per_station,
        max_total=max_total, recent_attempts=None, start_offset=start_offset,
    )


# --------------------------------------------------------------------------
# max_total must not cap breadth
# --------------------------------------------------------------------------

def test_the_item_cap_no_longer_ends_the_station_walk(monkeypatch):
    """The headline regression. 100 stations x 2 passes against a 150-item cap
    used to stop at station 75; every station after it was never looked at."""
    synthetic_passes(monkeypatch)
    net = StubNetwork([Stn(i) for i in range(1, 101)])

    run(net, max_per_station=2, max_total=150)

    assert len(net.read) == 100, "the walk stopped early at the item cap"


def test_a_full_basket_still_stops_adding_items(monkeypatch):
    """max_total is still a real backstop on how much gets submitted - it just
    no longer decides how far the walk goes."""
    synthetic_passes(monkeypatch)
    net = StubNetwork([Stn(i) for i in range(1, 101)])

    preview = run(net, max_per_station=2, max_total=150)

    assert len(preview.items) == 150


def test_asking_each_station_for_more_no_longer_costs_stations(monkeypatch):
    """The knob that used to work backwards. At 3 per station the old outer
    break fired after 50 stations instead of 75, so raising it covered FEWER
    stations. Breadth is now independent of depth."""
    synthetic_passes(monkeypatch, per_station=3)
    a, b = StubNetwork([Stn(i) for i in range(1, 101)]), StubNetwork([Stn(i) for i in range(1, 101)])

    run(a, max_per_station=2, max_total=150)
    run(b, max_per_station=3, max_total=150)

    assert len(a.read) == len(b.read) == 100


def test_the_courtesy_limit_per_station_is_untouched(monkeypatch):
    """Breadth is the goal; depth on somebody else's station is not. No station
    may be asked for more than max_per_station however wide the run gets."""
    synthetic_passes(monkeypatch, per_station=5)
    net = StubNetwork([Stn(i) for i in range(1, 21)])

    preview = run(net, max_per_station=2, max_total=10_000)

    per_station = {}
    for item in preview.items:
        per_station[item.station_id] = per_station.get(item.station_id, 0) + 1
    assert per_station and max(per_station.values()) == 2


# --------------------------------------------------------------------------
# rotation: repeated clicks must reach new stations
# --------------------------------------------------------------------------

def test_two_runs_without_rotation_cover_the_same_stations(monkeypatch):
    """The baseline this exists to fix, asserted so the rotation test below
    is demonstrably doing something."""
    synthetic_passes(monkeypatch)
    a, b = StubNetwork([Stn(i) for i in range(1, 51)]), StubNetwork([Stn(i) for i in range(1, 51)])

    run(a, max_total=20)
    run(b, max_total=20)

    assert a.read == b.read


def test_an_offset_run_starts_further_into_the_catalogue(monkeypatch):
    """A second click with the cursor advanced must begin where the first got
    to, rather than re-reading the same prefix."""
    synthetic_passes(monkeypatch)
    net = StubNetwork([Stn(i) for i in range(1, 51)])

    run(net, start_offset=10)

    assert net.read[0] == 11


def test_rotation_wraps_around_rather_than_running_off_the_end(monkeypatch):
    """The cursor is persisted and the catalogue changes size between runs, so
    an offset past the end must wrap, not return nothing."""
    synthetic_passes(monkeypatch)
    net = StubNetwork([Stn(i) for i in range(1, 11)])

    run(net, start_offset=13)

    assert net.read[0] == 4
    assert sorted(net.read) == list(range(1, 11))


def test_rotation_visits_every_station_exactly_once(monkeypatch):
    """Rotating the start must not drop or duplicate anyone."""
    synthetic_passes(monkeypatch)
    net = StubNetwork([Stn(i) for i in range(1, 51)])

    run(net, start_offset=17)

    assert sorted(net.read) == list(range(1, 51))


def test_no_offset_is_the_old_behaviour(monkeypatch):
    """Default must be a no-op, so nothing changes for a caller that has not
    adopted the cursor yet."""
    synthetic_passes(monkeypatch)
    net = StubNetwork([Stn(i) for i in range(1, 21)])

    run(net, start_offset=0)

    assert net.read == list(range(1, 21))


@pytest.mark.parametrize("offset", [0, 1, 7, 19, 20, 41])
def test_any_offset_is_safe_on_a_small_catalogue(monkeypatch, offset):
    """This runs against whatever the API returned, including the degenerate
    sizes - an off-by-one here silently skips a station on every run."""
    synthetic_passes(monkeypatch)
    net = StubNetwork([Stn(i) for i in range(1, 21)])

    run(net, start_offset=offset)

    assert sorted(net.read) == list(range(1, 21))


def test_an_empty_catalogue_does_not_divide_by_zero(monkeypatch):
    """`start_offset % len(stations)` with nothing to walk."""
    synthetic_passes(monkeypatch)
    net = StubNetwork([])

    preview = run(net, start_offset=5)

    assert preview.items == []
