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
from ..services.control import ControlRefused
from ..services.rotctld_client import RotctldError

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
        # Await the cancellations, then LOOK at whichever task finished. An
        # exception on a done task that nobody retrieves is reported by asyncio
        # at garbage-collection time as "Task exception was never retrieved",
        # with a traceback detached from anything that explains it. On a display
        # that runs for months and reconnects on every reload, that is a steady
        # drip of tracebacks into the log — which is how a real fault ends up
        # being scrolled past.
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            # A browser navigating away closes mid-send, and Starlette asserts
            # rather than raising something specific. That is an ordinary
            # disconnect, not a fault, so it is retrieved and dropped.
            if exc and not isinstance(exc, (WebSocketDisconnect, AssertionError)):
                raise exc
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
        elif frame.type in _CONTROL_FRAMES:
            await _handle_control(ws, frame)
        # select_satellite is handled entirely in the browser: the backend
        # tracks GS_DEFAULT_NORAD because the antenna schedule must not change
        # because somebody clicked a different satellite on one of the screens.


_CONTROL_FRAMES = {"arm_control", "release_control", "slew", "stop"}


async def _handle_control(ws: WebSocket, frame: ClientFrame) -> None:
    """Control over the WebSocket, through the same interlock as the REST path.

    There is no separate authorisation here and there must never be one: this
    goes through ControlService exactly as /api/control does, so a gate closed
    to one is closed to both.
    """
    service = getattr(ws.app.state, "control", None)
    if service is None:
        await ws.send_json(Frame(type="error", data={
            "code": "unavailable", "msg": "control service not running",
        }).model_dump(mode="json"))
        return

    try:
        if frame.type == "arm_control":
            service.arm()
        elif frame.type == "release_control":
            service.release()
        elif frame.type == "stop":
            await service.stop()
        elif frame.type == "slew":
            await service.goto(
                float(frame.data["az"]), float(frame.data["el"])
            )
    except ControlRefused as exc:
        await ws.send_json(Frame(type="error", data={
            "code": "refused", "msg": str(exc), "blocked_by": exc.blocked_by,
        }).model_dump(mode="json"))
    except (KeyError, TypeError, ValueError) as exc:
        await ws.send_json(Frame(type="error", data={
            "code": "bad_frame", "msg": f"slew needs numeric az and el: {exc}",
        }).model_dump(mode="json"))
    except RotctldError as exc:
        await ws.send_json(Frame(type="error", data={
            "code": "rotctld", "msg": str(exc),
        }).model_dump(mode="json"))
