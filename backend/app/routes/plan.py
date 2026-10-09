"""The observation plan, and the switch that lets autopilot work it.

Reading the plan is always safe. Engaging autopilot is not, so it is refused
unless an operator already holds a control lease — the same consent manual
control needs — and every move autopilot makes still goes through the
interlock.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..services.planner_service import AutopilotRefused

router = APIRouter()


class AutopilotRequest(BaseModel):
    enabled: bool


def _planner(request: Request):
    svc = getattr(request.app.state, "planner", None)
    if svc is None:
        raise HTTPException(503, "planner is not running")
    return svc


def _executor(request: Request):
    svc = getattr(request.app.state, "executor", None)
    if svc is None:
        raise HTTPException(503, "autopilot is not available")
    return svc


@router.get("/plan")
async def plan(request: Request) -> dict:
    planner = _planner(request)
    if planner.plan is None:
        # First request before the background build has run. Off the event
        # loop and behind the planner's lock, like every other rebuild:
        # building inline here used to stall the rotator poll and the gate
        # checks for the length of a Skyfield search.
        await planner.rebuild_async()
    return planner.snapshot()


@router.post("/plan/rebuild")
async def rebuild(request: Request) -> dict:
    planner = _planner(request)
    await planner.rebuild_async()
    return planner.snapshot()


@router.get("/plan/autopilot")
async def autopilot_state(request: Request) -> dict:
    st = _executor(request).state
    return {"enabled": st.enabled, "phase": st.phase, "detail": st.detail,
            "current": st.current, "disengaged_because": st.disengaged_because}


@router.post("/plan/autopilot")
async def autopilot(request: Request, body: AutopilotRequest) -> dict:
    executor = _executor(request)
    if body.enabled:
        try:
            executor.enable()
        except AutopilotRefused as exc:
            raise HTTPException(409, {"error": "autopilot_refused", "detail": str(exc)})
    else:
        await executor.disable()
    return await autopilot_state(request)
