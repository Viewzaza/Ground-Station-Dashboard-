"""The priority file has to be readable by a tool that is not in this repo.

Station Schedule books observations by running the Libre Space Foundation's
`satnogs-auto-scheduler` as a child process and handing it our priority file
with `-P`. That tool parses the file with `csv.reader(delimiter=" ")` and
throws away any line that does not yield **exactly three fields**. It says so
in a warning, but under `-f` ("only priority") a thrown-away line just means
that satellite is never scheduled - and the run still exits 0, having booked
nothing, looking perfectly healthy.

This dashboard used to write two shapes that trip exactly that:

  * a 4th `manual` column, for the panel's per-row Auto/Manual toggle,
  * a `-` placeholder when no transmitter was pinned.

Both cost the reader the whole line. Verified against the real tool:
a file of 4-column lines parses to `{}`.

So these tests reimplement that reader - all twelve lines of it - rather than
importing `auto_scheduler`, for two reasons. The offline suite must not need a
dependency that is only installed in the Docker image; and a copy pinned here
fails loudly if someone changes our writer to suit a reader they imagined
rather than the one that exists.

The asymmetry these tests protect is deliberate: our WRITER is strict, our
READER is tolerant. That is what lets every file the dashboard has ever written
keep loading while everything it writes from now on is something the official
tool can actually read.
"""

from __future__ import annotations

import csv
import json

import pytest

from app.vendor.autoscheduler.priorities import (
    Priority,
    parse_priority_file,
    render_priority_file,
    write_priority_file,
)


# --- the official reader, reimplemented ------------------------------------
# Transcribed from auto_scheduler/io.py at commit 0f7ec0177 (strip_comments +
# read_priorities_transmitters). Keep it a transcription, not an improvement.
def _strip_comments(lines):
    for row in lines:
        raw = row.split("#")[0].strip()
        if raw:
            yield raw


def official_reader(text: str) -> tuple[dict, dict]:
    """(priorities, favourite_transmitters), keyed by NORAD **as a string**."""
    priorities: dict[str, float] = {}
    transmitters: dict[str, str] = {}
    for row in csv.reader(_strip_comments(text.splitlines()), delimiter=" "):
        if len(row) != 3:
            continue
        norad_id, prio, transmitter = row
        priorities[norad_id] = float(prio)
        transmitters[norad_id] = transmitter
    return priorities, transmitters


PINNED = Priority(67683, 1.0, "UatCXtfDnoBPeVBGHgj4Bc", 1, "manual")
PLAIN = Priority(7530, 0.25, "AbCdEfGhIjKlMnOpQrStUv", 2, "auto")
UNPINNED = Priority(98329, 0.8, None, 3, "auto")


# --- what we write ----------------------------------------------------------
def test_every_written_line_survives_the_official_reader():
    text = render_priority_file([PINNED, PLAIN])
    priorities, transmitters = official_reader(text)
    assert priorities == {"67683": 1.0, "7530": 0.25}, (
        "the official scheduler silently drops any line that is not exactly "
        "three space-separated fields, and a dropped line means that satellite "
        f"is never observed - it read {priorities} from:\n{text}"
    )
    assert transmitters["67683"] == PINNED.transmitter_uuid, (
        "the pinned transmitter must survive the round trip, or the scheduler "
        "picks a different one than the operator chose"
    )


def test_a_manual_row_writes_no_fourth_column():
    # PINNED is mode="manual". That used to append a 4th field.
    text = render_priority_file([PINNED])
    data = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert len(data) == 1
    assert len(data[0].split(" ")) == 3, (
        "a Manual row must still be three fields; the mode belongs in the "
        f"sidecar, because a 4th column loses the line entirely: {data[0]!r}"
    )


def test_fields_are_single_spaced_and_never_aligned():
    text = render_priority_file([PINNED, PLAIN])
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        assert "  " not in line, (
            f"a run of two spaces makes csv emit an empty field, so the "
            f"official reader counts four and drops the line: {line!r}"
        )
        assert "\t" not in line, (
            f"a tab is not the delimiter at all, so the whole line collapses "
            f"to one field and is dropped: {line!r}"
        )


def test_norad_is_never_zero_padded():
    # 7530 is the trap: five digits with a leading zero if anyone pads to 5.
    text = render_priority_file([PLAIN])
    priorities, _ = official_reader(text)
    assert "7530" in priorities, (
        "the official reader matches NORAD ids as STRINGS against "
        "str(tle['norad_cat_id']), so a padded '07530' compares unequal to "
        f"'7530' and silently matches nothing: got {list(priorities)}"
    )


def test_an_unpinned_row_is_not_written_at_all():
    text = render_priority_file([PINNED, UNPINNED])
    priorities, _ = official_reader(text)
    assert "98329" not in priorities, "a row with no UUID cannot be expressed in three fields"
    assert "67683" in priorities, (
        "and dropping it must not disturb the rows around it - the sidecar is "
        "what keeps the unpinned row, not this file"
    )


def test_comment_and_blank_lines_are_safe():
    text = render_priority_file([PINNED])
    assert text.startswith("#"), "the header comment is part of what we write"
    priorities, _ = official_reader(text)
    assert priorities == {"67683": 1.0}, (
        "strip_comments removes # lines and blanks before csv ever sees them, "
        "so the header costs nothing"
    )


# --- what we read -----------------------------------------------------------
@pytest.mark.parametrize(
    "line, expect_uuid, expect_mode",
    [
        ("67683 1.0 UatCXtfDnoBPeVBGHgj4Bc", "UatCXtfDnoBPeVBGHgj4Bc", "auto"),
        ("67683 1.0 UatCXtfDnoBPeVBGHgj4Bc manual", "UatCXtfDnoBPeVBGHgj4Bc", "manual"),
        ("67683 1.0 - manual", None, "manual"),
        ("67683      1.0     UatCXtfDnoBPeVBGHgj4Bc", "UatCXtfDnoBPeVBGHgj4Bc", "auto"),
        ("67683 1.0", None, "auto"),
    ],
)
def test_reader_still_accepts_every_shape_we_ever_wrote(tmp_path, line, expect_uuid, expect_mode):
    path = tmp_path / "legacy.txt"
    path.write_text(line + "\n", encoding="utf-8")
    entries = parse_priority_file(path)
    assert 67683 in entries, (
        "the reader is deliberately more tolerant than the writer - that "
        f"asymmetry is the whole backward-compatibility path, and {line!r} is "
        "a shape this dashboard really did write"
    )
    assert entries[67683].transmitter_uuid == expect_uuid
    assert entries[67683].mode == expect_mode


def test_a_bare_norad_gets_the_maximum_weight(tmp_path):
    """Upstream's behaviour, kept, but it is a trap worth pinning."""
    path = tmp_path / "bare.txt"
    path.write_text("67683\n", encoding="utf-8")
    entries = parse_priority_file(path)
    assert entries[67683].weight == 1.0, (
        "a stray number on its own line becomes the HIGHEST priority satellite "
        "in the list rather than an inert typo - surprising, but it is what "
        "upstream does and changing it would silently re-rank existing files"
    )


def test_round_trip_through_the_writer_keeps_pinned_rows(tmp_path):
    path = tmp_path / "list.txt"
    write_priority_file(path, [PINNED, PLAIN])
    back = parse_priority_file(path)
    assert set(back) == {67683, 7530}
    assert back[67683].weight == pytest.approx(1.0)
    assert back[7530].transmitter_uuid == PLAIN.transmitter_uuid


# --- the one-time migration -------------------------------------------------
# A legacy file is not merely untidy. Fed to the official tool it parses to
# nothing at all, so under -f the first real run books nothing and reports
# success. These cover the rewrite that has to happen before that can occur.
LEGACY = (
    "# Edited from the Station Schedule dashboard panel.\n"
    "# Format: norad_id  weight(0.0-1.0)  [transmitter_uuid|-]  [manual]\n"
    "\n"
    "67683 1.000 UatCXtfDnoBPeVBGHgj4Bc manual\n"
    "7530      0.250   AbCdEfGhIjKlMnOpQrStUv\n"
    "98329 0.800 - manual\n"
)


def _service(tmp_path):
    """A ScheduleService on a throwaway data dir, built the way tests here do.

    Constructed directly from Settings rather than get_settings(), which is
    lru_cache'd and would leak one test's data dir into the next.
    """
    from app.config import Settings
    from app.services.schedule_service import ScheduleService

    return ScheduleService(Settings(data_dir=tmp_path, mock=True))


def test_legacy_file_is_unreadable_by_the_official_tool_before_migration():
    priorities, _ = official_reader(LEGACY)
    assert priorities == {}, (
        "this is the whole reason the migration exists: every line of a "
        "legacy 4-column file is dropped, so the scheduler is handed an empty "
        "priority list and books nothing while exiting 0"
    )


def test_migration_rewrites_strict_and_harvests_modes(tmp_path):
    lists = tmp_path / "priority_lists"
    lists.mkdir(parents=True)
    (lists / "default.txt").write_text(LEGACY, encoding="utf-8")
    (lists / "manifest.json").write_text(
        json.dumps({"active": "default", "lists": [{"slug": "default", "name": "Default"}]}),
        encoding="utf-8",
    )

    _service(tmp_path)

    rewritten = (lists / "default.txt").read_text(encoding="utf-8")
    priorities, transmitters = official_reader(rewritten)
    assert priorities == {"67683": 1.0, "7530": 0.25}, (
        "after migration the official reader must see the pinned rows; it saw "
        f"{priorities}"
    )
    assert transmitters["67683"] == "UatCXtfDnoBPeVBGHgj4Bc"

    sidecar = json.loads((lists / "default.meta.json").read_text(encoding="utf-8"))
    assert sidecar["version"] == 1
    by_norad = {row["norad"]: row for row in sidecar["rows"]}
    assert by_norad[67683]["mode"] == "manual", (
        "the Auto/Manual pin has to survive the move out of the file, or every "
        "manually weighted row silently reverts to being recomputed on reorder"
    )
    assert by_norad[98329]["uuid"] is None and by_norad[98329]["mode"] == "manual", (
        "the unpinned row cannot be written to the .txt at all, so if the "
        "sidecar does not keep it the operator's row just disappears on save"
    )
    assert 98329 in by_norad, "the unpinned row must not be lost by the migration"


def test_migration_backs_up_once_and_is_idempotent(tmp_path):
    lists = tmp_path / "priority_lists"
    lists.mkdir(parents=True)
    (lists / "default.txt").write_text(LEGACY, encoding="utf-8")
    (lists / "manifest.json").write_text(
        json.dumps({"active": "default", "lists": [{"slug": "default", "name": "Default"}]}),
        encoding="utf-8",
    )

    _service(tmp_path)
    backup = lists / "default.bak"
    assert backup.is_file(), "the operator's original file must be recoverable"
    assert backup.read_text(encoding="utf-8") == LEGACY

    after_first = (lists / "default.txt").read_text(encoding="utf-8")
    sidecar_first = (lists / "default.meta.json").read_text(encoding="utf-8")

    _service(tmp_path)  # second construction, e.g. a container restart

    assert (lists / "default.txt").read_text(encoding="utf-8") == after_first, (
        "a second pass must change nothing, or every restart rewrites the file"
    )
    assert (lists / "default.meta.json").read_text(encoding="utf-8") == sidecar_first
    assert backup.read_text(encoding="utf-8") == LEGACY, (
        "and the .bak must not be overwritten by the already-migrated file, "
        "which would destroy the only copy of the original"
    )


def test_an_already_strict_file_is_not_rewritten_but_still_gets_a_sidecar(tmp_path):
    lists = tmp_path / "priority_lists"
    lists.mkdir(parents=True)
    strict = render_priority_file([PINNED, PLAIN])
    (lists / "default.txt").write_text(strict, encoding="utf-8")
    (lists / "manifest.json").write_text(
        json.dumps({"active": "default", "lists": [{"slug": "default", "name": "Default"}]}),
        encoding="utf-8",
    )

    _service(tmp_path)

    assert not (lists / "default.bak").exists(), (
        "nothing was wrong with this file, so there is nothing to back up"
    )
    assert (lists / "default.txt").read_text(encoding="utf-8") == strict
    assert (lists / "default.meta.json").is_file(), (
        "it still needs a sidecar, or the migration revisits it on every start"
    )
