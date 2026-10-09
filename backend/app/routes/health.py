from __future__ import annotations

import time

from fastapi import APIRouter, Request

router = APIRouter()
_STARTED = time.monotonic()


@router.get("/health")
async def health(request: Request) -> dict:
    s = request.app.state.settings
    scheduler = request.app.state.scheduler
    return {
        "ok": True,
        "version": request.app.version,
        "uptime_s": round(time.monotonic() - _STARTED, 1),
        "mock": s.mock,
        "components": dict(scheduler.components),
    }


@router.get("/config")
async def config(request: Request) -> dict:
    """Everything the frontend needs to render itself.

    The frontend has no build step and no baked-in constants: panel ids, camera
    stream names and station coordinates all arrive from here, so deployment is
    a matter of editing .env.
    """
    s = request.app.state.settings
    return {
        "station": {
            "id": s.station_id,
            "name": s.station_name,
            "lat": s.station_lat,
            "lon": s.station_lon,
            "alt_m": s.station_alt_m,
            "grid": s.station_grid,
            "min_elevation_deg": s.min_elevation_deg,
            "min_culmination_deg": s.min_culmination_deg,
            "timezone": s.timezone,
        },
        "default_norad": s.default_norad,
        "pinned_norad": s.pinned_norad_ids,
        "grafana": {
            "base": s.grafana_base,
            "uid": s.grafana_uid,
            "slug": s.grafana_slug,
            "panels": s.grafana_panel_ids,
            "range": s.grafana_range,
            "vars": s.grafana_var_map,
        },
        "features": {
            "globe3d": True,
            "rotator": True,
            "rotator_control": s.rotator_control_enabled,
        },
        "mock": s.mock,
    }
