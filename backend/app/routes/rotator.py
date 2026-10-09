from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/rotator")
async def rotator(request: Request) -> dict:
    """Current antenna position.

    Read-only: there is deliberately no move endpoint on this router. Control
    arrives with its interlock, not before.
    """
    service = getattr(request.app.state, "rotator", None)
    if service is None:
        return {"sample": None, "source": None, "link": "down",
                "detail": "rotator service not running"}

    caps = service.client.caps
    return {
        "sample": service.last.model_dump(mode="json") if service.last else None,
        "source": service.source,
        "link": service.last.link if service.last else "down",
        "verified": service.verified,
        "fatal": service._fatal,
        "caps": None if caps is None else {
            "model": caps.model,
            "name": caps.name,
            "min_az": caps.min_az,
            "max_az": caps.max_az,
            "min_el": caps.min_el,
            "max_el": caps.max_el,
        },
    }


@router.get("/antenna")
async def antenna(request: Request) -> dict:
    """Who has the antenna, and which satellite it is working.

    Read-only, like everything on this router, and display-only: nothing that
    gates a command reads it. The control loop publishes it as the `antenna`
    frame; this is the same state for anything that would rather ask, computed
    fresh if the loop has not ticked yet.
    """
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return {"owner": "unknown", "activity": "scheduler not running",
                "focus_norad": None, "focus_has_elements": False,
                "focus_source": "none"}
    return scheduler.antenna_last or scheduler.compute_antenna()
