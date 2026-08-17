"""Host-side event plumbing: a synchronous pub/sub bus over protocol Events."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from sbxloop.log import get_logger
from sbxloop_worker.protocol import Event

log = get_logger(__name__)

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
                    log.exception("event subscriber %r failed for %s", fn, event.type)

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


# The data keys worth surfacing in a one-line summary, first present wins:
# the "what happened" of the event (a state, some text, a tool, an outcome …).
SUMMARY_KEYS: tuple[str, ...] = (
    "state",
    "content",
    "text",
    "reply",
    "tool",
    "op",
    "line",
    "message",
    "outcome",
    "error",
    "url",
    "path",
)
SUMMARY_CLIP = 160


def _clip(value: Any, limit: int) -> str:
    flat = " ".join(str(value).split())
    if len(flat) <= limit:
        return flat
    keep = (limit - 1) // 2
    return f"{flat[:keep]}…{flat[-keep:]}"


def summarize_event(event: Event) -> dict[str, Any]:
    """The dense one-line reading of an event as structured fields — what
    ``sbxloop logs`` prints and what the daemon's log sink emits: task/agent
    ids, the first present :data:`SUMMARY_KEYS` value (as ``summary``, with
    the key it came from as ``summary_key``), the resource snapshot, tool
    args and any error — each clipped to a display line."""
    data = event.data
    out: dict[str, Any] = {}
    if data.get("task_id"):
        out["task"] = data["task_id"]
    if data.get("agent"):
        out["agent"] = data["agent"]
    picked = ""
    for key in SUMMARY_KEYS:
        if data.get(key):
            picked = key
            out["summary_key"] = key
            out["summary"] = str(data[key]).replace("\n", " ")[:SUMMARY_CLIP]
            break
    if data.get("disk_used_pct") is not None:
        out["disk"] = f"{data['disk_used_pct']}%"
        if data.get("mem_used_pct") is not None:
            out["mem"] = f"{data['mem_used_pct']}%"
        if data.get("load1") is not None:
            out["load"] = data["load1"]
        if data.get("level"):
            out["resource_level"] = data["level"]
    if data.get("args"):
        out["args"] = _clip(data["args"], 120)
    if picked != "error" and data.get("error"):
        out["error"] = _clip(data["error"], SUMMARY_CLIP)
    return out


class HostEventTypes:
    """Host-emitted event types (the worker's live in protocol.EventTypes)."""

    RUN_START = "run.start"
    RUN_STATE = "run.state"
    RUN_CONFIG_DRIFT = "run.config_drift"
    RUN_ARTIFACTS = "run.artifacts"
    RUN_DELIVER = "run.deliver"
    RUN_REPORT = "run.report"
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
    # A gc sweep removed the run's on-disk payload (runs/<run>/); recorded on
    # the run so `logs` explains the missing directory and `resume` refuses.
    DAEMON_GC = "daemon.gc"


__all__ = [
    "SUMMARY_KEYS",
    "Event",
    "EventBus",
    "Hook",
    "HostEventTypes",
    "Subscriber",
    "summarize_event",
]
