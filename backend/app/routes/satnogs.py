"""SatNOGS station activity.

Served from the poller's cached state rather than proxied. The dashboard may be
open on several screens for months at a time, and each one hitting the Network
API on every render would be a lot of traffic aimed at a volunteer-run service
for data that changes every minute or two.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _service(request: Request):
    service = getattr(request.app.state, "satnogs", None)
    if service is None:
        raise HTTPException(503, "satnogs service not running")
    return service


@router.get("/satnogs")
async def satnogs(request: Request) -> dict:
    return _service(request).snapshot()


@router.get("/satnogs/observations")
async def observations(request: Request) -> dict:
    service = _service(request)
    return {
        "station_id": service.s.station_id,
        "observations": service.observations,
    }
