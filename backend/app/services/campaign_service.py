"""Network campaign scheduling: automates requesting other SatNOGS community
stations to record the mission satellite (KNACKSAT-2), a manual workflow the
operator otherwise repeats daily on network.satnogs.org's own web form
because SatNOGS itself won't accept a booking more than ~48h ahead.

This is the first (and only) place in this codebase that ever submits a
real booking (`NetworkClient.schedule(execute=True)`). Every other feature
built before this deliberately left that path unused - see
`network_client.py`'s `schedule()` docstring. `_build_autoscheduler_settings()`
below is the one and only place a real network token reaches `AutoSettings`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import Settings
from ..vendor.autoscheduler.cache import Cache
from ..vendor.autoscheduler.campaign import (
    CampaignItem, CampaignPreview, _campaign_preview_payload, build_campaign,
)
from ..vendor.autoscheduler.config import Settings as AutoSettings
from ..vendor.autoscheduler.db_client import DbClient
from ..vendor.autoscheduler.network_client import NetworkClient, to_schedule_item
from .schedule_service import ScheduleService

log = logging.getLogger(__name__)


class CampaignService:
    def __init__(self, settings: Settings, schedule_service: ScheduleService, on_state=None) -> None:
        self.s = settings
        self.schedule_service = schedule_service
        self.on_state = on_state or (lambda component, state, detail="": None)

        self.preview_path = settings.data_dir / "campaign_last_preview.json"
        self.result_path = settings.data_dir / "campaign_last_run.json"
        self.history_path = settings.data_dir / "campaign_history.json"

        self._run_lock = asyncio.Lock()
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def _effective_mock(self) -> bool:
        return self.s.mock if self.s.campaign_mock is None else self.s.campaign_mock

    # --- settings ---------------------------------------------------------
    def _build_autoscheduler_settings(self) -> AutoSettings:
        # Reuses ScheduleService's own builder for station_id/db_token/
        # priority_file/etc., and adds the one field it always leaves blank.
        auto_settings = self.schedule_service._build_autoscheduler_settings(0.0)
        auto_settings.network_token = self.schedule_service._effective_network_token()
        return auto_settings

    # --- preview -----------------------------------------------------------
    async def preview_campaign(self) -> dict:
        if self._running:
            return {"status": "running"}
        async with self._run_lock:
            if self._running:
                return {"status": "running"}
            self._running = True
        try:
            if self._effective_mock():
                result = self._mock_preview()
            else:
                result = await asyncio.to_thread(self._preview_sync)
            self._write_json(self.preview_path, result)
            self.on_state("campaign", "ok", f"{len(result.get('items', []))} candidate(s)")
            return result
        except Exception as exc:
            log.exception("campaign preview failed")
            result = {
                "status": "error", "error": str(exc),
                "generated_utc": datetime.now(timezone.utc).isoformat(),
            }
            self._write_json(self.preview_path, result)
            self.on_state("campaign", "degraded", str(exc))
            return result
        finally:
            self._running = False

    def _preview_sync(self) -> dict:
        cache = Cache(self.schedule_service.cache_dir, offline=self.s.offline)
        auto_settings = self._build_autoscheduler_settings()
        db = DbClient(auto_settings, cache)
        network = NetworkClient(auto_settings, cache)
        preview = build_campaign(
            network, db,
            mission_norad=self.s.default_norad,
            transmitter_uuid=None,
            now=datetime.now(timezone.utc),
            exclude_station_id=self.schedule_service._effective_station_id(),
            max_per_station=self.schedule_service.campaign_max_per_station(),
            max_total=self.schedule_service.campaign_max_total(),
        )
        return _campaign_preview_payload(preview)

    def _mock_preview(self) -> dict:
        now = datetime.now(timezone.utc)
        return _campaign_preview_payload(CampaignPreview(
            generated_utc=now,
            window_start=now + timedelta(minutes=11),
            window_end=now + timedelta(minutes=2880),
            considered_stations=3,
            items=[
                CampaignItem(
                    station_id=999001, station_name="Mock Station A",
                    transmitter_uuid="mock-transmitter",
                    start=now + timedelta(hours=2), end=now + timedelta(hours=2, minutes=8),
                    max_elevation_deg=42.0,
                ),
            ],
            skipped=[{
                "station_id": 999002, "station_name": "Mock Station B",
                "reason": "no qualifying pass in the campaign window",
            }],
        ))

    def get_last_preview(self) -> dict:
        return self._read_json(self.preview_path, {"status": "never_run"})

    # --- commit --------------------------------------------------------------
    async def commit_campaign(self, items: list[dict] | None = None, trigger: str = "manual") -> dict:
        if self._running:
            return {"status": "running"}
        async with self._run_lock:
            if self._running:
                return {"status": "running"}
            self._running = True
        try:
            if self._effective_mock():
                result = self._mock_commit(items, trigger)
            else:
                result = await asyncio.to_thread(self._commit_sync, items, trigger)
            self._write_json(self.result_path, result)
            self._append_history(result)
            state = "ok" if result.get("status") != "error" else "degraded"
            self.on_state("campaign", state, f"{result.get('accepted', 0)} booked")
            return result
        except Exception as exc:
            log.exception("campaign commit failed")
            result = {
                "status": "error", "trigger": trigger, "error": str(exc),
                "generated_utc": datetime.now(timezone.utc).isoformat(),
                "submitted": 0, "accepted": 0, "errors": [],
            }
            self._write_json(self.result_path, result)
            self._append_history(result)
            self.on_state("campaign", "degraded", str(exc))
            return result
        finally:
            self._running = False

    def _commit_sync(self, items: list[dict] | None, trigger: str) -> dict:
        auto_settings = self._build_autoscheduler_settings()
        if not auto_settings.network_token:
            return {
                "status": "error", "trigger": trigger,
                "generated_utc": datetime.now(timezone.utc).isoformat(),
                "error": "no network token configured - set one in Station Schedule settings first",
                "submitted": 0, "accepted": 0, "errors": [],
            }

        cache = Cache(self.schedule_service.cache_dir, offline=self.s.offline)
        network = NetworkClient(auto_settings, cache)

        if items is None:
            db = DbClient(auto_settings, cache)
            preview = build_campaign(
                network, db,
                mission_norad=self.s.default_norad,
                transmitter_uuid=None,
                now=datetime.now(timezone.utc),
                exclude_station_id=self.schedule_service._effective_station_id(),
                max_per_station=self.schedule_service.campaign_max_per_station(),
                max_total=self.schedule_service.campaign_max_total(),
            )
            items = _campaign_preview_payload(preview)["items"]

        schedule_items = [
            to_schedule_item(
                item["station_id"], item["transmitter_uuid"],
                datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"]),
            )
            for item in items
        ]
        # The one and only execute=True call in this codebase.
        result = network.schedule(schedule_items, execute=True)
        return {
            "status": "ok" if not result.errors else "ok_with_warnings",
            "trigger": trigger,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "submitted": result.submitted,
            "accepted": result.accepted,
            "errors": result.errors,
        }

    def _mock_commit(self, items: list[dict] | None, trigger: str) -> dict:
        count = len(items) if items is not None else 1
        return {
            "status": "ok", "trigger": trigger,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "submitted": count, "accepted": count, "errors": [],
        }

    def get_last_run(self) -> dict:
        return self._read_json(self.result_path, {"status": "never_run"})

    def get_history(self) -> list[dict]:
        return self._read_json(self.history_path, [])

    def _append_history(self, result: dict) -> None:
        history = self.get_history()
        submitted = result.get("submitted", 0)
        accepted = result.get("accepted", 0)
        history.append({
            "generated_utc": result.get("generated_utc"),
            "trigger": result.get("trigger", "manual"),
            "status": result.get("status"),
            "submitted": submitted,
            "accepted": accepted,
            "rejected": submitted - accepted,
        })
        # A daily cadence means even a year of history is small, but there's
        # no reason to let this grow forever.
        self._write_json(self.history_path, history[-200:])

    # --- the timer -----------------------------------------------------------
    async def run_auto_cycle(self) -> None:
        """Called only by scheduler.py's timer loop. Always previews first;
        only auto-commits if the operator has explicitly turned that on."""
        preview = await self.preview_campaign()
        if not self.schedule_service.campaign_auto_commit_enabled():
            return
        if preview.get("status") == "error" or not preview.get("items"):
            return
        await self.commit_campaign(items=preview["items"], trigger="auto")

    # --- small json helpers ----------------------------------------------------
    def _read_json(self, path: Path, default):
        if not path.is_file():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not read %s: %s", path, exc)
            return default

    def _write_json(self, path: Path, data) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
