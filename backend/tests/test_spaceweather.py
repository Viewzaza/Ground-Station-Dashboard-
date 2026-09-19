"""Space weather parsing.

The panel this feeds sits next to two others that answer "did we hear anything",
so its job is to be checkable against a second opinion — the operator's other
tab on spaceweatherlive.com. Most of what is tested here is therefore agreement
with what SWPC itself publishes, to the digit, rather than internal consistency.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    rows[30]["long"] = 1e-5            # an M1 flare, one minute wide
    out = downsample_peak(rows, buckets=6)
    assert max(p["long"] for p in out) == 1e-5


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


def test_a_gap_in_the_feed_leaves_a_gap_in_the_points():
    """GOES XRS drops out. Those minutes must not come back as data.

    The panel breaks its stroke on a step much wider than `bucket_seconds`, so
    what matters here is that the missing hour produces no points at all rather
    than being interpolated across.
    """
    rows = [raw(m, 1e-7) for m in range(30)] + [raw(m, 1e-7) for m in range(90, 120)]
    out = downsample_peak(rows, buckets=12)
    stamps = [parse_ts(p["t"]) for p in out]
    steps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
    assert max(steps) > 3 * bucket_seconds(rows)


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

def test_a_storm_in_progress_beats_the_daily_scale():
    """SWPC publishes G once a day; Kp is three-hourly.

    A storm that began this afternoon is in Kp and not yet in the daily summary,
    and for a "should I expect trouble" readout the failure that matters is
    under-reporting it.
    """
    svc = service()
    svc.scales = {"R": 0, "S": 0, "G": 0, "observed_at": "2026-09-19T00:30:00Z"}
    svc.kp = 7.0
    snap = svc.snapshot()
    assert snap["scales"]["G"] == 3
    assert snap["scales"]["g_from_kp"] is True


def test_the_daily_scale_wins_when_it_is_the_worse_of_the_two():
    """Kp has since settled; the storm still happened today."""
    svc = service()
    svc.scales = {"R": 1, "S": 0, "G": 4, "observed_at": "2026-09-19T00:30:00Z"}
    svc.kp = 2.0
    snap = svc.snapshot()
    assert snap["scales"]["G"] == 4
    assert snap["scales"]["g_from_kp"] is False


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
    svc.xray = [{"t": at(0).isoformat(), "long": 9.9e-6, "short": 1e-9}]
    assert svc.snapshot()["xray"]["class"] == "C2.4"


def test_the_goes_spacecraft_is_carried_through_as_provenance():
    """`/primary/` follows whichever bird SWPC designates, so it is never pinned."""
    svc = service()
    svc.satellite = 19
    assert svc.snapshot()["satellite"] == 19
