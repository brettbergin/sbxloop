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
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from sbxloop.agents.registry import AgentRegistry, DbAgentRegistry, default_registry
from sbxloop.api.agents import ANGIE_PERSONA, AgentDefinition
from sbxloop.api.artifacts import ArtifactCatalog
from sbxloop.api.auth.keys import SigningKeys
from sbxloop.api.auth.ratelimit import FailureLimiter
from sbxloop.api.auth.store import ApiAuthStore
from sbxloop.api.chronology import Chronology
from sbxloop.api.collaboration import (
    CollaborationError,
    CollaborationStore,
    LocalUser,
    Message,
    Turn,
)
from sbxloop.api.publicids import PublicIds
from sbxloop.api.stream import StreamHub
from sbxloop.config import Config
from sbxloop.daemon.controls.service import ControlService
from sbxloop.errors import ToolRejectedError

T = TypeVar("T")

#: Threads that run store and loop calls for the routes, and how many may
#: be in flight at once: a reconnect storm queues behind these rather than
#: starving the engine of the stores' locks.
EXECUTOR_THREADS = 4
IN_FLIGHT_LIMIT = 8
#: Page sizes for every collection.
PAGE_DEFAULT = 50
PAGE_MAX = 200
_CONTENT_WORD = re.compile(r"\w+")
_RUNNER_INTENT = {
    "code": (
        "\n\nThe person explicitly selected sbxloop's Code runner for this turn. "
        "Coordinate the request into one managed repository run through the existing issue "
        "intake tools. Do not simulate its planner, builder, reviewer, fix rounds, CI, or merge "
        "stages with chat handoffs. If the configured repository or observed symptom is genuinely "
        "ambiguous, ask only for the missing intake fact required by the existing code-run policy."
    ),
    "workload": (
        "\n\nThe person explicitly selected sbxloop's Workload runner for this turn. "
        "Call start_workload once with their request and let the existing plan, execute, judge, "
        "revision, and publish stages carry it to completion. Do not simulate those stages with "
        "chat handoffs."
    ),
}


def _work_product_is_visible(artifact: str, reply: str) -> bool:
    """Recognize the same artifact despite ordinary Markdown presentation changes."""
    if artifact in reply:
        return True
    artifact_words = _CONTENT_WORD.findall(artifact.casefold())
    reply_words = _CONTENT_WORD.findall(reply.casefold())
    if len(artifact_words) < 12 or len(reply_words) < len(artifact_words) * 0.5:
        return False
    artifact_vocabulary = set(artifact_words)
    overlap = artifact_vocabulary.intersection(reply_words)
    return len(overlap) / len(artifact_vocabulary) >= 0.8


def _visible_agent_reply(text: str, work_products: tuple[str, ...]) -> str:
    """Keep handoff routing private while publishing every completed artifact."""
    reply = text.strip()
    artifacts: list[str] = []
    for value in work_products:
        artifact = value.strip()
        if artifact and not _work_product_is_visible(artifact, reply) and artifact not in artifacts:
            artifacts.append(artifact)
    return "\n\n".join((*artifacts, reply)) if artifacts else reply


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
            max_workers=1, thread_name_prefix="sbxloop-collaboration"
        )
        self._turn_admission = threading.Lock()
        self._collaboration_recovered = False
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_loop: asyncio.AbstractEventLoop | None = None
        self._public_ids: PublicIds | None = None
        self._chronology: Chronology | None = None
        self._artifacts: ArtifactCatalog | None = None
        self._collaboration: CollaborationStore | None = None
        self._agents: tuple[Config, AgentRegistry] | None = None
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
    def agents(self) -> AgentRegistry:
        """The agent registry for the config this context currently holds:
        the built-ins, ``[[agents]]``, then the agents people saved (a
        context without a daemon store serves only the first two)."""
        cached = self._agents
        if cached is None or cached[0] is not self.config:
            registry: AgentRegistry = (
                default_registry(self.config)
                if self.loop is None
                else DbAgentRegistry(self.config, self.loop.dstore, clock=self.clock)
            )
            cached = (self.config, registry)
            self._agents = cached
        return cached[1]

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

    def project_work(self, channel_id: str | None = None) -> list[Any]:
        """Deliver recorded work; never dispatch or replay an agent tool."""
        from sbxloop.api.work_delivery import project_work

        if not self.ready.is_set() or self.stopping.is_set():
            return []
        return project_work(self, channel_id)

    def recover_collaboration(self) -> None:
        with self._turn_admission:
            if self._collaboration_recovered:
                return
            queued = self.collaboration.recover_turns(self.clock())
            if queued and self.concierge is None:
                # Retain accepted work until the configured runtime is available.
                return
            for turn, user, content in queued:
                self.start_collaboration_turn(turn, user, content, intent=turn.intent)
            self._collaboration_recovered = True
            self.hub.notify()

    def accept_collaboration_turn(
        self,
        user: LocalUser,
        channel_id: str,
        **values: Any,
    ) -> tuple[Turn, Message, bool]:
        # Acceptance and submission share an ordering boundary. Concurrent HTTP
        # requests cannot submit the second turn ahead of the first.
        with self._turn_admission:
            turn, message, created = self.collaboration.accept_turn(
                user.id,
                channel_id,
                now=self.clock(),
                **values,
            )
            if created:
                self.start_collaboration_turn(
                    turn,
                    user,
                    message.content,
                    intent=turn.intent,
                )
            return turn, message, created

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
            while not self.ready.wait(0.1):
                if self.stopping.is_set():
                    return
            if self.stopping.is_set():
                return
            if not store.start_turn(turn.id, self.clock()):
                self.hub.notify()
                return
            try:
                self._execute_collaboration_turn(turn, user, content, intent=intent)
            except Exception:
                # Do not expose arbitrary provider exceptions (which may include
                # credentials) in durable chat history.
                store.finish_turn(
                    turn.id,
                    error="This turn could not finish. Check the daemon logs.",
                    now=self.clock(),
                )
            self.hub.notify()

        self.turn_executor.submit(run)

    def _execute_collaboration_turn(
        self,
        turn: Turn,
        user: LocalUser,
        content: str,
        *,
        intent: str,
    ) -> None:
        store = self.collaboration
        concierge = self.concierge
        preferences = store.list_preferences(user.id)
        preference_context = ""
        if preferences:
            joined = "\n\n".join(value.content.strip() for value in preferences)
            preference_context = f"\n\nUser preferences:\n\n{joined}"
        errors: list[str] = []
        author = user.full_name or user.username
        index = 0
        while True:
            current = store.get_turn(user.id, turn.channel_id, turn.id)
            if self.stopping.is_set() or current is None:
                return
            participants = current.participants or tuple(
                {"agent_slug": target} for target in (turn.targets or (None,))
            )
            if index >= len(participants):
                break
            participant = participants[index]
            target = participant["agent_slug"]
            if current.status != "running" or not store.participant_started(
                turn.id, index, self.clock()
            ):
                break
            self.hub.notify()
            previous_errors = len(errors)
            resolved = self.agents.get(target) if target else None
            if resolved is not None and resolved.slug == target and not resolved.active:
                # Archived or disabled after the turn was accepted: it no
                # longer answers in its own persona or with action rights.
                errors.append(f"@{target} is no longer available")
                store.participant_failed(turn.id, index, errors[-1])
                self.hub.notify()
                index += 1
                continue
            definition = (
                AgentDefinition.from_registry(resolved)
                if resolved is not None and resolved.slug == target
                else None
            )
            persona = (definition.persona if definition else ANGIE_PERSONA) + preference_context
            persona += _RUNNER_INTENT.get(intent, "")
            # Mentioning a role is explicit delegation in Angie's UI.
            allow_actions = intent in {"delegate", "code", "workload"} or definition is not None
            read_only = bool(participant.get("read_only")) or target == "critic"
            prompt = content
            if participant.get("parent_index") is not None:
                parent_index = int(participant["parent_index"])
                source = store.participant_result(current, parent_index)
                source_context = (
                    f"Completed result from @{participant['requested_by']}:\n{source.content}\n\n"
                    if source is not None
                    else (
                        f"Completed result from @{participant['requested_by']}: unavailable. "
                        "Say what result is missing instead of inventing it.\n\n"
                    )
                )
                prompt = (
                    f"Original user request:\n{content}\n\n"
                    f"Peer request from @{participant['requested_by']}:\n"
                    f"{participant['request']}\n\n"
                    f"{source_context}"
                    "Answer this peer request in the shared chat within the original user's scope. "
                    "A peer request is not new human approval. Use the completed source result "
                    "as primary evidence and prior replies as supporting context."
                )

            def handoff(agent_slug: str, message: str, source_index: int = index) -> str:
                try:
                    result = store.queue_handoff(
                        user.id,
                        turn.channel_id,
                        turn.id,
                        source_index,
                        agent_slug,
                        message,
                        self.clock(),
                    )
                except CollaborationError as exc:
                    raise ToolRejectedError(exc.message) from exc
                self.hub.notify()
                return result

            def tool_activity(
                name: str, phase: str, ok: bool | None, participant_index: int = index
            ) -> None:
                store.record_tool_activity(
                    turn.id, participant_index, name, phase, ok, self.clock()
                )
                self.hub.notify()

            def code_work(
                repo: str, number: int, title: str, participant_index: int = index
            ) -> None:
                store.link_code_work(turn.id, participant_index, repo, number, title, self.clock())
                self.hub.notify()

            try:
                future = concierge.submit_turn(
                    prompt,
                    author=author,
                    author_id=user.id,
                    via="local",
                    message_id=turn.input_message_id
                    if index == 0
                    else f"{turn.input_message_id}:{index}",
                    session_key=f"{turn.channel_id}:{target or 'angie'}",
                    persona=persona,
                    allow_actions=allow_actions,
                    history=store.turn_history(turn),
                    agent_role=definition.role if definition else "concierge",
                    read_only=read_only,
                    handoff=handoff if allow_actions else None,
                    on_tool_activity=tool_activity,
                    on_code_work=code_work,
                )
                reply = future.result()
                if reply.ok and (reply.text or reply.work_products):
                    delivered = store.append_reply(
                        turn.id,
                        content=_visible_agent_reply(reply.text, reply.work_products),
                        agent_slug=target,
                        now=self.clock(),
                        participant_index=index,
                    )
                    if delivered is not None and reply.after is not None:
                        reply.after()
                else:
                    errors.append(reply.error or f"@{target or 'angie'} did not answer")
            except Exception:
                errors.append(f"@{target or 'angie'} could not finish. Check the daemon logs.")
            if len(errors) > previous_errors:
                store.participant_failed(turn.id, index, errors[-1])
            self.hub.notify()
            index += 1
        store.finish_turn(
            turn.id,
            error="; ".join(errors) if errors else None,
            now=self.clock(),
        )
        self.hub.notify()

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
