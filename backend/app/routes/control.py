"""Rotator control endpoints.

Every route here can move a physical antenna, so each one goes through
ControlService, which re-checks the interlock on every call. A refusal is a 409
carrying the failing gates: the operator needs to know *which* gate is shut,
because "blocked" alone gives them nothing to act on.

The read-only position endpoint stays in routes/rotator.py. Keeping the two
apart means a reader of that file can still see at a glance that nothing in it
moves anything.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..services.control import ControlRefused
from ..services.rotctld_client import RotctldError

router = APIRouter()


class GotoRequest(BaseModel):
    # Bounds are the widest any supported rotator accepts; the real limits come
    # from dump_caps and are applied in the client, which is the only place that
    # knows what is actually plugged in.
    az: float = Field(ge=-180.0, le=540.0)
    el: float = Field(ge=-20.0, le=210.0)


class TrackRequest(BaseModel):
    norad: int | None = None


def _service(request: Request):
    service = getattr(request.app.state, "control", None)
    if service is None:
        raise HTTPException(503, "control service not running")
    return service


def _refused(exc: ControlRefused) -> HTTPException:
    return HTTPException(409, {"error": str(exc), "blocked_by": exc.blocked_by})


@router.get("/control")
async def get_control(request: Request) -> dict:
    service = _service(request)
    state = service.state()
    payload = state.model_dump(mode="json")
    caps = service.rotator.client.caps
    payload["can_park"] = bool(caps and caps.can_park)
    payload["guard_s"] = service.s.gate_guard_s
    payload["lease_s"] = service.s.control_lease_s
    payload["deadband_deg"] = service.s.track_deadband_deg
    payload["park"] = {"az": service.s.park_az, "el": service.s.park_el}
    return payload


@router.post("/control/arm")
async def arm(request: Request) -> dict:
    try:
        return _service(request).arm().model_dump(mode="json")
    except ControlRefused as exc:
        raise _refused(exc)


@router.post("/control/release")
async def release(request: Request) -> dict:
    return _service(request).release().model_dump(mode="json")


@router.post("/control/goto")
async def goto(request: Request, body: GotoRequest) -> dict:
    try:
        state = await _service(request).goto(body.az, body.el)
    except ControlRefused as exc:
        raise _refused(exc)
    except RotctldError as exc:
        raise HTTPException(502, str(exc))
    return state.model_dump(mode="json")


@router.post("/control/park")
async def park(request: Request) -> dict:
    try:
        state = await _service(request).park()
    except ControlRefused as exc:
        raise _refused(exc)
    except RotctldError as exc:
        raise HTTPException(502, str(exc))
    return state.model_dump(mode="json")


@router.post("/control/track")
async def track(request: Request, body: TrackRequest) -> dict:
    try:
        state = await _service(request).track(body.norad)
    except ControlRefused as exc:
        raise _refused(exc)
    except RotctldError as exc:
        raise HTTPException(502, str(exc))
    return state.model_dump(mode="json")


@router.post("/control/stop")
async def stop(request: Request) -> dict:
    """Deliberately not gated — see ControlService.stop."""
    try:
        state = await _service(request).stop()
    except RotctldError as exc:
        raise HTTPException(502, str(exc))
    return state.model_dump(mode="json")
