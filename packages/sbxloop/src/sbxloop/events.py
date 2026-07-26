"""Host-side event plumbing: a synchronous pub/sub bus over protocol Events."""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from sbxloop_worker.protocol import Event

logger = logging.getLogger(__name__)

Subscriber = Callable[[Event], None]


@runtime_checkable
class Hook(Protocol):
    """Extension point: receives every event published on the bus.

    Exceptions are contained: ``EventBus.publish`` catches and logs anything
    a hook raises, so a misbehaving hook can never break a run and hook
    authors need no defensive try/except of their own. Hooks run
    synchronously on the publishing thread, though, so keep them fast.
    """

    def on_event(self, event: Event) -> None: ...


class EventBus:
    """Synchronous fan-out of protocol events to subscribers.

    Subscriber exceptions are isolated: they are logged and swallowed so
    telemetry consumers can never fail a run.

    Publishing is thread-safe: parallel provisioning/install threads emit
    concurrently, and the lock keeps subscriber invocations serialized so
    consumers that are not thread-safe themselves (the SQLite persister,
    console printers) never see two events at once. Reentrant (a subscriber
    may emit) — hooks still run synchronously on the publishing thread.
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._publish_lock = threading.RLock()

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        """Register a subscriber; returns an unsubscribe callable."""
        self._subscribers.append(fn)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(fn)

        return unsubscribe

    def attach_hook(self, hook: Hook) -> Callable[[], None]:
        return self.subscribe(hook.on_event)

    def publish(self, event: Event) -> None:
        with self._publish_lock:
            for fn in list(self._subscribers):
                try:
                    fn(event)
                except Exception:
                    logger.exception("event subscriber %r failed for %s", fn, event.type)

    def emit(
        self,
        type: str,
        run_id: str,
        job_id: str | None = None,
        **data: Any,
    ) -> Event:
        """Construct an Event stamped now, publish it, and return it."""
        event = Event.now(type, run_id, job_id=job_id, **data)
        self.publish(event)
        return event


class HostEventTypes:
    """Host-emitted event types (the worker's live in protocol.EventTypes)."""

    RUN_START = "run.start"
    RUN_STATE = "run.state"
    RUN_CONFIG_DRIFT = "run.config_drift"
    RUN_ARTIFACTS = "run.artifacts"
    RUN_DELIVER = "run.deliver"
    RUN_KEEP = "run.keep"
    RUN_END = "run.end"
    TASK_START = "task.start"
    TASK_STATE = "task.state"
    TASK_END = "task.end"
    PHASE_START = "phase.start"
    PHASE_END = "phase.end"
    POLICY_ALLOW = "policy.allow"
    POLICY_DENY = "policy.deny"
    SANDBOX_PROVISION_START = "sandbox.provision_start"
    SANDBOX_READY = "sandbox.ready"
    SANDBOX_PREBAKED = "sandbox.prebaked"
    SANDBOX_CLEANUP = "sandbox.cleanup"
    # Interactive chat: a user message entering the loop, the agent's reply,
    # and the resulting course change (when the reply's action was not
    # "continue").
    CHAT_MESSAGE = "chat.message"
    CHAT_REPLY = "chat.reply"
    CHAT_ACTION = "chat.action"


__all__ = ["Event", "EventBus", "Hook", "HostEventTypes", "Subscriber"]
