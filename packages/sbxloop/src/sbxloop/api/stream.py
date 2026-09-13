"""Waking every live stream when the chronology grows.

A stream holds no buffer: its state is a cursor, and it reads by ``seq``
from the store when told there may be more. So a slow client blocks
nobody — it falls behind in the durable history and catches up — and the
hub's whole job is a thread-safe "something changed" that the projector,
the frontend and the operation store can raise from their own threads.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading


class StreamHub:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None
        self._clients = 0
        self._lock = threading.Lock()

    def _bind(self) -> asyncio.Event:
        running = asyncio.get_running_loop()
        if self._loop is not running or self._event is None:
            self._loop = running
            self._event = asyncio.Event()
        return self._event

    def notify(self) -> None:
        """From any thread: wake every waiter."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        # A loop closing under us is a shutdown, not an error.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._release)

    def _release(self) -> None:
        event = self._event
        if event is not None:
            event.set()
            self._event = asyncio.Event()

    async def wait(self, timeout: float) -> bool:
        """Wait for a notification or ``timeout`` seconds; True when woken."""
        event = self._bind()
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            return False
        return True

    # -- admission ----------------------------------------------------------------

    @property
    def clients(self) -> int:
        return self._clients

    def admit(self, limit: int) -> bool:
        with self._lock:
            if self._clients >= limit:
                return False
            self._clients += 1
            return True

    def leave(self) -> None:
        with self._lock:
            self._clients = max(0, self._clients - 1)
