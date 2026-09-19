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

# How long we keep treating an item this process just submitted as occupied,
# independent of what SatNOGS Network's own read API reports back. Measured
# against the real API: its booking list can still omit an observation more
# than an hour after the write that created it succeeded - long enough that
# a routine backend restart (e.g. to deploy a fix) can easily outlast it,
# which is exactly why this is persisted to disk rather than kept in memory
# only. Without it, the very next preview recomputes the identical
# "available" slot it just used - and, worse, everything already rejected
# as a conflict too, since nothing distinguishes "still lagging" from
# "genuinely free" on our end.
RECENT_ATTEMPT_TTL = timedelta(hours=2)


class CampaignService:
    def __init__(self, settings: Settings, schedule_service: ScheduleService, on_state=None) -> None:
        self.s = settings
        self.schedule_service = schedule_service
        self.on_state = on_state or (lambda component, state, detail="": None)

        self.preview_path = settings.data_dir / "campaign_last_preview.json"
        self.result_path = settings.data_dir / "campaign_last_run.json"
        self.history_path = settings.data_dir / "campaign_history.json"
        self.recent_attempts_path = settings.data_dir / "campaign_recent_attempts.json"

        self._run_lock = asyncio.Lock()
        self._running = False
        # [(station_id, start, end, attempted_at), ...] - every item this
        # process has submitted recently, success or rejection alike; see
        # RECENT_ATTEMPT_TTL and _recent_bookings_by_station(). Persisted so
        # a restart can't discard it out from under an in-flight lag window.
        self._recent_attempts: list[tuple[int, datetime, datetime, datetime]] = (
            self._load_recent_attempts()
        )

    def is_running(self) -> bool:
        return self._running

    def _effective_mock(self) -> bool:
        return self.s.mock if self.s.campaign_mock is None else self.s.campaign_mock

    def _record_attempts(self, items: list[dict], now: datetime) -> None:
        cutoff = now - RECENT_ATTEMPT_TTL
        self._recent_attempts = [a for a in self._recent_attempts if a[3] >= cutoff]
        for item in items:
            self._recent_attempts.append((
                item["station_id"],
                datetime.fromisoformat(item["start"]),
                datetime.fromisoformat(item["end"]),
                now,
            ))
        self._write_json(self.recent_attempts_path, [
            [station_id, start.isoformat(), end.isoformat(), attempted_at.isoformat()]
            for station_id, start, end, attempted_at in self._recent_attempts
        ])

    def _recent_bookings_by_station(self, now: datetime) -> dict[int, list[tuple[datetime, datetime]]]:
        cutoff = now - RECENT_ATTEMPT_TTL
        out: dict[int, list[tuple[datetime, datetime]]] = {}
        for station_id, start, end, attempted_at in self._recent_attempts:
            if attempted_at >= cutoff:
                out.setdefault(station_id, []).append((start, end))
        return out

    def _load_recent_attempts(self) -> list[tuple[int, datetime, datetime, datetime]]:
        raw = self._read_json(self.recent_attempts_path, [])
        now = datetime.now(timezone.utc)
        cutoff = now - RECENT_ATTEMPT_TTL
        out: list[tuple[int, datetime, datetime, datetime]] = []
        for entry in raw:
            try:
                station_id, start, end, attempted_at = entry
                attempted_dt = datetime.fromisoformat(attempted_at)
                if attempted_dt >= cutoff:
                    out.append((station_id, datetime.fromisoformat(start),
                                datetime.fromisoformat(end), attempted_dt))
            except (ValueError, TypeError) as exc:
                log.warning("skipping unparseable recent-attempt entry %r: %s", entry, exc)
        return out

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
        now = datetime.now(timezone.utc)
        preview = build_campaign(
            network, db,
            mission_norad=self.s.default_norad,
            transmitter_uuid=None,
            now=now,
            exclude_station_id=self.schedule_service._effective_station_id(),
            max_per_station=self.schedule_service.campaign_max_per_station(),
            max_total=self.schedule_service.campaign_max_total(),
            recent_attempts=self._recent_bookings_by_station(now),
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
        now = datetime.now(timezone.utc)

        if items is None:
            db = DbClient(auto_settings, cache)
            preview = build_campaign(
                network, db,
                mission_norad=self.s.default_norad,
                transmitter_uuid=None,
                now=now,
                exclude_station_id=self.schedule_service._effective_station_id(),
                max_per_station=self.schedule_service.campaign_max_per_station(),
                max_total=self.schedule_service.campaign_max_total(),
                recent_attempts=self._recent_bookings_by_station(now),
            )
            items = _campaign_preview_payload(preview)["items"]

        # Recorded before submitting, not just on acceptance - a rejected
        # item is still "just tried", and retrying it immediately (before
        # SatNOGS's own read view has any chance of catching up) would only
        # reproduce the same rejection. See RECENT_ATTEMPT_TTL.
        self._record_attempts(items, now)

        schedule_items = [
            to_schedule_item(
                item["station_id"], item["transmitter_uuid"],
                datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"]),
            )
            for item in items
        ]
        # The one and only execute=True call in this codebase.
        result = network.schedule(schedule_items, execute=True)

        # `schedule_items` is built 1:1 from `items` and `accepted_items` holds
        # those same dict objects back, so identity maps the API's answer onto
        # the richer preview rows without re-parsing anything. Keeping the
        # per-item detail is what later lets a run be cross-checked against the
        # real calendar - the aggregate counts alone cannot say which booking
        # it was that landed.
        accepted_ids = {id(sent) for sent in result.accepted_items}
        accepted_detail = [
            {
                "station_id": original["station_id"],
                "station_name": original.get("station_name", ""),
                "transmitter_uuid": original["transmitter_uuid"],
                "start": original["start"],
                "end": original["end"],
            }
            for original, sent in zip(items, schedule_items)
            if id(sent) in accepted_ids
        ]

        return {
            "status": "ok" if not result.errors else "ok_with_warnings",
            "trigger": trigger,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "submitted": result.submitted,
            "accepted": result.accepted,
            "errors": result.errors,
            "accepted_items": accepted_detail,
        }

    def _mock_commit(self, items: list[dict] | None, trigger: str) -> dict:
        count = len(items) if items is not None else 1
        return {
            "status": "ok", "trigger": trigger,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "submitted": count, "accepted": count, "errors": [],
            "accepted_items": list(items or []),
        }

    def get_last_run(self) -> dict:
        return self._read_json(self.result_path, {"status": "never_run"})

    # --- cross-check ----------------------------------------------------------
    async def verify_last_run(self) -> dict:
        """Ask SatNOGS what is actually on each station's calendar now, and
        match the last run's accepted bookings against it.

        The commit result records what the API *said* it took. This is the
        independent read-back: an accepted POST and an observation that is
        really on the calendar are not the same claim, and only the second one
        means the station will actually record anything.
        """
        if self._effective_mock():
            return self._mock_verify()
        return await asyncio.to_thread(self._verify_sync)

    def _verify_sync(self) -> dict:
        run = self.get_last_run()
        items = run.get("accepted_items") or []
        now = datetime.now(timezone.utc)
        checked: list[dict] = []

        if not items:
            return {
                "status": "nothing_to_check",
                "generated_utc": now.isoformat(),
                "run_generated_utc": run.get("generated_utc"),
                "items": [],
            }

        auto_settings = self._build_autoscheduler_settings()
        cache = Cache(self.schedule_service.cache_dir, offline=self.s.offline)
        network = NetworkClient(auto_settings, cache)
        mission_norad = self.s.default_norad

        # One live read per station, not per booking - a station with two
        # accepted passes is one calendar.
        by_station: dict[int, list[dict]] = {}
        for item in items:
            by_station.setdefault(item["station_id"], []).append(item)

        for station_id, station_items in by_station.items():
            try:
                bookings = network.future_bookings(station_id, now=now)
            except Exception as exc:
                log.warning("could not read back station %d: %s", station_id, exc)
                for item in station_items:
                    checked.append({**item, "state": "unknown",
                                    "detail": f"could not read this station's calendar: {exc}"})
                continue

            for item in station_items:
                start = datetime.fromisoformat(item["start"])
                end = datetime.fromisoformat(item["end"])
                # future_bookings only returns observations that have not
                # started yet, so anything already underway or finished is
                # simply not in that feed - reporting it "missing" would be
                # wrong, and this is reachable whenever a booking is verified
                # more than ~11 minutes after it was made.
                if start <= now:
                    checked.append({**item, "state": "started",
                                    "detail": "already under way or past; "
                                              "the scheduling feed only lists future observations"})
                    continue
                # Overlap rather than an exact start match: SatNOGS splits long
                # passes into segments of its own choosing, so what comes back
                # need not share our boundaries.
                hit = next(
                    (b for b in bookings
                     if b.norad_cat_id == mission_norad and b.start < end and b.end > start),
                    None,
                )
                if hit is None:
                    checked.append({**item, "state": "missing",
                                    "detail": "no matching observation on this station's calendar"})
                else:
                    checked.append({**item, "state": "on_schedule",
                                    "observation_id": hit.id,
                                    "observation_start": hit.start.isoformat(),
                                    "observation_end": hit.end.isoformat(),
                                    "detail": f"observation {hit.id}"})

        return {
            "status": "ok",
            "generated_utc": now.isoformat(),
            "run_generated_utc": run.get("generated_utc"),
            "stations_checked": len(by_station),
            "items": checked,
        }

    def _mock_verify(self) -> dict:
        now = datetime.now(timezone.utc)
        run = self.get_last_run()
        items = run.get("accepted_items") or []
        return {
            "status": "ok",
            "generated_utc": now.isoformat(),
            "run_generated_utc": run.get("generated_utc"),
            "stations_checked": len({i["station_id"] for i in items}),
            "items": [
                {**item, "state": "on_schedule", "observation_id": 900000 + n,
                 "detail": f"observation {900000 + n}"}
                for n, item in enumerate(items)
            ],
        }

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
