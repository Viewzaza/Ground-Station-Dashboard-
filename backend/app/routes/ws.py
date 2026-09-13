"""The live WebSocket.

A client gets a `hello`, then a `snapshot` carrying the latest frame of every
type, so a browser opening mid-pass has a complete picture immediately rather
than a blank dashboard until each source next polls.

Outbound frames go through a bounded per-connection queue: a stalled client
drops its own oldest frames and never blocks a producer. A dashboard wants the
newest value, not a backlog.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..hub import hub
from ..schemas import ClientFrame, Frame

log = logging.getLogger(__name__)
router = APIRouter()

HEARTBEAT_S = 10.0


@router.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    await ws.accept()
    settings = ws.app.state.settings
    conn = hub.register()

    try:
        await ws.send_json(Frame(type="hello", data={
            "server_time": datetime.now(timezone.utc).isoformat(),
            "mock": settings.mock,
            "station_id": settings.station_id,
            "default_norad": settings.default_norad,
            "heartbeat_s": HEARTBEAT_S,
        }).model_dump(mode="json"))
        await ws.send_json(hub.snapshot().model_dump(mode="json"))

        sender = asyncio.create_task(_send_loop(ws, conn))
        receiver = asyncio.create_task(_receive_loop(ws))
        done, pending = await asyncio.wait(
            {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("websocket failed")
    finally:
        hub.unregister(conn)


async def _send_loop(ws: WebSocket, conn) -> None:
    while True:
        try:
            frame = await asyncio.wait_for(conn.queue.get(), HEARTBEAT_S)
        except asyncio.TimeoutError:
            # Keep the connection warm and let the client notice silence.
            await ws.send_json(Frame(type="status", data={
                "component": "backend", "state": "ok", "detail": "heartbeat",
            }).model_dump(mode="json"))
            continue
        await ws.send_json(frame.model_dump(mode="json"))


async def _receive_loop(ws: WebSocket) -> None:
    while True:
        raw = await ws.receive_json()
        try:
            frame = ClientFrame.model_validate(raw)
        except Exception:
            await ws.send_json(Frame(type="error", data={
                "code": "bad_frame", "msg": "unrecognised message",
            }).model_dump(mode="json"))
            continue

        if frame.type == "ping":
            await ws.send_json(Frame(type="status", data={
                "component": "backend", "state": "ok",
                "detail": datetime.now(timezone.utc).isoformat(),
            }).model_dump(mode="json"))
        elif frame.type == "resync":
            await ws.send_json(hub.snapshot().model_dump(mode="json"))
        elif frame.type == "subscribe":
            topics = frame.data.get("topics")
            # None means everything; a phone can ask for less.
            ws.scope.setdefault("state", {})["topics"] = topics
        # select_satellite / arm_control / slew arrive with rotator control,
        # which is not wired up yet. Unknown-but-valid types are ignored rather
        # than erroring, so an older client does not spew warnings.
