"""Space weather.

Served from the poller's cache rather than proxied, for the same reason the
SatNOGS route is: a wall display that is open on several screens for months
must not turn into several screens' worth of traffic aimed at SWPC. The poller
asks once on its own cadence and every browser reads the answer from here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/spaceweather")
async def spaceweather(request: Request) -> dict:
    service = getattr(request.app.state, "spaceweather", None)
    if service is None:
        raise HTTPException(503, "space weather service not running")
    return service.snapshot()
