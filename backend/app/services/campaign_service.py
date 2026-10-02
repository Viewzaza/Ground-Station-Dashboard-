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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from ..config import Settings
from ..vendor.autoscheduler.cache import Cache
from ..vendor.autoscheduler.campaign import (
    ELEVATION_BAND_FLOORS, CampaignItem, CampaignPreview, _band_of,
    _campaign_preview_payload, build_campaign,
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

# Backstop for campaign_loop_until_exhausted. The loop ends on its own once
# every station hits its per-station cap or runs out of free passes, since
# each round can only pick what earlier rounds did not; this just bounds it
# if that reasoning is ever wrong.
MAX_LOOP_ROUNDS = 20

# Most items sent in one booking POST. The server validates a whole POST
# before it saves any of it, but the save loop that follows is not wrapped in
# a transaction, and every item costs it database work - so a POST big enough
# to outlast gunicorn's 30 s worker timeout is killed mid-save, leaving an
# unknown subset booked (the SatnogsOutcomeUnknown path: nothing in it can be
# resubmitted). 50 keeps each POST far from that while a 600-item run is still
# ~12 POSTs, not the 600 a one-at-a-time rule would cost.
CAMPAIGN_POST_CHUNK = 50

# What NetworkClient.schedule() writes into an error when the connection never
# completed. Matched as text because that is the only form schedule() reports
# it in; every such message it produces contains this phrase.
_UNREACHABLE = "could not reach SatNOGS"


@dataclass
class _BatchOutcome:
    """What one commit round's POSTs came back with, across every chunk."""
    submitted: int = 0
    errors: list[str] = field(default_factory=list)
    accepted: list[dict] = field(default_factory=list)
    # Sent, but no reliable answer: possibly booked. Never resubmitted.
    uncertain: list[dict] = field(default_factory=list)
    # Items in later POSTs that were never attempted because SatNOGS had
    # become unreachable. (A POST that failed part-way says in its own error
    # how many of its items went unsent.)
    not_sent: int = 0
    unreachable: bool = False


def _result_row(item: dict) -> dict:
    """The per-booking row a run record keeps: the preview row's own fields,
    re-keyed from the original rather than the POST body so nothing the
    operator saw (station name, band, which downlink) is lost."""
    return {
        "station_id": item["station_id"],
        "station_name": item.get("station_name", ""),
        "transmitter_uuid": item["transmitter_uuid"],
        "transmitter_description": item.get("transmitter_description", ""),
        "fallback": bool(item.get("fallback", False)),
        "start": item["start"],
        "end": item["end"],
        "max_elevation_deg": item.get("max_elevation_deg"),
    }


def _band_labels() -> list[str]:
    # "90-75", "75-60", ... "15-0" - the same bands build_campaign spreads
    # across, labelled from ELEVATION_BAND_FLOORS so the two cannot drift.
    ceilings = (90.0, *ELEVATION_BAND_FLOORS[:-1])
    return [f"{top:g}-{floor:g}" for top, floor in zip(ceilings, ELEVATION_BAND_FLOORS)]


def _band_counts(rows: Iterable[dict]) -> list[dict]:
    counts = [0] * len(ELEVATION_BAND_FLOORS)
    for row in rows:
        elevation = row.get("max_elevation_deg")
        if elevation is None:
            continue  # a row from an older client: its band is unknown, not 15-0
        counts[_band_of(float(elevation))] += 1
    return [{"band": label, "count": n} for label, n in zip(_band_labels(), counts)]


def _preference_ranked(uuids: Iterable[str], preference: list[str]) -> list[str]:
    """Distinct uuids, configured primary first, then its fallbacks in their
    configured order, then anything else in first-seen order (sorted() is
    stable, so the unranked keep the order they arrived in)."""
    rank = {uuid: position for position, uuid in enumerate(preference)}
    return sorted(dict.fromkeys(uuids), key=lambda uuid: rank.get(uuid, len(rank)))


def _transmitter_summary(rows: list[dict], preference: list[str]) -> list[dict]:
    groups: dict[str, dict] = {}
    for row in rows:
        group = groups.setdefault(row["transmitter_uuid"], {
            "uuid": row["transmitter_uuid"], "description": "",
            "fallback": False, "stations": set(), "bookings": 0,
        })
        group["description"] = group["description"] or row.get("transmitter_description", "")
        group["fallback"] = group["fallback"] or bool(row.get("fallback", False))
        group["stations"].add(row["station_id"])
        group["bookings"] += 1
    return [
        {**groups[uuid], "stations": len(groups[uuid]["stations"])}
        for uuid in _preference_ranked(groups, preference)
    ]


def _chunks_by_transmitter(items: list[dict], preference: list[str],
                           size: int = CAMPAIGN_POST_CHUNK) -> list[list[dict]]:
    """Split a round into POSTs of ONE transmitter uuid each, `size` at most.

    One uuid per POST because a batch that mixes two makes the server fetch
    the whole SatNOGS DB transmitter list, with a 2 s timeout, to validate it -
    and a timeout there fails the entire batch ("Error in DB API connection").
    The primary downlink goes first: if SatNOGS goes away part-way through a
    run, the bookings already made are the more productive kind.
    """
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item["transmitter_uuid"], []).append(item)
    chunks: list[list[dict]] = []
    for uuid in _preference_ranked(groups, preference):
        group = groups[uuid]
        chunks.extend(group[i:i + size] for i in range(0, len(group), size))
    return chunks


def _calendar_sources(network) -> dict[str, int]:
    # Which feed served each calendar read on this client - "jobs" is free,
    # "observations" spends the token's 240/hour. getattr because a client
    # that predates the counter (or a test stub) simply reports nothing.
    return {str(k): int(v) for k, v in (getattr(network, "calendar_sources", None) or {}).items()}


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
        # station_id -> that station's future bookings, as build_campaign read
        # them. A preview fills it and the commit that follows reuses it for
        # EVERY round, which is what makes a looped commit cost no reads after
        # the first build: re-reading every calendar each round is what used
        # to burn the 240/hour observation budget (682 pages for a 4-round
        # commit that needed 175). Stale is safe here, for two reasons: what
        # this process submitted since the read is overlaid as occupied by
        # _recent_attempts, and anything someone ELSE booked since is refused
        # by SatNOGS itself - it answers 409 to any item overlapping an
        # existing observation on that station, and a refused batch books
        # nothing. See _calendar_cache_for() for the expiry rules. Never used
        # by verify_last_run, which exists to be an independent read-back.
        self._calendar_cache: dict[int, list] = {}
        self._calendar_cache_at: datetime | None = None

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

    def _calendar_cache_for(self, now: datetime) -> dict[int, list]:
        """The calendar cache, emptied first if it is older than
        campaign_calendar_ttl_s (or the clock has stepped backwards).

        Age is measured from when the cache was last emptied, i.e. from before
        its oldest entry was read, so it errs towards re-reading. A commit
        calls this ONCE and holds on to the dict for all its rounds - rounds
        2+ must not re-read even if the TTL runs out mid-commit, since
        everything the commit itself booked is already overlaid by
        _recent_attempts. A TTL of 0 therefore still shares calendars between
        the rounds of one commit, just never across operations.
        """
        at = self._calendar_cache_at
        if (at is None or now < at
                or (now - at).total_seconds() >= self.s.campaign_calendar_ttl_s):
            self._calendar_cache = {}
            self._calendar_cache_at = now
        return self._calendar_cache

    def _drop_calendar_cache(self) -> None:
        """Forget cached calendars once this process has POSTed to them.

        After a booking the cached calendars no longer describe the stations,
        and cap_counts_existing only counts what is ON a calendar: a second
        click inside the TTL would otherwise see none of the first click's
        bookings and stack another max_per_station onto every station. A POST
        that was refused (409) proves the cache stale too.

        Rebinds rather than clear()s on purpose: the commit in progress keeps
        its own reference for its remaining rounds, where _recent_attempts
        already covers what it booked.
        """
        self._calendar_cache = {}
        self._calendar_cache_at = None

    # --- settings ---------------------------------------------------------
    def _build_autoscheduler_settings(self) -> AutoSettings:
        # Reuses ScheduleService's own builder for station_id/db_token/
        # priority_file/etc., and adds the one field it always leaves blank.
        auto_settings = self.schedule_service._build_autoscheduler_settings(0.0)
        auto_settings.network_token = self.schedule_service._effective_network_token()
        return auto_settings

    def _transmitter_preference(self) -> list[str]:
        """Every campaign downlink, most wanted first, whatever the policy -
        used to ORDER things (POSTs, summaries), never to choose them."""
        primary = self.s.campaign_transmitter_uuid
        return ([primary] if primary else []) + [
            uuid for uuid in self.s.campaign_fallback_uuids if uuid != primary
        ]

    def _transmitter_choice(self) -> tuple[str | None, list[str]]:
        """(transmitter_uuid, fallback_transmitter_uuids) for build_campaign,
        from the operator's policy. See config.py's campaign_transmitter_uuid
        note for what each policy means and why "any" is not "preferred"."""
        policy = self.schedule_service.campaign_transmitter_policy()
        primary = self.s.campaign_transmitter_uuid or None
        if policy == "any" or primary is None:
            # A primary deliberately unset (GS_CAMPAIGN_TRANSMITTER_UUID=None)
            # means the automatic per-station pick; fallbacks of nothing mean
            # nothing, so none are passed.
            return None, []
        if policy == "preferred":
            return primary, [uuid for uuid in self.s.campaign_fallback_uuids if uuid != primary]
        return primary, []  # "pinned"

    def _build_kwargs(self, now: datetime, auto_settings: AutoSettings,
                      calendar_cache: dict[int, list] | None) -> dict:
        """build_campaign's inputs, shared by the preview and every commit
        round so the two can never plan from different settings."""
        transmitter_uuid, fallbacks = self._transmitter_choice()
        return {
            "mission_norad": self.s.default_norad,
            "transmitter_uuid": transmitter_uuid,
            "fallback_transmitter_uuids": fallbacks,
            "now": now,
            "exclude_station_id": self.schedule_service._effective_station_id(),
            "max_per_station": self.schedule_service.campaign_max_per_station(),
            "max_total": self.schedule_service.campaign_max_total(),
            "recent_attempts": self._recent_bookings_by_station(now),
            "buffer_s": auto_settings.buffer_s,
            "calendar_cache": calendar_cache,
            # The per-station cap means "KNACKSAT-2 observations per station
            # in the 48h window", not "per click": KNACKSAT-2 passes already
            # on a calendar use up allowance, so a second click or the daily
            # timer tops stations up instead of stacking another
            # max_per_station onto each one.
            "cap_counts_existing": True,
        }

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
            network, db, **self._build_kwargs(now, auto_settings, self._calendar_cache_for(now)),
        )
        payload = _campaign_preview_payload(preview)
        payload["calendar_sources"] = _calendar_sources(network)
        return payload

    def _mock_preview(self) -> dict:
        now = datetime.now(timezone.utc)
        transmitter_uuid, fallbacks = self._transmitter_choice()
        # The configured uuids and the real SatNOGS DB descriptions, so the
        # panel's transmitter labels and fallback marking run exactly the code
        # path real data does. One item is the fallback for that reason.
        primary = self.s.campaign_transmitter_uuid or "mock-telemetry"
        fallback = (self.s.campaign_fallback_uuids or ["mock-digipeater"])[0]
        return {
            **_campaign_preview_payload(CampaignPreview(
                generated_utc=now,
                window_start=now + timedelta(minutes=11),
                window_end=now + timedelta(minutes=2880),
                considered_stations=3,
                items=[
                    CampaignItem(
                        station_id=999001, station_name="Mock Station A",
                        transmitter_uuid=primary,
                        start=now + timedelta(hours=2), end=now + timedelta(hours=2, minutes=8),
                        max_elevation_deg=42.0,
                        transmitter_description="Mode U - FSK9k6 -TLM",
                    ),
                    CampaignItem(
                        station_id=999003, station_name="Mock Station C",
                        transmitter_uuid=fallback,
                        start=now + timedelta(hours=5), end=now + timedelta(hours=5, minutes=7),
                        max_elevation_deg=18.0,
                        transmitter_description="Mode V/V - FSK9k6 - Digipeater",
                        is_fallback=True,
                    ),
                ],
                skipped=[{
                    "station_id": 999002, "station_name": "Mock Station B",
                    "reason": "no qualifying pass in the campaign window",
                }],
                calendars_read=2,
                stations_reachable=2,
                params={
                    "max_per_station": self.schedule_service.campaign_max_per_station(),
                    "max_total": self.schedule_service.campaign_max_total(),
                    "transmitter_uuid": transmitter_uuid,
                    "fallback_transmitter_uuids": fallbacks,
                    "cap_counts_existing": True,
                },
            )),
            "calendar_sources": {"jobs": 2},
        }

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

    def _build_items(self, network: NetworkClient, db: DbClient, auto_settings: AutoSettings,
                     booked_counts: dict[int, int] | None = None,
                     calendar_cache: dict[int, list] | None = None) -> dict:
        """One build for a commit round. Returns the whole preview payload,
        not just its items, so the caller can see that a build was cut short
        (stopped_early) instead of mistaking a truncated plan for "nothing
        left to book"."""
        now = datetime.now(timezone.utc)
        preview = build_campaign(
            network, db, booked_counts=booked_counts,
            **self._build_kwargs(now, auto_settings, calendar_cache),
        )
        return _campaign_preview_payload(preview)

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
        loop = self.schedule_service.campaign_loop_until_exhausted()
        db: DbClient | None = None
        # Taken once for the whole commit - see _calendar_cache_for(). When
        # the preview that produced `items` is still fresh this is the very
        # calendars it read, so round 2 onwards costs no reads at all; the
        # calendars predate round 1's bookings, which is why booked_counts and
        # cap_counts_existing never count the same booking twice.
        calendar_cache = self._calendar_cache_for(datetime.now(timezone.utc))
        # One note per build that was cut short, surfaced in stopped_reason.
        cut_short: list[str] = []

        def build(round_no: int, counts: dict[int, int] | None) -> tuple[list[dict], bool]:
            nonlocal db
            db = db or DbClient(auto_settings, cache)
            built = self._build_items(network, db, auto_settings, counts,
                                      calendar_cache=calendar_cache)
            early = built.get("stopped_early")
            if early:
                cut_short.append(
                    f"round {round_no}'s plan was cut short ({early.get('reason', 'unknown reason')}"
                    f"; {early.get('unread_stations', '?')} station(s) not read)"
                )
            return list(built.get("items") or []), bool(early)

        first_build_early = False
        if items is None:
            items, first_build_early = build(1, None)

        submitted = 0
        errors: list[str] = []
        accepted_detail: list[dict] = []
        uncertain: list[dict] = []
        not_sent = 0
        booked_counts: dict[int, int] = {}
        rounds = 0
        stopped = "one batch per commit (looping is off)"

        while True:
            rounds += 1
            if rounds == 1 and not items:
                # Nothing to send is not "every booking rejected", which is
                # what the checks below would otherwise call it.
                stopped = ("nothing could be planned" if first_build_early
                           else "no bookings to submit")
                break
            batch = self._submit_batch(network, items)
            submitted += batch.submitted
            errors += batch.errors
            accepted_detail += batch.accepted
            uncertain += batch.uncertain
            not_sent += batch.not_sent
            # Uncertain items count against the per-station cap as well: they
            # may well be on the calendar, and the cap is the courtesy limit
            # for what we ask of one station. (Their windows are already
            # excluded from later rounds via _recent_attempts.)
            for row in batch.accepted + batch.uncertain:
                booked_counts[row["station_id"]] = booked_counts.get(row["station_id"], 0) + 1
            self.on_state("campaign", "ok",
                          f"round {rounds}: {len(accepted_detail)} booked so far")

            if batch.unreachable:
                # Not a verdict on any booking, and rebuilding would only send
                # the next round into the same dead connection.
                stopped = f"round {rounds} lost its connection to SatNOGS"
                break
            if not loop:
                break
            if not batch.accepted:
                # A round where SatNOGS took nothing is a systemic answer (rate
                # limit, bad token, their outage), not a few stale slots -
                # rebuilding and resubmitting would just hammer it again.
                stopped = f"round {rounds} had every booking rejected"
                break
            if rounds >= MAX_LOOP_ROUNDS:
                stopped = f"hit the {MAX_LOOP_ROUNDS}-round safety limit"
                break
            # The next round sees everything submitted so far as occupied via
            # _recent_attempts, and every station's bookings so far via
            # booked_counts, so it can only pick up what is still left.
            items, early = build(rounds + 1, booked_counts)
            if not items:
                # A build that was cut short and found nothing has not shown
                # the network is exhausted - only that it could not look. The
                # cut_short note appended below says why.
                stopped = "nothing more could be planned" if early else "no bookings left"
                break

        if cut_short:
            stopped = f"{stopped}; " + "; ".join(cut_short)

        return {
            "status": "ok" if not errors else "ok_with_warnings",
            "trigger": trigger,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "submitted": submitted,
            "accepted": len(accepted_detail),
            "errors": errors,
            "accepted_items": accepted_detail,
            "stations_booked": len({row["station_id"] for row in accepted_detail}),
            "accepted_by_transmitter": _transmitter_summary(
                accepted_detail, self._transmitter_preference()),
            "accepted_band_counts": _band_counts(accepted_detail),
            "uncertain_items": uncertain,
            "not_sent": not_sent,
            "calendar_sources": _calendar_sources(network),
            "rounds": rounds,
            "stopped_reason": stopped,
        }

    def _submit_batch(self, network: NetworkClient, items: list[dict]) -> _BatchOutcome:
        """Submit one round: one POST per transmitter per CAMPAIGN_POST_CHUNK
        items (see _chunks_by_transmitter), stopping if SatNOGS goes away."""
        outcome = _BatchOutcome()
        if not items:
            return outcome
        chunks = _chunks_by_transmitter(items, self._transmitter_preference())
        for position, chunk in enumerate(chunks):
            # Recorded before submitting, not just on acceptance - a rejected
            # item is still "just tried", and retrying it immediately (before
            # SatNOGS's own read view has any chance of catching up) would only
            # reproduce the same rejection. See RECENT_ATTEMPT_TTL. Per chunk,
            # so items in chunks never sent (below) stay bookable next time.
            self._record_attempts(chunk, datetime.now(timezone.utc))

            schedule_items = [
                to_schedule_item(
                    item["station_id"], item["transmitter_uuid"],
                    datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"]),
                )
                for item in chunk
            ]
            # The one and only execute=True call in this codebase.
            result = network.schedule(schedule_items, execute=True)
            self._drop_calendar_cache()

            # `schedule_items` is built 1:1 from `chunk` and the result's item
            # lists hold those same dict objects back, so identity maps the
            # API's answer onto the richer preview rows without re-parsing
            # anything. Keeping the per-item detail is what later lets a run be
            # cross-checked against the real calendar - the aggregate counts
            # alone cannot say which booking it was that landed.
            accepted_ids = {id(sent) for sent in result.accepted_items}
            uncertain_ids = {id(sent) for sent in getattr(result, "uncertain_items", None) or []}
            for original, sent in zip(chunk, schedule_items):
                if id(sent) in accepted_ids:
                    outcome.accepted.append(_result_row(original))
                elif id(sent) in uncertain_ids:
                    outcome.uncertain.append(_result_row(original))
            outcome.submitted += result.submitted
            outcome.errors += list(result.errors)

            if any(_UNREACHABLE in error for error in result.errors):
                outcome.unreachable = True
                rest = chunks[position + 1:]
                if rest:
                    outcome.not_sent = sum(len(c) for c in rest)
                    outcome.errors.append(
                        f"SatNOGS became unreachable, so {outcome.not_sent} more "
                        f"item(s) in {len(rest)} later POST(s) were not sent; "
                        "nothing was booked for them"
                    )
                break
        return outcome

    def _mock_commit(self, items: list[dict] | None, trigger: str) -> dict:
        rows = [_result_row(item) for item in
                (items if items is not None else self._mock_preview()["items"])]
        return {
            "status": "ok", "trigger": trigger,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "submitted": len(rows), "accepted": len(rows), "errors": [],
            "accepted_items": rows,
            "stations_booked": len({row["station_id"] for row in rows}),
            "accepted_by_transmitter": _transmitter_summary(rows, self._transmitter_preference()),
            "accepted_band_counts": _band_counts(rows),
            "uncertain_items": [],
            "not_sent": 0,
            "calendar_sources": {},
            "rounds": 1,
            "stopped_reason": "mock run",
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

        # future_bookings only returns observations that have not started
        # yet, so anything already underway or finished is simply not in that
        # feed - reporting it "missing" would be wrong, and this is reachable
        # whenever a booking is verified more than ~11 minutes after it was
        # made.
        def started(item: dict) -> dict:
            return {**item, "state": "started",
                    "detail": "already under way or past; "
                              "the scheduling feed only lists future observations"}

        stations_read = 0
        for station_id, station_items in by_station.items():
            if all(datetime.fromisoformat(i["start"]) <= now for i in station_items):
                # Nothing a read could confirm: every booking here has already
                # dropped out of the feed. Skipping it is free accuracy - a
                # 600-booking run verified the next day would otherwise spend
                # a read per station to learn nothing.
                checked.extend(started(item) for item in station_items)
                continue
            stations_read += 1
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
                if start <= now:
                    checked.append(started(item))
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
            "stations_read": stations_read,
            "calendar_sources": _calendar_sources(network),
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
        uncertain = len(result.get("uncertain_items") or [])
        history.append({
            "generated_utc": result.get("generated_utc"),
            "trigger": result.get("trigger", "manual"),
            "status": result.get("status"),
            "submitted": submitted,
            "accepted": accepted,
            # Outcome-unknown items are not rejections - some may be booked -
            # so they are counted apart rather than hidden in "rejected".
            "rejected": max(0, submitted - accepted - uncertain),
            "uncertain": uncertain,
            "stations_booked": result.get("stations_booked", 0),
            "rounds": result.get("rounds", 1),
        })
        # A daily cadence means even a year of history is small, but there's
        # no reason to let this grow forever.
        self._write_json(self.history_path, history[-200:])

    # --- the timer -----------------------------------------------------------
    def auto_cycle_delay_s(self, now: datetime | None = None) -> float:
        """How long scheduler.py's timer should wait before its FIRST cycle
        after a (re)start: whatever is left of campaign_poll_s since the last
        preview (manual or timed), or 0 if there is none.

        The loop used to run a cycle the moment it started, and uvicorn
        --reload restarts it on every edit under backend/app - so each edit
        fired a full real preview (~145-222 calendar reads) and, with
        auto-commit on, real bookings on community stations. A daily timer
        has no reason to fire twice in a day because the process restarted.
        """
        poll_s = float(self.s.campaign_poll_s)
        last = self.get_last_preview()
        raw = last.get("generated_utc") if isinstance(last, dict) else None
        if not raw:
            return 0.0
        try:
            generated = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return 0.0
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        age_s = ((now or datetime.now(timezone.utc)) - generated).total_seconds()
        # Clamped to one poll period so a preview stamped in the future (a
        # clock step) delays the timer by at most one cycle, never longer.
        return max(0.0, min(poll_s, poll_s - age_s))

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
