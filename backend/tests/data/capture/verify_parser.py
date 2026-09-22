"""Run the dashboard's own parse_output() over the captured transcript and
check every field of every row against the raw text.

Host python3 is enough; autoscheduler_cli imports nothing third-party.

    python3 backend/tests/data/capture/verify_parser.py
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE.parent
REPO = HERE.parents[3]
CLI = REPO / "backend" / "app" / "services" / "autoscheduler_cli.py"
LOG = DATA / "schedule_dry_run.log"


def load_cli():
    spec = importlib.util.spec_from_file_location("autoscheduler_cli", CLI)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    cli = load_cli()
    lines = LOG.read_text().splitlines()
    parsed = cli.parse_output(lines)

    # Ground truth straight off the text, independent of the parser.
    raw_rows = []
    in_table = False
    for raw in lines:
        _lvl, _lg, msg = cli.split_prefix(raw)
        if msg.strip().startswith(cli.TABLE_HEADER):
            in_table = True
            continue
        if in_table and re.match(r"^\s*\d+ \| [YN]  ", msg):
            raw_rows.append(msg)
    raw_y = [r for r in raw_rows if r.split(" | ")[1].strip() == "Y"]
    raw_n = [r for r in raw_rows if r.split(" | ")[1].strip() == "N"]

    print(f"raw table rows: {len(raw_rows)}  (Y={len(raw_y)}, N={len(raw_n)})")
    print(f"parsed        : planned={len(parsed.planned)} "
          f"already_scheduled={len(parsed.already_scheduled)}")
    print(f"efficiency    : {parsed.efficiency}")
    print(f"no_passes={parsed.no_passes} attempted_booking={parsed.attempted_booking} "
          f"booked_log={parsed.booked_log}")
    print(f"notices       : {len(parsed.notices)}")
    print(f"classify_failure: {cli.classify_failure(lines, 0)}")

    problems = []
    if len(parsed.planned) != len(raw_n):
        problems.append(f"planned {len(parsed.planned)} != raw N rows {len(raw_n)}")
    if len(parsed.already_scheduled) != len(raw_y):
        problems.append(
            f"already_scheduled {len(parsed.already_scheduled)} != raw Y rows {len(raw_y)}"
        )

    # Field-by-field against the text.
    by_key = {}
    for row in parsed.planned + parsed.already_scheduled:
        by_key.setdefault((row.norad, row.start.strftime("%Y-%m-%dT%H:%M:%S")), []).append(row)

    duration_mismatches = []
    for msg in raw_rows:
        f = msg.split(" | ")
        norad_txt, start_txt, end_txt, dur_txt = f[2].strip(), f[3].strip(), f[4].strip(), f[5].strip()
        geo, prio_txt, uuid_txt = f[6], f[7].strip(), f[8].strip()
        mode_txt, freq_txt, name_txt = f[9].strip(), f[10].strip(), " | ".join(f[11:]).strip()
        key = (int(norad_txt), start_txt)
        cands = by_key.get(key)
        if not cands:
            problems.append(f"row not parsed at all: {msg!r}")
            continue
        row = cands.pop(0)

        checks = {
            "norad": (row.norad, int(norad_txt)),
            "start": (row.start.strftime("%Y-%m-%dT%H:%M:%S"), start_txt),
            "end": (row.end.strftime("%Y-%m-%dT%H:%M:%S"), end_txt),
            "priority": (row.priority, float(prio_txt)),
            "uuid": (row.transmitter_uuid, uuid_txt),
            "mode": (row.mode, mode_txt),
            "misuse": (row.frequency_violator, freq_txt == "Y"),
            "name": (row.name, name_txt),
            "sch": (row.already_scheduled, f[1].strip() == "Y"),
        }
        g = geo.split()
        checks["az_rise"] = (row.az_rise, float(g[0]))
        checks["elevation"] = (row.elevation, float(g[1]))
        checks["az_set"] = (row.az_set, float(g[2]))

        for what, (got, want) in checks.items():
            if got != want:
                problems.append(f"{what}: parsed {got!r} != text {want!r}  in {msg!r}")

        if row.start.tzinfo is None or row.start.utcoffset() != dt.timedelta(0):
            problems.append(f"start not UTC-aware: {row.start!r}")
        if row.end.tzinfo is None or row.end.utcoffset() != dt.timedelta(0):
            problems.append(f"end not UTC-aware: {row.end!r}")

        # Derived duration vs the tool's own Duration column.
        h, m, s = (int(x) for x in dur_txt.split(":"))
        printed = h * 3600 + m * 60 + s
        if row.duration_s != printed:
            duration_mismatches.append((msg, printed, row.duration_s))

    print(f"\nduration_s vs printed Duration column: "
          f"{len(duration_mismatches)}/{len(raw_rows)} rows disagree")
    for msg, printed, derived in duration_mismatches[:6]:
        f = msg.split(" | ")
        print(f"   {f[2].strip()} {f[3].strip()} -> {f[4].strip()}: "
              f"printed={printed}s derived={derived}s (delta {derived - printed:+d})")

    # Priority-list entries that produced nothing.
    prio_norads = []
    for line in (HERE / "priorities.txt").read_text().splitlines():
        bare = line.split("#")[0].strip()
        if bare:
            prio_norads.append(int(bare.split()[0]))
    notices = cli.missing_priority_notices(prio_norads, parsed)
    print(f"\nmissing_priority_notices: {len(notices)}")
    for n in notices:
        print("   ", n["message"])

    print("\n--- problems ---")
    if not problems:
        print("none")
    for p in problems:
        print("  ", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
