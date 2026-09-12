"""The thread that keeps the public chronology current.

Woken by the engine's event bus (a subscriber that only sets an event —
no I/O on the publishing thread) and by a timer, it projects new engine
events, tells the stream hub there is something to send, and prunes
history on the retention schedule. Reads never wait for it: a route
projects before it reads, so the thread's job is latency for the streams,
not correctness.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from sbxloop.api.chronology import Chronology
from sbxloop.api.stream import StreamHub
from sbxloop.log import get_logger

log = get_logger(__name__)

POLL_S = 1.0
PRUNE_EVERY_S = 3600.0


class Projector:
    def __init__(
        self,
        chronology: Chronology,
        hub: StreamHub,
        *,
        clock: Callable[[], float],
        retention_s: float,
        poll_s: float = POLL_S,
    ) -> None:
        self.chronology = chronology
        self.hub = hub
        self.clock = clock
        self.retention_s = retention_s
        self.poll_s = poll_s
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_prune = 0.0
        self._last_seen: int | None = None

    def wake(self) -> None:
        """Called from any thread: there is something to project."""
        self._wake.set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="sbxloop-api-projector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def step(self) -> int:
        """One pass: project, notify, prune when due. Returns rows projected."""
        now = self.clock()
        try:
            copied = self.chronology.project(now)
        except Exception:
            log.warning("api.projection_failed", exc_info=True)
            return 0
        # Any writer moves the high-water mark — the operation store from a
        # ctl command, the frontend, this projection — and every stream
        # hears of it here, whichever thread wrote.
        newest = self.chronology.watermark()
        if copied or newest != self._last_seen:
            self._last_seen = newest
            self.hub.notify()
        if now - self._last_prune >= PRUNE_EVERY_S:
            self._last_prune = now
            try:
                self.chronology.prune(now - self.retention_s)
            except Exception:
                log.warning("api.prune_failed", exc_info=True)
        return copied

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.poll_s)
            self._wake.clear()
            if self._stop.is_set():
                return
            self.step()
