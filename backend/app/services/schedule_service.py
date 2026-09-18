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
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ..config import Settings
from ..vendor.autoscheduler import cli as auto_cli
from ..vendor.autoscheduler.config import Settings as AutoSettings
from ..vendor.autoscheduler.priorities import Priority, parse_priority_file, write_priority_file
from ..vendor.autoscheduler.report import _selection_payload

log = logging.getLogger(__name__)

_DEFAULT_PRIORITIES = Path(__file__).resolve().parent.parent / "vendor/autoscheduler/priorities.default.txt"


class ScheduleService:
    def __init__(self, settings: Settings, on_state=None) -> None:
        self.s = settings
        self.on_state = on_state or (lambda component, state, detail="": None)

        self.priority_file = settings.data_dir / "priorities.txt"
        self.cache_dir = settings.data_dir / "autoscheduler_cache"
        self.result_path = settings.data_dir / "schedule_last_run.json"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if not self.priority_file.is_file() and _DEFAULT_PRIORITIES.is_file():
            shutil.copyfile(_DEFAULT_PRIORITIES, self.priority_file)

        self._run_lock = asyncio.Lock()
        self._priorities_lock = asyncio.Lock()
        self._running = False
        self._last_error: str | None = None

    # --- schedule runs -------------------------------------------------------
    def _build_autoscheduler_settings(self, hours: float) -> AutoSettings:
        return AutoSettings(
            station_id=self.s.station_id,
            db_token=self.s.satnogs_db_token,
            cache_dir=self.cache_dir,
            hours=hours,
            mission_norad=self.s.default_norad,
            priority_file=self.priority_file,
            offline=self.s.offline,
            db_base_url=self.s.satnogs_db,
            network_base_url=self.s.satnogs_network,
        )

    def _mock_result(self) -> dict:
        """A small canned plan, so GS_MOCK=1 never touches the live SatNOGS APIs."""
        now = datetime.now(timezone.utc).isoformat()
        return {
            "station": self.s.station_id,
            "generated_utc": now,
            "considered": 2,
            "rejected_conflict": 0,
            "rejected_capped": 0,
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
        station, selection = outcome
        return _selection_payload(selection, station.id)

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

    # --- priorities ------------------------------------------------------------
    def get_priorities(self) -> list[dict]:
        if not self.priority_file.is_file():
            return []
        entries = parse_priority_file(self.priority_file)
        return [
            {
                "norad_cat_id": p.norad_cat_id,
                "weight": p.weight,
                "transmitter_uuid": p.transmitter_uuid,
            }
            for p in sorted(entries.values(), key=lambda p: p.line)
        ]

    async def save_priorities(self, entries: list[dict]) -> None:
        async with self._priorities_lock:
            priorities = [
                Priority(
                    norad_cat_id=int(e["norad_cat_id"]),
                    weight=max(0.0, min(1.0, float(e["weight"]))),
                    transmitter_uuid=e.get("transmitter_uuid") or None,
                )
                for e in entries
            ]
            await asyncio.to_thread(write_priority_file, self.priority_file, priorities)
