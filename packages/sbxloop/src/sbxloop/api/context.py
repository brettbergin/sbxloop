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
import functools
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from sbxloop.api.agents import AGENTS_BY_SLUG, ANGIE_PERSONA
from sbxloop.api.artifacts import ArtifactCatalog
from sbxloop.api.auth.keys import SigningKeys
from sbxloop.api.auth.ratelimit import FailureLimiter
from sbxloop.api.auth.store import ApiAuthStore
from sbxloop.api.chronology import Chronology
from sbxloop.api.collaboration import CollaborationStore, LocalUser, Turn
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
        concierge: Any = None,
    ) -> None:
        self.config = config
        self.loop = loop
        self.auth = auth
        self.keys = keys
        self.concierge = concierge
        self.clock = clock
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.limiter = FailureLimiter()
        self.executor = ThreadPoolExecutor(
            max_workers=EXECUTOR_THREADS, thread_name_prefix="sbxloop-api-worker"
        )
        self.turn_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="sbxloop-collaboration"
        )
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_loop: asyncio.AbstractEventLoop | None = None
        self._public_ids: PublicIds | None = None
        self._chronology: Chronology | None = None
        self._artifacts: ArtifactCatalog | None = None
        self._collaboration: CollaborationStore | None = None
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

    @property
    def artifacts(self) -> ArtifactCatalog:
        """The artifact catalog over the daemon's stores, built on first use."""
        if self._artifacts is None:
            self._artifacts = ArtifactCatalog(
                self.loop.dstore,
                self.loop.store,
                self.config.paths,
                exclude=self.config.artifacts.exclude,
                clock=self.clock,
            )
        return self._artifacts

    @property
    def collaboration(self) -> CollaborationStore:
        """Product collaboration state over the daemon's one store."""
        if self._collaboration is None:
            self._collaboration = CollaborationStore(self.loop.dstore)
        return self._collaboration

    @property
    def collaboration_available(self) -> bool:
        return self.concierge is not None and not self.stopping.is_set()

    def start_collaboration_turn(
        self,
        turn: Turn,
        user: LocalUser,
        content: str,
        *,
        intent: str,
    ) -> None:
        """Run an accepted chat turn away from the HTTP event loop.

        Explicit agent targets are sequential today because the existing
        concierge owns one sandbox session executor. Each role still has its
        own durable session key, and every reply is recorded independently.
        """
        concierge = self.concierge
        if concierge is None:
            raise RuntimeError("the collaboration agent runtime is unavailable")

        def run() -> None:
            store = self.collaboration
            if not store.start_turn(turn.id, self.clock()):
                self.hub.notify()
                return
            preferences = store.list_preferences(user.id)
            preference_context = ""
            if preferences:
                joined = "\n\n".join(value.content.strip() for value in preferences)
                preference_context = f"\n\nUser preferences:\n\n{joined}"
            targets: tuple[str | None, ...] = tuple(turn.targets) or (None,)
            errors: list[str] = []
            author = user.full_name or user.username
            for target in targets:
                definition = AGENTS_BY_SLUG.get(target) if target else None
                persona = (definition.persona if definition else ANGIE_PERSONA) + preference_context
                # Mentioning a role is explicit delegation in Angie's UI.
                allow_actions = intent == "delegate" or definition is not None
                try:
                    future = concierge.submit_turn(
                        content,
                        author=author,
                        author_id=user.id,
                        via="local",
                        message_id=turn.input_message_id,
                        session_key=f"{turn.channel_id}:{target or 'angie'}",
                        persona=persona,
                        allow_actions=allow_actions,
                    )
                    reply = future.result()
                    if reply.ok and reply.text:
                        store.append_reply(
                            turn.id,
                            content=reply.text,
                            agent_slug=target,
                            now=self.clock(),
                        )
                        if reply.after is not None:
                            reply.after()
                    else:
                        errors.append(reply.error or f"@{target or 'angie'} did not answer")
                except BaseException as exc:
                    errors.append(str(exc)[:300] or type(exc).__name__)
                self.hub.notify()
            store.finish_turn(
                turn.id,
                error="; ".join(errors) if errors and len(errors) == len(targets) else None,
                now=self.clock(),
            )
            self.hub.notify()

        self.turn_executor.submit(run)

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

    async def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run ``fn`` on the executor, bounded; the event loop never
        touches a store or the loop's locks."""
        async with self._sem():
            running = asyncio.get_running_loop()
            call = functools.partial(fn, *args, **kwargs)
            return await running.run_in_executor(self.executor, call)

    def generation(self) -> str | None:
        return getattr(self.loop, "generation", None)

    def close(self) -> None:
        self.stopping.set()
        # Every live stream sees `stopping` on its next wake and ends.
        self.hub.notify()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.turn_executor.shutdown(wait=False, cancel_futures=True)
