"""Validated gateway results and bounded, cancellation-safe DALI transactions.

No HA or HTTP dependency: the hub supplies the actual request coroutine.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

from .dali import Frame

FrameSender = Callable[[Frame], Awaitable[list[int] | None]]


class DaliError(Exception):
    """A failed operation; dependent frames must not be sent."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"DALI operation failed: {reason}")


def parse_response(body: object, frame: Frame) -> list[int] | None:
    """Accept the gateway's result contract, not merely HTTP 200.

    None is exclusively a successful query with no backward response, or a
    successful write. Execution failures raise, including for non-query writes.
    """
    if not isinstance(body, dict):
        raise DaliError("malformed_response")
    for key in ("success", "bus_busy", "collision_detected"):
        if type(body.get(key)) is not bool:
            raise DaliError("malformed_response")
    if body["bus_busy"]:
        raise DaliError("bus_busy")
    if body["collision_detected"]:
        raise DaliError("collision")
    if not body["success"]:
        raise DaliError("gateway_rejected")
    if not frame.wait_for_answer:
        return None
    if "data" not in body:
        raise DaliError("malformed_response")
    answer = body["data"]
    if answer is None:
        return None
    if type(answer) is int:
        answer = [answer]
    if (not isinstance(answer, list) or len(answer) != 1
            or type(answer[0]) is not int or not 0 <= answer[0] <= 255):
        raise DaliError("malformed_response")
    return answer


class DaliTransport:
    """Serialize every integration request; admit at most eight transactions.

    Cancellation waits for an in-flight HTTP request to finish before releasing
    the lock, then aborts the sequence. It never sends subsequent ACTIVATE.
    No retry: a partially applied transaction cannot safely be replayed.
    """

    def __init__(self, request: FrameSender) -> None:
        self._request = request
        self._lock = asyncio.Lock()
        self._pending = 0
        self._closed = False

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            pass  # drain the current bounded request before unloading

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[FrameSender]:
        if self._closed:
            raise DaliError("closed")
        if self._pending >= 8:
            raise DaliError("queue_full")
        self._pending += 1
        try:
            async with self._lock:
                if self._closed:
                    raise DaliError("closed")
                yield self._send
        finally:
            self._pending -= 1

    async def _send(self, frame: Frame) -> list[int] | None:
        task = asyncio.create_task(self._request(frame))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                if not cancelled:
                    raise
        if cancelled:
            # Retrieve a possible exception so asyncio does not log it later.
            if not task.cancelled():
                task.exception()
            raise asyncio.CancelledError
        return task.result()

    async def send_sequence(self, frames: Sequence[Frame]) -> list[list[int] | None]:
        async with self.transaction() as send:
            return [await send(frame) for frame in frames]

    async def send_frame(self, frame: Frame) -> list[int] | None:
        return (await self.send_sequence([frame]))[0]
