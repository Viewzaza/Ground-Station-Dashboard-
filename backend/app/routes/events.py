"""The station logbook, read-only.

Nothing here writes to the log or changes anything; the log is written by the
services it watches. Reading it is always safe, and nothing else in the
backend reads it at all — it is history, never permission.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response

from ..services.events import QUERY_MAX

router = APIRouter()


def _log(request: Request):
    svc = getattr(request.app.state, "events", None)
    if svc is None:
        raise HTTPException(503, "the station logbook is not running")
    return svc


@router.get("/events")
async def events(
    request: Request,
    since: str | None = Query(None, description="an event id, or an ISO time"),
    until: str | None = Query(None, description="an event id, or an ISO time (exclusive)"),
    kinds: str | None = Query(None, description="comma separated, e.g. control,autopilot"),
    min_sev: str | None = Query(None, description="info, warn or bad"),
    q: str | None = Query(None, description="text to search for"),
    limit: int = Query(200, ge=1, le=QUERY_MAX),
) -> dict:
    """Records newest first. `more` says there are older matches past `limit`;
    ask again with `until` set to the oldest id returned."""
    try:
        items, more = await _log(request).query(
            since=since, until=until, kinds=kinds, min_sev=min_sev, q=q, limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"items": items, "more": more}


@router.get("/events/days")
async def days(request: Request) -> list[dict]:
    """The day files on disk, newest first. Days are UTC."""
    return await _log(request).days()


@router.get("/events/day/{day}.jsonl")
async def day_file(request: Request, day: str) -> Response:
    """One UTC day, exactly as written: one JSON record per line."""
    try:
        content = await _log(request).read_day(day)
    except ValueError:
        raise HTTPException(404, "not a day: expected YYYY-MM-DD")
    if content is None:
        raise HTTPException(404, f"no log for {day}")
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="station-log-{day}.jsonl"'},
    )
