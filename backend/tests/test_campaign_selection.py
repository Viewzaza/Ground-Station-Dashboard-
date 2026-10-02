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

import dataclasses
import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.vendor.autoscheduler import campaign as camp
from app.vendor.autoscheduler.campaign import (
    ELEVATION_BAND_FLOORS,
    ELEVATION_BAND_LABELS,
    MAX_ELEVATION_SACRIFICE_DEG,
    WINDOW_HARD_END_MIN,
    WINDOW_START_MARGIN_MIN,
    CampaignItem,
    CampaignPreview,
    _campaign_preview_payload,
    band_counts_payload,
    build_campaign,
)
from app.vendor.autoscheduler.db_client import Tle, Transmitter, frequency_is_covered
from app.vendor.autoscheduler.network_client import (
    Antenna,
    Booking,
    RateLimitedError,
    Station,
)
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


def build(net, db=None, **overrides):
    """build_campaign with every keyword open to the test."""
    kwargs = dict(mission_norad=MISSION, transmitter_uuid=None, now=NOW,
                  exclude_station_id=None, max_per_station=2, max_total=50)
    kwargs.update(overrides)
    return build_campaign(net, db or Db(), **kwargs)


def per_station(preview) -> dict[int, int]:
    counts: dict[int, int] = {}
    for item in preview.items:
        counts[item.station_id] = counts.get(item.station_id, 0) + 1
    return counts


def mission_obs(hours: float, minutes: float = 10, norad: int = MISSION) -> Booking:
    """An observation already on a station's calendar, `hours` from NOW."""
    start = NOW + timedelta(hours=hours)
    return Booking(id=int(hours * 100), norad_cat_id=norad, start=start,
                   end=start + timedelta(minutes=minutes), status="future")


# KNACKSAT-2's two real downlinks, by their real SatNOGS DB uuids.
TEL = "UatCXtfDnoBPeVBGHgj4Bc"     # 400.630 MHz telemetry
DIGI = "JR28wAEjmpuDQ4FrPWAiwf"    # 145.825 MHz V/V digipeater
VHF = Antenna(band="VHF", low_hz=144e6, high_hz=148e6, kind="yagi")
UHF_400 = Antenna(band="UHF", low_hz=400e6, high_hz=403e6, kind="yagi")
# Why 81 of the 102 stations that hear neither downlink hear neither: UHF that
# starts above 400.630 MHz, most often at 430.
UHF_AMATEUR = Antenna(band="UHF", low_hz=430e6, high_hz=440e6, kind="yagi")


class RadioDb(Db):
    """Both downlinks, each heard only by an antenna covering its frequency -
    the same test the SatNOGS server applies. Digipeater listed first, as the
    DB lists it: that is the order pick_transmitter's tie-break falls back on,
    since both are 9600 baud and neither is a transponder."""

    TRANSMITTERS = [
        Transmitter(uuid=DIGI, norad_cat_id=MISSION,
                    description="Mode V/V - FSK9k6 - Digipeater", mode="FSK",
                    baud=9600.0, downlink_hz=145_825_000, type="Transceiver",
                    status="active", service="Amateur"),
        Transmitter(uuid=TEL, norad_cat_id=MISSION,
                    description="Mode U - FSK9k6 - AX.25 G3RUH - TLM", mode="FSK",
                    baud=9600.0, downlink_hz=400_630_000, type="Transmitter",
                    status="active", service="Amateur"),
    ]

    def transmitters_for_station(self, segments):
        return {MISSION: [tx for tx in self.TRANSMITTERS
                          if frequency_is_covered(tx.downlink_hz, segments)]}


def radio_station(sid: int, *antennas: Antenna) -> Station:
    return dataclasses.replace(station(sid), antennas=list(antennas))


def radio_network():
    """1 hears both, 2 only the digipeater, 3 only telemetry, 4 neither."""
    return Net([radio_station(1, VHF, UHF_400), radio_station(2, VHF),
                radio_station(3, UHF_400), radio_station(4, UHF_AMATEUR)])


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


# --- looped commits: the allowance ---------------------------------------------

def test_a_looped_round_never_exceeds_a_stations_remaining_allowance(monkeypatch):
    """Catches: selection ignoring booked_counts. A looped commit calls
    build_campaign again with what each station was already given; the cap is
    per station across ALL rounds, so a station with 1 of its 2 already booked
    may take one more, not two. Phase 1 only skips stations that are FULLY
    capped, so a partially booked station reaches selection and is the case
    that needs this."""
    passes_by_station(monkeypatch, {1: [(1, 70.0), (6, 60.0), (11, 50.0)],
                                    2: [(2, 70.0), (7, 60.0), (12, 50.0)]})
    preview = build_campaign(Net([station(1), station(2)]), Db(), mission_norad=MISSION,
                             transmitter_uuid=None, now=NOW, exclude_station_id=None,
                             max_per_station=2, max_total=50, booked_counts={1: 1})
    counts = {}
    for item in preview.items:
        counts[item.station_id] = counts.get(item.station_id, 0) + 1
    assert counts.get(1, 0) == 1, f"station 1 had 1 of 2 already and got {counts.get(1, 0)} more"
    assert counts.get(2, 0) == 2


def test_a_fully_booked_station_is_skipped_before_any_read(monkeypatch):
    """The loop session's read economy, kept: a station already at its cap is
    passed over before its calendar is ever fetched."""
    passes_by_station(monkeypatch, spread(3, per=3))
    net = Net([station(1), station(2), station(3)])
    build_campaign(net, Db(), mission_norad=MISSION, transmitter_uuid=None, now=NOW,
                   exclude_station_id=None, max_per_station=2, max_total=50,
                   booked_counts={2: 2})
    assert 2 not in net.attempts


# --- which downlink each station records -----------------------------------------

def test_the_primary_wins_wherever_it_is_heard_and_the_fallback_fills_the_rest(monkeypatch):
    """Catches: a fallback that is not ordered, or that is not a fallback. The
    stations that hear both downlinks must record telemetry (pick_transmitter's
    own tie-break would hand them the digipeater, DB order), the ones that hear
    only the digipeater get it and are marked as the weaker kind, and a station
    that hears neither is told so in words that mention the fallback too."""
    passes_by_station(monkeypatch, spread(4, per=1))
    preview = build(radio_network(), RadioDb(), transmitter_uuid=TEL,
                    fallback_transmitter_uuids=[DIGI], max_per_station=1)

    chosen = {i.station_id: (i.transmitter_uuid, i.is_fallback) for i in preview.items}
    assert chosen == {1: (TEL, False), 2: (DIGI, True), 3: (TEL, False)}
    by_station = {i.station_id: i for i in preview.items}
    assert by_station[2].transmitter_description == "Mode V/V - FSK9k6 - Digipeater"
    assert by_station[1].transmitter_description.endswith("TLM")
    reasons = {row["station_id"]: row["reason"] for row in preview.skipped}
    assert reasons == {4: "none of the campaign's transmitters is in the station's antenna range"}


@pytest.mark.parametrize("fallbacks", [None, [], [TEL]])
def test_without_a_real_fallback_the_primary_is_still_a_hard_filter(monkeypatch, fallbacks):
    """Today's behaviour, kept exactly - including when the "fallback" is just
    the primary again, which offers no other downlink."""
    passes_by_station(monkeypatch, spread(4, per=1))
    preview = build(radio_network(), RadioDb(), transmitter_uuid=TEL,
                    fallback_transmitter_uuids=fallbacks, max_per_station=1)

    assert {i.station_id for i in preview.items} == {1, 3}
    assert {i.transmitter_uuid for i in preview.items} == {TEL}
    assert not any(i.is_fallback for i in preview.items)
    reasons = {row["station_id"]: row["reason"] for row in preview.skipped}
    assert reasons[2] == reasons[4] == "the pinned transmitter is not in the station's antenna range"


def test_a_fallback_station_does_not_log_a_false_priority_file_warning(monkeypatch, caplog):
    """Catches: implementing the fallback as pick_transmitter(candidates, TEL).
    It does pick the right transmitter, but for every station lacking TEL it
    warns that a "priority file pins transmitter" it cannot find - ~77 false
    warnings on every build, repeated each round of a looped commit."""
    passes_by_station(monkeypatch, spread(4, per=1))
    with caplog.at_level(logging.WARNING):
        preview = build(radio_network(), RadioDb(), transmitter_uuid=TEL,
                        fallback_transmitter_uuids=[DIGI], max_per_station=1)
    assert any(i.is_fallback for i in preview.items)
    assert not [r for r in caplog.records if "priority file" in r.getMessage()]


def test_fallbacks_mean_nothing_without_a_primary(monkeypatch):
    """The "any downlink" policy: pick_transmitter chooses, exactly as before
    (DB order breaks the tie, so the both-station records the digipeater), and
    with no primary nothing is a fallback - nor does the preview claim one was
    applied."""
    passes_by_station(monkeypatch, spread(4, per=1))
    preview = build(radio_network(), RadioDb(), transmitter_uuid=None,
                    fallback_transmitter_uuids=[DIGI], max_per_station=1)

    chosen = {i.station_id: i.transmitter_uuid for i in preview.items}
    assert chosen == {1: DIGI, 2: DIGI, 3: TEL}
    assert not any(i.is_fallback for i in preview.items)
    assert preview.params["fallback_transmitter_uuids"] == []
    reasons = {row["station_id"]: row["reason"] for row in preview.skipped}
    assert reasons[4] == "no transmitter for this satellite is in the station's antenna range"


# --- the calendar cache ------------------------------------------------------------

def test_a_cached_calendar_is_used_instead_of_a_read(monkeypatch):
    """Catches: a cache that is filled but never consulted. The cached calendar
    holds a booking the network's answer does not, and it must be what blocks
    the pass - with no request sent for that station."""
    passes_by_station(monkeypatch, {1: [(2, 60.0), (10, 50.0)], 2: [(2, 60.0)]})
    cache = {1: [mission_obs(2, norad=0)]}
    net = Net([station(1), station(2)])

    preview = build(net, calendar_cache=cache, max_per_station=2)

    assert 1 not in net.attempts
    assert [i.start for i in preview.items if i.station_id == 1] == [NOW + timedelta(hours=10)]
    assert (preview.calendars_cached, preview.calendars_read) == (1, 1)


def test_a_second_build_on_the_same_cache_reads_nothing(monkeypatch):
    """What the cache is for: a looped commit's later rounds. Every successful
    read is stored, and the next build on the same cache sends no request at
    all yet plans exactly as the first did."""
    passes_by_station(monkeypatch, spread(20, per=2))
    net = Net([station(i) for i in range(1, 21)])
    cache: dict[int, list] = {}

    first = build(net, calendar_cache=cache, max_total=50, max_per_station=2)
    reads = list(net.attempts)
    assert set(cache) == set(reads) and first.calendars_read == len(reads)

    second = build(net, calendar_cache=cache, max_total=50, max_per_station=2)
    assert net.attempts == reads, "the second build sent calendar reads"
    assert second.calendars_read == 0
    assert second.calendars_cached == len(reads)
    assert [(i.station_id, i.start) for i in second.items] == \
        [(i.station_id, i.start) for i in first.items]


def test_a_throttle_leaves_only_genuinely_read_calendars_in_the_cache(monkeypatch):
    """Nothing may be cached for a station whose read was refused: an empty
    entry would read as a free calendar on the next build and book blind."""
    passes_by_station(monkeypatch, spread(30, per=1))
    net = Net([station(i) for i in range(1, 31)], budget=8)
    cache: dict[int, list] = {}

    preview = build(net, calendar_cache=cache, max_total=30, max_per_station=1)

    assert set(cache) == set(net.asked)
    assert len(cache) == 8
    assert preview.stopped_early is not None


# --- the per-station cap counts what is already booked ----------------------------

def test_observations_already_booked_count_against_the_cap(monkeypatch):
    """Catches: a cap that is per click. Station 1 already holds two KNACKSAT-2
    observations in the window, so a cap of 3 leaves room for one more; station
    2 holds none and takes all three. Without this a second click, or the auto
    timer, stacks another full cap onto every station."""
    table = {1: [(1, 70.0), (6, 60.0), (11, 50.0), (16, 40.0)],
             2: [(1, 70.0), (6, 60.0), (11, 50.0), (16, 40.0)]}
    passes_by_station(monkeypatch, table)
    net = Net([station(1), station(2)], bookings={1: [mission_obs(3), mission_obs(8)]})

    preview = build(net, max_per_station=3)

    assert per_station(preview) == {1: 1, 2: 3}


def test_a_station_already_at_its_cap_is_not_picked_and_says_why(monkeypatch):
    passes_by_station(monkeypatch, spread(2, per=4))
    net = Net([station(1), station(2)],
              bookings={1: [mission_obs(3), mission_obs(8), mission_obs(13)]})

    preview = build(net, max_per_station=3)

    assert 1 not in per_station(preview)
    reasons = {row["station_id"]: row["reason"] for row in preview.skipped}
    assert reasons[1] == ("already has 3 observation(s) of this satellite booked in "
                          "the window (per-station cap 3)")


def test_only_this_satellite_inside_the_window_counts(monkeypatch):
    """Catches: counting every booking, or every booking of ours. Other
    satellites' observations are the owner's business - conflicts, not a share
    of our cap - and one outside [window_start, hard_end] is not "in the 48 h
    window" (a cached calendar can still hold one that has since ended)."""
    passes_by_station(monkeypatch, {1: [(1, 70.0), (6, 60.0), (11, 50.0)]})
    before_window = Booking(id=1, norad_cat_id=MISSION, start=NOW,
                            end=NOW + timedelta(minutes=WINDOW_START_MARGIN_MIN),
                            status="future")
    past_the_edge = Booking(id=2, norad_cat_id=MISSION,
                            start=NOW + timedelta(minutes=WINDOW_HARD_END_MIN),
                            end=NOW + timedelta(minutes=WINDOW_HARD_END_MIN + 10),
                            status="future")
    others = [mission_obs(h, norad=25544) for h in (3, 8, 13)]
    net = Net([station(1)], bookings={1: [before_window, past_the_edge, *others]})

    preview = build(net, max_per_station=3)

    assert per_station(preview) == {1: 3}


def test_our_own_recent_attempts_block_slots_but_do_not_use_the_cap(monkeypatch):
    """recent_attempts holds refused attempts too, so counting it would cap a
    station on the word of our own failed requests. It stays a conflict."""
    passes_by_station(monkeypatch, {1: [(1, 70.0), (6, 60.0), (11, 50.0), (16, 40.0)]})
    attempts = {1: [(NOW + timedelta(hours=h), NOW + timedelta(hours=h, minutes=10))
                    for h in (1, 3, 8)]}

    preview = build(Net([station(1)]), max_per_station=3, recent_attempts=attempts)

    starts = [i.start for i in preview.items]
    assert len(starts) == 3
    assert NOW + timedelta(hours=1) not in starts


def test_cap_counts_existing_off_is_the_old_per_build_cap(monkeypatch):
    table = {1: [(1, 70.0), (6, 60.0), (11, 50.0)]}
    passes_by_station(monkeypatch, table)
    net = Net([station(1)], bookings={1: [mission_obs(3), mission_obs(8)]})

    assert per_station(build(net, max_per_station=3, cap_counts_existing=False)) == {1: 3}


def test_earlier_rounds_and_existing_bookings_both_count(monkeypatch):
    """booked_counts (this commit's earlier rounds) and the calendar (read
    before the commit began, so it does not hold them) are disjoint and add."""
    passes_by_station(monkeypatch, spread(2, per=4))
    net = Net([station(1), station(2)],
              bookings={1: [mission_obs(3)], 2: [mission_obs(3), mission_obs(8)]})

    preview = build(net, max_per_station=3, booked_counts={1: 1, 2: 1})

    assert per_station(preview) == {1: 1}
    reasons = {row["station_id"]: row["reason"] for row in preview.skipped}
    assert reasons[2] == ("already has 2 observation(s) of this satellite booked in "
                          "the window (per-station cap 3), plus 1 from earlier rounds "
                          "of this commit")


def test_a_cached_calendar_counts_against_the_cap_too(monkeypatch):
    passes_by_station(monkeypatch, spread(1, per=4))
    net = Net([station(1)])
    cache = {1: [mission_obs(3), mission_obs(8)]}

    preview = build(net, max_per_station=3, calendar_cache=cache)

    assert per_station(preview) == {1: 1}
    assert net.attempts == []


def test_a_capped_station_is_not_counted_as_unread_after_a_throttle(monkeypatch):
    """A station found at its cap WAS read; a throttle later in the run must
    not report it among the stations nobody looked at."""
    passes_by_station(monkeypatch, spread(10, per=2))
    full = [mission_obs(3), mission_obs(8)]
    net = Net([station(i) for i in range(1, 11)],
              bookings={sid: full for sid in range(1, 11)}, budget=4)

    preview = build(net, max_per_station=2, max_total=50)

    assert preview.items == []
    assert preview.stopped_early is not None
    assert preview.stopped_early["unread_stations"] == 10 - 4


# --- elevation: the sacrifice bound is for a station's first pick -----------------

def test_extra_picks_fill_the_thin_bands(monkeypatch):
    """Catches: holding every pick to the sacrifice bound. Each station offers
    80, 78 and 12 degrees. Its first pick must be a good one; its second is
    free to fill the empty 15..0 band, which is what the operator asked for
    (an even spread over 90..0). Bounded, the 12s were never booked: measured
    on the live catalogue, 25 of 166 low passes made a 600-booking plan."""
    passes_by_station(monkeypatch, {sid: [(1, 80.0), (6, 78.0), (11, 12.0)]
                                    for sid in range(1, 11)})
    preview = run(Net([station(i) for i in range(1, 11)]), max_total=50, max_per_station=2)

    for sid in range(1, 11):
        mine = sorted(i.max_elevation_deg for i in preview.items if i.station_id == sid)
        assert mine[-1] >= 80.0 - MAX_ELEVATION_SACRIFICE_DEG, f"station {sid}: {mine}"
    low_band = len(ELEVATION_BAND_FLOORS) - 1
    assert sum(1 for i in preview.items if camp._band_of(i.max_elevation_deg) == low_band) == 10


# --- what the preview publishes ---------------------------------------------------

def test_the_payload_carries_the_plan_stats(monkeypatch):
    """Catches: stats that are computed but never published, and the
    transmitter list putting the busier downlink first. The primary leads even
    when the fallback has more bookings, because "how much of this plan is the
    weaker downlink" is read against it."""
    table = spread(6, per=2)
    table[6] = []                                    # hears TEL, no pass: not reachable
    passes_by_station(monkeypatch, table)
    net = Net([radio_station(1, UHF_400), radio_station(2, VHF), radio_station(3, VHF),
               radio_station(4, VHF), radio_station(5, UHF_AMATEUR),
               radio_station(6, UHF_400)])
    cache = {2: []}

    preview = build(net, RadioDb(), transmitter_uuid=TEL, fallback_transmitter_uuids=[DIGI],
                    max_per_station=2, max_total=50, calendar_cache=cache)
    payload = _campaign_preview_payload(preview)

    assert payload["stations_reachable"] == 4
    assert payload["stations_booked"] == 4
    assert (payload["calendars_read"], payload["calendars_cached"]) == (3, 1)
    assert payload["params"] == {
        "max_per_station": 2, "max_total": 50, "transmitter_uuid": TEL,
        "fallback_transmitter_uuids": [DIGI], "cap_counts_existing": True,
    }
    assert payload["transmitters"] == [
        {"uuid": TEL, "description": "Mode U - FSK9k6 - AX.25 G3RUH - TLM",
         "fallback": False, "stations": 1, "bookings": 2},
        {"uuid": DIGI, "description": "Mode V/V - FSK9k6 - Digipeater",
         "fallback": True, "stations": 3, "bookings": 6},
    ]
    assert [b["band"] for b in payload["band_counts"]] == list(ELEVATION_BAND_LABELS)
    assert sum(b["count"] for b in payload["band_counts"]) == len(payload["items"]) == 8
    rows = {(row["station_id"], row["fallback"], row["transmitter_description"][:6])
            for row in payload["items"]}
    assert rows == {(1, False, "Mode U"), (2, True, "Mode V"), (3, True, "Mode V"),
                    (4, True, "Mode V")}


def test_band_labels_and_counts():
    """Every band listed high to low, empty ones as zero, banded from the
    elevation the rows publish (rounded to 0.1) so the commit result, which only
    has those published values, bands an accepted booking the same way."""
    assert ELEVATION_BAND_LABELS == ("90-75", "75-60", "60-45", "45-30", "30-15", "15-0")
    assert band_counts_payload([80.0, 75.0, 14.9, 0.0]) == [
        {"band": "90-75", "count": 2}, {"band": "75-60", "count": 0},
        {"band": "60-45", "count": 0}, {"band": "45-30", "count": 0},
        {"band": "30-15", "count": 0}, {"band": "15-0", "count": 2},
    ]
    item = CampaignItem(station_id=1, station_name="s", transmitter_uuid=TEL,
                        start=NOW, end=NOW + timedelta(minutes=8), max_elevation_deg=74.96)
    payload = _campaign_preview_payload(CampaignPreview(
        generated_utc=NOW, window_start=NOW, window_end=NOW, items=[item]))
    assert payload["items"][0]["max_elevation_deg"] == 75.0
    assert payload["band_counts"][0] == {"band": "90-75", "count": 1}


def test_a_preview_built_by_hand_still_serialises():
    """CampaignService's mock preview constructs CampaignPreview directly and
    will not set every new field; the payload must still be complete."""
    payload = _campaign_preview_payload(CampaignPreview(
        generated_utc=NOW, window_start=NOW, window_end=NOW))
    assert payload["params"] == {}
    assert payload["transmitters"] == []
    assert payload["stations_booked"] == 0
    assert [b["count"] for b in payload["band_counts"]] == [0] * len(ELEVATION_BAND_FLOORS)
