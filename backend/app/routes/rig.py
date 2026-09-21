"""What the receiver is tuned to, and whether it agrees with us.

Read-only throughout. satnogs-client owns this rig; the dashboard watches it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/rig")
async def rig(request: Request) -> dict:
    service = getattr(request.app.state, "rig", None)
    settings = request.app.state.settings
    if service is None:
        raise HTTPException(503, "rig service not running")

    return {
        "enabled": settings.rigctld_enabled,
        "host": settings.rigctld_host,
        "port": settings.rigctld_port,
        # None means rigctld has not answered. That is a normal deployment —
        # a station whose SDR is tuned inside the flowgraph has no rig to read
        # — so the panel says "no rig" rather than showing a fault.
        "sample": service.last,
    }
