"""Station Schedule: the autoscheduler's last run, and its priority list.

A run against live SatNOGS can take about 75 seconds (see schedule_service's
own comment on history pages), so `POST /schedule/run` never awaits it -
it starts the run in the background and the caller polls `GET /schedule`.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from fastapi.responses import PlainTextResponse

from ..schemas import (
    CampaignCommitRequest, PriorityListCreate, PriorityListRename, PriorityUpdate,
    ScheduleConfigUpdate, ScheduleRunRequest, StationVerifyRequest,
)

router = APIRouter()


def _service(request: Request):
    service = getattr(request.app.state, "schedule_service", None)
    if service is None:
        raise HTTPException(503, "schedule service not running")
    return service


def _campaign(request: Request):
    service = getattr(request.app.state, "campaign_service", None)
    if service is None:
        raise HTTPException(503, "campaign service not running")
    return service


@router.get("/schedule")
async def last_run(request: Request) -> dict:
    """The last run, plus whether one is happening right now.

    `running` and `progress` are not in the stored result - they are live
    state. Without them the panel has to infer "still going" from the result
    file not changing, which is indistinguishable from a run that died.
    """
    service = _service(request)
    payload = dict(service.get_last_run())
    payload["running"] = service.is_running()
    payload["progress"] = service.progress()
    next_run = service.next_auto_run()
    payload["auto_run_next_utc"] = next_run.isoformat() if next_run else None
    payload["auto_run_enabled"] = service.auto_run_enabled()
    payload["auto_run_dry_run"] = service.auto_run_dry_run()
    return payload


@router.post("/schedule/run")
async def run(request: Request, body: ScheduleRunRequest) -> dict:
    """Start a run. `dry_run` is required - see ScheduleRunRequest.

    Still fire-and-forget: a cold run can take minutes, so the caller polls
    GET /schedule rather than holding a request open.
    """
    service = _service(request)
    if service.is_running():
        return {"status": "running", "dry_run": body.dry_run}
    asyncio.create_task(service.run_plan(dry_run=body.dry_run, trigger="manual"))
    return {"status": "started", "dry_run": body.dry_run}


@router.get("/schedule/log", response_class=PlainTextResponse)
async def run_log(request: Request, lines: int = 400) -> str:
    """The raw transcript of the last run.

    The parsed result cannot carry everything the tool said, and when a run
    does something surprising the transcript is the only place the answer
    exists.
    """
    return _service(request).read_log(lines)


@router.get("/schedule/priorities/export", response_class=PlainTextResponse)
async def export_priorities(request: Request) -> PlainTextResponse:
    """The active list in the exact format satnogs-auto-scheduler reads.

    This is the concrete proof of the compatibility claim: the file it
    downloads can be dropped straight into an existing command line with -P.
    """
    text = await _service(request).export_priority_text()
    return PlainTextResponse(
        text,
        headers={"Content-Disposition": 'attachment; filename="prio.txt"'},
    )


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
    """Apply only the keys the client actually sent.

    `exclude_unset` is the "leave it unchanged" convention expressed properly:
    the panel already omits keys it does not mean to touch (it sets them to
    `undefined`, which JSON.stringify drops), so an omitted key and an
    explicit null are no longer the same thing.
    """
    try:
        return await _service(request).save_config(**body.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


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


# --- network campaign --------------------------------------------------------
# Requesting other SatNOGS community stations to record the mission satellite.
# This is the only feature in the dashboard that ever submits a real booking -
# see campaign_service.py's module docstring.

@router.get("/schedule/campaign")
async def campaign_last_run(request: Request) -> dict:
    return _campaign(request).get_last_run()


@router.post("/schedule/campaign/preview")
async def campaign_preview_start(request: Request) -> dict:
    service = _campaign(request)
    if service.is_running():
        return {"status": "running"}
    asyncio.create_task(service.preview_campaign())
    return {"status": "started"}


@router.get("/schedule/campaign/preview")
async def campaign_preview_last(request: Request) -> dict:
    return _campaign(request).get_last_preview()


@router.post("/schedule/campaign/commit")
async def campaign_commit(request: Request, body: CampaignCommitRequest) -> dict:
    service = _campaign(request)
    if service.is_running():
        return {"status": "running"}
    items = [item.model_dump() for item in body.items] if body.items is not None else None
    asyncio.create_task(service.commit_campaign(items=items, trigger="manual"))
    return {"status": "started"}


@router.get("/schedule/campaign/history")
async def campaign_history(request: Request) -> dict:
    return {"history": _campaign(request).get_history()}


# Read-only, and deliberately not behind the preview/commit single-flight
# guard: checking what is on the calendar is exactly what an operator wants
# to do while a long run is in flight. One live read per station in the last
# run's accepted set (typically a handful), so it answers in seconds rather
# than the minutes a full campaign computation takes.
@router.post("/schedule/campaign/verify")
async def campaign_verify(request: Request) -> dict:
    return await _campaign(request).verify_last_run()
