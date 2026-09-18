"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console

from . import __version__
from .cache import Cache
from .config import (
    DEFAULT_BUFFER_S, DEFAULT_HOURS, DEFAULT_MAX_DURATION_S, DEFAULT_MAX_SCHEDULE,
    DEFAULT_MIN_CULMINATION_DEG, DEFAULT_MIN_DURATION_S, DEFAULT_MISSION_NORAD, Settings,
)
from .db_client import DbClient, pick_transmitter
from .http import SatnogsHTTPError
from .network_client import NetworkClient, Station, to_schedule_item
from .predictor import Predictor
from .priorities import (
    count_errors, generate_priority_file, parse_priority_file, scarcity_bonus,
    validate_priorities,
)
from .report import (
    PlanReport, console, print_findings, print_plan, print_station, write_csv, write_json,
)
from .selector import TIER_MISSION, build_candidates, select

log = logging.getLogger("autoscheduler")

HISTORY_PAGES_DEFAULT = 12     # 300 observations, about 75 seconds, cached a day


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autoscheduler",
        description="Plan and book SatNOGS observations for a ground station.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-s", "--station", type=int,
                        help="SatNOGS ground station id (or set SATNOGS_STATION_ID)")
    common.add_argument("--cache-dir", help="where to keep cached API data (default: ./cache)")
    common.add_argument("--offline", action="store_true", default=None,
                        help="use cached data only; never touch the network")
    common.add_argument("-v", "--verbose", action="store_true", default=None,
                        help="log what is happening")

    planning = argparse.ArgumentParser(add_help=False)
    planning.add_argument("--hours", type=float, default=None,
                          help=f"how far ahead to plan (default: {DEFAULT_HOURS:g})")
    planning.add_argument("--min-culmination", type=float, default=None, dest="min_culmination",
                          help="skip passes peaking below this elevation "
                               "(default: the station's own min_culmination)")
    planning.add_argument("--min-horizon", type=float, default=None, dest="min_horizon",
                          help="elevation that counts as AOS/LOS "
                               "(default: the station's own min_horizon)")
    planning.add_argument("--min-duration", type=float, default=None, dest="min_duration_s",
                          metavar="SECONDS",
                          help=f"skip passes shorter than this (default: {DEFAULT_MIN_DURATION_S:g})")
    planning.add_argument("--max-duration", type=float, default=None, dest="max_duration_s",
                          metavar="SECONDS",
                          help=f"clamp long passes to this (default: {DEFAULT_MAX_DURATION_S:g})")
    planning.add_argument("--buffer", type=float, default=None, dest="buffer_s",
                          metavar="SECONDS",
                          help=f"idle gap to leave between observations, for the rotator "
                               f"(default: {DEFAULT_BUFFER_S:g})")
    planning.add_argument("--max-schedule", type=int, default=None,
                          help=f"never book more than this many in one run "
                               f"(default: {DEFAULT_MAX_SCHEDULE})")
    planning.add_argument("-P", "--priority-file", type=Path, default=None,
                          help="file of 'norad weight [transmitter_uuid]' lines")
    planning.add_argument("--mission-norad", type=int, default=None,
                          help=f"satellite that outranks everything "
                               f"(default: {DEFAULT_MISSION_NORAD}, KNACKSAT-2)")
    planning.add_argument("--no-mission", action="store_true",
                          help="do not treat any satellite as the mission satellite")
    planning.add_argument("--exclude", type=int, nargs="+", metavar="NORAD",
                          help="never schedule these satellites")
    planning.add_argument("--only", type=int, nargs="+", metavar="NORAD",
                          help="schedule only these satellites")
    planning.add_argument("--modes", nargs="+", metavar="MODE",
                          help="only these transmitter modes, e.g. BPSK GFSK FSK")
    planning.add_argument("--service", nargs="+", metavar="SERVICE", dest="services",
                          help="only these SatNOGS DB services, e.g. Amateur. Most "
                               "records say Unknown, so this filters hard.")
    planning.add_argument("--only-priority", action="store_true", default=None,
                          help="schedule only satellites named in the priority file "
                               "(the official scheduler's -f)")
    planning.add_argument("-M", "--min-priority", type=float, default=None,
                          help="ignore priority entries weighted below this (default: 0.0)")
    planning.add_argument("--allow-frequency-violators", action="store_true", default=None,
                          help="include satellites the DB flags as transmitting out of "
                               "band. SatNOGS may refuse to schedule them.")
    planning.add_argument("--history-pages", type=int, default=HISTORY_PAGES_DEFAULT,
                          help=f"pages of station history to weigh scarcity from, 25 per page "
                               f"(default: {HISTORY_PAGES_DEFAULT}; 0 turns scarcity off)")
    planning.add_argument("--csv", type=Path, help="also write the plan to this CSV file")
    planning.add_argument("--json", type=Path, dest="json_out",
                          help="also write the plan to this JSON file")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stations", parents=[common],
                   help="show what the Network knows about a station")

    sub.add_parser("plan", parents=[common, planning],
                   help="work out what to observe, and book nothing")

    schedule = sub.add_parser("schedule", parents=[common, planning],
                              help="work out what to observe, and book it")
    schedule.add_argument("--execute", action="store_true", default=None,
                          help="actually submit to SatNOGS. Without it this is a dry run.")
    schedule.add_argument("-y", "--yes", action="store_true", default=None, dest="assume_yes",
                          help="do not ask for confirmation before booking")

    cache_cmd = sub.add_parser("cache", parents=[common], help="inspect or clear cached API data")
    cache_cmd.add_argument("--clear", action="store_true", help="delete everything cached")

    prio = sub.add_parser("priorities", parents=[common],
                          help="write a starter priority file from station history")
    prio.add_argument("-o", "--out", type=Path, default=Path("priorities.txt"),
                      help="where to write it (default: priorities.txt)")
    prio.add_argument("--history-pages", type=int, default=HISTORY_PAGES_DEFAULT)

    validate = sub.add_parser("validate", parents=[common],
                              help="check a priority file against the live SatNOGS DB")
    validate.add_argument("-P", "--priority-file", type=Path, default=None,
                          help="the file to check (or set SATNOGS_PRIORITY_FILE)")

    return parser


def resolve_settings(args) -> Settings:
    settings = Settings.from_env().merge_args(args)
    if not settings.station_id and args.command != "cache":
        raise SystemExit(
            "No station given. Pass --station <id>, or set SATNOGS_STATION_ID in .env"
        )
    return settings


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if not verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)


def resolve_limits(settings: Settings, station: Station) -> tuple[float, float]:
    """Work out the horizon and culmination this run will use.

    A station can mark either limit "hard", meaning the owner does not want
    observations below it. We honour that: a hard limit is a floor nothing on
    the command line may go under. A soft limit is only a default, which is
    what lets the official tool's `-m 3` record 3 degree passes on a station
    that publishes 10.
    """
    horizon = settings.min_horizon if settings.min_horizon is not None else station.min_horizon
    if station.horizon_hard_limit:
        horizon = max(horizon, station.min_horizon)

    culmination = (settings.min_culmination if settings.min_culmination is not None
                   else DEFAULT_MIN_CULMINATION_DEG)
    if station.min_culmination_hard_limit:
        culmination = max(culmination, station.min_culmination)
    return horizon, culmination


def plan(settings: Settings, args) -> tuple[Station, object, PlanReport] | None:
    """Run the whole pipeline and return the station, the selection, and a
    PlanReport of everything worth flagging along the way.

    The PlanReport is purely additive: every console.print() below stays
    exactly as it was, so the CLI's own output is unchanged. It exists so a
    caller that isn't a terminal (the dashboard) can see what would otherwise
    only ever reach stdout.
    """
    cache = Cache(settings.cache_dir, offline=settings.offline)
    network = NetworkClient(settings, cache)
    db = DbClient(settings, cache)
    report = PlanReport()

    console.print(f"[dim]Reading station {settings.station_id}...[/]")
    station = network.get_station(settings.station_id)
    report.has_antennas = bool(station.antennas)
    if not station.antennas:
        console.print("[red]This station publishes no antennas, so nothing can be matched "
                      "to it.[/]")
        return None
    report.schedulable = station.schedulable
    if not station.schedulable:
        console.print(
            f"[red]Station {station.id} is {station.status.lower()} and not connected.[/] "
            "SatNOGS refuses bookings for a station that is not connected, so this run "
            "would fail at submission. Planning anyway."
        )

    min_horizon, min_culmination = resolve_limits(settings, station)

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(hours=settings.hours)

    # Read the priority file before anything is filtered, because naming a
    # satellite here exempts it from the mode and service filters below.
    priorities = {}
    if settings.priority_file:
        priorities = parse_priority_file(settings.priority_file)
        if settings.min_priority:
            priorities = {n: p for n, p in priorities.items()
                          if p.weight >= settings.min_priority}

    console.print("[dim]Reading transmitters, satellites and TLEs from SatNOGS DB...[/]")
    in_orbit = db.satellites_in_orbit(
        skip_frequency_violators=not settings.allow_frequency_violators
    )
    # The mission satellite and everything you listed by hand are exempt from
    # the mode and service filters, so a --service Amateur run cannot quietly
    # drop a satellite you explicitly asked for. The frequency check still
    # applies to everyone - naming a satellite does not make it audible.
    protected = set(priorities)
    if settings.mission_norad:
        protected.add(settings.mission_norad)
    by_norad = db.transmitters_for_station(
        station.segments, settings.modes or None, settings.services or None,
        always_include=protected,
    )
    tles = db.tles()

    # Only satellites that are up there, that this station can hear, and that we
    # have a current element set for.
    norads = set(by_norad) & set(in_orbit) & set(tles)
    if settings.only:
        norads &= settings.only
    norads -= settings.exclude
    if not norads:
        console.print("[red]No satellite matches this station's antennas and your filters.[/]")
        return None

    console.print(f"[dim]Propagating {len(norads)} satellite(s) over "
                  f"{settings.hours:g} h...[/]")
    predictor = Predictor(station.lat, station.lng, station.altitude_m)
    skipped = predictor.load_tles({n: tles[n] for n in norads}, now=now)
    usable = [n for n in norads if n not in skipped]
    passes = predictor.all_passes(usable, now, window_end, min_horizon)

    # The quality gates: high enough to be worth recording, long enough to be
    # worth the rotator moving.
    gated = [
        p for p in passes
        if p.max_el >= min_culmination and p.duration_s >= settings.min_duration_s
    ]
    report.passes_found = len(passes)
    report.passes_gated = len(gated)
    console.print(f"[dim]{len(passes)} pass(es) above {min_horizon:g}deg, "
                  f"{len(gated)} clear the {min_culmination:g}deg / "
                  f"{settings.min_duration_s / 60:g} min gates.[/]")
    if not gated:
        console.print("[yellow]Nothing to schedule in this window.[/]")
        return station, select([], [], buffer_s=settings.buffer_s, max_schedule=0,
                               max_duration_s=settings.max_duration_s), report

    if settings.priority_file:
        # Validate before using. A stale or mistyped UUID is silently dropped by
        # the official scheduler; here it gets said out loud, then we carry on
        # with whatever is still usable.
        findings = validate_priorities(
            priorities, station,
            db.transmitters_by_uuid(), db.satellites_by_norad(), set(tles),
        )
        report.findings = findings
        if count_errors(findings):
            print_findings(findings, settings.priority_file)
        # A satellite you asked for by name should never just not appear. If one
        # dropped out of the candidate set, say which and why - being flagged a
        # frequency violator is the usual reason, and the official scheduler
        # skips those with no way to know it happened.
        for norad in sorted(set(priorities) - norads):
            if norad not in by_norad:
                why = "this station's antennas cannot reach its transmitter"
            elif norad not in tles:
                why = "SatNOGS DB has no TLE for it"
            elif norad in settings.exclude:
                why = "you excluded it with --exclude"
            elif settings.only and norad not in settings.only:
                why = "--only does not list it"
            elif not settings.allow_frequency_violators:
                why = ("the DB flags it as a frequency violator - pass "
                       "--allow-frequency-violators to schedule it anyway")
            else:
                why = "it is not marked 'in orbit' in SatNOGS DB"
            console.print(f"[yellow]Priority satellite {norad} was not considered:[/] {why}")
            report.skipped.append({"norad": norad, "reason": why})

        if settings.only_priority:
            keep = set(priorities)
            if settings.mission_norad:
                keep.add(settings.mission_norad)
            before = len(gated)
            gated = [p for p in gated if p.norad_cat_id in keep]
            console.print(f"[dim]--only-priority: {len(gated)} of {before} passes are on "
                          f"listed satellites.[/]")
    elif settings.only_priority:
        console.print("[yellow]--only-priority does nothing without a priority file.[/]")

    history = {}
    scarcity = {}
    pages = getattr(args, "history_pages", HISTORY_PAGES_DEFAULT)
    if pages > 0:
        console.print(f"[dim]Counting this station's own history "
                      f"({pages} page(s), cached for a day)...[/]")
        history = network.observation_history(settings.station_id, pages=pages)
        candidate_sats = {p.norad_cat_id for p in gated}
        scarcity = scarcity_bonus(history, candidate_sats)
        # If the history barely overlaps the candidates, almost everything ties
        # at "never observed" and the scarcity signal decides nothing - geometry
        # silently takes over. Say so rather than letting it look intentional.
        known = len(candidate_sats & set(history))
        if candidate_sats and known < len(candidate_sats) * 0.1:
            thin_msg = (
                f"Thin history: this station has recorded only {known} of "
                f"{len(candidate_sats)} candidate satellites, so the under-observed "
                f"score is near-flat and pass geometry is effectively deciding. "
                f"Raise --history-pages, or give a priority file with -P, to get a "
                f"plan that reflects what you care about."
            )
            console.print(f"[yellow]{thin_msg}[/]")
            report.thin_history = thin_msg

    candidates = build_candidates(
        gated, by_norad, priorities, scarcity, history,
        settings.mission_norad, pick_transmitter,
    )

    console.print("[dim]Reading what is already booked...[/]")
    bookings = network.future_bookings(settings.station_id, now=now)
    booked = [(b.start, b.end) for b in bookings]

    selection = select(
        candidates, booked,
        buffer_s=settings.buffer_s,
        max_schedule=settings.max_schedule,
        max_duration_s=settings.max_duration_s,
    )
    console.print(f"[dim]{len(bookings)} existing booking(s) were left untouched.[/]")
    return station, selection, report


def command_plan(settings: Settings, args) -> int:
    outcome = plan(settings, args)
    if outcome is None:
        return 1
    station, selection, _report = outcome
    print_plan(selection, dry_run=True)
    _write_exports(args, selection, station.id)
    return 0


def command_schedule(settings: Settings, args) -> int:
    outcome = plan(settings, args)
    if outcome is None:
        return 1
    station, selection, _report = outcome
    print_plan(selection, dry_run=not settings.execute)
    _write_exports(args, selection, station.id)

    if not selection.slots:
        return 0
    if not settings.execute:
        console.print("[yellow]Dry run.[/] Nothing was submitted. "
                      "Add [bold]--execute[/] to book these.")
        return 0
    if not station.schedulable:
        console.print("[red]Refusing to submit: the station is not connected, "
                      "so SatNOGS would reject the batch.[/]")
        return 1

    items = [
        to_schedule_item(station.id, slot.candidate.transmitter.uuid, slot.start, slot.end)
        for slot in selection.slots
    ]
    if not settings.assume_yes:
        console.print(f"\nAbout to book [bold]{len(items)}[/] observation(s) on "
                      f"station {station.id}.")
        try:
            answer = input("Type 'yes' to go ahead: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            console.print("[yellow]Nothing submitted.[/]")
            return 0

    cache = Cache(settings.cache_dir, offline=settings.offline)
    network = NetworkClient(settings, cache)
    result = network.schedule(items, execute=True)
    console.print(f"[green]Booked {result.accepted} of {result.submitted} "
                  f"observation(s).[/]")
    for error in result.errors:
        console.print(f"  [red]rejected[/] {error}")
    return 0 if not result.errors else 1


def command_stations(settings: Settings, args) -> int:
    cache = Cache(settings.cache_dir, offline=settings.offline)
    station = NetworkClient(settings, cache).get_station(settings.station_id)
    print_station(station)
    return 0


def command_cache(settings: Settings, args) -> int:
    cache = Cache(settings.cache_dir, offline=settings.offline)
    if args.clear:
        console.print(f"Removed {cache.clear()} cached file(s) from {cache.dir}.")
        return 0
    entries = sorted(cache.dir.glob("*.json"))
    if not entries:
        console.print(f"Nothing cached in {cache.dir}.")
        return 0
    console.print(f"Cached in {cache.dir}:")
    for path in entries:
        age = cache.age_s(path.stem)
        age_text = f"{age / 3600:.1f} h old" if age is not None else "unreadable"
        console.print(f"  {path.stem:<24} {path.stat().st_size / 1024:8.1f} kB  {age_text}")
    return 0


def command_priorities(settings: Settings, args) -> int:
    cache = Cache(settings.cache_dir, offline=settings.offline)
    network = NetworkClient(settings, cache)
    history = network.observation_history(settings.station_id, pages=args.history_pages)
    written = generate_priority_file(args.out, history)
    console.print(f"Wrote {written} satellite(s) to {args.out}. "
                  "Edit the weights, then pass it with -P.")
    return 0


def command_validate(settings: Settings, args) -> int:
    if not settings.priority_file:
        console.print("[red]Nothing to check.[/] Pass -P <file>, or set "
                      "SATNOGS_PRIORITY_FILE in .env")
        return 2
    cache = Cache(settings.cache_dir, offline=settings.offline)
    station = NetworkClient(settings, cache).get_station(settings.station_id)
    db = DbClient(settings, cache)

    console.print(f"[dim]Checking against station {station.id} and the live SatNOGS DB...[/]")
    priorities = parse_priority_file(settings.priority_file)
    findings = validate_priorities(
        priorities, station,
        db.transmitters_by_uuid(), db.satellites_by_norad(), set(db.tles()),
    )
    print_findings(findings, settings.priority_file)
    return 1 if count_errors(findings) else 0


COMMANDS = {
    "plan": command_plan,
    "schedule": command_schedule,
    "stations": command_stations,
    "cache": command_cache,
    "priorities": command_priorities,
    "validate": command_validate,
}


def _write_exports(args, selection, station_id: int) -> None:
    if getattr(args, "csv", None):
        write_csv(args.csv, selection, station_id)
        console.print(f"[dim]Wrote {args.csv}[/]")
    if getattr(args, "json_out", None):
        write_json(args.json_out, selection, station_id)
        console.print(f"[dim]Wrote {args.json_out}[/]")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(bool(getattr(args, "verbose", False)))
    try:
        settings = resolve_settings(args)
        return COMMANDS[args.command](settings, args)
    except SatnogsHTTPError as exc:
        console.print(f"[red]SatNOGS API error:[/] {exc}")
        if exc.body:
            console.print(f"[dim]{exc.body[:1000]}[/]")
        return 2
    except (RuntimeError, FileNotFoundError) as exc:
        console.print(f"[red]{exc}[/]")
        return 2
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted. Nothing was submitted.[/]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
