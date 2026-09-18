"""Station Schedule: the autoscheduler's last run, and its priority list.

A run against live SatNOGS can take about 75 seconds (see schedule_service's
own comment on history pages), so `POST /schedule/run` never awaits it -
it starts the run in the background and the caller polls `GET /schedule`.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from ..schemas import PriorityUpdate

router = APIRouter()


def _service(request: Request):
    service = getattr(request.app.state, "schedule_service", None)
    if service is None:
        raise HTTPException(503, "schedule service not running")
    return service


@router.get("/schedule")
async def last_run(request: Request) -> dict:
    return _service(request).get_last_run()


@router.post("/schedule/run")
async def run(request: Request) -> dict:
    service = _service(request)
    if service.is_running():
        return {"status": "running"}
    asyncio.create_task(service.run_plan())
    return {"status": "started"}


@router.get("/schedule/priorities")
async def priorities(request: Request) -> dict:
    return {"entries": _service(request).get_priorities()}


@router.post("/schedule/priorities")
async def save_priorities(request: Request, body: PriorityUpdate) -> dict:
    service = _service(request)
    await service.save_priorities([e.model_dump() for e in body.entries])
    return {"entries": service.get_priorities()}
