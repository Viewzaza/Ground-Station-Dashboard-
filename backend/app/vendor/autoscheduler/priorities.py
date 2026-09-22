"""Priority files and the station-local scarcity signal.

The file format matches the official satnogs-auto-scheduler so an existing
priorities file drops straight in, and so one we write drops straight back out:

    # norad_id weight transmitter_uuid
    67683 1.000 UatCXtfDnoBPeVBGHgj4Bc

``weight`` runs 0.0 to 1.0.

Note the asymmetry between the two halves of this module, which is deliberate
and is what makes the round trip safe:

  * ``write_priority_file`` is STRICT. Exactly three single-space-separated
    fields per line, because that is all the official reader accepts.
  * ``parse_priority_file`` is TOLERANT. It still reads 1, 2, 3 and 4 field
    lines and runs of whitespace, so every file ever written by an older
    version of this dashboard - or by hand - keeps loading.

A row with no transmitter UUID cannot be expressed in three fields at all.
Those rows live in ``ScheduleService``'s ``.meta.json`` sidecar alongside the
list, together with each row's Auto/Manual ``mode``, and get a transmitter
resolved for them at run time.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# How the two signals trade off. A hand-written priority is worth more than a
# scarcity hunch, so it carries twice the weight.
PRIORITY_WEIGHT = 2.0
SCARCITY_WEIGHT = 1.0


@dataclass(frozen=True)
class Priority:
    norad_cat_id: int
    weight: float
    transmitter_uuid: str | None = None
    line: int = 0
    # "auto" (default): the dashboard is free to recompute this weight from
    # list order (e.g. on a drag-reorder). "manual": the operator pinned this
    # exact weight and it must survive reorders elsewhere in the list. The
    # scheduler itself only ever reads `weight` - this flag is dashboard-side
    # bookkeeping. It used to be a 4th column in the file, which silently cost
    # the official reader the whole line; it now round-trips through
    # ScheduleService's .meta.json sidecar instead.
    mode: str = "auto"


def parse_priority_file(path: Path) -> dict[int, Priority]:
    """Read a priorities file. Bad lines are warned about, not fatal.

    Deliberately more forgiving than the official reader, which splits on a
    single space through ``csv`` and calls any line without exactly three
    fields malformed - so two spaces between columns silently loses a line.
    Splitting on runs of whitespace accepts every file that reader accepts,
    and some it does not.

    This tolerance is not laziness, it is the backward-compatibility path: the
    writer is strict, the reader is not, so every file the dashboard has ever
    written still loads. In particular it still accepts the two shapes this
    module no longer writes - a 4th "manual" field, and a literal "-" standing
    in for an absent transmitter - so a file written before the sidecar existed
    keeps its Auto/Manual flags on first read. ``_migrate_priority_files()``
    then harvests them into the sidecar once.

    A 1-field line (a bare NORAD) is also accepted, and defaults its weight to
    1.0 - the MAXIMUM. That is upstream's behaviour and is kept for
    compatibility, but it means a stray number on its own line becomes the
    highest-priority satellite in the list rather than an inert typo.
    """
    priorities: dict[int, Priority] = {}
    if not path.is_file():
        raise FileNotFoundError(f"no priority file at {path}")

    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        try:
            norad = int(fields[0])
            weight = float(fields[1]) if len(fields) > 1 else 1.0
        except (ValueError, IndexError):
            log.warning("%s:%d is not 'norad weight [uuid]', skipping: %r", path, lineno, raw)
            continue
        uuid = fields[2] if len(fields) > 2 and fields[2] != "-" else None
        mode = "manual" if len(fields) > 3 and fields[3] == "manual" else "auto"
        if not 0.0 <= weight <= 1.0:
            log.warning("%s:%d weight %s is outside 0.0-1.0, clamping", path, lineno, weight)
            weight = min(1.0, max(0.0, weight))
        if norad in priorities:
            # The official reader lets the last line win without comment, so a
            # satellite listed twice quietly loses its first transmitter.
            log.warning("%s:%d NORAD %d is already listed on line %d; the later line wins",
                        path, lineno, norad, priorities[norad].line)
        priorities[norad] = Priority(norad, weight, uuid, lineno, mode)

    log.info("loaded %d priorit%s from %s",
             len(priorities), "y" if len(priorities) == 1 else "ies", path)
    return priorities


def write_priority_file(path: Path, entries: list[Priority]) -> None:
    """Serialize entries to the format the OFFICIAL satnogs-auto-scheduler reads.

    Local addition (not in the upstream CLI, which only ever seeds a fresh file
    from history via ``generate_priority_file`` and would discard existing
    weights/UUIDs). This is what lets the dashboard round-trip an edited
    priority list back to the file the scheduler itself reads.

    Every data line is exactly three single-space-separated fields::

        67683 1.000 UatCXtfDnoBPeVBGHgj4Bc

    That shape is not a style choice, it is the whole contract. The official
    reader parses with ``csv.reader(delimiter=" ")`` and discards any line that
    does not yield exactly three fields, so all of these lose the satellite:

      * a 4th column (this writer used to emit ``manual`` there - four fields),
      * a 2-field line with no UUID,
      * any run of two or more spaces, because ``csv`` emits an empty field
        between repeated delimiters - so no column alignment, ever,
      * a tab anywhere, which is not the delimiter at all, so the line
        collapses to one field.

    The drop is not silent - it logs a warning per line - but under ``-f``
    ("only priority") a dropped line means that satellite is simply never
    scheduled, and the run still exits 0 looking perfectly healthy.

    NORAD ids are written bare (``str(int(...))``) because the official reader
    matches them as strings against ``str(tle["norad_cat_id"])``. A zero-padded
    ``067683`` compares unequal to ``67683`` and never matches anything.

    An entry with no transmitter UUID is NOT written: there is no three-field
    way to say "no UUID". Those rows are not lost - ``ScheduleService`` keeps
    the full list, unpinned rows included, in a ``.meta.json`` sidecar and
    resolves a transmitter for them at run time. This file is the projection of
    that list which the scheduler can actually read.

    ``mode`` is deliberately NOT written any more (it used to be a 4th column).
    It lives in the sidecar now, for the reason above.

    Atomic write, matching the tmp-write + ``Path.replace()`` pattern
    ``cache.py`` uses, so a crash mid-write cannot truncate the file the next
    scheduler run depends on.
    """
    tmp = path.with_suffix(".tmp")
    tmp.write_text(render_priority_file(entries), encoding="utf-8")
    tmp.replace(path)


def render_priority_file(entries: list[Priority]) -> str:
    """The exact text ``write_priority_file`` would write.

    Split out so the dashboard's export endpoint can hand the operator the same
    bytes the scheduler reads without going through a file, which keeps one
    definition of the format rather than two that can drift.
    """
    lines = [
        "# Edited from the Station Schedule dashboard panel.",
        "# Format: norad_id weight(0.0-1.0) transmitter_uuid - exactly 3 fields,",
        "# single spaces. Comment and blank lines are ignored by the scheduler.",
        "",
    ]
    for entry in entries:
        if not entry.transmitter_uuid:
            # Nothing sensible to write: a 2-field line would be dropped by the
            # official reader anyway. The sidecar keeps this row.
            continue
        lines.append(
            f"{int(entry.norad_cat_id)} {entry.weight:.3f} {entry.transmitter_uuid}"
        )
    return "\n".join(lines) + "\n"


# -- validation ------------------------------------------------------------

ERROR, WARNING, OK = "error", "warning", "ok"


@dataclass(frozen=True)
class Finding:
    """One verdict on one priority-file entry."""
    line: int
    norad_cat_id: int
    satellite: str
    severity: str
    message: str


def validate_priorities(
    priorities: dict[int, Priority],
    station,
    transmitters_by_uuid: dict[str, dict],
    satellites_by_norad: dict[int, dict],
    tle_norads: set[int],
) -> list[Finding]:
    """Check every entry against the live catalogue and the station's antennas.

    The official scheduler never validates a priority file. A UUID that is
    stale, mistyped, or belongs to another satellite simply fails an equality
    test deep inside pass selection and the satellite vanishes from the
    schedule - no error, no warning, nothing at any log level. This function
    exists to make that failure visible before it costs you observations.
    """
    from .db_client import frequency_is_covered

    findings: list[Finding] = []
    for norad in sorted(priorities, key=lambda n: priorities[n].line):
        priority = priorities[norad]
        satellite = satellites_by_norad.get(norad)
        name = (satellite or {}).get("name") or "unknown"

        def add(severity: str, message: str) -> None:
            findings.append(Finding(priority.line, norad, name, severity, message))

        if satellite is None:
            add(ERROR, "NORAD id is not in SatNOGS DB at all")
            continue
        if satellite.get("status") != "in orbit":
            add(ERROR, f"satellite status is {satellite.get('status')!r}, not 'in orbit'")
            continue
        if norad not in tle_norads:
            add(ERROR, "SatNOGS DB has no TLE for this satellite, so it cannot be predicted")
            continue
        if satellite.get("is_frequency_violator"):
            add(WARNING, "DB flags this as a frequency violator; SatNOGS may refuse it")

        uuid = priority.transmitter_uuid
        if not uuid:
            add(OK, "no transmitter pinned; the best available one will be chosen")
            continue

        transmitter = transmitters_by_uuid.get(uuid)
        if transmitter is None:
            hint = ""
            # A single mistyped or appended character is the common case, so
            # say which real transmitter was probably meant.
            near = [
                other for other, record in transmitters_by_uuid.items()
                if record.get("norad_cat_id") == norad
                and (uuid.startswith(other) or other.startswith(uuid))
            ]
            if near:
                hint = f" - did you mean {near[0]} ({len(near[0])} chars)?"
            add(ERROR, f"transmitter UUID is {len(uuid)} chars and matches nothing in "
                       f"SatNOGS DB{hint}")
            continue
        if transmitter.get("norad_cat_id") != norad:
            add(ERROR, f"that transmitter belongs to NORAD "
                       f"{transmitter.get('norad_cat_id')}, not {norad}")
            continue
        if not transmitter.get("alive") or transmitter.get("status") != "active":
            add(ERROR, f"transmitter is {transmitter.get('status')!r} "
                       f"(alive={bool(transmitter.get('alive'))})")
            continue

        downlink = transmitter.get("downlink_low")
        if not downlink:
            add(ERROR, "transmitter has no downlink frequency")
            continue
        if station is not None and not frequency_is_covered(float(downlink), station.segments):
            add(ERROR, f"{float(downlink) / 1e6:.3f} MHz is outside this station's "
                       f"antenna coverage")
            continue

        add(OK, f"{float(downlink) / 1e6:.3f} MHz {transmitter.get('mode') or ''}".strip())

    return findings


def count_errors(findings: list[Finding]) -> int:
    return sum(1 for f in findings if f.severity == ERROR)


def scarcity_bonus(history: Counter, norads: set[int]) -> dict[int, float]:
    """Map each candidate satellite to a 0.0-1.0 "we have neglected this" score.

    A satellite the station has never recorded scores 1.0; the one it has
    recorded most scores 0.0. The scale is relative to the candidate set, not
    to the whole catalogue, so the signal stays meaningful whether the station
    has fifty observations or five thousand.
    """
    if not norads:
        return {}
    counts = {n: history.get(n, 0) for n in norads}
    busiest = max(counts.values())
    if busiest == 0:
        # Nothing in this set has ever been observed here - no signal to give.
        return {n: 1.0 for n in norads}
    return {n: 1.0 - (c / busiest) for n, c in counts.items()}


def generate_priority_file(path: Path, history: Counter, top: int = 40) -> int:
    """Write a starter priorities file seeded from what the station observes.

    The point is to give you something to edit rather than a blank page: the
    satellites you already record most are the ones you most likely care about.
    """
    lines = [
        "# Generated by satnogs-autoscheduler from this station's own history.",
        "# Format: norad_id  weight(0.0-1.0)  [transmitter_uuid]",
        "# Edit freely - higher weight wins slots against lower.",
        "",
    ]
    for norad, count in history.most_common(top):
        lines.append(f"{norad:<10} 0.5      # observed {count} time(s) here")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return min(top, len(history))
