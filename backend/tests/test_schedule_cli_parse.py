"""Read satnogs-auto-scheduler's transcript the way the operator has to trust it.

`run()` spawns the official tool and then throws the process away. Everything
the Station Schedule tab shows afterwards - what will be recorded tonight, what
was already on the calendar, whether anything went wrong - is whatever
`parse_output()` and `classify_failure()` managed to read back out of the text.
A parser bug here does not crash anything. It quietly shows the operator a
schedule that is not the one the station is going to run.

These tests are anchored on `data/schedule_dry_run.log`, which is the
**unedited output of the real tool** (0.5.dev17+g0f7ec0177) run against a
fully offline stub API - real binary, real `print_scheduledpass_summary()`,
invented data. See `data/README.md`. That matters: a hand-written table row
only ever encodes what its author believed the format to be, which is exactly
the mistake that produced two of the three bugs below. Lines are hand-written
here only for shapes the capture does not contain (a booking confirmation, a
crash, an empty result), and they are kept to upstream's own wording.

Three real failures this file exists to keep fixed:

  * **Duration was computed from `end - start`.** The tool prints both
    timestamps with `strftime`, truncating sub-second precision, but formats
    the Duration column from the full-precision `timedelta`. Subtracting the
    printed endpoints therefore overstates the pass by one second whenever its
    fractions cross a second boundary - on **31 of this transcript's 51 rows**.
    Across the 47 planned passes that put our total 31 s above the tool's own
    efficiency line, so the dashboard and the tool disagreed about how much of
    the night was booked. `test_duration_comes_from_the_printed_column...`
    re-reads the printed column for every single row and refuses the shortcut.

  * **The `script` launcher reported zero notices.** Under
    `launcher="script"` there is no pre-seeded root logger, so upstream's own
    `format="%(message)s"` arrives with no severity field at all. Severity then
    has to be recovered from the message text (`_CONTENT_SEVERITY`). Before it
    was, a run that printed ten "No TLE found for ..." warnings showed the
    operator a clean sheet. The transcript is re-parsed here with every prefix
    stripped, which is byte-for-byte what that launcher produces.

  * **A multi-line log record lost its second half.** Upstream logs the
    malformed-priority-line warning with an embedded newline, so it reaches us
    as two physical lines and the second carries no prefix. Dropping it told
    the operator a line in their priority file was malformed but never which
    expectation it broke - which is the only part of the message they can act
    on.

Nothing here spawns a process, touches the network, or reads the clock; the
functions under test are pure and the fixture is a file on disk.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.autoscheduler_cli import (
    TABLE_HEADER,
    ParsedRun,
    ScheduleRow,
    _parse_duration,
    classify_failure,
    missing_priority_notices,
    parse_output,
    split_prefix,
)

FIXTURE = Path(__file__).parent / "data" / "schedule_dry_run.log"

# The capture's own numbers, quoted from data/README.md. If any of these move,
# either the fixture was re-captured or the parser changed its mind about what
# the tool said - both are things a reviewer must look at deliberately.
PLANNED_ROWS = 47
ALREADY_SCHEDULED_ROWS = 4
TOTAL_ROWS = PLANNED_ROWS + ALREADY_SCHEDULED_ROWS
NOTICE_COUNT = 10
EFFICIENCY = {
    "selected": 47,
    "considered": 2653,
    "scheduled_s": 27983,
    "total_s": 86400,
    "percent": 32.388,
}
# Sum of the Duration column over the 47 planned rows. Subtracting the printed
# endpoints instead gives 27986 - see the duration tests.
PLANNED_DURATION_S = 27955
UTC = timezone.utc


@pytest.fixture
def transcript() -> list[str]:
    """The real transcript, exactly as the child process emitted it."""
    return FIXTURE.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def parsed(transcript: list[str]) -> ParsedRun:
    return parse_output(transcript)


def one(rows: list[ScheduleRow], norad: int) -> ScheduleRow:
    """The single row for `norad`, failing loudly if it is not single."""
    matches = [row for row in rows if row.norad == norad]
    assert len(matches) == 1, (
        f"expected exactly one row for NORAD {norad} in this transcript but "
        f"found {len(matches)}; the test below identifies a pass by NORAD and "
        f"cannot tell two passes of the same satellite apart"
    )
    return matches[0]


def header_index(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        if split_prefix(line)[2].strip().startswith(TABLE_HEADER):
            return index
    raise AssertionError(
        "the captured transcript no longer contains the summary-table header; "
        "every row test below depends on it"
    )


# --------------------------------------------------------------------------
# the table: header, sub-header, and the N/Y split
# --------------------------------------------------------------------------


def test_the_whole_table_is_read_and_the_sub_header_is_never_a_row(
    transcript: list[str], parsed: ParsedRun
) -> None:
    # Upstream prints one sub-header under the real header:
    # f"{' ' * 136} | misuse | ". It is skipped by count, not by content, so
    # this asserts the transcript still contains the line being skipped.
    assert any("| misuse |" in line for line in transcript), (
        "the captured transcript no longer contains the sub-header line, so "
        "this test can no longer prove the parser skips it"
    )

    rows = parsed.planned + parsed.already_scheduled
    assert len(rows) == TOTAL_ROWS, (
        f"the tool listed {TOTAL_ROWS} passes and the parser found "
        f"{len(rows)}; the Station Schedule tab shows the operator a different "
        f"night's work than the one the tool planned"
    )
    assert all(row.norad > 0 for row in rows), (
        "a row was parsed with no NORAD id, which means the sub-header or "
        "some other non-row line was read as a pass and will appear on the "
        "schedule as a phantom observation"
    )


def test_the_sub_header_on_its_own_produces_no_row() -> None:
    lines = [
        f"INFO\troot\t  {TABLE_HEADER} | End time | Duration | Satellite name",
        "INFO\troot\t" + " " * 136 + " | misuse | ",
    ]
    result = parse_output(lines)
    assert result.planned == [] and result.already_scheduled == [], (
        "the line directly under the table header is upstream's sub-header, "
        "not a pass; parsing it as one puts a fictional observation in front "
        "of the operator"
    )


def test_the_sch_column_splits_planned_from_already_scheduled(
    parsed: ParsedRun,
) -> None:
    assert len(parsed.planned) == PLANNED_ROWS, (
        f"the tool selected {PLANNED_ROWS} new passes but the parser reports "
        f"{len(parsed.planned)}; this is the number the operator reads as "
        f"'what this run would book'"
    )
    assert len(parsed.already_scheduled) == ALREADY_SCHEDULED_ROWS, (
        f"{ALREADY_SCHEDULED_ROWS} passes were already on the station's "
        f"calendar (Sch=Y) but the parser reports "
        f"{len(parsed.already_scheduled)}; counting those as new makes the run "
        f"look like it is about to double-book the station"
    )
    assert {row.norad for row in parsed.already_scheduled} == {
        25544,
        43768,
        48274,
        60133,
    }, (
        "the Sch=Y rows are the passes SatNOGS already holds for this station; "
        "putting the wrong satellites in that bucket tells the operator a pass "
        "is safe when nothing has been booked for it"
    )
    assert all(not row.already_scheduled for row in parsed.planned), (
        "a row in the planned list is flagged as already scheduled; the two "
        "lists mean opposite things to the operator"
    )
    assert all(row.already_scheduled for row in parsed.already_scheduled), (
        "a row in the already-scheduled list is not flagged as such, so the UI "
        "will offer to book a pass SatNOGS has already accepted"
    )


# --------------------------------------------------------------------------
# every field of a specific row
# --------------------------------------------------------------------------


def test_every_field_of_a_planned_row_is_read_correctly(parsed: ParsedRun) -> None:
    # NORAD 40056, the only satellite with exactly one pass in this capture:
    # 5024 | N | 40056 | 2023-02-18T15:26:35 | 2023-02-18T15:33:49 |  0:07:13
    #      |  44 12 127 | 0.550000 | E6m7ov6W37ttWcVhQwidfJ | BPSK | N
    #      | NLS 7.2/CANX 5
    row = one(parsed.planned, 40056)

    assert row.start == datetime(2023, 2, 18, 15, 26, 35, tzinfo=UTC), (
        "the pass start was misread; the rotator would be driven to the "
        "horizon at the wrong moment and the recording would miss AOS"
    )
    assert row.end == datetime(2023, 2, 18, 15, 33, 49, tzinfo=UTC), (
        "the pass end was misread, so the observation window shown to the "
        "operator does not match the one SatNOGS would book"
    )
    assert row.start.tzinfo is not None and row.end.tzinfo is not None, (
        "the tool prints UTC with no offset in the string; a naive datetime "
        "gets re-read in the server's local zone and the whole schedule "
        "silently shifts by that offset"
    )
    assert row.start.utcoffset() == timedelta(0), (
        "pass times must be UTC instants - the tool parses and prints them as "
        "UTC, and the station's own clock is UTC"
    )
    assert row.end.utcoffset() == timedelta(0), (
        "pass times must be UTC instants - the tool parses and prints them as "
        "UTC, and the station's own clock is UTC"
    )
    # 0:07:13, not the 434 s that (end - start) yields. This row is one of the
    # 31 the truncation bug used to inflate.
    assert row.duration_s == 433, (
        "the Duration column the tool printed is 0:07:13 = 433 s; any other "
        "value puts the dashboard's booked-time total at odds with the tool's "
        "own efficiency figure"
    )
    assert (row.az_rise, row.elevation, row.az_set) == (44.0, 12.0, 127.0), (
        "the tool prints rise azimuth, max elevation and set azimuth as one "
        "space-separated group; splitting it wrong points the antenna at the "
        "wrong patch of sky"
    )
    assert row.priority == pytest.approx(0.55), (
        "priority drives which pass wins a clash; reading it wrong reorders "
        "the operator's own preferences"
    )
    assert row.transmitter_uuid == "E6m7ov6W37ttWcVhQwidfJ", (
        "the transmitter UUID is what SatNOGS books against; the wrong one "
        "records the wrong downlink and the pass is wasted"
    )
    assert row.mode == "BPSK", (
        "the mode is what the operator checks the receiver against; a wrong "
        "mode means a demodulator that produces nothing"
    )
    assert row.frequency_violator is False, (
        "this transmitter is not flagged as a frequency misuser; a false flag "
        "makes the operator drop a perfectly good pass"
    )
    assert row.name == "NLS 7.2/CANX 5", (
        "the satellite name is the only human-readable identifier on the row; "
        "a name containing a dot and a slash must survive intact"
    )
    assert row.already_scheduled is False, (
        "this row is Sch=N - a pass the run would book, not one already on the "
        "calendar"
    )


def test_every_field_of_an_already_scheduled_row_is_read_correctly(
    parsed: ParsedRun,
) -> None:
    # NORAD 43768. Upstream prints Sch=Y rows from a different branch: zeroed
    # az/el, an empty Mode column, and here the is_frequency_violator flag set.
    row = one(parsed.already_scheduled, 43768)

    assert row.start == datetime(2023, 2, 18, 18, 42, 11, tzinfo=UTC), (
        "the start of an already-booked pass was misread; the operator cannot "
        "line it up against the station's real calendar"
    )
    assert row.end == datetime(2023, 2, 18, 18, 53, 47, tzinfo=UTC), (
        "the end of an already-booked pass was misread, so the gap before the "
        "next pass looks longer or shorter than it is"
    )
    assert row.start.utcoffset() == timedelta(0), (
        "already-scheduled rows must be UTC-aware too, or they will not sort "
        "against the planned ones on the same timeline"
    )
    assert row.end.utcoffset() == timedelta(0), (
        "already-scheduled rows must be UTC-aware too, or they will not sort "
        "against the planned ones on the same timeline"
    )
    assert row.duration_s == 696, (
        "0:11:36 is 696 s; the already-scheduled rows carry the time the "
        "station has already committed and it must be counted exactly once"
    )
    assert (row.az_rise, row.elevation, row.az_set) == (0.0, 0.0, 0.0), (
        "upstream zeroes the geometry group on Sch=Y rows; inventing pointing "
        "numbers for them would send the rotator somewhere upstream never "
        "predicted"
    )
    assert row.priority == pytest.approx(1.0), (
        "priority is still printed on already-scheduled rows and is what the "
        "operator sees when deciding whether to cancel one"
    )
    assert row.transmitter_uuid == "XJsHG6GRFqrBrmP3uvF93E", (
        "the transmitter UUID identifies the existing booking; without it the "
        "operator cannot find the observation on SatNOGS"
    )
    assert row.mode == "", (
        "the Mode column is empty on Sch=Y rows; filling it in from a "
        "neighbouring column would misdescribe an existing booking"
    )
    assert row.frequency_violator is True, (
        "the column headed 'Freq' actually carries the is-frequency-violator "
        "flag ('misuse' in the sub-header); missing it hides that this "
        "transmitter is transmitting outside its coordinated allocation"
    )
    assert row.name == "AISTECHSAT-2", (
        "the satellite name on an already-scheduled row is how the operator "
        "recognises what is already booked"
    )
    assert row.already_scheduled is True, (
        "Sch=Y means SatNOGS already holds this observation; treating it as "
        "new leads straight to a double booking"
    )


# --------------------------------------------------------------------------
# awkward satellite names
# --------------------------------------------------------------------------


def test_a_name_with_a_space_and_parentheses_survives(parsed: ParsedRun) -> None:
    row = one(parsed.already_scheduled, 48274)
    assert row.name == "CSS (Tianhe)", (
        "satellite names are unbounded text and routinely contain spaces and "
        "parentheses; truncating at the first space would show the operator "
        "'CSS' and leave them guessing which spacecraft the pass is for"
    )


def test_the_row_with_no_satellite_name_still_parses(
    transcript: list[str], parsed: ParsedRun
) -> None:
    # NORAD 60133 is not in the satellite catalogue, so upstream prints an
    # empty name. The line ends "| N      | " with a trailing space and yields
    # exactly 12 " | "-separated fields, the parser's bare minimum.
    raw = [line for line in transcript if "60133" in line]
    assert len(raw) == 1, (
        "the empty-name row is the one that proves a 12-field line parses; "
        "the capture must still contain exactly one"
    )
    fields = split_prefix(raw[0])[2].split(" | ")
    assert len(fields) == 12, (
        f"the empty-name row should be exactly 12 fields (it is the parser's "
        f"minimum) but this one has {len(fields)}; the capture changed shape"
    )

    row = one(parsed.already_scheduled, 60133)
    assert row.name == "", (
        "a satellite with no catalogue entry has no name, and the row must "
        "still appear: this is a real booked observation and dropping it makes "
        "the station look free when it is not"
    )
    assert row.duration_s == 570, (
        "0:09:30 is 570 s of the station's night that is already committed, "
        "nameless satellite or not"
    )


# --------------------------------------------------------------------------
# duration: the printed column, never (end - start)
# --------------------------------------------------------------------------


def test_duration_comes_from_the_printed_column_not_the_endpoints(
    transcript: list[str], parsed: ParsedRun
) -> None:
    """Walk every row and compare `duration_s` to the tool's own Duration cell.

    WHY THIS IS NOT REDUNDANT WITH THE FIELD TESTS ABOVE: the tool truncates
    the printed timestamps to whole seconds with `strftime`, but formats the
    Duration column from the full-precision `timedelta`. So `end - start` is
    one second too long on any pass whose fractional seconds cross a boundary -
    31 of the 51 rows in this capture. It is the sort of error that looks like
    a rounding nit until the dashboard's booked-time total no longer agrees
    with the efficiency line the tool printed, and nobody can say which is
    right. The printed column is authoritative; this walks all 51 rows and
    re-reads field index 5 from the raw text rather than trusting any of them.
    """
    # Independent of _parse_duration on purpose: this is the check, not a
    # second call to the code under test. No row in this capture is a day long.
    printed: dict[tuple[int, datetime], int] = {}
    for line in transcript:
        fields = split_prefix(line)[2].split(" | ")
        if len(fields) < 12 or fields[1].strip() not in ("Y", "N"):
            continue
        hours, minutes, seconds = (int(bit) for bit in fields[5].strip().split(":"))
        start = datetime.strptime(fields[3].strip(), "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=UTC
        )
        printed[(int(fields[2]), start)] = hours * 3600 + minutes * 60 + seconds

    assert len(printed) == TOTAL_ROWS, (
        f"re-read {len(printed)} Duration cells straight from the transcript "
        f"but the capture has {TOTAL_ROWS} rows; this check has stopped "
        f"covering every row"
    )

    disagreements = 0
    for row in parsed.planned + parsed.already_scheduled:
        key = (row.norad, row.start)
        assert key in printed, (
            f"the parser produced a row for NORAD {row.norad} at {row.start} "
            f"that is not in the transcript at all"
        )
        assert row.duration_s == printed[key], (
            f"NORAD {row.norad} at {row.start}: the tool printed "
            f"{printed[key]} s in its Duration column but the parser reports "
            f"{row.duration_s} s. Do not derive duration from (end - start): "
            f"the printed timestamps are truncated to whole seconds and the "
            f"difference overstates the pass, putting the dashboard's total "
            f"booked time above what the station will actually record"
        )
        if int((row.end - row.start).total_seconds()) != printed[key]:
            disagreements += 1

    assert disagreements == 31, (
        f"{disagreements} of {TOTAL_ROWS} rows disagree between the printed "
        f"Duration and (end - start); this capture has 31, and if that number "
        f"reaches 0 the fixture no longer exercises the truncation bug at all"
    )


def test_the_planned_durations_add_up_to_the_tools_own_total(
    parsed: ParsedRun,
) -> None:
    total = sum(row.duration_s for row in parsed.planned)
    assert total == PLANNED_DURATION_S, (
        f"the 47 planned passes add up to {PLANNED_DURATION_S} s of recording "
        f"but the parser makes it {total} s; this figure is what the operator "
        f"compares against the tool's efficiency line before deciding whether "
        f"to let the run book anything"
    )
    naive = sum(int((row.end - row.start).total_seconds()) for row in parsed.planned)
    assert naive != PLANNED_DURATION_S, (
        "the fixture is supposed to contain rows where (end - start) differs "
        "from the printed Duration; if it no longer does, the test above "
        "proves nothing"
    )


# --------------------------------------------------------------------------
# the efficiency line, and the script launcher's prefix-less shape
# --------------------------------------------------------------------------


def test_the_efficiency_line_is_read_exactly(parsed: ParsedRun) -> None:
    assert parsed.efficiency == EFFICIENCY, (
        "the efficiency line is the tool's own summary of how much of the "
        "night it filled; it is printed BEFORE the table header, so a parser "
        "that only looks after the table loses it entirely and the operator "
        "has nothing to check our totals against"
    )


def test_a_transcript_with_no_severity_prefixes_still_parses_fully(
    transcript: list[str],
) -> None:
    """`launcher="script"` emits exactly this: upstream's own format, no level.

    The console script does not go through our `-c` bootstrap, so
    `logging.basicConfig(format="%(message)s")` wins and every line arrives
    with no `LEVEL\\tlogger\\t` prefix. Severity then has to come from the
    message text. This once reported zero notices for a run that printed ten
    warnings - the operator was shown a clean sheet for a run that had skipped
    ten transmitters.
    """
    unprefixed = [split_prefix(line)[2] for line in transcript]
    assert unprefixed != transcript, (
        "stripping the prefixes changed nothing, so this test is parsing the "
        "same shape as every other test and cannot fail for the right reason"
    )

    result = parse_output(unprefixed)

    assert len(result.planned) == PLANNED_ROWS, (
        f"under the script launcher the parser found {len(result.planned)} of "
        f"the {PLANNED_ROWS} planned passes; which launcher the dashboard "
        f"happens to use must not change what the operator sees"
    )
    assert len(result.already_scheduled) == ALREADY_SCHEDULED_ROWS, (
        "the already-scheduled rows vanished under the script launcher, so "
        "that path would offer to re-book passes SatNOGS already holds"
    )
    assert result.efficiency == EFFICIENCY, (
        "the efficiency line went missing under the script launcher; it is "
        "plain text with no prefix and must parse identically either way"
    )
    assert len(result.notices) == NOTICE_COUNT, (
        f"the script launcher produced {len(result.notices)} notices where the "
        f"module launcher produces {NOTICE_COUNT}. Upstream's own log format "
        f"carries no severity field, so severity has to be recovered from the "
        f"message text; without that, a run that skipped ten transmitters is "
        f"reported to the operator as entirely clean"
    )
    assert all(notice["severity"] == "warning" for notice in result.notices), (
        "every notice in this capture is a 'No TLE found for ...' warning; "
        "getting the severity wrong either buries it or raises a false alarm"
    )
    assert all("No TLE found for" in n["message"] for n in result.notices), (
        "the notice text must survive with the prefix stripped - it names the "
        "transmitter and NORAD the tool skipped, which is the only way the "
        "operator learns that satellite was never considered"
    )


def test_the_module_launcher_reports_the_same_notices(parsed: ParsedRun) -> None:
    assert len(parsed.notices) == NOTICE_COUNT, (
        f"the tool printed {NOTICE_COUNT} warnings about transmitters it "
        f"skipped for want of a TLE; reporting {len(parsed.notices)} means the "
        f"operator is not told why a satellite they care about was never "
        f"scheduled"
    )
    assert all(n["severity"] == "warning" for n in parsed.notices), (
        "a WARNING-level record must be reported as a warning; promoting it to "
        "an error makes a normal run look broken, and demoting it hides it"
    )


# --------------------------------------------------------------------------
# a log record that arrives as two physical lines
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "first, shape",
    [
        (
            "WARNING\troot\tMalformed line in priority file /w/prio.txt,",
            "module launcher (severity prefix present)",
        ),
        (
            "Malformed line in priority file /w/prio.txt,",
            "script launcher (no prefix; severity from the text)",
        ),
    ],
)
def test_a_warning_split_across_two_lines_is_rejoined(first: str, shape: str) -> None:
    # Upstream logs this one with an embedded newline, so it reaches us as two
    # physical lines and the continuation carries no prefix and a leading
    # space. Both halves matter: the first says WHICH file, the second says
    # what was wrong with it.
    result = parse_output([first, " expected 3 parameters but found 4"])

    assert len(result.notices) == 1, (
        f"{shape}: one log record arrived as two physical lines and produced "
        f"{len(result.notices)} notices; either the operator sees the same "
        f"warning twice or the half that explains it is shown on its own with "
        f"no context"
    )
    message = result.notices[0]["message"]
    assert "Malformed line in priority file /w/prio.txt," in message, (
        f"{shape}: the rejoined notice must still name the priority file, or "
        f"the operator does not know which file to go and fix"
    )
    assert "expected 3 parameters but found 4" in message, (
        f"{shape}: the second physical line is the half that says WHAT was "
        f"wrong. Dropping it tells the operator a line is malformed and never "
        f"which expectation it broke - and under '-f' a dropped priority line "
        f"means that satellite is silently never scheduled"
    )
    assert result.notices[0]["severity"] == "warning", (
        f"{shape}: a malformed priority line is a warning the operator has to "
        f"act on, not a debug detail"
    )


def test_a_continuation_is_not_glued_onto_an_unrelated_later_notice() -> None:
    lines = [
        "WARNING\troot\tNo TLE found for transmitter abc on NORAD 99206, skipping.",
        "INFO\troot\tDownload TLEs from SatNOGS DB...",
        " this indented line follows an INFO, not a warning",
        "WARNING\troot\tNo TLE found for transmitter def on NORAD 99237, skipping.",
    ]
    result = parse_output(lines)

    assert len(result.notices) == 2, (
        f"expected the two warnings and nothing else, got "
        f"{len(result.notices)}; an indented line that follows an unrelated "
        f"INFO is not a continuation of the previous warning"
    )
    assert "this indented line" not in result.notices[0]["message"], (
        "text from an unrelated later record was appended to an earlier "
        "warning, which puts words in the tool's mouth in front of the "
        "operator"
    )


# --------------------------------------------------------------------------
# split_prefix
# --------------------------------------------------------------------------


def test_split_prefix_reads_our_pre_seeded_prefix() -> None:
    assert split_prefix("WARNING\troot\tNo TLE found for transmitter abc") == (
        "WARNING",
        "root",
        "No TLE found for transmitter abc",
    ), (
        "the pre-seeded root logger writes LEVEL\\tlogger\\tmessage; failing to "
        "split it leaves the severity glued to the text, so every notice is "
        "reported at the wrong level and the table header never matches"
    )


def test_split_prefix_leaves_an_unprefixed_line_completely_alone() -> None:
    line = "5024 | N   | 07530 | 2023-02-18T09:00:00 | 2023-02-18T09:03:52"
    assert split_prefix(line) == ("", "", line), (
        "under the script launcher, and for anything the child writes straight "
        "to its own stdout, there is no prefix; inventing one or eating the "
        "first field would destroy the row"
    )


def test_a_line_whose_first_field_is_not_a_level_is_not_treated_as_a_prefix() -> None:
    line = "5024\tN\tsomething else entirely"
    assert split_prefix(line) == ("", "", line), (
        "only the five logging level names introduce a prefix; treating any "
        "tab-separated line as one would silently swallow the first two fields "
        "of the tool's output"
    )


def test_split_prefix_does_not_mangle_a_message_containing_tabs() -> None:
    assert split_prefix("ERROR\troot\tcolumn\tone\ttwo") == (
        "ERROR",
        "root",
        "column\tone\ttwo",
    ), (
        "the split is limited to two, so a message that itself contains tabs "
        "arrives whole; splitting further truncates the operator's error "
        "message at its first tab"
    )


# --------------------------------------------------------------------------
# _parse_duration
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("0:08:05", 485),
        (" 0:03:52", 232),  # right-justified exactly as the tool prints it
        ("  0:12:30", 750),
        ("1 day, 0:00:00", 86400),  # str(timedelta) once a pass crosses 24h
        ("2 days, 1:00:30", 176430),
        ("not a duration", None),
        ("", None),
        ("   ", None),
        ("0:08", None),
        ("1 day, oops", None),
    ],
)
def test_parse_duration_reads_the_tools_timedelta_format(
    text: str, expected: int | None
) -> None:
    assert _parse_duration(text) == expected, (
        f"{text!r} should read as {expected}; the Duration column is a plain "
        f"str(timedelta) right-justified into a width the tool is free to "
        f"change, and misreading it puts the wrong length on every pass the "
        f"operator sees"
    )


def test_an_unreadable_duration_falls_back_rather_than_losing_the_row() -> None:
    line = (
        "INFO\troot\t5024 | N   | 07530 | 2023-02-18T09:00:00 | "
        "2023-02-18T09:03:52 |  ?:??:?? | 325 12 301 | 1.000000 | "
        "epmfL93RZ2Vf5WKK4X2auf | CW          | N      | OSCAR 7 (AO-7)"
    )
    result = parse_output([f"INFO\troot\t  {TABLE_HEADER} | rest", "sub | header", line])

    assert len(result.planned) == 1, (
        "a Duration cell the parser cannot read must not cost the whole row: "
        "the pass is still real and the operator still needs to see it"
    )
    assert result.planned[0].duration_s == 232, (
        "with no readable Duration column the only figure left is "
        "(end - start); it may be a second long, but it is far better than "
        "showing the operator a pass of length zero"
    )


# --------------------------------------------------------------------------
# transcripts that stop early or say nothing happened
# --------------------------------------------------------------------------


def test_a_transcript_truncated_mid_table_parses_as_far_as_it_got(
    transcript: list[str],
) -> None:
    # Exactly what a killed child leaves behind: the table is cut off, and the
    # last line is half-written because the pipe closed mid-write.
    start = header_index(transcript) + 2
    truncated = transcript[: start + 8] + [transcript[start + 8][:45]]

    result = parse_output(truncated)

    rows = result.planned + result.already_scheduled
    assert len(rows) == 8, (
        f"a run killed by the idle timeout leaves a truncated table; the "
        f"parser must still report the {8} complete rows it did get (it "
        f"reported {len(rows)}) so the operator can see how far the tool "
        f"had got"
    )
    assert result.efficiency == EFFICIENCY, (
        "the efficiency line is printed before the table, so it survives a "
        "mid-table truncation and is the one honest number available about a "
        "run that was killed"
    )


def test_no_appropriate_passes_is_reported_as_such() -> None:
    lines = [
        "INFO\troot\tSearch passes for 566 transmitters across 311 satellites:",
        "INFO\troot\tNo appropriate passes found for scheduling.",
        "INFO\troot\tDone.",
    ]
    result = parse_output(lines)

    assert result.no_passes is True, (
        "the tool said in so many words that it found nothing to schedule; "
        "without this flag an empty table is indistinguishable from a parser "
        "that failed to read the table, and the operator cannot tell a quiet "
        "night from a broken dashboard"
    )
    assert result.planned == [] and result.already_scheduled == [], (
        "there is no table in this transcript at all, so any row here was "
        "invented"
    )


# --------------------------------------------------------------------------
# booking: attempted, confirmed, or never mentioned
# --------------------------------------------------------------------------


def test_a_booking_attempt_and_its_confirmation_are_both_recorded() -> None:
    lines = [
        "INFO\troot\tScheduling all unscheduled passes listed above.",
        "DEBUG\tauto_scheduler.satnogs_client\tScheduled 7 passes!",
        "INFO\troot\tDone.",
    ]
    result = parse_output(lines)

    assert result.attempted_booking is True, (
        "once the tool says it is scheduling the passes above, a booking POST "
        "may already have reached SatNOGS; a run killed after this point can "
        "never be reported to the operator as a clean failure that changed "
        "nothing"
    )
    assert result.booked_log == 7, (
        "'Scheduled 7 passes!' is the tool's own confirmation of how many "
        "observations SatNOGS accepted - the number the operator will look for "
        "on the station's calendar"
    )


def test_booked_log_stays_none_when_the_tool_never_said(parsed: ParsedRun) -> None:
    assert parsed.booked_log is None, (
        "this is a dry run: the tool never printed a 'Scheduled N passes!' "
        "line. None means 'it never said', which is not the same as 0 - "
        "reporting 0 here would tell the operator SatNOGS accepted nothing "
        "when in fact nothing was ever offered to it"
    )
    assert parsed.attempted_booking is False, (
        "a dry run must never look like it tried to book; if it does, a "
        "failure afterwards has to be reported as 'possibly partly booked' and "
        "the operator will go hunting for observations that do not exist"
    )


# --------------------------------------------------------------------------
# classify_failure
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line, expected, consequence",
    [
        (
            "CRITICAL\troot\tNo value for SATNOGS_API_TOKEN found in environment",
            "token_missing",
            "the operator must be told a token is empty - including for a dry "
            "run, which validates the whole config before it looks at -n",
        ),
        (
            "CRITICAL\troot\tInvalid value for SATNOGS_DB_API_TOKEN: bad format",
            "token_invalid",
            "a badly formatted token must be named as such, or the operator "
            "re-pastes the same wrong string",
        ),
        (
            "ERROR\troot\tNo ground station information found!",
            "station_unknown",
            "SatNOGS does not know this station id; the operator has to check "
            "the id and the account the Network token belongs to",
        ),
        (
            "ERROR\troot\tStation is neither in 'online' nor in 'testing' mode",
            "station_offline",
            "an offline station cannot be booked, and a dry run skips this "
            "check - so this is the error that only ever appears on the real "
            "run",
        ),
        (
            "ERROR\troot\tNo permission to schedule observations on this station",
            "no_permission",
            "the token's account is not allowed to schedule here; nothing the "
            "operator changes in the dashboard will help",
        ),
        (
            "ERROR\troot\tDownload from SatNOGS Network failed.",
            "network_download",
            "the existing schedule could not be read, so the run stopped "
            "rather than risk double-booking the station",
        ),
        (
            "ERROR\troot\tFailed to batch-schedule observations.",
            "batch_failed",
            "SatNOGS rejected the whole batch and nothing was booked; the "
            "operator must know the night is still empty",
        ),
        (
            "ERROR\troot\tFailed to schedule pass at 2023-02-18T09:00:00",
            "pass_failed",
            "individual passes were rejected, so the run is partly booked - "
            "the operator has to reconcile against SatNOGS by hand",
        ),
        (
            "Traceback (most recent call last):",
            "crashed",
            "the tool crashed; saying so points the operator at the traceback "
            "in the raw log instead of leaving them with a blank schedule",
        ),
    ],
)
def test_each_known_failure_mode_is_recognised(
    line: str, expected: str, consequence: str
) -> None:
    result = classify_failure(["INFO\troot\tStarting up.", line], 1)

    assert result is not None, (
        f"a run that printed {line!r} failed, and reporting no cause at all "
        f"leaves the operator reading a raw log: {consequence}"
    )
    code, explanation = result
    assert code == expected, (
        f"expected cause {expected!r} but got {code!r}; every failure path in "
        f"the tool is a bare sys.exit(1), so this text is the only thing that "
        f"tells the operator what went wrong - {consequence}"
    )
    assert explanation.strip(), (
        f"cause {expected!r} came back with no explanation; the code is for "
        f"the UI, the sentence is for the operator"
    )


def test_the_first_matching_cause_is_the_one_reported() -> None:
    # A download failure that is then re-raised: both needles are present.
    # _FAILURES is ordered most specific first, and the specific one is the
    # one that tells the operator what to do about it.
    lines = [
        "ERROR\troot\tDownload from SatNOGS Network failed.",
        "Traceback (most recent call last):",
        "  File \"/usr/lib/python3/schedule_single_station.py\", line 1",
        "RuntimeError: boom",
    ]
    result = classify_failure(lines, 1)

    assert result is not None and result[0] == "network_download", (
        "when a transcript carries several causes, the most specific one "
        "explains the run; reporting 'it crashed' for a failed Network "
        "download sends the operator to a traceback instead of to their "
        "connectivity"
    )


def test_a_clean_transcript_with_a_zero_exit_is_not_a_failure(
    transcript: list[str],
) -> None:
    assert classify_failure(transcript, 0) is None, (
        "this is a complete, successful dry run of the real tool; flagging it "
        "as a failure would train the operator to ignore the failure banner"
    )


def test_an_unexplained_non_zero_exit_is_still_reported(
    transcript: list[str],
) -> None:
    result = classify_failure(transcript, 137)

    assert result is not None, (
        "the child exited non-zero and nothing in the transcript explains it "
        "(137 is what an OOM kill looks like); staying silent would show the "
        "operator a successful run that did not happen"
    )
    code, explanation = result
    assert code == "exit_nonzero", (
        f"an unexplained non-zero exit must be reported as exactly that, not "
        f"as {code!r}"
    )
    assert "137" in explanation, (
        "the exit status is the only fact available about this failure, so it "
        "belongs in the sentence the operator reads"
    )


def test_usage_text_is_a_failure_even_though_the_tool_exited_zero() -> None:
    lines = [
        "usage: schedule_single_station [-h] [-s STATION] [-t STARTTIME]",
        "schedule_single_station: error: the following arguments are required",
    ]
    result = classify_failure(lines, 0)

    assert result is not None and result[0] == "bad_invocation", (
        "argparse prints its help and exits 0 when the station id is missing, "
        "which is otherwise indistinguishable from a clean run that found "
        "nothing to schedule - the operator would see an empty schedule night "
        "after night and no error at all"
    )


# --------------------------------------------------------------------------
# missing_priority_notices
# --------------------------------------------------------------------------


def test_a_priority_norad_with_no_pass_is_named_in_a_warning(
    parsed: ParsedRun,
) -> None:
    # 43017 is in the capture's priority file and was deliberately chosen so
    # that no pass is ever selected for it: its pinned transmitter is not in
    # this station's candidate set. Upstream says nothing whatsoever about it.
    notices = missing_priority_notices([7530, 43017], parsed)

    assert len(notices) == 1, (
        f"expected exactly one warning, got {len(notices)}; a priority entry "
        f"that never produces a pass is invisible in the tool's own output, "
        f"and under '-f' it is the exact symptom of a pinned transmitter the "
        f"station cannot hear"
    )
    assert "43017" in notices[0]["message"], (
        "the warning has to name the NORAD it is about, or the operator knows "
        "only that something in their priority list is not working"
    )
    assert "7530" not in notices[0]["message"], (
        "NORAD 7530 was scheduled nine times in this run; naming it here would "
        "send the operator looking for a fault that does not exist"
    )
    assert notices[0]["severity"] == "warning", (
        "a priority entry that never schedules is a warning the operator acts "
        "on, not an error that stops the run"
    )


def test_a_priority_norad_that_was_selected_produces_no_warning(
    parsed: ParsedRun,
) -> None:
    assert missing_priority_notices([7530, 37855, 40056], parsed) == [], (
        "all three of these satellites have passes in this run; warning about "
        "them would bury the one entry that really is misconfigured"
    )


def test_a_priority_norad_that_is_only_already_scheduled_counts_as_present(
    parsed: ParsedRun,
) -> None:
    # 48274 appears only as an Sch=Y row - SatNOGS already holds the pass, so
    # the tool correctly selected nothing new for it.
    assert 48274 not in {row.norad for row in parsed.planned}, (
        "48274 is supposed to appear only as an already-scheduled row in this "
        "capture; this test proves nothing otherwise"
    )
    assert missing_priority_notices([48274], parsed) == [], (
        "a priority satellite whose pass is already on the station's calendar "
        "is working exactly as intended; warning about it would tell the "
        "operator to go and fix a priority entry that is doing its job"
    )
