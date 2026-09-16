"""Wire contract.

The WebSocket carries one envelope shape for every message, tagged by `type`.
`seq` is a single counter across all types so a client can spot a gap and ask
for a fresh snapshot. Write this file first and everything else is written
against it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

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
        "rig",
    ]
    state: ComponentState
    detail: str = ""
    since: datetime = Field(default_factory=_now)
