"""What every route reaches: the daemon, its stores, the auth store, and
the one way to call any of them.

The stores hold one SQLite connection each behind a lock, and the loop's
methods take its locks; none of that may run on the event loop thread. So
every call goes through :meth:`ApiContext.call` — a bounded executor under
a semaphore — and the routes await it. ``ready`` is set by the daemon once
recovery has established execution ownership; until then reads answer and
mutations are refused (503). ``stopping`` ends every live stream.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from sbxloop.api.auth.keys import SigningKeys
from sbxloop.api.auth.ratelimit import FailureLimiter
from sbxloop.api.auth.store import ApiAuthStore
from sbxloop.api.chronology import Chronology
from sbxloop.api.publicids import PublicIds
from sbxloop.api.stream import StreamHub
from sbxloop.config import Config
from sbxloop.daemon.controls.service import ControlService

T = TypeVar("T")

#: Threads that run store and loop calls for the routes, and how many may
#: be in flight at once: a reconnect storm queues behind these rather than
#: starving the engine of the stores' locks.
EXECUTOR_THREADS = 4
IN_FLIGHT_LIMIT = 8
#: Page sizes for every collection.
PAGE_DEFAULT = 50
PAGE_MAX = 200


class ApiContext:
    def __init__(
        self,
        config: Config,
        *,
        loop: Any,
        auth: ApiAuthStore,
        keys: SigningKeys,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.loop = loop
        self.auth = auth
        self.keys = keys
        self.clock = clock
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.limiter = FailureLimiter()
        self.executor = ThreadPoolExecutor(
            max_workers=EXECUTOR_THREADS, thread_name_prefix="sbxloop-api-worker"
        )
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_loop: asyncio.AbstractEventLoop | None = None
        self._public_ids: PublicIds | None = None
        self._chronology: Chronology | None = None
        #: Wakes every live stream; the projector, the frontend and the
        #: routes raise it from their own threads.
        self.hub = StreamHub()
        #: The projection thread, when the listener runs one (the daemon);
        #: a test drives the chronology directly.
        self.projector: Any = None

    @property
    def api(self) -> Any:
        return self.config.api

    @property
    def public_ids(self) -> PublicIds:
        """The public-id mapping over the daemon's store, built on first use."""
        if self._public_ids is None:
            self._public_ids = PublicIds(self.loop.dstore)
        return self._public_ids

    @property
    def chronology(self) -> Chronology:
        """The public chronology over the daemon's store, built on first use."""
        if self._chronology is None:
            self._chronology = Chronology(self.loop.dstore)
        return self._chronology

    def service(self) -> ControlService:
        """A service over the loop; one per request, since it collects the
        operations that request recorded."""
        return ControlService(self.loop)

    def _sem(self) -> asyncio.Semaphore:
        running = asyncio.get_running_loop()
        if self._semaphore is None or self._semaphore_loop is not running:
            self._semaphore = asyncio.Semaphore(IN_FLIGHT_LIMIT)
            self._semaphore_loop = running
        return self._semaphore

    async def call(self, fn: Callable[..., T], *args: Any) -> T:
        """Run ``fn`` on the executor, bounded; the event loop never
        touches a store or the loop's locks."""
        async with self._sem():
            running = asyncio.get_running_loop()
            return await running.run_in_executor(self.executor, fn, *args)

    def generation(self) -> str | None:
        return getattr(self.loop, "generation", None)

    def close(self) -> None:
        self.stopping.set()
        # Every live stream sees `stopping` on its next wake and ends.
        self.hub.notify()
        self.executor.shutdown(wait=False, cancel_futures=True)
