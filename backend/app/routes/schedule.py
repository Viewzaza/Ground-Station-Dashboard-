"""Station Schedule: the autoscheduler's last run, and its priority list.

A run against live SatNOGS can take about 75 seconds (see schedule_service's
own comment on history pages), so `POST /schedule/run` never awaits it -
it starts the run in the background and the caller polls `GET /schedule`.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from ..schemas import (
    PriorityListCreate, PriorityListRename, PriorityUpdate, ScheduleConfigUpdate,
    StationVerifyRequest,
)

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
    return {"entries": await _service(request).get_priorities()}


@router.post("/schedule/priorities")
async def save_priorities(request: Request, body: PriorityUpdate) -> dict:
    service = _service(request)
    await service.save_priorities([e.model_dump() for e in body.entries])
    return {"entries": await service.get_priorities()}


@router.get("/schedule/transmitters/{norad_cat_id}")
async def transmitters(request: Request, norad_cat_id: int) -> dict:
    service = _service(request)
    try:
        return await service.get_transmitters(norad_cat_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        # Unlike get_priorities()'s enrichment, this endpoint has no fallback
        # display to degrade to - an empty list here would look identical to
        # "this satellite really has no transmitters", so a DB/network hiccup
        # has to fail loudly instead.
        raise HTTPException(503, f"SatNOGS DB temporarily unavailable: {exc}") from exc


# --- station id / token config ---------------------------------------------

@router.get("/schedule/config")
async def get_config(request: Request) -> dict:
    return await _service(request).get_config()


@router.post("/schedule/config")
async def save_config(request: Request, body: ScheduleConfigUpdate) -> dict:
    return await _service(request).save_config(body.station_id, body.db_token)


@router.post("/schedule/config/verify-station")
async def verify_station(request: Request, body: StationVerifyRequest) -> dict:
    return await _service(request).verify_station(body.station_id)


# --- named priority lists ----------------------------------------------------

@router.get("/schedule/priority-lists")
async def priority_lists(request: Request) -> dict:
    return await _service(request).list_priority_lists()


@router.post("/schedule/priority-lists")
async def create_priority_list(request: Request, body: PriorityListCreate) -> dict:
    return await _service(request).create_priority_list(body.name, body.duplicate_current)


@router.post("/schedule/priority-lists/{slug}/load")
async def load_priority_list(request: Request, slug: str) -> dict:
    service = _service(request)
    try:
        entries = await service.load_priority_list(slug)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"active": slug, "entries": entries}


@router.post("/schedule/priority-lists/{slug}/rename")
async def rename_priority_list(request: Request, slug: str, body: PriorityListRename) -> dict:
    service = _service(request)
    try:
        return await service.rename_priority_list(slug, body.name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
