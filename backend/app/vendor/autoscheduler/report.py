"""Terminal tables and file exports."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .network_client import Station
from .priorities import Finding
from .selector import TIER_MISSION, Selection

console = Console()


@dataclass
class PlanReport:
    """Everything a `plan()` run found worth flagging, beyond the selection
    itself - previously only ever printed to the terminal (see cli.py) and
    discarded. Kept separate from `Selection` because it is diagnostic, not
    part of what got booked.
    """

    schedulable: bool = True
    has_antennas: bool = True
    passes_found: int = 0
    passes_gated: int = 0
    findings: list[Finding] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)   # [{"norad": int, "reason": str}]
    thin_history: str | None = None


def print_station(station: Station) -> None:
    ok = "[green]yes[/]" if station.schedulable else "[red]no[/]"
    status_colour = "green" if station.status.lower() == "online" else "yellow"

    table = Table(title=f"SatNOGS station {station.id} - {station.name}",
                  show_header=False, title_style="bold")
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row("Status", f"[{status_colour}]{station.status}[/]")
    table.add_row("Connected", "yes" if station.is_connected else "no")
    table.add_row("Can be scheduled", ok)
    table.add_row("Location",
                  f"{station.lat:.5f}, {station.lng:.5f}  {station.altitude_m:.0f} m"
                  + (f"  ({station.qthlocator})" if station.qthlocator else ""))
    table.add_row("Horizon", f"min_horizon {station.min_horizon:g}deg, "
                             f"min_culmination {station.min_culmination:g}deg")
    table.add_row("Observations", f"{station.observations} total, "
                                  f"{station.future_observations} upcoming, "
                                  f"{station.success_rate}% success")
    table.add_row("Owner", station.owner or "-")
    console.print(table)

    antennas = Table(title="Antenna coverage", title_style="bold")
    antennas.add_column("Band")
    antennas.add_column("From (MHz)", justify="right")
    antennas.add_column("To (MHz)", justify="right")
    antennas.add_column("Type")
    for a in sorted(station.antennas, key=lambda x: x.low_hz):
        antennas.add_row(a.band, f"{a.low_hz / 1e6:.3f}", f"{a.high_hz / 1e6:.3f}", a.kind)
    console.print(antennas)

    gaps = _coverage_gaps(station)
    if gaps:
        console.print(
            "[yellow]Note:[/] this station's coverage is not continuous. "
            "Nothing will be scheduled in "
            + ", ".join(f"{lo / 1e6:.3f}-{hi / 1e6:.3f} MHz" for lo, hi in gaps)
            + "."
        )


def _coverage_gaps(station: Station) -> list[tuple[float, float]]:
    segments = sorted(station.segments)
    return [
        (segments[i][1], segments[i + 1][0])
        for i in range(len(segments) - 1)
        if segments[i + 1][0] > segments[i][1]
    ]


def print_plan(selection: Selection, dry_run: bool) -> None:
    title = "Planned observations" + (" (dry run - nothing booked)" if dry_run else "")
    table = Table(title=title, title_style="bold", padding=(0, 1))
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Start UTC", no_wrap=True)
    table.add_column("Min", justify="right", no_wrap=True)
    table.add_column("El", justify="right", no_wrap=True)
    table.add_column("NORAD", justify="right", no_wrap=True)
    table.add_column("Satellite", max_width=20, overflow="ellipsis", no_wrap=True)
    table.add_column("MHz", justify="right", no_wrap=True)
    table.add_column("Mode", max_width=8, overflow="ellipsis", no_wrap=True)
    table.add_column("Seen", justify="right", no_wrap=True)
    table.add_column("Score", justify="right", no_wrap=True)

    for n, slot in enumerate(selection.slots, 1):
        c = slot.candidate
        mission = c.tier == TIER_MISSION
        table.add_row(
            str(n),
            slot.start.strftime("%d %b %H:%M"),
            f"{slot.duration_s / 60:.0f}",
            f"{c.pass_.max_el:.0f}",
            str(c.pass_.norad_cat_id),
            ("* " if mission else "") + c.pass_.name,
            f"{c.transmitter.downlink_mhz:.3f}",
            c.transmitter.mode or "-",
            str(c.observed_here),
            f"{c.score:.2f}",
            style="bold cyan" if mission else None,
        )
    console.print(table)

    skipped = selection.considered - len(selection.slots) - selection.rejected_conflict
    console.print(
        f"[dim]{len(selection.slots)} booked out of {selection.considered} candidate "
        f"pass(es). {selection.rejected_conflict} clashed with something already on "
        f"the calendar; {skipped} were never reached because the "
        f"--max-schedule cap was already met. El is max elevation in degrees, Seen is "
        f"how often this station has recorded that satellite, * is the mission "
        f"satellite.[/]"
    )


def print_findings(findings, path) -> None:
    """Render priority-file validation results."""
    from .priorities import ERROR, OK, WARNING

    mark = {ERROR: "[red]ERROR[/]", WARNING: "[yellow]warn[/]", OK: "[green]ok[/]"}
    table = Table(title=f"Priority file check - {path}", title_style="bold", padding=(0, 1))
    table.add_column("Line", justify="right", style="dim", no_wrap=True)
    table.add_column("", no_wrap=True)
    table.add_column("NORAD", justify="right", no_wrap=True)
    table.add_column("Satellite", max_width=20, overflow="ellipsis", no_wrap=True)
    table.add_column("Detail", overflow="fold")

    for f in findings:
        table.add_row(str(f.line), mark.get(f.severity, f.severity),
                      str(f.norad_cat_id), f.satellite, f.message)
    console.print(table)

    errors = sum(1 for f in findings if f.severity == ERROR)
    warnings = sum(1 for f in findings if f.severity == WARNING)
    if errors:
        console.print(f"[red]{errors} entr{'y' if errors == 1 else 'ies'} will never be "
                      f"scheduled.[/] The official scheduler drops these silently; fix the "
                      f"file and re-run this check.")
    elif warnings:
        console.print(f"[yellow]{warnings} warning(s), but every entry is schedulable.[/]")
    else:
        console.print("[green]Every entry checks out.[/]")


def write_csv(path: Path, selection: Selection, station_id: int) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "station", "start_utc", "end_utc", "duration_s", "norad_cat_id",
            "satellite", "max_elevation_deg", "aos_az_deg", "los_az_deg",
            "transmitter_uuid", "downlink_hz", "mode", "observed_here", "score",
        ])
        for slot in selection.slots:
            c = slot.candidate
            writer.writerow([
                station_id,
                slot.start.isoformat(), slot.end.isoformat(), f"{slot.duration_s:.0f}",
                c.pass_.norad_cat_id, c.pass_.name, f"{c.pass_.max_el:.1f}",
                f"{c.pass_.aos_az:.1f}", f"{c.pass_.los_az:.1f}",
                c.transmitter.uuid, c.transmitter.downlink_hz, c.transmitter.mode,
                c.observed_here, f"{c.score:.4f}",
            ])


def write_json(path: Path, selection: Selection, station_id: int) -> None:
    path.write_text(json.dumps(_selection_payload(selection, station_id), indent=2),
                     encoding="utf-8")


def _selection_payload(selection: Selection, station_id: int) -> dict:
    """The plain-dict shape shared by ``write_json`` and the vendored dashboard
    service, which needs the same payload in memory without a round trip
    through disk.
    """
    return {
        "station": station_id,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "considered": selection.considered,
        "rejected_conflict": selection.rejected_conflict,
        "rejected_capped": selection.rejected_capped,
        "observations": [
            {
                "start": slot.start.isoformat(),
                "end": slot.end.isoformat(),
                "duration_s": round(slot.duration_s),
                "norad_cat_id": slot.candidate.pass_.norad_cat_id,
                "satellite": slot.candidate.pass_.name,
                "max_elevation_deg": round(slot.candidate.pass_.max_el, 1),
                "aos_azimuth_deg": round(slot.candidate.pass_.aos_az, 1),
                "los_azimuth_deg": round(slot.candidate.pass_.los_az, 1),
                "transmitter_uuid": slot.candidate.transmitter.uuid,
                "downlink_hz": slot.candidate.transmitter.downlink_hz,
                "mode": slot.candidate.transmitter.mode,
                "observed_here": slot.candidate.observed_here,
                "score": round(slot.candidate.score, 4),
                "is_mission": slot.candidate.tier == TIER_MISSION,
            }
            for slot in selection.slots
        ],
    }
