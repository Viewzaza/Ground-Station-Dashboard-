from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/passes")
async def passes(request: Request, norad: int, hours: float = 24.0,
                 min_el: float | None = None) -> dict:
    items = request.app.state.predictor.passes(norad, hours=hours, min_el=min_el)
    return {"norad": norad, "horizon_h": hours,
            "items": [p.model_dump(mode="json") for p in items]}


@router.get("/passes/next")
async def next_pass(request: Request, norad: int) -> dict | None:
    p = request.app.state.predictor.next_pass(norad)
    return p.model_dump(mode="json") if p else None


@router.get("/passes/{pass_id}/track")
async def pass_track(request: Request, pass_id: str) -> dict:
    """az/el samples for the polar-plot overlay.

    pass_id is `<norad>-<aos epoch seconds>`, so the pass is re-derived rather
    than held in server memory.
    """
    try:
        norad_s, aos_s = pass_id.rsplit("-", 1)
        norad, aos_epoch = int(norad_s), int(aos_s)
    except ValueError:
        raise HTTPException(400, "malformed pass_id")

    predictor = request.app.state.predictor
    for p in predictor.passes(norad, hours=36.0):
        if int(p.aos.timestamp()) == aos_epoch:
            return {"pass_id": pass_id,
                    "samples": predictor.track(norad, p.aos, p.los)}
    raise HTTPException(404, "pass not found in the current window")


@router.get("/satpos")
async def satpos(request: Request, norad: int, at: datetime | None = None) -> dict:
    pos = request.app.state.predictor.position(norad, at)
    if pos is None:
        raise HTTPException(404, f"no elements for NORAD {norad}")
    return pos.model_dump(mode="json")


@router.get("/groundtrack")
async def groundtrack(request: Request, norad: int, before_min: float = 45.0,
                      after_min: float = 45.0) -> dict:
    return {
        "norad": norad,
        "points": request.app.state.predictor.ground_track(
            norad, minutes_before=before_min, minutes_after=after_min
        ),
    }
