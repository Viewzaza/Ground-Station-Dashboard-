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
    payload["limits"] = _limits(service)
    return payload


def _limits(service) -> dict | None:
    """The travel limits in force, for the panel's click-to-fill.

    The client's own limits(): the configured station limits
    (GS_ROT_LIMIT_*), narrowed by the compiled caps once dump_caps has
    answered — on station 5024, -90..450 inside the -180..540 dump_caps
    claims. Before it has answered, RotctldClient reports the configured
    limits alone, and MockRotator always has caps, so with either shipped
    client this is never None, identified or not. A click on the polar plot
    is a compass bearing, and the panel turns it into the representation
    nearest where the antenna is *inside these*; chosen against the compiled
    range, it would fill in a branch the clamp then silently moves.

    The caps and None answers are for a client without limits(), and none
    ships today. The panel falls back to the station's range for None.
    """
    client = service.rotator.client
    limits = getattr(client, "limits", None)
    if callable(limits):
        min_az, max_az, min_el, max_el = limits()[:4]
    elif client.caps is not None:
        caps = client.caps
        min_az, max_az, min_el, max_el = caps.min_az, caps.max_az, caps.min_el, caps.max_el
    else:
        return None
    return {"min_az": min_az, "max_az": max_az, "min_el": min_el, "max_el": max_el}


@router.post("/control/arm")
async def arm(request: Request) -> dict:
    try:
        return _service(request).arm().model_dump(mode="json")
    except ControlRefused as exc:
        raise _refused(exc)


@router.post("/control/extend")
async def extend(request: Request) -> dict:
    """A live lease, pushed out to a full one. Refused with `armed` while there
    is none: it never becomes an arm — see ControlService.extend."""
    try:
        return _service(request).extend().model_dump(mode="json")
    except ControlRefused as exc:
        raise _refused(exc)


@router.post("/control/release")
async def release(request: Request) -> dict:
    return (await _service(request).release()).model_dump(mode="json")


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
