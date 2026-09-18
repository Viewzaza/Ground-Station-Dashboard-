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
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ..config import Settings
from ..vendor.autoscheduler import cli as auto_cli
from ..vendor.autoscheduler.cache import Cache
from ..vendor.autoscheduler.config import Settings as AutoSettings
from ..vendor.autoscheduler.db_client import DbClient
from ..vendor.autoscheduler.network_client import NetworkClient
from ..vendor.autoscheduler.priorities import Priority, parse_priority_file, write_priority_file
from ..vendor.autoscheduler.report import _selection_payload

log = logging.getLogger(__name__)

_DEFAULT_PRIORITIES = Path(__file__).resolve().parent.parent / "vendor/autoscheduler/priorities.default.txt"
_DEFAULT_LIST_SLUG = "default"
_DEFAULT_LIST_NAME = "Default"


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

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.priority_lists_dir.mkdir(parents=True, exist_ok=True)

        self._config = self._load_config()
        self._manifest = self._load_or_migrate_manifest()

        self._run_lock = asyncio.Lock()
        # Guards both "which list is active" and that list's file contents
        # together, so a save from one browser tab can never land in a
        # different file than the one that was active when the save started
        # (the race: tab A saving list A while tab B's load_priority_list
        # flips the active slug to B mid-write).
        self._lists_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self._running = False
        self._last_error: str | None = None

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

    def _effective_station_id(self) -> int:
        return self._config.get("station_id") or self.s.station_id

    def _effective_db_token(self) -> str:
        return self._config.get("db_token") or self.s.satnogs_db_token

    async def get_config(self) -> dict:
        async with self._config_lock:
            return {
                "station_id": self._effective_station_id(),
                "station_id_is_override": bool(self._config.get("station_id")),
                "db_token_set": bool(self._effective_db_token()),
            }

    async def save_config(self, station_id: int | None, db_token: str | None) -> dict:
        """`None` leaves a field unchanged; `""`/`0` clears the override back
        to the dashboard's own default."""
        async with self._config_lock:
            if station_id is not None:
                self._config["station_id"] = station_id or None
            if db_token is not None:
                self._config["db_token"] = db_token or None
            await asyncio.to_thread(self._write_config)
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

    def _mock_result(self) -> dict:
        """A small canned plan, so GS_MOCK=1 never touches the live SatNOGS APIs.

        Includes one canned notice so the "ok_with_warnings" UI path is
        exercised by default in dev, not only against a real run.
        """
        now = datetime.now(timezone.utc).isoformat()
        return {
            "status": "ok_with_warnings",
            "station": self._effective_station_id(),
            "generated_utc": now,
            "considered": 2,
            "rejected_conflict": 0,
            "rejected_capped": 0,
            "notices": [
                {"severity": "warning",
                 "message": "12345 was not considered: SatNOGS DB has no TLE for it"},
            ],
            "observations": [
                {
                    "start": now, "end": now, "duration_s": 300,
                    "norad_cat_id": self.s.default_norad, "satellite": "KNACKSAT-2",
                    "max_elevation_deg": 45.0, "aos_azimuth_deg": 10.0,
                    "los_azimuth_deg": 190.0, "transmitter_uuid": "mock-transmitter",
                    "downlink_hz": 400_630_000, "mode": "GFSK", "observed_here": 3,
                    "score": 2.0, "is_mission": True,
                },
            ],
        }

    def is_running(self) -> bool:
        return self._running

    async def run_plan(self, hours: float | None = None) -> dict:
        if self._running:
            return {"status": "running"}
        async with self._run_lock:
            if self._running:
                return {"status": "running"}
            self._running = True
        try:
            if self.s.mock:
                result = self._mock_result()
            else:
                result = await asyncio.to_thread(
                    self._run_plan_sync, hours or self.s.schedule_hours
                )
            self._write_result(result)
            self._last_error = None
            self.on_state("schedule", "ok",
                          f"{len(result.get('observations', []))} observation(s)")
            return result
        except Exception as exc:
            self._last_error = str(exc)
            log.exception("schedule run failed")
            self.on_state("schedule", "degraded", str(exc))
            return {"status": "error", "error": str(exc)}
        finally:
            self._running = False

    def _run_plan_sync(self, hours: float) -> dict:
        auto_settings = self._build_autoscheduler_settings(hours)
        args = SimpleNamespace(history_pages=self.s.schedule_history_pages)
        outcome = auto_cli.plan(auto_settings, args)
        if outcome is None:
            raise RuntimeError("planning produced nothing schedulable - see backend logs")
        station, selection, plan_report = outcome
        payload = _selection_payload(selection, station.id)

        notices: list[dict] = []
        for finding in plan_report.findings:
            if finding.severity == "ok":
                continue
            notices.append({
                "severity": finding.severity,
                "message": f"{finding.satellite} ({finding.norad_cat_id}): {finding.message}",
            })
        for skip in plan_report.skipped:
            notices.append({
                "severity": "warning",
                "message": f"{skip['norad']} was not considered: {skip['reason']}",
            })
        if plan_report.thin_history:
            notices.append({"severity": "warning", "message": plan_report.thin_history})

        payload["status"] = "ok_with_warnings" if notices else "ok"
        payload["notices"] = notices
        return payload

    def _write_result(self, result: dict) -> None:
        tmp = self.result_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=2), encoding="utf-8")
        tmp.replace(self.result_path)   # atomic, matching cache.py's own writes

    def get_last_run(self) -> dict:
        if not self.result_path.is_file():
            return {"status": "never_run"}
        try:
            return json.loads(self.result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not read %s: %s", self.result_path, exc)
            return {"status": "never_run"}

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
            else:
                await asyncio.to_thread(write_priority_file, new_path, [])
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

    # --- priorities (the active list's contents) ------------------------------
    async def get_priorities(self) -> list[dict]:
        if not self.priority_file.is_file():
            return []
        entries = sorted(parse_priority_file(self.priority_file).values(), key=lambda p: p.line)
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
            await asyncio.to_thread(write_priority_file, target, priorities)
