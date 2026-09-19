"""Space weather parsing.

The panel this feeds sits next to two others that answer "did we hear anything",
so its job is to be checkable against a second opinion — the operator's other
tab on spaceweatherlive.com. Most of what is tested here is therefore agreement
with what SWPC itself publishes, to the digit, rather than internal consistency.
"""

from __future__ import annotations

import asyncio
import contextlib

# Captured before run_ticks patches the module's asyncio.sleep. A fake client
# that wants to really block must use this, or it gets the patched one and
# never blocks at all.
_real_sleep = asyncio.sleep
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import Settings
from app.services.spaceweather import (
    SpaceWeatherService,
    bucket_seconds,
    downsample_peak,
    flare_class,
    flare_letter,
    g_scale_for_kp,
    latest_kp,
    parse_scales,
    parse_ts,
    poll_state,
    split_xray_rows,
)


def service(**overrides) -> SpaceWeatherService:
    return SpaceWeatherService(Settings(station_id=5024, **overrides))


def at(minutes: float) -> datetime:
    return datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def xray_row(minutes: float, energy: str, flux: float) -> dict:
    return {"time_tag": at(minutes).isoformat().replace("+00:00", "Z"),
            "satellite": 18, "energy": energy, "flux": flux}


# --------------------------------------------------------------------------
# flare class
# --------------------------------------------------------------------------

@pytest.mark.parametrize("flux, expected", [
    # Observed on 2026-09-19: SWPC's own xray-flares-latest.json published
    # current_class B3.0 for this long-channel flux. Rounding gives B3.1, and an
    # operator comparing the two panels has no way to know which to believe.
    (3.0689415098095196e-07, "B3.0"),
    # Same feed, same day: max_xrlong 5.5193e-7 published as max_class B5.5.
    (5.519300430023577e-07, "B5.5"),
    (1e-8, "A1.0"),
    (9.9e-8, "A9.9"),
    (1e-7, "B1.0"),
    (1e-6, "C1.0"),
    (1e-5, "M1.0"),
    (9.99e-5, "M9.9"),
    (1e-4, "X1.0"),
])
def test_flare_class_matches_what_swpc_publishes(flux, expected):
    assert flare_class(flux) == expected


def test_a_great_flare_drops_the_decimal():
    """X15, not X15.0 — the convention stops using a decimal above X9.9."""
    assert flare_class(1.5e-3) == "X15"
    assert flare_class(2.8e-3) == "X28"


def test_below_a1_is_still_quoted_in_the_a_band():
    """SWPC and SpaceWeatherLive both print A0.4 rather than calling it zero."""
    assert flare_class(4e-9) == "A0.4"


def test_an_unusable_flux_has_no_class():
    # Zero and negative are not low readings, they are absent ones — and this
    # value is drawn on a log axis, where there is nowhere to put them.
    assert flare_class(None) is None
    assert flare_class(0) is None
    assert flare_class(-1e-7) is None
    assert flare_class(float("nan")) is None
    assert flare_class(float("inf")) is None


def test_the_letter_is_the_band():
    assert flare_letter("M2.4") == "M"
    assert flare_letter("X15") == "X"
    assert flare_letter(None) is None
    assert flare_letter("") is None
    assert flare_letter("?2.0") is None


# --------------------------------------------------------------------------
# the interleaved XRS feed
# --------------------------------------------------------------------------

def test_the_two_channels_are_split_not_read_as_one_series():
    """The feed is one row per (time, channel). Read flat it sawtooths."""
    rows = split_xray_rows([
        xray_row(0, "0.1-0.8nm", 5e-7),
        xray_row(0, "0.05-0.4nm", 2e-9),
        xray_row(1, "0.1-0.8nm", 6e-7),
        xray_row(1, "0.05-0.4nm", 3e-9),
    ])
    assert len(rows) == 2
    assert rows[0]["long"] == 5e-7 and rows[0]["short"] == 2e-9
    assert rows[1]["long"] == 6e-7 and rows[1]["short"] == 3e-9


def test_rows_come_back_in_time_order_whatever_order_they_arrived_in():
    rows = split_xray_rows([
        xray_row(5, "0.1-0.8nm", 5e-7),
        xray_row(1, "0.1-0.8nm", 1e-7),
        xray_row(3, "0.1-0.8nm", 3e-7),
    ])
    assert [r["long"] for r in rows] == [1e-7, 3e-7, 5e-7]


def test_a_non_positive_flux_is_dropped_rather_than_carried_as_zero():
    """This is drawn on a log axis, where zero is undrawable, not small."""
    rows = split_xray_rows([
        xray_row(0, "0.1-0.8nm", 0),
        xray_row(1, "0.1-0.8nm", -1e-9),
        xray_row(2, "0.1-0.8nm", 4e-7),
    ])
    assert len(rows) == 1
    assert rows[0]["long"] == 4e-7


def test_an_unknown_energy_band_is_ignored():
    rows = split_xray_rows([
        xray_row(0, "0.1-0.8nm", 4e-7),
        xray_row(0, "1.0-8.0nm", 9e-7),
    ])
    assert rows[0]["short"] is None
    assert rows[0]["long"] == 4e-7


def test_a_repeated_reading_keeps_the_higher_one():
    rows = split_xray_rows([
        xray_row(0, "0.1-0.8nm", 4e-7),
        xray_row(0, "0.1-0.8nm", 9e-7),
        xray_row(0, "0.1-0.8nm", 5e-7),
    ])
    assert rows[0]["long"] == 9e-7


def test_junk_in_the_feed_is_skipped_not_fatal():
    rows = split_xray_rows([
        "not a dict",
        {"time_tag": "not a date", "energy": "0.1-0.8nm", "flux": 1e-7},
        {"energy": "0.1-0.8nm", "flux": 1e-7},
        xray_row(0, "0.1-0.8nm", 4e-7),
    ])
    assert len(rows) == 1


def test_a_feed_that_is_not_a_list_gives_nothing():
    assert split_xray_rows({"error": "nope"}) == []
    assert split_xray_rows(None) == []


# --------------------------------------------------------------------------
# downsampling
# --------------------------------------------------------------------------

def raw(minutes: float, long: float, short: float = 1e-9) -> dict:
    return {"t": at(minutes), "long": long, "short": short}


def test_a_bucket_keeps_its_peak_not_its_mean():
    """The whole reason this is not a slice or an average.

    A one-minute M1 spike inside a quiet bucket averages out to a C-class bump,
    and the graph would then contradict the flare class printed beside it.
    """
    rows = [raw(m, 1e-7) for m in range(60)]
    # Minute 33, not 30. With six buckets over sixty rows, minute 30 is exactly
    # a bucket boundary, so a naive "keep the first row of each bucket" would
    # also return the spike and the test would exclude the mean without
    # excluding anything else.
    rows[33]["long"] = 1e-5            # an M1 flare, one minute wide
    out = downsample_peak(rows, buckets=6)
    assert max(p["long"] for p in out) == 1e-5
    assert out[3]["long"] == 1e-5      # and in the bucket it actually happened in


def test_the_two_channels_are_maximised_independently():
    rows = [raw(0, 1e-7, 9e-9), raw(1, 4e-7, 2e-9), raw(2, 2e-7, 5e-9)]
    out = downsample_peak(rows, buckets=1)
    assert out[0]["long"] == 4e-7
    assert out[0]["short"] == 9e-9


def test_a_series_shorter_than_the_bucket_count_is_passed_through_whole():
    rows = [raw(0, 1e-7), raw(1, 2e-7)]
    out = downsample_peak(rows, buckets=180)
    assert len(out) == 2
    assert [p["long"] for p in out] == [1e-7, 2e-7]


def test_downsampling_never_returns_more_points_than_buckets():
    rows = [raw(m, 1e-7) for m in range(360)]
    assert len(downsample_peak(rows, buckets=180)) <= 180


def test_the_last_point_is_not_lost_off_the_end():
    """The final row indexes exactly onto the span and must be clamped in."""
    rows = [raw(m, 1e-7) for m in range(120)]
    rows[-1]["long"] = 7e-6
    out = downsample_peak(rows, buckets=10)
    assert out[-1]["long"] == 7e-6


def test_timestamps_are_whole_seconds():
    """A fractional bucket width must not leak six decimals onto the wire."""
    rows = [raw(m / 7, 1e-7) for m in range(400)]
    assert all("." not in p["t"].split("+")[0].split("T")[1] for p in downsample_peak(rows))


def test_an_empty_series_downsamples_to_nothing():
    assert downsample_peak([]) == []


def test_a_series_that_spans_no_time_does_not_divide_by_it():
    """Every reading at one instant: the span is zero and the bucket width
    would be a ZeroDivisionError. Collapse to the single point instead."""
    rows = [raw(0, 1e-7), raw(0, 2e-7), raw(0, 3e-7)]
    out = downsample_peak(rows, buckets=2)
    assert len(out) == 1


BUCKETS = 12  # both calls below must agree, or the threshold is meaningless


def gap_test_fires(rows) -> bool:
    """Whether the panel would break its stroke somewhere in this series.

    Mirrors what spaceweather.js does: break when a step exceeds three nominal
    buckets. Both bucket counts must be the same one — computing the series
    with 12 and the threshold with the default 180 makes the threshold fifteen
    times too small, which is a test that passes on contiguous data and would
    have missed the bug where every ordinary step read as a dropout.
    """
    out = downsample_peak(rows, buckets=BUCKETS)
    stamps = [parse_ts(p["t"]) for p in out]
    steps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
    return bool(steps) and max(steps) > 3 * bucket_seconds(rows, buckets=BUCKETS)


def test_a_gap_in_the_feed_leaves_a_gap_in_the_points():
    """GOES XRS drops out. Those minutes must not come back as data."""
    rows = [raw(m, 1e-7) for m in range(30)] + [raw(m, 1e-7) for m in range(90, 120)]
    assert gap_test_fires(rows)


def test_contiguous_data_does_not_read_as_a_gap():
    """The other half of the previous test, and the half that was missing.

    Without this, a threshold far too small passes the gap test on every
    series, including one with no gap in it — which is the failure that draws
    the graph as disconnected points and then as nothing at all.
    """
    assert not gap_test_fires([raw(m, 1e-7) for m in range(120)])


def test_a_short_series_is_not_read_as_one_long_dropout():
    """A series too short to thin is passed through whole, so the bucket width
    must describe the rows actually emitted rather than a 180-way split of
    them. 45 minutes of 1-minute data is 45 ordinary steps, not 45 dropouts."""
    assert not gap_test_fires([raw(m, 1e-7) for m in range(45)])
    assert bucket_seconds([raw(m, 1e-7) for m in range(45)]) == pytest.approx(60, abs=1)


def test_the_bucket_width_follows_the_span():
    rows = [raw(m, 1e-7) for m in range(361)]     # six hours, one-minute cadence
    assert bucket_seconds(rows, buckets=180) == pytest.approx(120, abs=1)


def test_a_series_too_short_to_bucket_has_no_bucket_width():
    assert bucket_seconds([]) is None
    assert bucket_seconds([raw(0, 1e-7)]) is None
    assert bucket_seconds([raw(0, 1e-7), raw(0, 2e-7)]) is None


# --------------------------------------------------------------------------
# timestamps
# --------------------------------------------------------------------------

def test_a_products_timestamp_without_an_offset_is_read_as_utc():
    """The Kp feed omits the offset and means UTC.

    Left naive it raises on the first comparison against an aware datetime;
    read as local time it would shift Kp by up to a day.
    """
    parsed = parse_ts("2026-09-19T09:00:00")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed == datetime(2026, 9, 19, 9, 0, tzinfo=timezone.utc)


def test_a_z_suffix_is_read_as_utc():
    assert parse_ts("2026-09-19T09:00:00Z") == datetime(2026, 9, 19, 9, 0, tzinfo=timezone.utc)


def test_an_unparsable_timestamp_is_none_not_an_exception():
    assert parse_ts("yesterday") is None
    assert parse_ts(None) is None
    assert parse_ts("") is None


# --------------------------------------------------------------------------
# Kp and the storm scales
# --------------------------------------------------------------------------

def test_the_one_minute_estimate_is_read_in_preference_to_the_synoptic_index():
    """The 3-hourly feed publishes at the END of its period and can be three
    hours behind; the estimate is what SWPC and SpaceWeatherLive show as now."""
    value, _ = latest_kp([{"time_tag": "2026-09-19T15:02:00", "estimated_kp": 4.33,
                           "kp_index": 4, "kp": "4Z"}])
    assert value == 4.33


def test_the_synoptic_field_still_parses():
    """Either feed can be pointed at this."""
    value, _ = latest_kp([{"time_tag": "2026-09-19T12:00:00", "Kp": 1.67, "a_running": 6}])
    assert value == 1.67


def test_the_newest_kp_wins_whatever_order_the_feed_is_in():
    value, when = latest_kp([
        {"time_tag": "2026-09-19T06:00:00", "Kp": 2.33},
        {"time_tag": "2026-09-19T12:00:00", "Kp": 5.67},
        {"time_tag": "2026-09-19T09:00:00", "Kp": 1.33},
    ])
    assert value == 5.67
    assert when is not None and when.startswith("2026-09-19T12:00")


def test_an_empty_kp_feed_is_none_rather_than_zero():
    """Zero is a real Kp — the quietest one there is — so it cannot double as
    'we do not know', which is what the panel needs to grey the readout out."""
    assert latest_kp([]) == (None, None)
    assert latest_kp("nope") == (None, None)


@pytest.mark.parametrize("kp, expected", [
    (0.0, 0), (4.67, 0), (5.0, 1), (5.67, 1), (6.0, 2),
    (7.0, 3), (8.0, 4), (9.0, 5),
])
def test_the_g_scale_follows_kp(kp, expected):
    assert g_scale_for_kp(kp) == expected


def test_an_unknown_kp_has_no_g_scale():
    assert g_scale_for_kp(None) is None


def test_todays_scales_are_read_and_the_forecast_rows_are_not():
    """`"0"` is today observed; `"1".."3"` carry probabilities, not scales."""
    scales = parse_scales({
        "0": {"DateStamp": "2026-09-19", "TimeStamp": "14:19:00",
              "R": {"Scale": "2"}, "S": {"Scale": "0"}, "G": {"Scale": "1"}},
        "1": {"R": {"Scale": None, "MinorProb": "35"}, "S": {"Scale": None},
              "G": {"Scale": "3"}},
        "-1": {"R": {"Scale": "5"}, "S": {"Scale": "4"}, "G": {"Scale": "4"}},
    })
    assert scales["R"] == 2 and scales["S"] == 0 and scales["G"] == 1
    assert scales["observed_at"] == "2026-09-19T14:19:00Z"


def test_a_row_with_no_date_has_no_observed_at():
    """Pasting an empty date to an empty time used to yield the string "Z",
    which passes every "have we got one" check and then fails to parse."""
    assert parse_scales({"0": {"R": {"Scale": "0"}}})["observed_at"] is None
    assert parse_scales({"0": {"DateStamp": "2026-09-19"}})["observed_at"] is None


def test_a_null_scale_is_unknown_not_quiet():
    scales = parse_scales({"0": {"R": {"Scale": None}, "S": {}, "G": {"Scale": "0"}}})
    assert scales["R"] is None
    assert scales["S"] is None
    assert scales["G"] == 0


def test_a_scales_payload_of_the_wrong_shape_is_survivable():
    for payload in ([], None, {"nope": 1}, {"0": "not a dict"}):
        assert parse_scales(payload) == {"R": None, "S": None, "G": None, "observed_at": None}


# --------------------------------------------------------------------------
# component health
# --------------------------------------------------------------------------

def test_a_clean_tick_is_ok():
    assert poll_state([], 0) == "ok"


def test_one_blip_is_degraded_not_down():
    """A public CDN hiccups. Flapping a health chip teaches people to ignore it."""
    assert poll_state(["xray"], 1) == "degraded"
    assert poll_state(["xray"], 2) == "degraded"


def test_the_xray_feed_failing_for_three_ticks_is_down():
    assert poll_state(["xray"], 3) == "down"


def test_a_side_feed_failing_forever_is_only_degraded():
    """Kp and F10.7 are readouts beside the graph, and they grey out on their
    own. The panel's reason to exist is still drawing, so the chip is not red."""
    assert poll_state(["kp", "f107", "scales", "flare"], 99) == "degraded"


# --------------------------------------------------------------------------
# the published snapshot
# --------------------------------------------------------------------------

def test_the_scales_are_passed_through_untouched():
    """SWPC's R/S/G is a 24-hour observed MAXIMUM and is reported as one.

    An earlier version took max() of this and a G derived from the current Kp,
    on the premise that the scales were a stale once-a-day summary. They are
    not — they re-timestamp every few minutes — so that rule only ever latched
    the day's peak and presented it as the weather now.
    """
    svc = service()
    svc.scales = {"R": 1, "S": 0, "G": 4, "observed_at": "2026-09-19T00:30:00Z"}
    svc.kp = 2.0                       # the storm is over; the maximum stands
    snap = svc.snapshot()
    assert (snap["scales"]["R"], snap["scales"]["S"], snap["scales"]["G"]) == (1, 0, 4)
    assert snap["scales"]["window"] == "24h-max"


def test_a_quiet_scale_is_not_inflated_by_a_high_kp():
    """The mirror of the above: no synthesised G either."""
    svc = service()
    svc.scales = {"R": 0, "S": 0, "G": 0, "observed_at": "2026-09-19T00:30:00Z"}
    svc.kp = 7.0
    snap = svc.snapshot()
    assert snap["scales"]["G"] == 0
    # It is reported beside the live Kp instead, in the same units, so the two
    # can be read together without either pretending to be the other.
    assert snap["kp_g"] == 3


def test_swpcs_own_class_is_preferred_over_our_arithmetic():
    """We already poll the feed that carries it, so agreement is structural."""
    svc = service()
    svc.current_flux = 2.4e-6          # computes to C2.4
    svc.current_class = "C2.3"         # what SWPC actually published
    assert svc.snapshot()["xray"]["class"] == "C2.3"


def test_our_arithmetic_is_the_fallback_when_swpc_has_no_flare_on_record():
    svc = service()
    svc.current_flux = 2.4e-6
    svc.current_class = None
    assert svc.snapshot()["xray"]["class"] == "C2.4"


@pytest.mark.parametrize("kp, expected", [(4.67, 0), (5.0, 1), (5.67, 1), (8.67, 4), (9.0, 5)])
def test_the_kp_readout_carries_its_g_level(kp, expected):
    svc = service()
    svc.kp = kp
    assert svc.snapshot()["kp_g"] == expected


def test_the_snapshot_carries_no_unread_kp_history():
    """A 24-hour Kp strip has nowhere to be drawn in a 470x150 cell, so it is
    not shipped on every frame for nobody to read."""
    assert "kp_history" not in service().snapshot()


def test_a_snapshot_with_nothing_fetched_yet_is_still_well_formed():
    """The panel renders this on the first paint, before any poll has landed."""
    snap = service().snapshot()
    assert snap["xray"]["series"] == []
    assert snap["xray"]["class"] is None
    assert snap["scales"]["G"] is None
    assert snap["age_s"] is None
    assert snap["kp"] is None


def test_the_class_is_read_off_the_live_flux_not_the_graph():
    """The last bucket carries a peak that may be two minutes old.

    Quoting it as "now" would read a tenth of a class high during a decay.
    """
    svc = service()
    svc.current_flux = 2.4e-6
    svc.current_class = None           # force the computed path
    svc.xray = [{"t": at(0).isoformat(), "long": 9.9e-6, "short": 1e-9}]
    assert svc.snapshot()["xray"]["class"] == "C2.4"


def test_the_goes_spacecraft_is_carried_through_as_provenance():
    """`/primary/` follows whichever bird SWPC designates, so it is never pinned."""
    svc = service()
    svc.satellite = 19
    assert svc.snapshot()["satellite"] == 19


# --------------------------------------------------------------------------
# the poll loop and the refresh methods
#
# These had no coverage at all, which is how two bugs survived a green suite:
# a shared failure counter that sent the first X-ray blip straight to "down",
# and a `current_flux` that came back None whenever the newest row carried only
# the short channel. Both are asserted below.
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeClient:
    """Answers by URL fragment. A value that is an Exception is raised."""

    def __init__(self, answers: dict):
        self.answers = answers

    async def get(self, url, params=None):
        for fragment, answer in self.answers.items():
            if fragment in url:
                if isinstance(answer, Exception):
                    raise answer
                return FakeResponse(answer)
        return FakeResponse([])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def xray_payload(minutes: int = 200, last_long: bool = True) -> list:
    out = []
    for m in range(minutes):
        out.append(xray_row(m, "0.05-0.4nm", 2e-8))
        if last_long or m < minutes - 1:
            out.append(xray_row(m, "0.1-0.8nm", 4e-7))
    return out


async def test_refresh_xray_fills_the_series_and_the_headline():
    svc = service()
    await svc.refresh_xray(FakeClient({"xrays": xray_payload()}))
    assert len(svc.xray) == 180
    assert svc.current_flux == 4e-7
    assert svc.satellite == 18
    assert svc.bucket_s == pytest.approx(66.4, abs=1)


async def test_current_flux_survives_a_payload_cut_after_the_short_row():
    """SWPC writes the short row before the long one for a given minute, so a
    truncated payload leaves a newest row with no long-channel value. Taking
    rows[-1]["long"] blindly returned None and blanked the whole readout."""
    svc = service()
    await svc.refresh_xray(FakeClient({"xrays": xray_payload(last_long=False)}))
    assert svc.current_flux == 4e-7
    assert flare_class(svc.current_flux) == "B4.0"


async def test_an_empty_xray_payload_is_an_error_not_a_quiet_success():
    """Returning instead of raising counted the tick as a success, so the chip
    went green while the panel republished an hour-old series."""
    svc = service()
    with pytest.raises(ValueError):
        await svc.refresh_xray(FakeClient({"xrays": []}))


async def test_the_short_channel_floor_is_not_drawn_as_a_reading():
    """298 of 358 short-channel rows in a real window are the float32 spelling
    of exactly 1e-9 — the clamp, not a measurement."""
    svc = service()
    payload = [xray_row(0, "0.1-0.8nm", 4e-7),
               xray_row(0, "0.05-0.4nm", 9.999999717180685e-10)]
    await svc.refresh_xray(FakeClient({"xrays": payload}))
    assert svc.xray[0]["long"] == 4e-7
    assert svc.xray[0]["short"] is None


async def test_the_reported_age_is_the_readings_age_not_the_polls():
    """A fresh poll of a file whose last half hour is zeroed is stale data.
    The poll stamp alone reported that as "0s" under a 30-minute-old class."""
    svc = service()
    stale = [
        {"time_tag": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
         "satellite": 18, "energy": "0.1-0.8nm", "flux": 4e-7},
        {"time_tag": (datetime.now(timezone.utc) - timedelta(minutes=29)).isoformat(),
         "satellite": 18, "energy": "0.1-0.8nm", "flux": 0.0},   # contaminated
    ]
    await svc.refresh_xray(FakeClient({"xrays": stale}))
    snap = svc.snapshot()
    assert snap["age_s"] == pytest.approx(1800, abs=30)
    assert snap["polled_s"] == pytest.approx(0, abs=5)


async def test_a_wrong_shaped_scales_field_does_not_kill_the_poller():
    """`"R": "0"` used to raise AttributeError, which the loop does not catch,
    taking the other four feeds down with it on every restart."""
    svc = service()
    await svc.refresh_scales(FakeClient({"scales": {"0": {"R": "0", "S": {}, "G": {"Scale": "2"}}}}))
    assert svc.scales["R"] is None
    assert svc.scales["G"] == 2


class FakeClock:
    """The loop is driven by monotonic time, not by tick count: an endpoint
    that has just failed is skipped until its interval elapses. A fake sleep
    that does not also advance the clock therefore runs the loop body once and
    then idles, which looks like a pass and tests nothing."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def monotonic(self) -> float:
        return self.t


async def run_ticks(svc, client, ticks: int) -> list[tuple[str, str]]:
    """Drive `run()` for a fixed number of ticks; collect the health states."""
    seen: list[tuple[str, str]] = []
    svc.on_state = lambda component, state, detail="": seen.append((state, detail))

    import app.services.spaceweather as mod
    clock = FakeClock()
    real_sleep = asyncio.sleep
    count = {"n": 0}

    async def fake_sleep(seconds):
        clock.t += seconds
        count["n"] += 1
        if count["n"] >= ticks:
            raise asyncio.CancelledError
        await real_sleep(0)

    saved = (mod.httpx.AsyncClient, mod.asyncio.sleep, mod.time)
    mod.httpx.AsyncClient = lambda **kw: client
    mod.asyncio.sleep = fake_sleep
    mod.time = clock
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await svc.run()
    finally:
        mod.httpx.AsyncClient, mod.asyncio.sleep, mod.time = saved
    return seen


def healthy_answers(**overrides) -> dict:
    answers = {
        "xrays": xray_payload(),
        "flares": [{"time_tag": "2026-09-19T15:00:00Z", "current_class": "B4.0",
                    "max_class": "B5.5", "max_time": "2026-09-19T05:36:00Z",
                    "begin_time": "2026-09-19T05:08:00Z", "end_time": "2026-09-19T06:06:00Z"}],
        "scales": {"0": {"DateStamp": "2026-09-19", "TimeStamp": "15:07:00",
                         "R": {"Scale": "0"}, "S": {"Scale": "0"}, "G": {"Scale": "0"}}},
        "k_index": [{"time_tag": "2026-09-19T15:02:00", "estimated_kp": 1.0}],
        "10cm": [{"flux": 96, "time_tag": "2026-09-18T20:00:00"}],
    }
    answers.update(overrides)
    return answers


async def test_a_healthy_poll_reports_ok():
    svc = service()
    seen = await run_ticks(svc, FakeClient(healthy_answers()), ticks=3)
    assert seen and seen[0] == ("ok", "")
    assert svc.snapshot()["xray"]["class"] == "B4.0"   # SWPC's own word for it


async def test_a_broken_side_feed_never_takes_the_chip_red():
    """The counter is per endpoint. A permanently failing flare feed shares the
    X-ray interval, so a shared counter was past the threshold before the X-ray
    feed had ever failed — and then its first blip went straight to "down"."""
    svc = service()
    answers = healthy_answers(flares=httpx.HTTPError("boom"))
    seen = await run_ticks(svc, FakeClient(answers), ticks=8)
    assert seen, "no state was ever reported"
    assert all(state == "degraded" for state, _ in seen), seen
    assert "flare" in seen[-1][1]


async def test_the_xray_feed_must_fail_three_times_running_to_go_down():
    svc = service()
    answers = healthy_answers(xrays=httpx.HTTPError("boom"))
    seen = await run_ticks(svc, FakeClient(answers), ticks=6)
    states = [state for state, _ in seen]
    assert states[:3] == ["degraded", "degraded", "down"], states


async def test_one_xray_blip_amongst_healthy_ticks_stays_degraded():
    """The counter resets on that endpoint's own success."""
    good = xray_payload()
    calls = {"n": 0}

    class Flaky(FakeClient):
        async def get(self, url, params=None):
            if "xrays" in url:
                calls["n"] += 1
                if calls["n"] == 3:
                    raise httpx.HTTPError("one blip")
                return FakeResponse(good)
            return await super().get(url, params)

    svc = service()
    seen = await run_ticks(svc, Flaky(healthy_answers()), ticks=8)
    assert "down" not in [state for state, _ in seen], seen


async def test_a_hung_endpoint_cannot_stall_the_loop():
    """httpx's timeout is per socket read, so a server that dribbles bytes
    holds the request open forever. These five run in series, so one such
    server stops the X-ray refresh and freezes every open browser under a chip
    that is still green."""
    class Hangs(FakeClient):
        async def get(self, url, params=None):
            if "xrays" in url:
                await _real_sleep(3600)
            return await super().get(url, params)

    svc = service(spaceweather_timeout_s=0.05)
    seen = await run_ticks(svc, Hangs(healthy_answers()), ticks=3)
    # The timeout fires against the fake clock's sleep, so the tick completes
    # and the failure is reported rather than the loop hanging.
    assert seen, "the loop never reported anything — it stalled"
    assert any("xray" in detail for _, detail in seen), seen


async def test_the_loop_does_not_run_at_all_when_disabled():
    svc = service(spaceweather_enabled=False)
    seen = await run_ticks(svc, FakeClient({}), ticks=3)
    assert seen == [("degraded", "disabled")]
