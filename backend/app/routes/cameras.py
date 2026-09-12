from __future__ import annotations

from fastapi import APIRouter, Request, Response

from ..services.cameras import CameraService

router = APIRouter()


def _service(request: Request) -> CameraService:
    svc = getattr(request.app.state, "cameras", None)
    if svc is None:
        svc = CameraService(request.app.state.settings)
        request.app.state.cameras = svc
    return svc


@router.get("/cameras")
async def cameras(request: Request) -> dict:
    return {"items": await _service(request).inventory()}


@router.get("/cameras/{stream}/snapshot.jpg")
async def snapshot(request: Request, stream: str) -> Response:
    status, body, content_type = await _service(request).snapshot(stream)
    return Response(
        content=body,
        status_code=status,
        media_type=content_type,
        # The fallback poller refreshes about once a second; caching would
        # freeze the image on the first frame.
        headers={"Cache-Control": "no-store"},
    )
