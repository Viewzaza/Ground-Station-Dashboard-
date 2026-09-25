"""The screen that keeps one dead satellite from crashing every booking run.

satnogs-auto-scheduler predicts passes for every receivable satellite - 700-odd
for station 5024, `-f` or not - and its predictor asserts ("Set event without
active pass") on a satellite whose SGP4 positions come back NaN. SatNOGS DB
carries such TLEs: NORAD 51840 "OBJECT S" was listed in orbit, with an active
401.650 MHz transmitter, on an element set five months old. Its crash took down
the first real run this dashboard ever made, before anything was booked.

The three TLEs below are real, copied from the station's cache on 2026-09-23.
The windows are pinned: the bad one fails across the start of each of them.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.services.autoscheduler_cli import (
    ParsedRun,
    RunOutcome,
    crashed_in_prediction,
    screen_tool_tles,
    screened_notice,
    unpropagatable_tles,
    without_screened_tle_warnings,
)

ISS = {
    "tle0": "0 ISS (ZARYA)",
    "tle1": "1 25544U 98067A   26265.85181744  .00007689  00000-0  14639-3 0  9995",
    "tle2": "2 25544  51.6316 176.7315 0004734 169.8211 190.2874 15.49234213586891",
    "norad_cat_id": 25544,
}
KNACKSAT_2 = {
    "tle0": "0 KNACKSAT-2",
    "tle1": "1 67683U 98067XZ  26265.24716633  .00058908  00000-0  48569-3 0  9998",
    "tle2": "2 67683  51.6250 162.9058 0008332 193.8286 166.2484 15.69728294 35478",
    "norad_cat_id": 67683,
}
OBJECT_S = {
    "tle0": "0 OBJECT S",
    "tle1": "1 51840U 22019S   26121.31511215  .17660270  25644-5  15609-3 0  9992",
    "tle2": "2 51840  97.3405 219.2395 0012643 277.8999  82.0872 16.51358146232864",
    "norad_cat_id": 51840,
}

# The window of the run that crashed, and the one the 18:00-Bangkok auto-run
# would have used.
CRASHED_RUN = datetime(2026, 9, 23, 7, 8, 31, tzinfo=timezone.utc)
EVENING_SLOT = datetime(2026, 9, 23, 11, 10, tzinfo=timezone.utc)


def test_a_tle_sgp4_cannot_propagate_is_found_and_good_ones_are_not():
    for start in (CRASHED_RUN, EVENING_SLOT):
        bad = unpropagatable_tles([ISS, OBJECT_S, KNACKSAT_2], start, start + timedelta(hours=24))
        assert bad == [OBJECT_S], (
            f"only the dead object must be flagged for the window at {start}; a "
            f"healthy TLE flagged here would be dropped from every run: {bad}"
        )


def test_a_malformed_entry_counts_as_unpropagatable():
    broken = {"tle0": "0 BROKEN", "tle1": "1 garbage", "tle2": "2 garbage", "norad_cat_id": 1}
    missing = {"tle0": "0 NO LINES", "norad_cat_id": 2}

    bad = unpropagatable_tles([ISS, broken, missing], CRASHED_RUN, CRASHED_RUN + timedelta(hours=1))

    assert broken in bad and missing in bad and ISS not in bad


def test_the_screen_rewrites_the_cache_without_the_dead_satellite(tmp_path):
    cache = tmp_path / "tles.json"
    cache.write_text(json.dumps([ISS, OBJECT_S, KNACKSAT_2]), encoding="utf-8")
    last_update = tmp_path / "last_update_5024.txt"
    last_update.write_text("2026-09-23T06:59:00\n", encoding="utf-8")

    screened = screen_tool_tles(tmp_path, EVENING_SLOT, EVENING_SLOT + timedelta(hours=24))

    assert screened == [{"norad_cat_id": "51840", "name": "OBJECT S", "epoch": "2026-05-01"}]
    kept = json.loads(cache.read_text(encoding="utf-8"))
    assert kept == [ISS, KNACKSAT_2], (
        "everything else has to reach the tool exactly as SatNOGS DB sent it"
    )
    assert last_update.read_text(encoding="utf-8") == "2026-09-23T06:59:00\n", (
        "the tool's freshness marker must not move, or the screen would make it "
        "skip - or force - a refetch it would not otherwise have done"
    )


def test_a_clean_cache_is_left_byte_for_byte_alone(tmp_path):
    cache = tmp_path / "tles.json"
    original = json.dumps([ISS, KNACKSAT_2], indent=1)
    cache.write_text(original, encoding="utf-8")

    assert screen_tool_tles(tmp_path, CRASHED_RUN, CRASHED_RUN + timedelta(hours=24)) == []
    assert cache.read_text(encoding="utf-8") == original


def test_no_cache_or_an_unreadable_one_is_not_an_error(tmp_path):
    assert screen_tool_tles(tmp_path, CRASHED_RUN, CRASHED_RUN + timedelta(hours=1)) == []
    (tmp_path / "tles.json").write_text("{not json", encoding="utf-8")
    assert screen_tool_tles(tmp_path, CRASHED_RUN, CRASHED_RUN + timedelta(hours=1)) == []


def test_the_notice_says_whether_a_priority_satellite_was_lost():
    screened = [{"norad_cat_id": "51840", "name": "OBJECT S", "epoch": "2026-05-01"}]

    quiet = screened_notice(screened, [67683, 68795])
    loud = screened_notice(screened, [51840, 67683])

    assert "none of them is in the priority list" in quiet["message"]
    assert "NORAD 51840 OBJECT S (TLE from 2026-05-01)" in quiet["message"]
    assert "INCLUDING priority NORAD 51840" in loud["message"], (
        "a priority satellite that cannot be booked is the one the operator "
        "has to hear about"
    )
    assert screened_notice([], [67683]) is None


def _outcome(lines, *, failure=("crashed", "crashed"), **parsed):
    return RunOutcome(exit_code=1, failure=failure, lines=lines, parsed=ParsedRun(**parsed))


PREDICTION_TRACEBACK = [
    "INFO\troot\tSearch passes for 937 transmitters across 701 satellites:",
    "Traceback (most recent call last):",
    '  File "/usr/local/lib/python3.12/site-packages/auto_scheduler/pass_predictor.py", '
    "line 98, in find_constrained_passes",
    '  File "/usr/local/lib/python3.12/site-packages/satnogs_predict/propagation/'
    'propagator.py", line 227, in _find_visibility_intervals',
    "AssertionError: Set event without active pass",
]


def test_a_crash_in_pass_prediction_is_recognised():
    assert crashed_in_prediction(_outcome(PREDICTION_TRACEBACK)) is True


def test_a_crash_after_booking_started_is_never_a_prediction_crash():
    """The retry is only safe because a prediction crash comes before any POST."""
    assert crashed_in_prediction(_outcome(PREDICTION_TRACEBACK, attempted_booking=True)) is False
    assert crashed_in_prediction(_outcome(PREDICTION_TRACEBACK, booked_log=2)) is False


def test_other_failures_are_not_prediction_crashes():
    elsewhere = ["Traceback (most recent call last):", "requests.exceptions.ConnectionError: boom"]
    assert crashed_in_prediction(_outcome(elsewhere)) is False
    assert crashed_in_prediction(
        _outcome(PREDICTION_TRACEBACK, failure=("batch_failed", "x"))
    ) is False
    assert crashed_in_prediction(_outcome(PREDICTION_TRACEBACK, failure=None)) is False


def test_the_tools_per_transmitter_no_tle_lines_fold_into_the_one_notice():
    """Fifty 'No TLE found' warnings on the board say what one line already did."""
    screened = [{"norad_cat_id": "51840", "name": "OBJECT S", "epoch": "2026-05-01"}]
    notices = [
        {"severity": "warning",
         "message": "No TLE found for transmitter EmoZotnoHhrHQikzSwMg6h on NORAD 51840, skipping."},
        {"severity": "warning",
         "message": "No TLE found for transmitter AAAAAAAAAAAAAAAAAAAAAA on NORAD 12345, skipping."},
        {"severity": "warning", "message": "NORAD 51840 something else entirely"},
    ]

    kept = without_screened_tle_warnings(notices, screened)

    assert kept == notices[1:], (
        "only the tool's no-TLE line for a satellite WE removed is folded away; "
        "a missing TLE the screen did not cause is still news"
    )
    assert without_screened_tle_warnings(notices, []) == notices
