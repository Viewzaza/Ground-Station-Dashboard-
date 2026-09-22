"""Wire contract.

The WebSocket carries one envelope shape for every message, tagged by `type`.
`seq` is a single counter across all types so a client can spot a gap and ask
for a fresh snapshot. Write this file first and everything else is written
against it.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 24-hour wall clock, matching util/nextfire.TIME_RE. Duplicated rather
# than imported so the schema layer does not depend on a service helper.
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

ServerFrameType = Literal[
    "hello", "snapshot", "rotator", "satpos", "pass_next", "passes",
    "tle", "satnogs", "pointing", "control", "status", "log", "error",
    "rig",
]

ClientFrameType = Literal[
    "select_satellite", "subscribe", "arm_control", "release_control",
    "slew", "stop", "ping", "resync",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Frame(BaseModel):
    """Server -> client envelope."""

    v: int = 1
    type: ServerFrameType
    ts: datetime = Field(default_factory=_now)
    seq: int = 0
    data: dict[str, Any] = Field(default_factory=dict)


class ClientFrame(BaseModel):
    """Client -> server envelope."""

    v: int = 1
    type: ClientFrameType
    data: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# payloads
# --------------------------------------------------------------------------

LinkState = Literal["up", "degraded", "down"]
ComponentState = Literal["ok", "degraded", "down"]


class RotatorSample(BaseModel):
    # Azimuth is kept RAW. A SPID reports -180..540, and a reading of 412 deg
    # means the rotator is wound past north — operationally important, so it is
    # never clamped. az_rose is the 0-360 value, for the compass only.
    az_raw: float
    el: float
    az_rose: float
    source: Literal["rotctld", "mock"]
    link: LinkState = "up"
    rprt: int = 0
    latency_ms: float = 0.0
    wrap: Literal["none", "cw", "ccw"] = "none"
    stale_s: float = 0.0


class SatPos(BaseModel):
    norad: int
    name: str = ""
    lat: float
    lon: float
    alt_km: float
    vel_km_s: float
    az: float
    el: float
    range_km: float
    range_rate_km_s: float
    doppler_hz: float | None = None
    footprint_km: float
    tle_age_days: float | None = None


class Pass(BaseModel):
    pass_id: str
    norad: int
    name: str = ""
    aos: datetime
    tca: datetime
    los: datetime
    duration_s: float
    max_el: float
    aos_az: float
    los_az: float
    state: Literal["upcoming", "in_progress", "complete"] = "upcoming"
    seconds_to_aos: float = 0.0


class TleInfo(BaseModel):
    norad: int
    name: str = ""
    tle1: str
    tle2: str
    source: str
    fetched_at: datetime
    epoch: datetime | None = None
    age_days: float = 0.0
    state: Literal["fresh", "aging", "stale"] = "fresh"


class ControlState(BaseModel):
    """Why the dashboard may or may not command the rotator right now."""

    enabled: bool                       # GS_ROTATOR_CONTROL_ENABLED
    armed: bool
    lease_expires_at: datetime | None = None
    gates: dict[str, bool] = Field(default_factory=dict)
    blocked_by: list[str] = Field(default_factory=list)
    mode: Literal["idle", "manual", "track"] = "idle"
    target_norad: int | None = None


class Status(BaseModel):
    component: Literal[
        "backend", "rotctld", "tle", "satnogs", "camera", "predictor", "control",
        "rig", "schedule", "campaign",
    ]
    state: ComponentState
    detail: str = ""
    since: datetime = Field(default_factory=_now)


class PriorityEntry(BaseModel):
    """One line of the station's priority file.

    ``satellite``, ``transmitter_desc`` and ``transmitter_status`` are
    display-only, filled in by the backend from the SatNOGS DB catalogue on a
    GET; a client may send them back unchanged on a POST (they are ignored -
    only norad/weight/uuid are ever written to the file) or omit them
    entirely.
    """

    norad_cat_id: int
    weight: float = Field(ge=0.0, le=1.0)
    transmitter_uuid: str | None = None
    # "auto" (default): the dashboard may recompute this weight from list
    # order on a drag-reorder. "manual": the operator pinned this exact
    # weight and it must survive reorders elsewhere in the list.
    mode: Literal["auto", "manual"] = "auto"
    satellite: str | None = None
    transmitter_desc: str | None = None
    # The DB's own status for a *pinned* transmitter (e.g. "active",
    # "inactive", "future") - unlike the transmitter-picker's option list,
    # which only ever lists "active" candidates, a saved pin can go stale
    # after the fact. None means "auto" or "not found in the DB", neither of
    # which has a status to show.
    transmitter_status: str | None = None


class PriorityUpdate(BaseModel):
    entries: list[PriorityEntry]


class PriorityListInfo(BaseModel):
    slug: str
    name: str


class PriorityListCreate(BaseModel):
    name: str
    duplicate_current: bool = False


class PriorityListRename(BaseModel):
    name: str


class ScheduleConfigUpdate(BaseModel):
    # extra="forbid" so a misspelled key is a 400 rather than a setting that
    # silently never takes effect. Pydantic's default would drop it before the
    # service ever saw it, which is the failure this is guarding against: an
    # operator turning something off and it staying on.
    model_config = ConfigDict(extra="forbid")

    station_id: int | None = None
    # "" clears the override back to the dashboard's own default; None
    # leaves the current value unchanged.
    db_token: str | None = None
    # Unlike db_token (inert everywhere in this codebase), a network token
    # enables real bookings via the Network Campaign feature - see
    # network_client.py's schedule() docstring for the one call site that
    # ever uses it.
    network_token: str | None = None
    # A plain bool, not subject to the ""/0-clears convention: omit to leave
    # unchanged, pass explicitly to change it. Defaults False everywhere it
    # is read (CampaignService) so a fresh setup never auto-books.
    campaign_auto_commit_enabled: bool | None = None
    campaign_max_per_station: int | None = None
    campaign_max_total: int | None = None

    # --- station auto run ---------------------------------------------------
    # Ships off, and dry-run-only, on a fresh install: two deliberate switches
    # between a new setup and anything booking unattended.
    auto_run_enabled: bool | None = None
    auto_run_mode: Literal["times", "interval"] | None = None
    auto_run_times: list[str] | None = None
    auto_run_interval_min: int | None = Field(default=None, ge=5, le=1440)
    auto_run_dry_run: bool | None = None

    # --- run flags ----------------------------------------------------------
    # 96h is well past SatNOGS's own ~48h booking horizon; anything beyond it
    # plans passes nobody can book.
    schedule_hours: float | None = Field(default=None, gt=0, le=96)
    min_culmination_deg: float | None = Field(default=None, ge=0, le=90)
    only_priority: bool | None = None
    max_observation_minutes: int | None = Field(default=None, ge=1, le=120)
    start_lead_minutes: int | None = Field(default=None, ge=0, le=1440)
    run_timeout_s: int | None = Field(default=None, ge=60, le=7200)

    @field_validator("auto_run_times")
    @classmethod
    def _times_are_wall_clock(cls, value):
        """24-hour HH:MM only.

        Rejected rather than silently dropped, because a time the scheduler
        ignores looks identical to one it is waiting for.
        """
        if value is None:
            return value
        bad = [t for t in value if not _HHMM_RE.fullmatch((t or "").strip())]
        if bad:
            raise ValueError(
                f"auto-run times must be 24-hour HH:MM: {', '.join(map(repr, bad))}"
            )
        return [t.strip() for t in value]


class ScheduleRunRequest(BaseModel):
    """Deliberately has NO default.

    A stale client that still posts `{}` - which is exactly what this
    dashboard's own api.runSchedule() used to do - gets a 422 rather than
    silently starting a run that books real observations. Failing closed is
    worth more here than backward compatibility, because the failure mode of
    the alternative is unattended bookings nobody asked for.
    """
    dry_run: bool


class StationVerifyRequest(BaseModel):
    station_id: int


class ScheduleObservation(BaseModel):
    start: str
    end: str
    duration_s: int
    norad_cat_id: int
    satellite: str
    max_elevation_deg: float
    aos_azimuth_deg: float
    los_azimuth_deg: float
    transmitter_uuid: str
    downlink_hz: int
    mode: str | None = None
    observed_here: int
    score: float
    is_mission: bool


class ScheduleNotice(BaseModel):
    severity: Literal["warning", "error"]
    message: str


class ScheduleRun(BaseModel):
    # "error" and "never_run" are both really written by ScheduleService -
    # the Literal used to exclude them, which nothing caught because no route
    # declares this as a response_model.
    status: Literal["ok", "ok_with_warnings", "error", "never_run", "running"] = "ok"
    station: int
    generated_utc: str
    considered: int
    # No upstream equivalent in the official scheduler's output. Always 0, and
    # said so rather than back-computed from considered-minus-planned, which
    # would attribute elevation filtering to scheduling conflicts.
    rejected_conflict: int
    rejected_capped: int
    notices: list[ScheduleNotice] = Field(default_factory=list)
    observations: list[ScheduleObservation]

    error: str | None = None
    # False means this run really booked. The two are never inferred from each
    # other: a dry run reports booked_state "dry_run", not "failed".
    dry_run: bool = True
    trigger: Literal["manual", "auto"] = "manual"
    planned: int = 0
    booked: int = 0
    # "mock" is its own state, not a flavour of the others: a simulated run
    # spawns nothing, so it neither booked nor failed to book, and collapsing
    # it into "confirmed" told the operator an observation existed that did not.
    booked_state: Literal[
        "dry_run", "confirmed", "partial", "unconfirmed", "failed", "mock"
    ] = "dry_run"
    # Passes the run found already on the station's calendar. The tool prints
    # these with zeroed azimuth/elevation, so they are kept apart rather than
    # rendered as if they were planned now.
    already_scheduled: list[ScheduleObservation] = Field(default_factory=list)
    efficiency: dict | None = None
    exit_code: int | None = None
    killed_by: str = ""
    run_duration_s: float = 0.0
    log_tail: list[str] = Field(default_factory=list)
    cli_version: str = ""


class CampaignItem(BaseModel):
    station_id: int
    station_name: str
    transmitter_uuid: str
    start: str
    end: str
    max_elevation_deg: float


class CampaignSkip(BaseModel):
    station_id: int | None = None
    station_name: str = ""
    reason: str


class CampaignPreview(BaseModel):
    status: Literal["ok", "error", "running"] = "ok"
    generated_utc: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    considered_stations: int = 0
    items: list[CampaignItem] = Field(default_factory=list)
    skipped: list[CampaignSkip] = Field(default_factory=list)
    error: str | None = None


class CampaignCommitRequest(BaseModel):
    # Omit to recompute a fresh preview and submit that; pass the exact
    # items a client already previewed to submit precisely what was shown.
    items: list[CampaignItem] | None = None


class CampaignCommitResult(BaseModel):
    status: Literal["ok", "ok_with_warnings", "error", "running"]
    trigger: Literal["manual", "auto"] = "manual"
    generated_utc: str | None = None
    submitted: int = 0
    accepted: int = 0
    errors: list[str] = Field(default_factory=list)
    error: str | None = None


class CampaignHistoryEntry(BaseModel):
    generated_utc: str | None = None
    trigger: Literal["manual", "auto"] = "manual"
    status: str | None = None
    submitted: int = 0
    accepted: int = 0
    rejected: int = 0
