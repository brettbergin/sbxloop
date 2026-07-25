"""Host-side event plumbing: a synchronous pub/sub bus over protocol Events."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from sbxloop_worker.protocol import Event

logger = logging.getLogger(__name__)

Subscriber = Callable[[Event], None]


@runtime_checkable
class Hook(Protocol):
    """Extension point: receives every event published on the bus.

    Implementations must be fast and must not raise; exceptions are caught
    and logged so one misbehaving hook cannot break a run.
    """

    def on_event(self, event: Event) -> None: ...


class EventBus:
    """Synchronous fan-out of protocol events to subscribers.

    Subscriber exceptions are isolated: they are logged and swallowed so
    telemetry consumers can never fail a run.
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

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
    RUN_ARTIFACTS = "run.artifacts"
    RUN_DELIVER = "run.deliver"
    RUN_END = "run.end"
    TASK_START = "task.start"
    TASK_STATE = "task.state"
    TASK_END = "task.end"
    PHASE_START = "phase.start"
    PHASE_END = "phase.end"
    SANDBOX_PROVISION_START = "sandbox.provision_start"
    SANDBOX_READY = "sandbox.ready"
    SANDBOX_PREBAKED = "sandbox.prebaked"
    SANDBOX_CLEANUP = "sandbox.cleanup"


__all__ = ["Event", "EventBus", "Hook", "HostEventTypes", "Subscriber"]
