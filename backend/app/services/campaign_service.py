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
from typing import Callable, Iterable

import requests

from ..config import Settings
from ..vendor.autoscheduler.cache import Cache
from ..vendor.autoscheduler.campaign import (
    CampaignItem, CampaignPreview, _campaign_preview_payload, band_counts_payload,
    build_campaign,
)
from ..vendor.autoscheduler.config import Settings as AutoSettings
from ..vendor.autoscheduler.db_client import DbClient
from ..vendor.autoscheduler.http import SatnogsHTTPError, SatnogsOutcomeUnknown
from ..vendor.autoscheduler.network_client import (
    NetworkClient, RateLimitedError, to_schedule_item,
)
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

# What SatNOGS answers (HTTP 400) for a booking on a station the requesting
# account may not schedule on - "No permission to schedule observations on
# station: 40", or "...on stations: [...]" for several (satnogs-network
# base/perms.py has_perm_to_schedule_on_station). The same words cover two
# different refusals. While the account owns no useable station (ours not
# Online - see own_station_state) it is refused on EVERY station, and every
# later POST would get the same answer. While it does, the verdict is per
# TARGET station: `return target_station.is_available`, so one station that
# went unavailable after our catalogue read (cached for an hour) is refused
# alone while the rest book fine - records show this on stations 12, 16 and
# 36. _submit_batch tells the two apart (_refusal_is_account_wide). Matched as
# text for the same reason as _UNREACHABLE: schedule() reports refusals only
# as error strings.
_NO_PERMISSION = "No permission to schedule observations"

# What run_auto_cycle() returns when it could not even preview because
# another campaign operation (a manual preview or commit, the auto-run chain,
# or a cross-check) held the single-flight guard. The timer retries soon on it
# instead of sleeping a whole campaign_poll_s - see Scheduler._campaign_loop.
AUTO_CYCLE_BUSY = "busy"

# What run_auto_cycle() returns when its preview failed on a transient SatNOGS
# error (is_transient_error): nothing was sent, so the timer runs the cycle
# again in minutes rather than a day - see Scheduler._campaign_loop.
AUTO_CYCLE_RETRY = "retry"

# How a stale-commit refusal names the commit that booked in the meantime.
_TRIGGER_LABELS = {
    "manual": "a manual submit",
    "auto": "the campaign timer",
    "chained": "the Station Schedule auto-run chain",
}

# How old SatnogsService's last poll of our own station may be before its
# status stops counting as evidence. It polls every satnogs_station_poll_s
# (60 s), so ten missed polls means the poller itself is failing - and a
# status we no longer refresh must not keep the campaign switched off
# indefinitely; see own_station_state() for why staleness fails OPEN here.
OWN_STATION_FRESH_S = 600.0

# The cross-check's per-item detail for a calendar it did not get to read
# because the read budget ran out part-way (see _verify_sync).
_VERIFY_BUDGET_DETAIL = ("not read: the SatNOGS read budget ran out (rate limited) - "
                         "run the cross-check again later")


@dataclass
class _BatchOutcome:
    """What one commit round's POSTs came back with, across every chunk."""
    submitted: int = 0
    errors: list[str] = field(default_factory=list)
    accepted: list[dict] = field(default_factory=list)
    # Sent, but no reliable answer: possibly booked. Never resubmitted.
    uncertain: list[dict] = field(default_factory=list)
    # Items in later POSTs that were never attempted because SatNOGS had
    # become unreachable or had refused us permission. (A POST that failed
    # part-way says in its own error how many of its items went unsent.)
    not_sent: int = 0
    unreachable: bool = False
    # A whole POST came back "No permission to schedule observations" AND
    # that reads as the account, not the stations in it (see
    # _refusal_is_account_wide): the account cannot book other people's
    # stations right now - in practice our own station is not Online - so
    # nothing after it was sent.
    no_permission: bool = False
    # Stations a whole POST was refused on for permission while the account
    # itself was fine - unavailable targets. The commit's later rounds do not
    # plan them again.
    refused_stations: set[int] = field(default_factory=set)


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


def _band_counts(rows: Iterable[dict]) -> list[dict]:
    # campaign.py's own helper, so accepted bookings are banded with exactly
    # the labels and floors the preview's band_counts used.
    return band_counts_payload(
        float(row["max_elevation_deg"]) for row in rows
        # A row from an older client has no elevation: its band is unknown,
        # not 15-0.
        if row.get("max_elevation_deg") is not None
    )


def _parse_utc(raw) -> datetime | None:
    """An ISO timestamp this service wrote, as an aware UTC datetime; None for
    anything else. A naive one is taken as UTC, which is all we ever write."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


def is_transient_error(exc: BaseException) -> bool:
    """Whether a failed campaign preview is worth running again in a few
    minutes: SatNOGS was unreachable or answered 5xx.

    Decided on the exception, never its text. SatNOGS is flaky rather than
    down: in the 90 minutes after the 2026-10-04 11:00Z slot lost its chain
    to "GET .../stations/ failed after 3 attempts: ... -> HTTP 500", 12 of our
    126 requests got a 500 and the rest a 200. That error reaches
    preview_campaign as http.request's own SatnogsHTTPError, now carrying
    the last attempt's status and chained from the last attempt's error
    (VENDORED.md patch 19). Status None counts only when that cause is a
    requests transport error - the last attempt got no answer at all. A
    status-less SatnogsHTTPError without one is paginate's "expected a list
    from ..., got dict": SatNOGS did answer 200, just not with a list, and
    the same read gets the same answer in ten minutes.

    Not transient: RateLimitedError (the budget, or a Retry-After, applies to
    every request still to come - asking again in ten minutes is exactly what
    it says not to do), any other 4xx (the request itself is wrong), and
    anything that is not a transport failure at all (ValueError, KeyError, a
    RuntimeError from the cache or catalogue). SatnogsOutcomeUnknown is a
    write's and cannot come out of a preview; it is excluded anyway, since
    "may have booked" must never lead to anything being repeated.
    """
    if isinstance(exc, (RateLimitedError, SatnogsOutcomeUnknown)):
        return False
    if isinstance(exc, SatnogsHTTPError):
        if exc.status is not None:
            return exc.status >= 500
        return isinstance(exc.__cause__, requests.exceptions.RequestException)
    return isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError))


def _calendar_sources(network) -> dict[str, int]:
    # Which feed served each calendar read on this client - "jobs" is free,
    # "observations" spends the token's 240/hour. getattr because a client
    # that predates the counter (or a test stub) simply reports nothing.
    return {str(k): int(v) for k, v in (getattr(network, "calendar_sources", None) or {}).items()}


class CampaignService:
    def __init__(self, settings: Settings, schedule_service: ScheduleService, on_state=None,
                 *, own_station: Callable[[], dict | None] | None = None) -> None:
        self.s = settings
        self.schedule_service = schedule_service
        self.on_state = on_state or (lambda component, state, detail="": None)
        # What SatnogsService last polled for our own station: {"id", "status",
        # "last_seen", "age_s"}, or None before its first poll. Wired by
        # scheduler.py; absent (tests, scripts) means "unknown", which never
        # blocks a booking - see own_station_state().
        self._own_station = own_station

        self.preview_path = settings.data_dir / "campaign_last_preview.json"
        self.result_path = settings.data_dir / "campaign_last_run.json"
        self.history_path = settings.data_dir / "campaign_history.json"
        self.recent_attempts_path = settings.data_dir / "campaign_recent_attempts.json"
        # When this process last POSTed a booking, and for which trigger -
        # what stale_commit() holds a previewed plan against. Persisted so a
        # reload between the auto-run chain's commit and an operator's CONFIRM
        # cannot make the older plan look fresh again.
        self.last_submit_path = settings.data_dir / "campaign_last_submit.json"
        # When the campaign's OWN timer last ran a cycle - what
        # auto_cycle_delay_s() anchors on, so that previews from other
        # triggers (the auto-run chain, the operator) do not move the timer.
        self.auto_cycle_path = settings.data_dir / "campaign_last_auto_cycle.json"

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

    def own_station_state(self) -> dict:
        """Whether our own station's SatNOGS status rules out booking anyone
        else's right now.

        SatNOGS lets an account book a station it does not own only while the
        account owns at least one "useable, non-testing" station: connected
        (seen within the heartbeat window), is_available, located, and
        testing=False (satnogs-network base/perms.py
        has_perm_to_schedule_on_station, users/models.py useable_stations).
        The station API's "status" is that same test: "Online" is connected +
        available + not testing, and "Testing" and "Offline" both fail it.
        Our account's only other station, 5022, is not available for
        scheduling (is_available=false, testing=true when this was written),
        so the configured station being Online is the permission. On
        2026-10-02 station 5024 lost power: the automatic campaign sent 100
        bookings and all 100 came back HTTP 400 "No permission to schedule
        observations on station: N".

        Unknown or stale status NEVER blocks. The server's refusal is the
        backstop (_submit_batch stops once a whole POST is refused for
        permission across two or more stations - see
        _refusal_is_account_wide), whereas a poller that has stopped
        updating - or was never wired - would otherwise switch the campaign
        off silently and indefinitely.
        """
        snapshot = None
        if self._own_station is not None:
            try:
                snapshot = self._own_station()
            except Exception:  # noqa: BLE001 - the gate must fail open, never crash a commit
                log.warning("could not read our own station's status", exc_info=True)
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        status = snapshot.get("status")
        # Anything but a non-empty string is "we don't know", not "not Online".
        status = status if isinstance(status, str) and status else None
        age_s = snapshot.get("age_s")
        fresh = isinstance(age_s, (int, float)) and age_s <= OWN_STATION_FRESH_S
        return {
            "station_id": snapshot.get("id"),
            "status": status,
            "last_seen": snapshot.get("last_seen"),
            "age_s": age_s,
            "fresh": fresh,
            "blocks_booking": fresh and status is not None and status != "Online",
        }

    def _record_attempts(self, items: list[dict], now: datetime) -> list[tuple]:
        """Remember `items` as just tried; returns the entries added, so a
        caller that learns they were never booked can take exactly those back
        (_forget_attempts)."""
        cutoff = now - RECENT_ATTEMPT_TTL
        self._recent_attempts = [a for a in self._recent_attempts if a[3] >= cutoff]
        added = [
            (item["station_id"], datetime.fromisoformat(item["start"]),
             datetime.fromisoformat(item["end"]), now)
            for item in items
        ]
        self._recent_attempts.extend(added)
        self._persist_recent_attempts()
        return added

    def _forget_attempts(self, entries: list[tuple]) -> None:
        """Drop exactly these _record_attempts() entries - by identity, so an
        identical window recorded by an earlier POST is kept."""
        drop = {id(entry) for entry in entries}
        self._recent_attempts = [a for a in self._recent_attempts if id(a) not in drop]
        self._persist_recent_attempts()

    def _persist_recent_attempts(self) -> None:
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
            # Taken after the build, so it is as fresh as the plan it sits
            # next to: the panel stops its one-click flows on it before they
            # reach a commit that would be refused anyway.
            result["own_station"] = gate = self.own_station_state()
            self._write_json(self.preview_path, result)
            if gate["blocks_booking"]:
                self.on_state("campaign", "degraded", self._blocked_reason(gate))
            else:
                self.on_state("campaign", "ok", f"{len(result.get('items', []))} candidate(s)")
            return result
        except Exception as exc:
            log.exception("campaign preview failed")
            result = {
                "status": "error", "error": str(exc),
                "generated_utc": datetime.now(timezone.utc).isoformat(),
                "own_station": self.own_station_state(),
                # A preview sends nothing, so any failure here is safe to
                # repeat; this says whether repeating it soon can help. The
                # auto-run chain and the timer retry on it.
                "retryable": is_transient_error(exc),
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
    async def commit_campaign(self, items: list[dict] | None = None, trigger: str = "manual",
                              preview_generated_utc: str | None = None) -> dict:
        """Submit `items` (a plan previewed earlier), or build and submit a
        fresh plan when `items` is None.

        `preview_generated_utc` is the generated_utc of the preview `items`
        came from. Given, the commit is refused as "stale" - nothing sent, no
        run record, no history row - if this process has POSTed bookings since
        that preview was built; see stale_commit()."""
        if self._running:
            return {"status": "running"}
        async with self._run_lock:
            if self._running:
                return {"status": "running"}
            self._running = True
        try:
            # Ahead of the own-station gate, and never written as the last
            # run: the last run on disk is the commit that made this plan
            # stale, and the cross-check reads its accepted items back.
            if items is not None:
                stale = self.stale_commit(preview_generated_utc, trigger)
                if stale is not None:
                    log.warning("campaign commit refused: %s", stale["stopped_reason"])
                    return stale
            # Checked before anything is built or sent, for every trigger and
            # in mock mode too: with our own station not Online every item
            # would come back "No permission" (2026-10-02: 100 sent, 100
            # refused), so the honest run is the one that sends nothing and
            # says why.
            gate = self.own_station_state()
            if gate["blocks_booking"]:
                result = self._blocked_result(gate, trigger)
                self._write_json(self.result_path, result)
                self._append_history(result)
                self.on_state("campaign", "degraded", self._blocked_reason(gate))
                return result
            if self._effective_mock():
                result = self._mock_commit(items, trigger)
            else:
                result = await asyncio.to_thread(self._commit_sync, items, trigger)
            self._write_json(self.result_path, result)
            self._append_history(result)
            if result.get("status") == "error":
                self.on_state("campaign", "degraded", f"{result.get('accepted', 0)} booked")
            elif result.get("no_permission"):
                # The gate let it through (status unknown or stale) and the
                # server said no - the same outage, found the expensive way.
                self.on_state("campaign", "degraded",
                              "SatNOGS refused permission - is our own station Online?")
            else:
                self.on_state("campaign", "ok", f"{result.get('accepted', 0)} booked")
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

    @staticmethod
    def _blocked_reason(gate: dict) -> str:
        return f"blocked: station {gate['station_id']} is {gate['status']}"

    @staticmethod
    def _blocked_result(gate: dict, trigger: str) -> dict:
        """The run record of a commit the own-station gate stopped. Shaped like
        any other result (and kept in history as "blocked") so the panel and
        the history table need no special case to show it - and with no
        attempts recorded, since nothing was tried: every slot stays bookable
        the moment the station is back Online."""
        return {
            "status": "blocked", "trigger": trigger,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "submitted": 0, "accepted": 0, "errors": [], "accepted_items": [],
            "stations_booked": 0, "uncertain_items": [], "rounds": 0,
            "own_station": gate,
            "stopped_reason": (
                f"station {gate['station_id']} is {gate['status']} "
                f"(last seen {gate['last_seen'] or 'unknown'}) - SatNOGS refuses bookings "
                "on other people's stations until one of ours is Online, so nothing was sent"
            ),
        }

    def _note_submit(self, trigger: str) -> None:
        """Remember that a booking POST is about to go out - the evidence
        stale_commit() checks a previewed plan against. Written BEFORE the
        POST, like _record_attempts: a POST whose answer never arrives may
        still have booked, so it makes older plans stale all the same."""
        self._write_json(self.last_submit_path, {
            "at": datetime.now(timezone.utc).isoformat(), "trigger": trigger,
        })

    def last_submit_at(self) -> datetime | None:
        """When this process last started a booking POST (see _note_submit),
        or None if it never has - or the record is unreadable."""
        last = self._read_json(self.last_submit_path, None)
        return _parse_utc(last.get("at")) if isinstance(last, dict) else None

    def stale_commit(self, preview_generated_utc: str | None,
                     trigger: str = "manual") -> dict | None:
        """The "stale" answer for a commit of a plan previewed at
        `preview_generated_utc`, or None if that plan may still be sent.

        A commit of previewed items sends them as they are: round 1 is not
        re-planned, so nothing re-checks them against the per-station cap.
        That is right while the plan is the newest word on those calendars,
        and wrong once anything has booked since. The auto-run chain makes
        this routine: an operator reads a preview at 10:58Z, the 11:00Z slot
        previews afresh and books - a different shuffle, so other passes on
        the same stations - and CONFIRM at 11:03Z then lands the older plan
        too: twice max_per_station on those stations, with no SatNOGS overlap
        to stop it (measured in review: cap 1, two per station). So a plan is
        refused once this process has POSTed after it was built. A preview
        and a commit never overlap (one guard), so "POSTed after the build
        began" is exactly "booked by a commit that ran after this preview".

        No stamp (an older client, or a commit that builds its own plan) is
        not checked. A stamp that does not parse is refused: it cannot show
        the plan is fresh."""
        if preview_generated_utc is None:
            return None
        last = self._read_json(self.last_submit_path, None)
        last_at = _parse_utc(last.get("at")) if isinstance(last, dict) else None
        if last_at is None:
            return None  # nothing POSTed since this was first recorded
        planned = _parse_utc(preview_generated_utc)
        if planned is not None and last_at < planned:
            return None
        by = _TRIGGER_LABELS.get(last.get("trigger"), "another campaign submit")
        return {
            "status": "stale", "trigger": trigger,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "preview_generated_utc": preview_generated_utc,
            "last_submit_utc": last_at.isoformat(),
            "submitted": 0, "accepted": 0, "errors": [], "accepted_items": [], "rounds": 0,
            "stopped_reason": (
                f"this plan was computed at {planned:%Y-%m-%d %H:%M:%S} UTC, and {by} has "
                f"booked since (at {last_at:%H:%M:%S} UTC) - the plan does not count "
                "those bookings, so it could put more passes on a station than the "
                "per-station cap allows. Nothing was sent; run PREVIEW again"
                if planned is not None else
                f"this plan's timestamp ({preview_generated_utc!r}) could not be read, "
                "so it cannot be shown to be newer than the last submit. Nothing was "
                "sent; run PREVIEW again"
            ),
        }

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
        no_permission = False
        booked_counts: dict[int, int] = {}
        # Stations SatNOGS refused a whole POST on for permission while the
        # account was fine (_BatchOutcome.refused_stations).
        refused_stations: set[int] = set()
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
            batch = self._submit_batch(network, items, trigger=trigger)
            submitted += batch.submitted
            errors += batch.errors
            accepted_detail += batch.accepted
            uncertain += batch.uncertain
            not_sent += batch.not_sent
            refused_stations |= batch.refused_stations
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
            if batch.no_permission:
                # Checked before `loop`: even a single-batch commit should say
                # why it stopped short. Rebuilding cannot help - the refusal
                # is about the account, not about any slot.
                no_permission = True
                stopped = (
                    f"round {rounds}: SatNOGS refused permission to schedule on other "
                    "people's stations (\"No permission to schedule observations\") - "
                    "most likely our own station is not Online; nothing more was sent"
                )
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
            # booked_counts, so it can only pick up what is still left. A
            # station refused for permission is handed over as already at its
            # cap: it is unavailable, not short of free passes, and planning
            # its other passes again is exactly how such a station ends up
            # alone in a POST of its own (see _refusal_is_account_wide).
            counts = dict(booked_counts)
            if refused_stations:
                cap = self.schedule_service.campaign_max_per_station()
                counts.update({sid: max(counts.get(sid, 0), cap) for sid in refused_stations})
            items, early = build(rounds + 1, counts)
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
            "no_permission": no_permission,
            "calendar_sources": _calendar_sources(network),
            "rounds": rounds,
            "stopped_reason": stopped,
        }

    def _submit_batch(self, network: NetworkClient, items: list[dict],
                      trigger: str = "manual") -> _BatchOutcome:
        """Submit one round: one POST per transmitter per CAMPAIGN_POST_CHUNK
        items (see _chunks_by_transmitter), stopping if SatNOGS goes away or
        refuses us permission outright."""
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
            attempts = self._record_attempts(chunk, datetime.now(timezone.utc))
            self._note_submit(trigger)

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
            took = maybe = 0
            for original, sent in zip(chunk, schedule_items):
                if id(sent) in accepted_ids:
                    outcome.accepted.append(_result_row(original))
                    took += 1
                elif id(sent) in uncertain_ids:
                    outcome.uncertain.append(_result_row(original))
                    maybe += 1
            outcome.submitted += result.submitted
            outcome.errors += list(result.errors)
            rest = chunks[position + 1:]

            if any(_UNREACHABLE in error for error in result.errors):
                outcome.unreachable = True
                if rest:
                    outcome.not_sent = sum(len(c) for c in rest)
                    outcome.errors.append(
                        f"SatNOGS became unreachable, so {outcome.not_sent} more "
                        f"item(s) in {len(rest)} later POST(s) were not sent; "
                        "nothing was booked for them"
                    )
                break

            if (not took and not maybe and not result.accepted and result.errors
                    and all(_NO_PERMISSION in error for error in result.errors)):
                # The whole POST was refused for permission. schedule() already
                # took the refusal as the verdict for every station it listed,
                # so the chunk cost one POST, not one per item. Only a clean
                # sweep counts; a permission error mixed in with other outcomes
                # is left to today's per-item handling.
                refused = {item["station_id"] for item in chunk}
                if not self._refusal_is_account_wide(refused):
                    # About these stations, not us: they went unavailable
                    # (see _NO_PERMISSION). Unlike the account-wide case their
                    # windows STAY in _recent_attempts - they were really
                    # refused, and forgetting them would have the next commit
                    # inside the catalogue hour plan them again - and every
                    # later POST still goes out, since it is for other stations.
                    outcome.refused_stations |= refused
                    log.warning(
                        "SatNOGS refused a POST of %d item(s) for permission on "
                        "station(s) %s; that reads as those stations being "
                        "unavailable, not as our account, so the run goes on",
                        len(chunk), sorted(refused),
                    )
                    continue
                # Account-wide (see own_station_state): every later POST would
                # get the same answer. This is the backstop for when the
                # own-station gate could not see the outage (status stale or
                # unknown; SatNOGS itself only marks a station Offline about an
                # hour after it goes quiet), or the station dropped mid-commit.
                outcome.no_permission = True
                # A definitive refusal booked nothing and says nothing about
                # the slots, so unlike any other rejection they must not sit
                # in _recent_attempts for two hours: they are bookable again
                # the moment the station is back Online. Exactly this chunk's
                # entries - earlier chunks' bookings are real and stay.
                self._forget_attempts(attempts)
                if rest:
                    outcome.not_sent = sum(len(c) for c in rest)
                    outcome.errors.append(
                        f"SatNOGS refused permission to schedule on other people's "
                        f"stations, so {outcome.not_sent} more item(s) in {len(rest)} "
                        "later POST(s) were not sent - most likely our own station is "
                        "not Online; nothing was booked for them"
                    )
                break
        return outcome

    def _refusal_is_account_wide(self, stations: set[int]) -> bool:
        """Whether a POST refused for permission on every one of `stations`
        means the ACCOUNT may not book anyone else's station right now, as
        opposed to those stations being unavailable.

        SatNOGS answers both with the same words (see _NO_PERMISSION), and it
        grants the account-level permission on exactly what our own station's
        status reports - connected, available, not testing - so that status
        decides when we have it:

        * fresh and "Online": SatNOGS sees a useable station of ours, so the
          refusal can only be about the targets. Treating it as an outage
          used to stop a looped commit at the first chunk that held one
          unavailable station alone, forget its attempts and tell the
          operator our station was down while the gate showed it Online;
        * fresh and anything else: our station dropped during the commit
          (the gate let it start) - account-wide;
        * stale or unknown: no evidence either way, so the refusal's breadth
          decides. An outage refuses every station of every POST; one
          unavailable station is refused alone. Two or more distinct stations
          is the outage - a single one costs at most one more POST to tell.

        Read live, not snapshotted at commit start: a looped commit can run
        for minutes, and the poller (every ~60 s) may have seen the station
        drop since.
        """
        gate = self.own_station_state()
        if gate["fresh"] and gate["status"] is not None:
            return gate["status"] != "Online"
        return len(stations) >= 2

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
            "no_permission": False,
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

        Behind the same single-flight guard as preview and commit. It used to
        run alongside them, but a verify during a commit reads the very
        calendars that commit is booking onto, checks the PREVIOUS run's
        record while the next one is being written, and draws on the same read
        budget the commit's own rebuilds need. A verify in flight likewise
        makes preview/commit answer "running".
        """
        if self._running:
            return {"status": "running"}
        async with self._run_lock:
            if self._running:
                return {"status": "running"}
            self._running = True
        try:
            if self._effective_mock():
                return self._mock_verify()
            return await asyncio.to_thread(self._verify_sync)
        finally:
            self._running = False

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
                "stations_unread": 0,
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

        def has_started(item: dict) -> bool:
            return datetime.fromisoformat(item["start"]) <= now

        # Calendars attempted: answered, or failed for that station alone. A
        # station the read budget never reached is counted in stations_unread.
        stations_read = 0
        stations_unread = 0
        stopped_reason: str | None = None
        stations = list(by_station.items())
        for position, (station_id, station_items) in enumerate(stations):
            if all(has_started(i) for i in station_items):
                # Nothing a read could confirm: every booking here has already
                # dropped out of the feed. Skipping it is free accuracy - a
                # 600-booking run verified the next day would otherwise spend
                # a read per station to learn nothing.
                checked.extend(started(item) for item in station_items)
                continue
            try:
                bookings = network.future_bookings(station_id, now=now)
            except RateLimitedError as exc:
                # Caught before the catch-all below, which is for ONE flaky
                # station and carries on to the next. A throttle is about the
                # budget, so it applies to every read still to come: carrying
                # on would only collect the same refusal ~200 times, and on
                # the /observations/ fallback each of those can first sit out
                # the pacing wait. Stop reading, and say plainly that what
                # is left was not checked - "unknown", never "missing".
                log.warning("cross-check stopped at station %d: %s", station_id, exc)
                for _sid, unread_items in stations[position:]:
                    needed_read = False
                    for item in unread_items:
                        if has_started(item):
                            checked.append(started(item))
                        else:
                            needed_read = True
                            checked.append({**item, "state": "unknown",
                                            "detail": _VERIFY_BUDGET_DETAIL})
                    stations_unread += needed_read
                stopped_reason = (
                    f"SatNOGS is rate-limiting calendar reads ({exc}); stopped after "
                    f"{stations_read} station read(s) with {stations_unread} station(s) "
                    "not read - run the cross-check again later"
                )
                break
            except Exception as exc:
                stations_read += 1
                log.warning("could not read back station %d: %s", station_id, exc)
                for item in station_items:
                    checked.append({**item, "state": "unknown",
                                    "detail": f"could not read this station's calendar: {exc}"})
                continue
            stations_read += 1

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

        verified = {
            "status": "ok",
            "generated_utc": now.isoformat(),
            "run_generated_utc": run.get("generated_utc"),
            "stations_checked": len(by_station),
            "stations_read": stations_read,
            "stations_unread": stations_unread,
            "calendar_sources": _calendar_sources(network),
            "items": checked,
        }
        if stopped_reason is not None:
            verified["stopped_reason"] = stopped_reason
        return verified

    def _mock_verify(self) -> dict:
        now = datetime.now(timezone.utc)
        run = self.get_last_run()
        items = run.get("accepted_items") or []
        return {
            "status": "ok",
            "generated_utc": now.isoformat(),
            "run_generated_utc": run.get("generated_utc"),
            "stations_checked": len({i["station_id"] for i in items}),
            "stations_unread": 0,
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
        after a (re)start: whatever is left of campaign_poll_s since the
        timer's own last cycle, or 0 if there is none.

        The loop used to run a cycle the moment it started, and uvicorn
        --reload restarts it on every edit under backend/app - so each edit
        fired a full real preview (~145-222 calendar reads) and, with
        auto-commit on, real bookings on community stations. A daily timer
        has no reason to fire twice in a day because the process restarted.

        Anchored on the TIMER's last cycle (auto_cycle_path), not on the last
        preview of any kind as it first was. With the auto-run chain on, the
        last preview is usually the chain's - started at a Station Schedule
        slot plus that day's own run - so every reload moved the timer onto
        the next day's slot, where whichever of the two previewed first made
        the other answer "running": with auto-commit off the timer's
        preview-only cycle then cost the chain its whole slot. Only when the
        timer has no record of its own (a data dir from before it was kept)
        does the last preview stand in, as before - and _keep_timer_anchor()
        stops a chained preview from ever being that stand-in.
        """
        poll_s = float(self.s.campaign_poll_s)
        own = self._read_json(self.auto_cycle_path, None)
        generated = _parse_utc(own.get("generated_utc")) if isinstance(own, dict) else None
        if generated is None:
            last = self.get_last_preview()
            generated = _parse_utc(last.get("generated_utc")) if isinstance(last, dict) else None
        if generated is None:
            return 0.0
        age_s = ((now or datetime.now(timezone.utc)) - generated).total_seconds()
        # Clamped to one poll period so a preview stamped in the future (a
        # clock step) delays the timer by at most one cycle, never longer.
        return max(0.0, min(poll_s, poll_s - age_s))

    def _keep_timer_anchor(self) -> None:
        """Before a chained preview overwrites the last preview: if the timer
        has no record of its own cycle yet, keep the preview it would fall
        back on (auto_cycle_delay_s) as that record.

        Without this the fallback is a window, not a one-off: until the
        timer's first cycle on this code - up to campaign_poll_s after the
        deploy - a chain slot can write the last preview, and a reload then
        anchors the timer on that slot for good, its own record carrying the
        slot's phase forward every day after."""
        if self.auto_cycle_path.exists():
            return
        last = self.get_last_preview()
        raw = last.get("generated_utc") if isinstance(last, dict) else None
        if _parse_utc(raw) is not None:
            self._write_json(self.auto_cycle_path, {"generated_utc": raw})

    async def run_auto_cycle(self) -> str:
        """Called only by scheduler.py's timer loop. Always previews first;
        only auto-commits if the operator has explicitly turned that on.

        Returns AUTO_CYCLE_BUSY when the preview was refused because another
        campaign operation held the guard - nothing ran, so the timer retries
        shortly instead of writing the cycle off - AUTO_CYCLE_RETRY when the
        preview failed on a transient SatNOGS error (it sent nothing, and a
        flaky SatNOGS usually answers the next try), and "done" otherwise."""
        preview = await self.preview_campaign()
        if preview.get("status") == "running":
            return AUTO_CYCLE_BUSY
        # Whatever came of the preview, the timer's cycle happened: this is
        # what the next restart's delay is measured from. A retry writes it
        # again, so a reload mid-retry waits a full period from the latest
        # attempt: retries live in this process only and never fire at boot.
        self._write_json(self.auto_cycle_path, {
            "generated_utc": preview.get("generated_utc")
            or datetime.now(timezone.utc).isoformat(),
        })
        if preview.get("status") == "error":
            return AUTO_CYCLE_RETRY if preview.get("retryable") else "done"
        if not self.schedule_service.campaign_auto_commit_enabled():
            return "done"
        if not preview.get("items"):
            return "done"
        await self.commit_campaign(items=preview["items"], trigger="auto",
                                   preview_generated_utc=preview.get("generated_utc"))
        return "done"

    async def run_chained_cycle(self) -> dict:
        """The worldwide campaign, chained onto a Station Schedule auto-run
        slot. Called only by scheduler.py, after run_plan(trigger="auto"), and
        only while the operator has auto_run_chain_campaign on.

        Preview, then commit exactly what was previewed (trigger "chained"),
        under the Network Campaign's own caps, downlink policy and loop
        setting. The own-station gate applies like on any commit: with our
        station not Online the result is "blocked" and nothing is sent.

        It deliberately does NOT consult campaign_auto_commit_enabled. That
        switch is consent for the campaign's OWN daily timer; the chain toggle
        is separate consent for this trigger, given behind a confirmation that
        names the caps and the slot times. Requiring both would make the
        operator's tick do nothing unless they also switched on a second,
        unrelated stream of unattended bookings they did not ask for.
        """
        self._keep_timer_anchor()
        preview = await self.preview_campaign()
        status = preview.get("status")
        if status == "running":
            # Another campaign operation holds the single-flight guard - a
            # manual preview/commit/cross-check, or (rarely, now that the
            # timer keeps its own phase) the timer's cycle. Not retried: the
            # caller moves on, and the next slot (or the operator's own run)
            # covers the window.
            return {"status": "running"}
        if status == "error":
            # The one skip worth retrying, and only on a transient SatNOGS
            # error: the preview failed before anything was sent (see
            # Scheduler._chain_retry_loop).
            return {"status": "skipped",
                    "reason": f"the campaign preview failed: {preview.get('error')}",
                    "retryable": bool(preview.get("retryable"))}
        if not preview.get("items"):
            early = preview.get("stopped_early") or {}
            return {"status": "skipped",
                    "reason": ("the preview was cut short before it planned anything "
                               f"({early.get('reason', 'unknown reason')})") if early
                    else "the preview found nothing to book",
                    "retryable": False}
        return await self.commit_campaign(items=preview["items"], trigger="chained",
                                          preview_generated_utc=preview.get("generated_utc"))

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
