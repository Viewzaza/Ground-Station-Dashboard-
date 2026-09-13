"""WebSocket fan-out.

One producer, many browsers. The hub keeps the latest frame of every type so a
client that connects mid-flight gets a complete picture immediately instead of
waiting for the next poll of each source.

Slow clients must never block a producer: each connection has a bounded queue,
and on overflow the oldest frame *of that type* is dropped. A dashboard wants
the newest value, never a backlog.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .schemas import Frame, ServerFrameType

log = logging.getLogger(__name__)

QUEUE_LIMIT = 64


class Connection:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Frame] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self.topics: set[str] | None = None      # None = everything

    def wants(self, frame_type: str) -> bool:
        return self.topics is None or frame_type in self.topics

    def offer(self, frame: Frame) -> None:
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            self._drop_oldest_of_type(frame.type)
            try:
                self.queue.put_nowait(frame)
            except asyncio.QueueFull:
                log.debug("dropping frame for a stalled client: %s", frame.type)

    def _drop_oldest_of_type(self, frame_type: str) -> None:
        """Re-queue everything except the oldest frame of this type."""
        kept: list[Frame] = []
        removed = False
        while not self.queue.empty():
            item = self.queue.get_nowait()
            if not removed and item.type == frame_type:
                removed = True
                continue
            kept.append(item)
        for item in kept:
            self.queue.put_nowait(item)


class Hub:
    def __init__(self) -> None:
        self._conns: set[Connection] = set()
        self._last: dict[str, Frame] = {}
        # `status` is the one frame type many producers share, so keeping only
        # the latest one per *type* would mean a newly connected client learns
        # the health of whichever component reported most recently and nothing
        # about the rest. Keep those per component instead.
        self._last_status: dict[str, Frame] = {}
        self._seq = 0

    # --- connections -------------------------------------------------------
    def register(self) -> Connection:
        conn = Connection()
        self._conns.add(conn)
        return conn

    def unregister(self, conn: Connection) -> None:
        self._conns.discard(conn)

    @property
    def client_count(self) -> int:
        return len(self._conns)

    # --- publishing --------------------------------------------------------
    def publish(self, frame_type: ServerFrameType, data: dict[str, Any]) -> Frame:
        self._seq += 1
        frame = Frame(type=frame_type, seq=self._seq, data=data)

        if frame_type == "status" and (component := data.get("component")):
            self._last_status[component] = frame
        else:
            self._last[frame_type] = frame

        for conn in self._conns:
            if conn.wants(frame_type):
                conn.offer(frame)
        return frame

    def snapshot(self) -> Frame:
        """Every latest frame, as one frame, for a newly connected client.

        A browser opening mid-pass gets a complete dashboard immediately rather
        than a blank one until each source next polls.
        """
        self._seq += 1
        frames = [
            f.model_dump(mode="json")
            for f in (*self._last.values(), *self._last_status.values())
        ]
        return Frame(type="snapshot", seq=self._seq, data={"frames": frames})

    def latest(self, frame_type: str) -> Frame | None:
        return self._last.get(frame_type)


hub = Hub()
