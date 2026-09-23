"""What the network campaign chooses, and why.

build_campaign had no tests for its selection at all: a review mutated it
thirteen ways and eight of the mutations survived the entire suite. That is how
a per-run rotation that never rotated (it advanced by 24 on a 24-hour timer, so
for any station count dividing 24 it stood still) and a band balancer that
booked a 3.7 degree pass over an 88 degree one both shipped. Each test below
names the specific wrong behaviour it exists to catch.

The predictor is replaced with one that gives each station its OWN passes,
keyed by station, because selection is precisely about choosing between
stations whose passes differ. Real Skyfield geometry is exercised elsewhere.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.vendor.autoscheduler import campaign as camp
from app.vendor.autoscheduler.campaign import (
    ELEVATION_BAND_FLOORS,
    MAX_ELEVATION_SACRIFICE_DEG,
    build_campaign,
)
from app.vendor.autoscheduler.db_client import Tle, Transmitter
from app.vendor.autoscheduler.network_client import Booking, RateLimitedError, Station
from app.vendor.autoscheduler.predictor import Pass

MISSION = 67683
NOW = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)
TLE1 = "1 67683U 26001A   26256.50000000  .00010000  00000-0  50000-3 0  9990"
TLE2 = "2 67683  97.4000 100.0000 0010000  90.0000 270.0000 15.20000000 10000"


def station(sid: int) -> Station:
    # lat carries the id, so the predictor below can tell stations apart.
    return Station(id=sid, name=f"Station {sid}", lat=float(sid), lng=100.0,
                   altitude_m=10.0, min_horizon=0.0, min_culmination=0.0,
                   status="Online", is_connected=True)


class Db:
    def tles(self):
        return {MISSION: Tle(norad_cat_id=MISSION, name="KNACKSAT-2", line1=TLE1,
                             line2=TLE2, updated="", source="frozen")}

    def transmitters_for_station(self, segments):
        return {MISSION: [Transmitter(
            uuid="tx", norad_cat_id=MISSION, description="UHF", mode="FSK",
            baud=9600.0, downlink_hz=436_000_000, type="Transmitter",
            status="active", service="Amateur",
        )]}


class Net:
    """Stations; optional existing bookings per station; optional read budget."""

    def __init__(self, stations, bookings=None, budget=None):
        self.stations = stations
        self.bookings = bookings or {}
        self.budget = budget
        self.asked: list[int] = []      # reads that succeeded
        self.attempts: list[int] = []   # every read tried, refused ones included

    def all_stations(self):
        return list(self.stations)

    def future_bookings(self, station_id, now=None):
        # Recorded before refusing: against the real API each attempt past the
        # limit is a request that earns a 429 and a Retry-After sleep of up to
        # 120 s, so an attempt that fails is not free.
        self.attempts.append(station_id)
        if self.budget is not None and len(self.asked) >= self.budget:
            raise RateLimitedError("read budget spent", status=429)
        self.asked.append(station_id)
        return list(self.bookings.get(station_id, []))


def passes_by_station(monkeypatch, table):
    """table: station_id -> [(hours_from_now, max_el), ...], 10-minute passes."""
    class P:
        def __init__(self, lat, lng, alt):
            self.sid = int(lat)

        def load_tles(self, tles, now=None):
            return {}

        def passes_for(self, norad, start, end, min_horizon):
            out = []
            for hours, el in table.get(self.sid, []):
                aos = NOW + timedelta(hours=hours)
                out.append(Pass(norad_cat_id=MISSION, name="K2", aos=aos, tca=aos,
                                los=aos + timedelta(minutes=10), max_el=el,
                                aos_az=0.0, los_az=180.0))
            return out

    monkeypatch.setattr(camp, "Predictor", P)


def run(net, *, now=NOW, max_total=50, max_per_station=2):
    return build_campaign(net, Db(), mission_norad=MISSION, transmitter_uuid=None,
                          now=now, exclude_station_id=None,
                          max_per_station=max_per_station, max_total=max_total)


def spread(n, per=3, el=45.0, gap=5):
    """n stations, each with `per` non-overlapping passes at elevation `el`."""
    return {sid: [(1 + gap * k, el) for k in range(per)] for sid in range(1, n + 1)}


# --- the caps -----------------------------------------------------------------

def test_the_cap_buys_breadth_before_depth(monkeypatch):
    """Catches: depth-first selection. With 40 stations and a budget of 40,
    every station gets one before any gets a second - 40 stations, not 14x3."""
    passes_by_station(monkeypatch, spread(40, per=3))
    preview = run(Net([station(i) for i in range(1, 41)]), max_total=40, max_per_station=3)
    booked = [item.station_id for item in preview.items]
    assert len(booked) == 40
    assert len(set(booked)) == 40


@pytest.mark.parametrize("n,total,per", [(10, 25, 2), (30, 7, 3), (5, 100, 4), (50, 50, 1)])
def test_neither_cap_is_ever_exceeded(monkeypatch, n, total, per):
    passes_by_station(monkeypatch, spread(n, per=6))
    preview = run(Net([station(i) for i in range(1, n + 1)]),
                  max_total=total, max_per_station=per)
    assert len(preview.items) <= total
    counts = {}
    for item in preview.items:
        counts[item.station_id] = counts.get(item.station_id, 0) + 1
    assert max(counts.values()) <= per


# --- fairness across runs -----------------------------------------------------

def test_consecutive_runs_reach_different_stations(monkeypatch):
    """Catches: a fixed or missing shuffle. Two runs a day apart must not book
    the same stations, and a week of runs must reach well past one run's cap."""
    passes_by_station(monkeypatch, spread(60, per=2))
    stations = [station(i) for i in range(1, 61)]
    days = [{i.station_id for i in run(Net(stations), now=NOW + timedelta(days=d),
                                        max_total=10, max_per_station=1).items}
            for d in range(7)]
    assert days[0] != days[1]
    assert len(set().union(*days)) > 30


@pytest.mark.parametrize("n", [24, 12, 48])
def test_the_rotation_is_not_locked_to_a_daily_cadence(monkeypatch, n):
    """Catches: the hour-index rotation that shipped. The campaign timer is
    86400 s, so an offset of int(timestamp // 3600) advances by 24 per run and
    reaches only n/gcd(n, 24) start positions - none at all for n dividing 24.
    With 24 stations and a cap of 10 it booked the same ten every day forever."""
    passes_by_station(monkeypatch, spread(n, per=2))
    stations = [station(i) for i in range(1, n + 1)]
    seen = set()
    for d in range(14):
        seen |= {i.station_id for i in run(Net(stations), now=NOW + timedelta(days=d),
                                           max_total=10, max_per_station=1).items}
    assert len(seen) >= min(n, 20), f"only {len(seen)} of {n} stations ever booked"


# --- elevation ----------------------------------------------------------------

def test_a_station_never_trades_a_good_pass_for_a_grazing_one(monkeypatch):
    """Catches: unbounded band balancing. It once booked a 3.7 degree pass for a
    station that had an 88 degree one, because the low band happened to be the
    thinnest when that station's turn came."""
    table = {sid: [(1, 80.0), (8, 78.0)] for sid in range(1, 30)}   # fill the high bands
    table[99] = [(3, 88.0), (12, 3.7)]
    passes_by_station(monkeypatch, table)
    preview = run(Net([station(i) for i in [*range(1, 30), 99]]),
                  max_total=30, max_per_station=1)
    mine = [i.max_elevation_deg for i in preview.items if i.station_id == 99]
    assert mine, "station 99 should be booked"
    assert mine[0] >= 88.0 - MAX_ELEVATION_SACRIFICE_DEG


def test_bands_are_balanced_when_it_costs_nothing(monkeypatch):
    """Catches: removing the band term. Every station offers 80 and 62 degrees,
    both within the sacrifice bound. Best-first alone would book only 80s; the
    spread rule should use both bands."""
    passes_by_station(monkeypatch, {sid: [(1, 80.0), (9, 62.0)] for sid in range(1, 41)})
    preview = run(Net([station(i) for i in range(1, 41)]), max_total=40, max_per_station=1)
    bands = {camp._band_of(i.max_elevation_deg) for i in preview.items}
    assert {0, 1} <= bands, f"bands used: {sorted(bands)}"
    assert len(ELEVATION_BAND_FLOORS) == 6


# --- conflicts ----------------------------------------------------------------

def test_a_pass_that_overlaps_an_existing_booking_is_never_booked(monkeypatch):
    passes_by_station(monkeypatch, {1: [(2, 60.0), (10, 50.0)]})
    taken = Booking(id=1, norad_cat_id=0, start=NOW + timedelta(hours=2, minutes=3),
                    end=NOW + timedelta(hours=2, minutes=20), status="future")
    preview = run(Net([station(1)], bookings={1: [taken]}), max_per_station=2)
    starts = [i.start for i in preview.items]
    assert NOW + timedelta(hours=2) not in starts
    assert NOW + timedelta(hours=10) in starts


def test_two_overlapping_passes_on_one_station_are_never_both_booked(monkeypatch):
    """Catches: dropping the in-selection re-check. The first pick has to go on
    the station's calendar so the second cannot land on top of it."""
    passes_by_station(monkeypatch, {1: [(2, 60.0), (2.05, 59.0)]})
    preview = run(Net([station(1)]), max_per_station=2)
    assert len(preview.items) == 1


# --- reads --------------------------------------------------------------------

def test_calendars_are_read_only_for_stations_about_to_be_picked(monkeypatch):
    """Catches: eager reads. Reading every calendar spent the whole SatNOGS read
    budget on every run and cut the plan to the lowest station ids."""
    passes_by_station(monkeypatch, spread(300, per=2))
    net = Net([station(i) for i in range(1, 301)])
    preview = run(net, max_total=50, max_per_station=1)
    assert len(preview.items) == 50
    assert len(net.asked) <= 60, f"{len(net.asked)} calendars read for a 50-item plan"
    assert preview.calendars_read == len(net.asked)


def test_a_throttle_stops_reading_but_keeps_what_was_read(monkeypatch):
    """Catches both ways of getting this wrong: dropping the stations already
    read (a throttle becomes an empty plan) and carrying on reading (booking
    over calendars nobody fetched)."""
    passes_by_station(monkeypatch, spread(50, per=2))
    net = Net([station(i) for i in range(1, 51)], budget=10)
    preview = run(net, max_total=50, max_per_station=1)
    booked = {i.station_id for i in preview.items}
    assert booked, "the stations read before the throttle must still be booked"
    assert booked <= set(net.asked), "a station was booked without its calendar being read"
    assert len(net.asked) == 10
    assert len(net.attempts) == 11, (
        f"{len(net.attempts) - 11} read(s) attempted after the throttle - each is a "
        "429 and a Retry-After sleep against the real API"
    )
    assert preview.stopped_early is not None
    assert preview.stopped_early["unread_stations"] == 40


def test_a_throttled_sample_is_not_the_lowest_ids(monkeypatch):
    """Catches: reading in arrival order. The part that fits inside a budget
    must be a sample of the network, not its oldest corner."""
    passes_by_station(monkeypatch, spread(300, per=1))
    net = Net([station(i) for i in range(1, 301)], budget=40)
    run(net, max_total=300, max_per_station=1)
    assert max(net.asked) > 150


# --- what the operator is told --------------------------------------------------

def test_every_unbooked_station_is_given_a_true_reason(monkeypatch):
    table = spread(20, per=1)
    table[5] = [(2, 60.0)]
    passes_by_station(monkeypatch, table)
    taken = Booking(id=9, norad_cat_id=0, start=NOW + timedelta(hours=1),
                    end=NOW + timedelta(hours=3), status="future")
    preview = run(Net([station(i) for i in range(1, 21)], bookings={5: [taken]}),
                  max_total=5, max_per_station=1)
    reasons = {row["station_id"]: row["reason"] for row in preview.skipped}
    booked = {i.station_id for i in preview.items}
    for sid in range(1, 21):
        assert sid in booked or sid in reasons, f"station {sid} vanished without a reason"
    # Station 5's only pass sits inside an existing booking, so it may truly be
    # "every qualifying pass conflicts". Nobody else may be told that - the
    # others only lost on budget, and the old code said "conflicts" for both.
    assert not any("every qualifying pass conflicts" in r
                   for sid, r in reasons.items() if sid != 5), (
        "a station that merely lost on budget was reported as fully conflicted"
    )


def test_items_come_out_sorted_by_station_then_time(monkeypatch):
    passes_by_station(monkeypatch, spread(30, per=3))
    preview = run(Net([station(i) for i in range(1, 31)]), max_total=60, max_per_station=3)
    keys = [(i.station_id, i.start) for i in preview.items]
    assert keys == sorted(keys)


def test_considered_stations_is_what_was_examined(monkeypatch):
    passes_by_station(monkeypatch, spread(25, per=1))
    preview = run(Net([station(i) for i in range(1, 26)]), max_total=5, max_per_station=1)
    assert preview.considered_stations == 25
    assert preview.stopped_early is None
