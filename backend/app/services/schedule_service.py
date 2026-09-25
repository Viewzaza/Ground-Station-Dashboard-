"""Bridges the vendored satnogs-autoscheduler into the dashboard.

The autoscheduler is a synchronous, network-bound CLI tool - a cold run
against live SatNOGS costs about 75 seconds, per its own history-pages
comment - so every call into it here runs off the event loop via
asyncio.to_thread, and a run is fire-and-forget from the route's point of
view: the frontend polls get_last_run() rather than a request blocking for
that long.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ..config import Settings
from ..util.nextfire import next_fire, normalize_times
from . import autoscheduler_cli
from ..vendor.autoscheduler.cache import Cache
from ..vendor.autoscheduler.config import Settings as AutoSettings
from ..vendor.autoscheduler.db_client import DbClient, pick_transmitter
from ..vendor.autoscheduler.network_client import NetworkClient
from ..vendor.autoscheduler.priorities import (
    Priority,
    parse_priority_file,
    render_priority_file,
    write_priority_file,
)

log = logging.getLogger(__name__)

_DEFAULT_PRIORITIES = Path(__file__).resolve().parent.parent / "vendor/autoscheduler/priorities.default.txt"

# Every key `save_config()` accepts, with how to cast it and whether a falsy
# value CLEARS the override back to its default. That distinction is the whole
# convention and it is not uniform: campaign_auto_commit_enabled and the
# auto-run booleans are stored literally, so False persists as False instead of
# clearing the key. Getting that wrong would silently re-enable unattended
# booking every time the operator turned it off.
#
# min_culmination_deg and start_lead_minutes do not clear either: 0 is a real
# value for both (the horizon, and "start immediately"), not an absence.
_CONFIG_FIELDS: dict[str, tuple] = {
    "station_id": (int, True),
    "db_token": (str, True),
    "network_token": (str, True),
    "campaign_auto_commit_enabled": (bool, False),
    "campaign_max_per_station": (int, True),
    "campaign_max_total": (int, True),
    "campaign_loop_until_exhausted": (bool, False),
    "auto_run_enabled": (bool, False),
    "auto_run_mode": (str, False),
    "auto_run_times": (normalize_times, False),
    "auto_run_interval_min": (int, True),
    "schedule_hours": (float, True),
    "min_culmination_deg": (float, False),
    "only_priority": (bool, False),
    "max_observation_minutes": (int, True),
    "start_lead_minutes": (int, False),
    "run_timeout_s": (int, True),
}

_DEFAULT_LIST_SLUG = "default"
_DEFAULT_LIST_NAME = "Default"

# Written into schedule_config.json once the one-time migration below has run.
# Every Station Schedule run books now - there is no dry run and no simulation -
# so a config from before that change has to be read with care, exactly once.
_REAL_ONLY_MARKER = "schedule_real_only"

# A stored result that did not come from a run that could book: the canned
# simulated result the removed mock mode wrote, and dry-run results. Either
# one on the board reads as a booking that never happened.
_NOT_REAL_BOOKED_STATES = ("mock", "dry_run")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "list"


class ScheduleService:
    def __init__(self, settings: Settings, on_state=None) -> None:
        self.s = settings
        self.on_state = on_state or (lambda component, state, detail="": None)

        self.cache_dir = settings.data_dir / "autoscheduler_cache"
        self.result_path = settings.data_dir / "schedule_last_run.json"
        self.config_file = settings.data_dir / "schedule_config.json"
        self.priority_lists_dir = settings.data_dir / "priority_lists"
        self.manifest_file = self.priority_lists_dir / "manifest.json"
        # Rewritten before every run and handed to the scheduler as -P.
        # Deliberately a real, stable path rather than a tempfile: when a
        # run books something surprising, the operator can open this and see
        # exactly what was fed in. A run never touches the operator's own
        # <slug>.txt.
        self.run_priorities_file = settings.data_dir / "schedule_run_priorities.txt"
        self.log_path = settings.data_dir / "schedule_last_run.log"

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.priority_lists_dir.mkdir(parents=True, exist_ok=True)

        self._config = self._load_config()
        self._migrate_to_real_only()
        self._purge_not_real_result()
        self._manifest = self._load_or_migrate_manifest()
        # Must happen before the first run can be triggered: a legacy
        # 4-column file handed to the official scheduler parses to NOTHING,
        # and under -f that is a run which books nothing and exits 0.
        self._migrate_priority_files()

        self._run_lock = asyncio.Lock()
        # Guards both "which list is active" and that list's file contents
        # together, so a save from one browser tab can never land in a
        # different file than the one that was active when the save started
        # (the race: tab A saving list A while tab B's load_priority_list
        # flips the active slug to B mid-write).
        self._lists_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self._config_changed = asyncio.Event()
        self._running = False
        self._last_error: str | None = None
        # A one-line answer to "did the image build right?", surfaced by
        # GET /api/schedule/config. Without it, a bad install is a failed
        # twenty-minute run instead of a glance.
        self.cli_version = self._probe_cli_version()
        # What the child last printed, so a silent six-minute cold run
        # looks like progress rather than a hang.
        self._progress = ""

    # --- station id / token overrides ---------------------------------------
    def _load_config(self) -> dict:
        if not self.config_file.is_file():
            return {}
        try:
            return json.loads(self.config_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not read %s: %s", self.config_file, exc)
            return {}

    def _write_config(self) -> None:
        tmp = self.config_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._config, indent=2), encoding="utf-8")
        tmp.replace(self.config_file)

    def _migrate_to_real_only(self) -> None:
        """One-time: read a config from before dry runs were removed.

        `auto_run_dry_run` used to default to True, so an enabled auto-run with
        the key absent or True was only ever PLANNING. Every fire books now.
        Carrying such a config over as-is would turn a timer the operator set
        up to preview into one that books real observations unattended, with
        nobody having asked for that - so it is switched off instead, and says
        so. Only an explicit `False` (the operator had already chosen BOOK FOR
        REAL) keeps running.

        The marker makes this a one-shot: a config written by this code never
        has the key, and must not be mistaken for a dry-run-only one on every
        restart.
        """
        if self._config.get(_REAL_ONLY_MARKER):
            return
        was_dry_run = self._config.get("auto_run_dry_run")
        if self._config.get("auto_run_enabled") and was_dry_run is not False:
            self._config["auto_run_enabled"] = False
            log.warning(
                "Station Schedule auto-run was set to dry runs only. Dry runs no "
                "longer exist - every run books real observations - so auto-run "
                "has been switched OFF. Re-enable it in the Station Schedule "
                "panel if unattended booking is what you want."
            )
        self._config.pop("auto_run_dry_run", None)
        self._config[_REAL_ONLY_MARKER] = True
        if not self.config_file.is_file():
            return  # a fresh install: nothing to migrate, and no file to create
        try:
            self._write_config()
        except OSError as exc:
            # Never fatal: the in-memory config is already migrated, and the
            # whole backend failing to boot over it would be far worse.
            log.warning("could not save the migrated %s: %s", self.config_file, exc)

    @staticmethod
    def _is_not_real(result: dict) -> bool:
        return (
            result.get("booked_state") in _NOT_REAL_BOOKED_STATES
            or result.get("dry_run") is True
            or result.get("cli_version") == "mock"
        )

    def _purge_not_real_result(self) -> None:
        """Delete a stored simulated or dry-run result, so it leaves the board."""
        if not self.result_path.is_file():
            return
        try:
            stored = json.loads(self.result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(stored, dict) or not self._is_not_real(stored):
            return
        log.warning(
            "removing %s: it records a %s run that booked nothing, and the "
            "Station Schedule now only shows real runs",
            self.result_path.name,
            "simulated" if stored.get("booked_state") == "mock" else "dry",
        )
        try:
            self.result_path.unlink(missing_ok=True)
        except OSError as exc:
            # get_last_run() refuses to show it either way.
            log.warning("could not remove %s: %s", self.result_path, exc)

    def _effective_station_id(self) -> int:
        return self._config.get("station_id") or self.s.station_id

    def _effective_db_token(self) -> str:
        return self._config.get("db_token") or self.s.satnogs_db_token

    def _effective_network_token(self) -> str:
        # Unlike station_id/db_token, there is no dashboard-wide default to
        # fall back to here - a network token is never configured anywhere
        # else in this codebase, on purpose (see network_client.py's
        # schedule() docstring). Blank means "no real bookings possible".
        return self._config.get("network_token") or ""

    def campaign_auto_commit_enabled(self) -> bool:
        return bool(self._config.get("campaign_auto_commit_enabled", False))

    def campaign_max_per_station(self) -> int:
        return int(self._config.get("campaign_max_per_station") or self.s.campaign_max_per_station)

    def campaign_max_total(self) -> int:
        return int(self._config.get("campaign_max_total") or self.s.campaign_max_total)

    def campaign_loop_until_exhausted(self) -> bool:
        """Off by default: one commit is one batch of at most max_total."""
        return bool(self._config.get("campaign_loop_until_exhausted", False))

    def auto_run_enabled(self) -> bool:
        """Defaults to False: a fresh install must not book unattended."""
        return bool(self._config.get("auto_run_enabled", False))

    def _auto_run_interval_min(self) -> int:
        # GS_SCHEDULE_POLL_S is only a seed for a fresh config; once the
        # operator has set an interval, this is authoritative.
        return int(self._cfg("auto_run_interval_min", max(5, self.s.schedule_poll_s // 60)))

    def _last_auto_fire(self) -> datetime | None:
        raw = self._config.get("auto_run_last_fire_utc")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None

    def next_auto_run(self, now: datetime | None = None) -> datetime | None:
        """When the timer should next fire, or None if it never should."""
        if not self.auto_run_enabled():
            return None
        return next_fire(
            mode=self._config.get("auto_run_mode") or "times",
            times=self._config.get("auto_run_times") or [],
            interval_min=self._auto_run_interval_min(),
            tz=self.s.timezone,
            now=now or datetime.now(timezone.utc),
            last_run=self._last_auto_fire(),
            not_before=self._auto_run_changed(),
        )

    def _auto_run_changed(self) -> datetime | None:
        raw = self._config.get("auto_run_changed_utc")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None

    async def mark_auto_run(self, when: datetime | None = None) -> str | None:
        """Remember that the timer fired, so a restart does not re-fire it.

        Returns whatever was recorded before, so a caller that turns out not
        to have run anything can put it back.
        """
        async with self._config_lock:
            previous = self._config.get("auto_run_last_fire_utc")
            self._config["auto_run_last_fire_utc"] = (
                when or datetime.now(timezone.utc)
            ).astimezone(timezone.utc).isoformat()
            await asyncio.to_thread(self._write_config)
        return previous

    async def restore_auto_run_mark(self, previous: str | None) -> None:
        """Undo a mark_auto_run() for a slot that did not actually run."""
        async with self._config_lock:
            self._config["auto_run_last_fire_utc"] = previous
            await asyncio.to_thread(self._write_config)

    async def wait_for_config_change(self, timeout: float) -> bool:
        """Sleep until the config changes or `timeout` elapses.

        This is how an edit in the browser takes effect without restarting the
        process: save_config() sets the event, the auto-run loop wakes and
        recomputes when it should next fire.
        """
        try:
            await asyncio.wait_for(self._config_changed.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self._config_changed.clear()
        return True

    async def get_config(self) -> dict:
        async with self._config_lock:
            next_run = self.next_auto_run()
            return {
                "station_id": self._effective_station_id(),
                "station_id_is_override": bool(self._config.get("station_id")),
                "db_token_set": bool(self._effective_db_token()),
                "network_token_set": bool(self._effective_network_token()),
                "campaign_auto_commit_enabled": self.campaign_auto_commit_enabled(),
                "campaign_max_per_station": self.campaign_max_per_station(),
                "campaign_max_total": self.campaign_max_total(),
                "campaign_loop_until_exhausted": self.campaign_loop_until_exhausted(),
                # --- station auto run ---
                "auto_run_enabled": self.auto_run_enabled(),
                "auto_run_mode": self._config.get("auto_run_mode") or "times",
                # _cfg, not `or`: an emptied list is a real setting meaning
                # "never fire", and `[] or [...]` would hand the panel back
                # two times the operator had just deleted - which they would
                # then save, booking twice a day by accident.
                "auto_run_times": self._cfg("auto_run_times", ["06:00", "18:00"]),
                "auto_run_interval_min": self._auto_run_interval_min(),
                "auto_run_last_fire_utc": self._config.get("auto_run_last_fire_utc"),
                "auto_run_next_utc": next_run.isoformat() if next_run else None,
                # --- run flags ---
                "schedule_hours": float(self._cfg("schedule_hours", self.s.schedule_hours)),
                "min_culmination_deg": float(self._cfg("min_culmination_deg", 3.0)),
                "only_priority": bool(self._cfg("only_priority", True)),
                "max_observation_minutes": int(self._cfg("max_observation_minutes", 30)),
                "start_lead_minutes": int(self._cfg("start_lead_minutes", 10)),
                "run_timeout_s": int(self._cfg("run_timeout_s", self.s.schedule_timeout_s)),
                "timezone": self.s.timezone,
                "cli_version": self.cli_version,
            }

    async def save_config(self, *, now: datetime | None = None, **fields) -> dict:
        """Apply only the keys actually passed.

        `now` is keyword-only and separate from **fields on purpose: fields is
        validated against _CONFIG_FIELDS and an unknown key raises, so a clock
        smuggled in there would be rejected. It exists because the
        auto_run_changed_utc stamp below used to read the wall clock directly,
        which made this method impossible to test at a fixed date - two tests
        asserting against 2026-09-21 fixtures passed when they were written and
        began failing permanently once real time moved past them. Production
        passes nothing and gets datetime.now, exactly as before.

        `None` is no longer how a caller says "leave unchanged" - simply not
        passing the key is, which is what the route's
        `model_dump(exclude_unset=True)` produces. A falsy value clears an
        override back to its default for the keys marked that way in
        `_CONFIG_FIELDS`, and is stored literally for the rest.

        An unknown key is a ValueError rather than a silent no-op, so a typo in
        a client becomes a 400 instead of a setting that never takes effect.
        """
        unknown = sorted(set(fields) - set(_CONFIG_FIELDS))
        if unknown:
            raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
        async with self._config_lock:
            for key, value in fields.items():
                if value is None:
                    continue
                cast, clears_on_falsy = _CONFIG_FIELDS[key]
                if clears_on_falsy:
                    self._config[key] = cast(value) if value else None
                else:
                    self._config[key] = cast(value)
            # A slot earlier than the moment the operator last changed these
            # settings was never "missed" - it simply was not configured yet.
            # Without this, adding a 12:30 time at 12:40 lands inside the
            # 30-minute catch-up grace and fires a REAL booking run seconds
            # after SAVE, for a slot the operator meant to start tomorrow.
            self._config["auto_run_changed_utc"] = (
                now or datetime.now(timezone.utc)
            ).isoformat()
            await asyncio.to_thread(self._write_config)
        # Wake the auto-run loop so a change to the timer takes effect now
        # rather than at the end of whatever sleep it is in.
        self._config_changed.set()
        return await self.get_config()

    async def verify_station(self, station_id: int) -> dict:
        """The only thing that can honestly be verified: does this station id
        exist on SatNOGS Network. Neither the DB token nor a network token is
        required by any read this service makes, so there is no call that
        would prove a token itself is valid - callers must not claim one."""
        return await asyncio.to_thread(self._verify_station_sync, station_id)

    def _verify_station_sync(self, station_id: int) -> dict:
        cache = Cache(self.cache_dir, offline=self.s.offline)
        auto_settings = self._build_autoscheduler_settings(0.0)
        network = NetworkClient(auto_settings, cache)
        try:
            station = network.get_station(station_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "station_name": station.name, "status": station.status}

    # --- schedule runs -------------------------------------------------------
    def _build_autoscheduler_settings(self, hours: float) -> AutoSettings:
        return AutoSettings(
            station_id=self._effective_station_id(),
            db_token=self._effective_db_token(),
            cache_dir=self.cache_dir,
            hours=hours,
            mission_norad=self.s.default_norad,
            priority_file=self.priority_file,
            offline=self.s.offline,
            db_base_url=self.s.satnogs_db,
            network_base_url=self.s.satnogs_network,
            # network_token is deliberately never set here - see
            # NetworkClient.schedule(), the only place it would matter, which
            # this service never calls. Booking stays off no matter what a
            # future settings field might hold.
        )

    def _probe_cli_version(self) -> str:
        """Ask the installed scheduler what it is, once, at startup."""
        try:
            proc = subprocess.run(
                [sys.executable, "-c",
                 "from auto_scheduler.cli.schedule_single_station import main; main()",
                 "--version"],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not probe satnogs-auto-scheduler: %s", exc)
            return "probe failed"
        if proc.returncode != 0:
            # Almost always one thing: the image predates the dependency, so
            # the code is live but the package is not there. Say that, rather
            # than surfacing the first line of a traceback - this string is
            # shown in the panel and is meant to answer "did the image build
            # right?" in one glance.
            log.error(
                "satnogs-auto-scheduler is not installed in this environment; "
                "a real run cannot work until the backend image is rebuilt. %s",
                (proc.stderr or "").strip()[-300:],
            )
            return "NOT INSTALLED - rebuild the backend image"
        # --version is one of the few things this tool puts on stdout.
        reported = (proc.stdout or proc.stderr).strip()
        return reported.splitlines()[0] if reported else ""

    def is_running(self) -> bool:
        return self._running

    def progress(self) -> str:
        return self._progress

    # --- schedule runs -------------------------------------------------------
    async def run_plan(
        self,
        hours: float | None = None,
        *,
        trigger: str = "manual",
    ) -> dict:
        """Plan and book. There is no dry run and no simulated run.

        Two callers reach this - the route and the auto-run loop - and both
        book real observations. What stops a run is the offline, not-installed
        and token guards in `_execute_run`, never a flag.
        """
        if self._running:
            return {"status": "running"}
        async with self._run_lock:
            if self._running:
                return {"status": "running"}
            self._running = True
        self._progress = "starting"
        try:
            result = await self._execute_run(hours, trigger=trigger)
        except asyncio.CancelledError:
            # A backend restart mid-run - on this stack any source save, via
            # uvicorn --reload. The child has been told to stop (see
            # autoscheduler_cli.run), but a booking POST may already have
            # landed, so this is recorded rather than lost: without it the
            # board keeps showing the previous run, and the natural next move
            # - running again - can double-book.
            interrupted = self._failure_result(
                "The run was interrupted by a backend restart before it finished. "
                "Some observations may have been booked - check "
                "network.satnogs.org before running again.",
                trigger=trigger,
            )
            interrupted["booked_state"] = "unconfirmed"
            try:
                self._write_result(interrupted)
            except OSError:
                log.warning("could not record the interrupted run", exc_info=True)
            raise
        except Exception as exc:  # noqa: BLE001 - every failure must be reportable
            log.exception("schedule run failed")
            result = self._failure_result(str(exc), trigger=trigger)
        finally:
            self._running = False
            self._progress = ""

        # Written on the failure path too. It used not to be, so a failed run
        # left the previous SUCCESS on screen and the panel's error branches
        # were unreachable.
        self._write_result(result)
        if result.get("status") == "error":
            self._last_error = result.get("error") or "run failed"
            self.on_state("schedule", "degraded", self._last_error)
        else:
            self._last_error = None
            self.on_state("schedule", "ok", self._state_detail(result))
        return result

    @staticmethod
    def _state_detail(result: dict) -> str:
        """What the header chip says."""
        planned = result.get("planned", len(result.get("observations", [])))
        booked = result.get("booked", 0)
        if result.get("booked_state") == "confirmed":
            return f"booked {booked}"
        return f"planned {planned}, {booked} booked ({result.get('booked_state', 'unknown')})"

    def _failure_result(
        self, error: str, *, trigger: str, notices: list | None = None
    ) -> dict:
        return {
            "status": "error",
            "error": error,
            "station": self._effective_station_id(),
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "considered": 0,
            "rejected_conflict": 0,
            "rejected_capped": 0,
            "observations": [],
            "notices": notices or [{"severity": "error", "message": error}],
            "trigger": trigger,
            "planned": 0,
            "booked": 0,
            "booked_state": "failed",
            "already_scheduled": [],
            "efficiency": None,
            "exit_code": None,
            "killed_by": "",
            "run_duration_s": 0.0,
            "log_tail": [],
            "cli_version": self.cli_version,
        }

    async def _execute_run(self, hours, *, trigger: str) -> dict:
        # GS_OFFLINE means "serve fixtures instead of hitting the internet",
        # and every other SatNOGS-touching call in this service honours it.
        # The child process cannot: the tool has no offline mode and would go
        # straight to the live API. Refusing is the only honest reading of the
        # flag - booking real observations on a dashboard configured not to
        # touch the network is the exact opposite of what it asks for.
        if self.s.offline:
            return self._failure_result(
                "GS_OFFLINE=1 is set, so the scheduler was not started. That "
                "flag means this dashboard must not reach the internet, and "
                "satnogs-auto-scheduler has no offline mode - it would book "
                "against the live SatNOGS API. Clear GS_OFFLINE to run it.",
                trigger=trigger,
            )

        # The startup probe already knows. Spawning anyway only buys a
        # ModuleNotFoundError traceback in place of a sentence.
        if self.cli_version.startswith("NOT INSTALLED"):
            return self._failure_result(
                "satnogs-auto-scheduler is not installed in this backend image, so "
                "the run was not started and nothing was booked. Rebuild the "
                "backend image and recreate the container.",
                trigger=trigger,
            )

        # Both tokens, always - satnogs-auto-scheduler validates its whole
        # configuration before it does anything else. Catching it here turns
        # an opaque child exit(1) into a sentence.
        problems = autoscheduler_cli.validate_tokens(
            self._effective_db_token(), self._effective_network_token()
        )
        if problems:
            return self._failure_result(
                problems[0],
                trigger=trigger,
                notices=[{"severity": "error", "message": p} for p in problems],
            )

        entries, notices = await asyncio.to_thread(self._resolve_priorities_sync)
        await asyncio.to_thread(
            write_priority_file, self.run_priorities_file, entries
        )
        await asyncio.to_thread(self._rotate_log)

        cfg = self._build_run_config(hours)
        screened = await asyncio.to_thread(self._screen_tool_tles_sync, cfg)
        outcome = await autoscheduler_cli.run(
            cfg, on_line=self._note_progress, log_path=self.log_path
        )
        if autoscheduler_cli.crashed_in_prediction(outcome):
            # The tool refreshes its own TLE cache once a day, at the start of
            # a run - after the screen above - and a refresh can bring a TLE
            # back that SGP4 cannot propagate. Its pass prediction then dies
            # before anything is booked. Screen the fresh cache and go again,
            # once, and only if the screen actually removed something: any
            # other crash is reported as it is.
            again = await asyncio.to_thread(self._screen_tool_tles_sync, cfg)
            if again:
                screened += again
                log.warning(
                    "satnogs-auto-scheduler crashed in pass prediction on a TLE "
                    "it had just downloaded; retrying once without %s",
                    ", ".join(s["norad_cat_id"] for s in again),
                )
                self._progress = "retrying without an unpropagatable TLE"
                await asyncio.to_thread(self._rotate_log)
                outcome = await autoscheduler_cli.run(
                    cfg, on_line=self._note_progress, log_path=self.log_path
                )
        notice = autoscheduler_cli.screened_notice(
            screened, [entry.norad_cat_id for entry in entries]
        )
        if notice is not None:
            notices = [*notices, notice]
            outcome.parsed.notices = autoscheduler_cli.without_screened_tle_warnings(
                outcome.parsed.notices, screened
            )
        return await self._build_result(outcome, entries, notices, trigger=trigger)

    def _screen_tool_tles_sync(self, cfg) -> list[dict]:
        """See autoscheduler_cli.screen_tool_tles. Never allowed to stop a run."""
        start = autoscheduler_cli.start_time(cfg)
        try:
            return autoscheduler_cli.screen_tool_tles(
                self.cache_dir, start, start + timedelta(hours=float(cfg.hours))
            )
        except Exception:  # noqa: BLE001 - a screen that fails leaves the run as it was
            log.warning("could not screen the scheduler's TLE cache", exc_info=True)
            return []

    def _note_progress(self, line: str) -> None:
        _level, _logger, message = autoscheduler_cli.split_prefix(line)
        message = message.strip()
        if message:
            self._progress = message[:160]

    def _rotate_log(self) -> None:
        """One generation back, so a failed run does not erase the one before."""
        if self.log_path.is_file():
            self.log_path.replace(self.log_path.with_suffix(".log.prev"))

    def _build_run_config(self, hours) -> autoscheduler_cli.RunConfig:
        return autoscheduler_cli.RunConfig(
            station_id=self._effective_station_id(),
            db_token=self._effective_db_token(),
            network_token=self._effective_network_token(),
            cache_dir=str(self.cache_dir),
            priorities_path=str(self.run_priorities_file),
            hours=float(hours if hours is not None else self._cfg("schedule_hours", self.s.schedule_hours)),
            min_culmination_deg=float(self._cfg("min_culmination_deg", 3.0)),
            max_observation_minutes=int(self._cfg("max_observation_minutes", 30)),
            only_priority=bool(self._cfg("only_priority", True)),
            start_lead_minutes=int(self._cfg("start_lead_minutes", 10)),
            network_base_url=self.s.satnogs_network,
            db_base_url=self.s.satnogs_db,
            launcher=self.s.schedule_launcher,
            timeout_s=int(self._cfg("run_timeout_s", self.s.schedule_timeout_s)),
            idle_timeout_s=self.s.schedule_idle_timeout_s,
        )

    def _cfg(self, key: str, default):
        value = self._config.get(key)
        return default if value is None else value

    def _resolve_priorities_sync(self) -> tuple[list, list[dict]]:
        """The active list with a transmitter chosen for every unpinned row.

        Resolution happens here, at run time, rather than when the operator
        saves: picking needs the station's antenna segments and the DB
        catalogue, and gating a save on a network read would make the panel
        unusable whenever SatNOGS is slow.
        """
        entries = self._load_rows_sync(self._manifest["active"])
        notices: list[dict] = []
        if not entries:
            return entries, notices

        cache = Cache(self.cache_dir, offline=self.s.offline)
        auto_settings = self._build_autoscheduler_settings(0.0)
        db = DbClient(auto_settings, cache)
        network = NetworkClient(auto_settings, cache)
        station = network.get_station(self._effective_station_id())
        # Once for the whole list. transmitters_for_station() re-reads and
        # re-parses megabytes of cached JSON on every call, so a per-row loop
        # would cost seconds per satellite.
        by_norad = db.transmitters_for_station(station.segments)

        resolved = []
        for entry in entries:
            candidates = by_norad.get(entry.norad_cat_id, [])
            choice = pick_transmitter(candidates, entry.transmitter_uuid)
            if choice is None:
                notices.append({
                    "severity": "warning",
                    "message": (
                        f"NORAD {entry.norad_cat_id} has no transmitter this station "
                        f"can hear, so it was left out of this run."
                    ),
                })
                continue
            if entry.transmitter_uuid and choice.uuid != entry.transmitter_uuid:
                # pick_transmitter logs this, but only to the backend log where
                # the operator never sees it.
                notices.append({
                    "severity": "warning",
                    "message": (
                        f"NORAD {entry.norad_cat_id}: the pinned transmitter "
                        f"{entry.transmitter_uuid} is not available at this station; "
                        f"{choice.uuid} was used instead."
                    ),
                })
            resolved.append(replace(entry, transmitter_uuid=choice.uuid))
        return resolved, notices

    async def _build_result(
        self, outcome, entries, notices: list[dict], *, trigger: str
    ) -> dict:
        parsed = outcome.parsed
        all_notices = list(notices) + list(parsed.notices)
        failed = outcome.failure is not None and not parsed.planned
        if not failed:
            # Only a run that got as far as selecting passes can say a priority
            # entry produced none. After a crash every entry would be listed,
            # each blaming its transmitter for a failure it had no part in.
            all_notices += autoscheduler_cli.missing_priority_notices(
                [entry.norad_cat_id for entry in entries], parsed
            )
        if outcome.failure is not None:
            all_notices.insert(0, {"severity": "error", "message": outcome.failure[1]})

        observations = await asyncio.to_thread(self._enrich_rows_sync, parsed.planned)
        already = [self._row_payload(row, {}) for row in parsed.already_scheduled]

        booked, booked_state, recon_notices = await self._reconcile(parsed, outcome)
        all_notices += recon_notices

        return {
            "status": "error" if failed else ("ok_with_warnings" if all_notices else "ok"),
            "error": outcome.failure[1] if failed else None,
            "station": self._effective_station_id(),
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "considered": (parsed.efficiency or {}).get("considered", 0),
            # No upstream equivalent. Left at zero rather than back-computed
            # from considered-minus-planned, which would be a lie: most of that
            # gap is elevation filtering, not scheduling conflicts.
            "rejected_conflict": 0,
            "rejected_capped": 0,
            "observations": observations,
            "notices": all_notices,
            "trigger": trigger,
            "planned": len(parsed.planned),
            "booked": booked,
            "booked_state": booked_state,
            "already_scheduled": already,
            "efficiency": parsed.efficiency,
            "exit_code": outcome.exit_code,
            "killed_by": outcome.killed_by,
            "run_duration_s": round(outcome.duration_s, 1),
            "log_tail": outcome.lines[-400:],
            "cli_version": self.cli_version,
        }

    def _row_payload(self, row, downlinks: dict) -> dict:
        return {
            "start": row.start.isoformat(),
            "end": row.end.isoformat(),
            "duration_s": row.duration_s,
            "norad_cat_id": row.norad,
            "satellite": row.name,
            "max_elevation_deg": row.elevation,
            "aos_azimuth_deg": row.az_rise,
            "los_azimuth_deg": row.az_set,
            "transmitter_uuid": row.transmitter_uuid,
            "downlink_hz": downlinks.get(row.transmitter_uuid, 0),
            "mode": row.mode,
            # The CLI cannot tell us this and inventing it would be worse than
            # admitting it: the summary table has no observation history.
            "observed_here": 0,
            "score": row.priority,
            "is_mission": row.norad == self.s.default_norad,
        }

    def _enrich_rows_sync(self, rows) -> list[dict]:
        """Add the downlink frequency, which the CLI output does not carry.

        The summary table's header says "Freq" but the column under it is the
        frequency-VIOLATOR flag; there is no frequency anywhere in that output.
        """
        downlinks: dict[str, int] = {}
        if rows:
            try:
                cache = Cache(self.cache_dir, offline=self.s.offline)
                db = DbClient(self._build_autoscheduler_settings(0.0), cache)
                # One pass over the catalogue for the whole table.
                downlinks = {
                    uuid: int(float(tx.get("downlink_low") or 0))
                    for uuid, tx in db.transmitters_by_uuid().items()
                }
            except Exception as exc:  # noqa: BLE001 - display-only enrichment
                log.warning("could not enrich downlink frequencies: %s", exc)
        return [self._row_payload(row, downlinks) for row in rows]

    # How close two starts must be to be the same observation. The tool books
    # exactly the start it printed, so this only has to absorb clock skew and
    # SatNOGS rounding.
    _RECONCILE_TOLERANCE_S = 90

    async def _reconcile(self, parsed, outcome) -> tuple[int, str, list]:
        """Ask SatNOGS what is actually on the calendar.

        The transcript alone cannot answer this honestly. "Scheduled N passes!"
        is logged at DEBUG, and if the run was killed during the booking POST -
        which carries no timeout of its own - observations may exist
        server-side for a run we believe failed. So after every run we go and
        look.
        """
        # Only failures that happen BEFORE the tool could post anything are
        # safe to report as "nothing booked" without looking. A booking-stage
        # failure is NOT one of them: upstream reacts to a rejected batch by
        # re-posting every observation individually
        # ("Fall-back to single-pass scheduling..."), so "Failed to
        # batch-schedule observations." routinely precedes real bookings.
        # Short-circuiting on it told the operator nothing was booked when
        # some passes were - and the obvious next move, running again, would
        # double-book them.
        if outcome.failure is not None and outcome.failure[0] in (
            "token_missing", "token_invalid", "station_offline",
            "station_unknown", "spawn_failed", "bad_invocation",
        ):
            return 0, "failed", []
        if not parsed.planned:
            if parsed.attempted_booking or parsed.booked_log:
                # The tool reached its booking step, but its table could not be
                # read back. Saying "nothing booked" here would invite a second
                # run on top of bookings that may well exist.
                return (parsed.booked_log or 0), "unconfirmed", [{
                    "severity": "warning",
                    "message": (
                        "satnogs-auto-scheduler reached its booking step, but its "
                        "summary table could not be read, so what it booked is "
                        "unknown. Check network.satnogs.org before running again."
                    ),
                }]
            # A crash or a network error with no table is a failed run - it
            # used to read "confirmed", 0 booked, which is not the same thing.
            return 0, ("failed" if outcome.failure is not None else "confirmed"), []

        # SatNOGS's read API trails its write API by a moment.
        await asyncio.sleep(5)
        try:
            bookings = await asyncio.to_thread(self._future_bookings_sync)
        except Exception as exc:  # noqa: BLE001 - never fail a run over this
            log.warning("could not reconcile against SatNOGS: %s", exc)
            return (
                parsed.booked_log or 0,
                "unconfirmed",
                [{
                    "severity": "warning",
                    "message": (
                        "Could not read the station's calendar back from SatNOGS, so "
                        f"this run's bookings are unconfirmed ({exc}). Check "
                        "network.satnogs.org directly."
                    ),
                }],
            )

        tolerance = timedelta(seconds=self._RECONCILE_TOLERANCE_S)
        now = datetime.now(timezone.utc)

        matched = 0
        missing = 0        # should have been in the feed, and was not
        unverifiable = 0   # could not be in the feed at all
        for row in parsed.planned:
            if any(
                booking.norad_cat_id == row.norad
                and abs(booking.start - row.start) <= tolerance
                for booking in bookings
            ):
                matched += 1
            elif row.start > now + tolerance:
                missing += 1
            else:
                # future_bookings() only lists observations that have not
                # STARTED, and stops walking the feed at the first one that
                # has. The tool books from now + start_lead_minutes (10 by
                # default), so on a run longer than that lead - a cold cache
                # easily exceeds it - its earliest passes have already begun
                # and cannot appear here. Counting those as missing would
                # report a perfectly good run as partial.
                unverifiable += 1

        notices: list[dict] = []
        if unverifiable:
            notices.append({
                "severity": "warning",
                "message": (
                    f"{unverifiable} of {len(parsed.planned)} planned observations had "
                    f"already started by the time this run finished, so SatNOGS's "
                    f"upcoming-passes feed cannot show them either way. They are not "
                    f"counted as booked below - check network.satnogs.org if you need "
                    f"to know."
                ),
            })

        if matched == len(parsed.planned):
            return matched, "confirmed", notices
        if missing == 0:
            # Everything we could check, checked out; the rest are merely
            # invisible. Not "partial" - nothing is known to have failed.
            return matched, "unconfirmed", notices
        if matched:
            notices.append({
                "severity": "warning",
                "message": (
                    f"{matched} of {matched + missing} checkable observations were "
                    f"found on the station's SatNOGS calendar. The rest may have been "
                    f"rejected - check network.satnogs.org."
                ),
            })
            return matched, "partial", notices
        notices.append({
            "severity": "warning",
            "message": (
                "None of the planned observations could be found on the station's "
                "SatNOGS calendar yet. That may just be replication lag - check "
                "network.satnogs.org before running again, so nothing is booked twice."
            ),
        })
        return 0, "unconfirmed", notices

    def _future_bookings_sync(self) -> list:
        cache = Cache(self.cache_dir, offline=self.s.offline)
        network = NetworkClient(self._build_autoscheduler_settings(0.0), cache)
        return network.future_bookings(self._effective_station_id())

    def _write_result(self, result: dict) -> None:
        tmp = self.result_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=2), encoding="utf-8")
        tmp.replace(self.result_path)   # atomic, matching cache.py's own writes

    def read_log(self, lines: int = 400) -> str:
        """The tail of the last run's raw transcript.

        Bounded because a cold run's transcript is thousands of lines of
        progress bar and nobody wants that in a browser.
        """
        if not self.log_path.is_file():
            # No transcript file. Either nothing has run, or the run that did
            # never spawned a child - it was refused by the offline, not-
            # installed or token guard. Fall back to whatever the stored result
            # kept, rather than telling the operator no run has happened when
            # the panel above plainly shows one.
            tail = self.get_last_run().get("log_tail") or []
            if tail:
                return "\n".join(tail)
            return "No run has been recorded yet."
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"Could not read {self.log_path.name}: {exc}"
        tail = text.splitlines()[-max(1, min(int(lines), 5000)):]
        return "\n".join(tail)

    def get_last_run(self) -> dict:
        if not self.result_path.is_file():
            return {"status": "never_run"}
        try:
            stored = json.loads(self.result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not read %s: %s", self.result_path, exc)
            return {"status": "never_run"}
        # Normally purged at startup; this covers a purge that could not
        # delete the file. A booking that never happened is never shown.
        if isinstance(stored, dict) and self._is_not_real(stored):
            return {"status": "never_run"}
        return stored

    # --- priority lists ------------------------------------------------------
    def _load_or_migrate_manifest(self) -> dict:
        if self.manifest_file.is_file():
            try:
                return json.loads(self.manifest_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.warning("could not read %s: %s", self.manifest_file, exc)

        # First boot after this feature landed (or a fresh install): fold
        # whatever priority data already exists into a single "Default" list
        # rather than orphaning it.
        default_path = self.priority_lists_dir / f"{_DEFAULT_LIST_SLUG}.txt"
        if not default_path.is_file():
            old = self.s.data_dir / "priorities.txt"
            if old.is_file():
                shutil.copyfile(old, default_path)
            elif _DEFAULT_PRIORITIES.is_file():
                shutil.copyfile(_DEFAULT_PRIORITIES, default_path)
            else:
                write_priority_file(default_path, [])

        manifest = {
            "active": _DEFAULT_LIST_SLUG,
            "lists": [{"slug": _DEFAULT_LIST_SLUG, "name": _DEFAULT_LIST_NAME}],
        }
        self._manifest = manifest
        self._write_manifest()
        return manifest

    def _write_manifest(self) -> None:
        tmp = self.manifest_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._manifest, indent=2), encoding="utf-8")
        tmp.replace(self.manifest_file)

    @property
    def priority_file(self) -> Path:
        return self.priority_lists_dir / f"{self._manifest['active']}.txt"

    def _list_entry(self, slug: str) -> dict | None:
        return next((entry for entry in self._manifest["lists"] if entry["slug"] == slug), None)

    def _unique_slug(self, name: str) -> str:
        base = _slugify(name)
        existing = {entry["slug"] for entry in self._manifest["lists"]}
        slug = base
        n = 2
        while slug in existing:
            slug = f"{base}-{n}"
            n += 1
        return slug

    async def list_priority_lists(self) -> dict:
        async with self._lists_lock:
            return {"active": self._manifest["active"], "lists": list(self._manifest["lists"])}

    async def load_priority_list(self, slug: str) -> list[dict]:
        """Loading makes a list active immediately - there is no separate
        "activate" step. The next auto-run (and any save) uses this list."""
        async with self._lists_lock:
            if self._list_entry(slug) is None:
                raise ValueError(f"no priority list named {slug!r}")
            self._manifest["active"] = slug
            await asyncio.to_thread(self._write_manifest)
        return await self.get_priorities()

    async def create_priority_list(self, name: str, duplicate_current: bool = False) -> dict:
        """Creating a list does not switch to it - only load_priority_list
        does that, so there is exactly one rule for "what's active"."""
        async with self._lists_lock:
            slug = self._unique_slug(name)
            new_path = self.priority_lists_dir / f"{slug}.txt"
            if duplicate_current and self.priority_file.is_file():
                await asyncio.to_thread(shutil.copyfile, self.priority_file, new_path)
                source_meta = self._meta_path(self._manifest["active"])
                if source_meta.is_file():
                    await asyncio.to_thread(
                        shutil.copyfile, source_meta, self._meta_path(slug)
                    )
            else:
                await asyncio.to_thread(write_priority_file, new_path, [])
                await asyncio.to_thread(self._write_meta_sync, slug, [])
            self._manifest["lists"].append({"slug": slug, "name": name})
            await asyncio.to_thread(self._write_manifest)
            return {"active": self._manifest["active"], "lists": list(self._manifest["lists"])}

    async def rename_priority_list(self, slug: str, new_name: str) -> dict:
        async with self._lists_lock:
            entry = self._list_entry(slug)
            if entry is None:
                raise ValueError(f"no priority list named {slug!r}")
            entry["name"] = new_name
            await asyncio.to_thread(self._write_manifest)
            return {"active": self._manifest["active"], "lists": list(self._manifest["lists"])}

    # --- priority list sidecar ------------------------------------------------
    # The .txt is the file satnogs-auto-scheduler reads, and it can express
    # exactly three things: norad, weight, transmitter uuid. Two things the
    # dashboard needs do not fit, and both used to be squeezed in anyway:
    #
    #   * a row's Auto/Manual mode, written as a 4th column,
    #   * a row with no transmitter pinned, written as a "-" placeholder.
    #
    # Either one makes the official reader discard the WHOLE LINE - it requires
    # exactly three fields - so a list full of them fed that tool nothing at
    # all. They live in a <slug>.meta.json sidecar now.
    #
    # The sidecar is authoritative for row order, mode, and unpinned rows; the
    # .txt is the projection of it that the scheduler can read. If the operator
    # hand-edits the .txt - which the export button actively encourages - its
    # mtime wins and the sidecar is reconciled against it rather than silently
    # overwriting the edit.
    _META_VERSION = 1

    def _list_path(self, slug: str) -> Path:
        return self.priority_lists_dir / f"{slug}.txt"

    def _meta_path(self, slug: str) -> Path:
        return self.priority_lists_dir / f"{slug}.meta.json"

    @staticmethod
    def _rows_to_priorities(rows: list) -> list[Priority]:
        out: list[Priority] = []
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                continue
            try:
                norad = int(row["norad"])
                weight = max(0.0, min(1.0, float(row.get("weight", 1.0))))
            except (KeyError, TypeError, ValueError):
                continue
            out.append(
                Priority(
                    norad_cat_id=norad,
                    weight=weight,
                    transmitter_uuid=(row.get("uuid") or None),
                    line=index,
                    mode="manual" if row.get("mode") == "manual" else "auto",
                )
            )
        return out

    def _read_meta_sync(self, slug: str) -> list[Priority] | None:
        """The sidecar's rows, or None if there isn't a usable one."""
        path = self._meta_path(slug)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Unlike the manifest, a bad sidecar is not rebuilt over the top of
            # itself - we fall back to the .txt, which still has most of the
            # information, and leave the broken file alone to be looked at.
            log.warning("could not read %s: %s", path, exc)
            return None
        if not isinstance(data, dict) or data.get("version") != self._META_VERSION:
            log.warning("%s is not a version %d sidecar, ignoring it", path, self._META_VERSION)
            return None
        rows = data.get("rows")
        if not isinstance(rows, list):
            return None
        return self._rows_to_priorities(rows)

    def _write_meta_sync(self, slug: str, entries: list[Priority]) -> None:
        path = self._meta_path(slug)
        payload = {
            "version": self._META_VERSION,
            "rows": [
                {
                    "norad": int(entry.norad_cat_id),
                    "weight": round(float(entry.weight), 3),
                    "uuid": entry.transmitter_uuid,
                    "mode": entry.mode,
                }
                for entry in entries
            ],
        }
        # ".meta.json.tmp", not with_suffix(".tmp"): a list whose slug collides
        # with another file's stem would otherwise share one tmp name.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _load_rows_sync(self, slug: str) -> list[Priority]:
        """The full list for a slug: sidecar if we have one, else the .txt."""
        txt = self._list_path(slug)
        file_rows: list[Priority] = []
        if txt.is_file():
            file_rows = sorted(parse_priority_file(txt).values(), key=lambda p: p.line)
        meta_rows = self._read_meta_sync(slug)
        if meta_rows is None:
            return file_rows
        if not txt.is_file():
            return meta_rows
        try:
            # One second of slack: we write the .txt first and the sidecar
            # immediately after, so a normal save always leaves the sidecar
            # newer. Only a real outside edit trips this.
            edited_by_hand = txt.stat().st_mtime > self._meta_path(slug).stat().st_mtime + 1.0
        except OSError:
            edited_by_hand = False
        if not edited_by_hand:
            return meta_rows
        return self._reconcile_rows(slug, file_rows, meta_rows)

    @staticmethod
    def _reconcile_rows(
        slug: str, file_rows: list[Priority], meta_rows: list[Priority]
    ) -> list[Priority]:
        """Take a hand-edited .txt as truth, keeping what it cannot express."""
        modes = {row.norad_cat_id: row.mode for row in meta_rows}
        in_file = {row.norad_cat_id for row in file_rows}
        merged = [
            Priority(
                norad_cat_id=row.norad_cat_id,
                weight=row.weight,
                transmitter_uuid=row.transmitter_uuid,
                line=index,
                mode=modes.get(row.norad_cat_id, "auto"),
            )
            for index, row in enumerate(file_rows, 1)
        ]
        # An unpinned row cannot appear in the .txt at all, so an edit there can
        # neither keep nor delete one. Keeping them is the safe reading of the
        # operator's intent; they land at the end because the file gives no
        # position to restore them to.
        carried = [
            row
            for row in meta_rows
            if row.transmitter_uuid is None and row.norad_cat_id not in in_file
        ]
        if carried:
            log.warning(
                "priority list %r was edited on disk; %d unpinned row(s) kept "
                "from the sidecar and appended at the end",
                slug,
                len(carried),
            )
        for offset, row in enumerate(carried, len(merged) + 1):
            merged.append(
                Priority(row.norad_cat_id, row.weight, None, offset, row.mode)
            )
        return merged

    @staticmethod
    def _needs_strict_rewrite(raw: str) -> bool:
        """True if any data line would be dropped by the official reader."""
        for line in raw.splitlines():
            data = line.split("#", 1)[0]
            if not data.strip():
                continue
            if data != data.strip():
                return True  # leading/trailing space becomes an empty csv field
            if "\t" in data or "  " in data:
                return True  # tabs are not the delimiter; runs of spaces add fields
            fields = data.split(" ")
            if len(fields) != 3 or fields[2] == "-":
                return True
        return False

    def _migrate_priority_files(self) -> None:
        """One-time, idempotent: give every list a sidecar and a strict .txt.

        Follows the _load_or_migrate_manifest() precedent. The presence of a
        sidecar is the "already done" marker, so a second pass changes nothing.
        """
        for path in sorted(self.priority_lists_dir.glob("*.txt")):
            slug = path.stem
            if self._meta_path(slug).is_file():
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                log.warning("could not read %s: %s", path, exc)
                continue
            try:
                entries = sorted(
                    parse_priority_file(path).values(), key=lambda p: p.line
                )
            except (OSError, ValueError) as exc:
                log.warning("could not parse %s, leaving it alone: %s", path, exc)
                continue

            if self._needs_strict_rewrite(raw):
                backup = path.with_suffix(".bak")
                if not backup.exists():
                    backup.write_text(raw, encoding="utf-8")
                    log.info("priority list %r kept as %s before rewrite", slug, backup.name)
                write_priority_file(path, entries)
                log.info(
                    "rewrote priority list %r in the strict 3-field format the "
                    "official scheduler requires",
                    slug,
                )
            # Written even when no rewrite was needed, so every list has a
            # sidecar from here on and this loop never revisits it.
            self._write_meta_sync(slug, entries)

    async def export_priority_text(self) -> str:
        """The active list exactly as the scheduler will read it."""
        async with self._lists_lock:
            slug = self._manifest["active"]
            entries = await asyncio.to_thread(self._load_rows_sync, slug)
        return render_priority_file(entries)

    # --- priorities (the active list's contents) ------------------------------
    async def get_priorities(self) -> list[dict]:
        slug = self._manifest["active"]
        if not self._list_path(slug).is_file() and not self._meta_path(slug).is_file():
            return []
        entries = await asyncio.to_thread(self._load_rows_sync, slug)
        try:
            return await asyncio.to_thread(self._enrich_priorities_sync, entries)
        except Exception as exc:
            # The satellite name and transmitter description are display-only -
            # a SatNOGS DB hiccup should not stop the priority list from
            # rendering (and definitely should not stop it from being saved).
            log.warning("priority enrichment failed: %s", exc)
            return [self._bare_entry(p) for p in entries]

    def _bare_entry(self, p: Priority) -> dict:
        return {
            "norad_cat_id": p.norad_cat_id,
            "weight": p.weight,
            "transmitter_uuid": p.transmitter_uuid,
            "mode": p.mode,
            "satellite": "",
            "transmitter_desc": "",
            "transmitter_status": None,
        }

    def _enrich_priorities_sync(self, entries: list[Priority]) -> list[dict]:
        """Look up each entry's satellite name and transmitter description.

        Uses the same cached DbClient the planner itself uses (24h TTL on both
        catalogues), so this only costs a real request the first time - or
        never, if a plan run has already warmed the cache.
        """
        cache = Cache(self.cache_dir, offline=self.s.offline)
        db = DbClient(self._build_autoscheduler_settings(0.0), cache)
        try:
            satellites = db.satellites_by_norad()
        except Exception as exc:
            log.warning("could not load satellite catalogue: %s", exc)
            satellites = {}
        try:
            transmitters = db.transmitters_by_uuid()
        except Exception as exc:
            log.warning("could not load transmitter catalogue: %s", exc)
            transmitters = {}

        out = []
        for p in entries:
            satellite = (satellites.get(p.norad_cat_id) or {}).get("name") or ""
            transmitter_status = None
            if not p.transmitter_uuid:
                transmitter_desc = "auto (best available)"
            else:
                tx = transmitters.get(p.transmitter_uuid)
                if tx is None:
                    transmitter_desc = "unknown transmitter"
                else:
                    mhz = (tx.get("downlink_low") or 0) / 1e6
                    transmitter_desc = f"{mhz:.3f} MHz {tx.get('mode') or ''}".strip()
                    # transmitters_by_uuid() is unfiltered by status (only the
                    # DB API's own alive=true query param is guaranteed) -
                    # unlike the picker's list, a pinned transmitter here can
                    # have gone inactive since it was chosen. That matters:
                    # pick_transmitter() only honours a pin among a station's
                    # *active* candidates, so a pin that has gone stale is
                    # silently dropped to auto at the next plan run with no
                    # other visible sign of it.
                    transmitter_status = tx.get("status") or None
            out.append({
                "norad_cat_id": p.norad_cat_id,
                "weight": p.weight,
                "transmitter_uuid": p.transmitter_uuid,
                "mode": p.mode,
                "satellite": satellite,
                "transmitter_desc": transmitter_desc,
                "transmitter_status": transmitter_status,
            })
        return out

    async def get_transmitters(self, norad_cat_id: int) -> dict:
        """Transmitters this station can actually hear for one satellite.

        Used by the priority-list picker to offer a real choice instead of a
        UUID nobody can read - deliberately narrower than the whole alive
        catalogue (`DbClient.transmitters_by_uuid()`), which would let an
        operator pin something station 5024's antennas can never record.
        """
        return await asyncio.to_thread(self._get_transmitters_sync, norad_cat_id)

    def _get_transmitters_sync(self, norad_cat_id: int) -> dict:
        cache = Cache(self.cache_dir, offline=self.s.offline)
        auto_settings = self._build_autoscheduler_settings(0.0)
        db = DbClient(auto_settings, cache)
        network = NetworkClient(auto_settings, cache)

        satellite = db.satellites_by_norad().get(norad_cat_id)
        if satellite is None:
            raise ValueError(f"NORAD {norad_cat_id} is not in the SatNOGS DB catalogue")

        station = network.get_station(self._effective_station_id())
        by_norad = db.transmitters_for_station(station.segments)
        transmitters = by_norad.get(norad_cat_id, [])

        return {
            "norad_cat_id": norad_cat_id,
            "satellite": satellite.get("name") or "",
            "transmitters": [
                {
                    "uuid": tx.uuid,
                    "downlink_hz": tx.downlink_hz,
                    "mode": tx.mode,
                    "description": tx.description,
                }
                for tx in transmitters
            ],
        }

    async def save_priorities(self, entries: list[dict]) -> None:
        async with self._lists_lock:
            # Resolved while holding the lock, so a concurrent
            # load_priority_list() can't switch the active list out from
            # under this write.
            target = self.priority_file
            priorities = [
                Priority(
                    norad_cat_id=int(e["norad_cat_id"]),
                    weight=max(0.0, min(1.0, float(e["weight"]))),
                    transmitter_uuid=e.get("transmitter_uuid") or None,
                    mode="manual" if e.get("mode") == "manual" else "auto",
                )
                for e in entries
            ]
            slug = self._manifest["active"]
            # Order matters: the .txt first, the sidecar second, so the
            # sidecar is always the newer of the two and _load_rows_sync
            # does not mistake our own save for a hand-edit.
            await asyncio.to_thread(write_priority_file, target, priorities)
            await asyncio.to_thread(self._write_meta_sync, slug, priorities)
