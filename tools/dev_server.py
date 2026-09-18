"""Single-process dev server: API, frontend and the go2rtc proxy on one origin.

Production serves the frontend from Caddy and the API from uvicorn. That needs
containers, which is a slow loop on a laptop — and on Windows, Docker Desktop
may not even be running. This runner mounts the same FastAPI app and serves the
static frontend beside it, so the browser still sees one origin and no CORS.

    python tools/dev_server.py            # http://localhost:8000

It also proxies /video to go2rtc, which Caddy does in production. Without that
the camera tiles can only ever reach their JPEG fallback, so the WebRTC path —
the one the operator actually watches — would never be exercised outside a full
container deployment. Run go2rtc separately and point GS_GO2RTC_URL at it:

    go2rtc -config deploy/go2rtc/go2rtc.yaml
    set GS_GO2RTC_URL=http://127.0.0.1:1984
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("GS_MOCK", "1")
os.environ.setdefault("GS_DATA_DIR", str(ROOT / "backend" / "data"))
os.environ.setdefault("GS_LOG_LEVEL", "info")

import httpx                                       # noqa: E402
import uvicorn                                     # noqa: E402
import websockets                                  # noqa: E402
from fastapi import Request, Response, WebSocket, WebSocketDisconnect   # noqa: E402
from fastapi.staticfiles import StaticFiles        # noqa: E402

from app.main import app                           # noqa: E402

GO2RTC = os.environ.get("GS_GO2RTC_URL", "http://127.0.0.1:1984").rstrip("/")


# --------------------------------------------------------------------------
# go2rtc proxy — Caddy's job in production, ours here
# --------------------------------------------------------------------------

@app.websocket("/video/api/ws")
async def video_ws(client: WebSocket) -> None:
    """Relay the camera WebSocket to go2rtc.

    go2rtc carries WebRTC signalling as text and MSE/MJPEG payloads as binary
    on the same socket, so both directions have to preserve frame type — a
    proxy that decodes everything to str turns the video path into mojibake.
    """
    await client.accept()
    query = client.scope.get("query_string", b"").decode()
    target = GO2RTC.replace("https://", "wss://").replace("http://", "ws://")
    target = f"{target}/api/ws" + (f"?{query}" if query else "")

    try:
        upstream = await websockets.connect(target, max_size=None)
    except Exception as exc:                       # go2rtc down: say so, close
        await client.close(code=1011, reason=f"go2rtc unreachable: {exc}"[:120])
        return

    async def pump_up() -> None:
        while True:
            message = await client.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect
            if (text := message.get("text")) is not None:
                await upstream.send(text)
            elif (data := message.get("bytes")) is not None:
                await upstream.send(data)

    async def pump_down() -> None:
        async for message in upstream:
            if isinstance(message, str):
                await client.send_text(message)
            else:
                await client.send_bytes(message)

    up = asyncio.create_task(pump_up())
    down = asyncio.create_task(pump_down())
    try:
        done, pending = await asyncio.wait(
            {up, down}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # Retrieve the finished task's exception. A viewer closing the tab ends
        # this proxy through pump_up raising WebSocketDisconnect, and leaving
        # that unretrieved makes asyncio print a traceback at collection time —
        # once per camera tile, every reload, for a normal disconnect.
        for task in done:
            task.exception()
    except Exception:
        pass
    finally:
        await upstream.close()


@app.api_route("/video/{path:path}",
               methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
async def video_http(path: str, request: Request) -> Response:
    url = f"{GO2RTC}/{path}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            upstream = await client.request(
                request.method, url,
                params=request.query_params,
                content=await request.body(),
                headers={k: v for k, v in request.headers.items()
                         if k.lower() not in ("host", "connection")},
            )
    except httpx.HTTPError as exc:
        return Response(f"go2rtc unreachable: {exc}", status_code=502,
                        media_type="text/plain")

    # hop-by-hop headers must not be forwarded
    headers = {k: v for k, v in upstream.headers.items()
               if k.lower() not in ("content-encoding", "content-length",
                                    "transfer-encoding", "connection")}
    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=headers,
                    media_type=upstream.headers.get("content-type"))


class NoCacheStatic(StaticFiles):
    """Serve the frontend with caching off.

    Without this the browser holds on to css/js between edits and you end up
    debugging a stale stylesheet — which is exactly as much fun as it sounds.
    Production is the opposite: Caddy should cache these aggressively.
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        return resp


# Mounted last so every /api route still wins.
app.mount("/", NoCacheStatic(directory=str(ROOT / "frontend"), html=True), name="frontend")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"dashboard  http://localhost:{port}")
    print(f"api docs   http://localhost:{port}/api/docs")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
