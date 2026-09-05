"""What the console reads, as plain snapshots.

Every read goes through :class:`~sbxloop.daemon.mailbox.MailboxClient`
(read-only) and every daemon question through the ``ctl`` queue
(:class:`~sbxloop.daemon.control.ControlClient`), off the UI thread; the
screens only ever see the frozen snapshots built here.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from sbxloop.daemon.control import CommandReply
from sbxloop.daemon.mailbox import MailboxClient
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import ChatThread, MergeGate, ReviewHold
from sbxloop.engine.model import RunRecord, TaskRecord
from sbxloop_worker.protocol import Event

#: A ``status`` question to the daemon: cheap (read-only verb), and the
#: ctl server refuses a request older than its start, so a short budget
#: says "no daemon" quickly without ever mis-executing.
STATUS_TIMEOUT_S = 3.0
#: The event types the Landing tab folds, newest of each.
LANDING_PREFIXES: tuple[str, ...] = (
    "run.deliver",
    "review.verdict",
    "review.reconciled",
    "fix.round",
    "ci.status",
    "landing.checks",
    "land.",
    "run.gated",
    "run.awaiting_review",
    "run.merged",
    "run.blocked",
)


class CtlClient(Protocol):
    def submit(self, cmd: str, *, timeout_s: float = ...) -> CommandReply | None: ...


@dataclass(frozen=True)
class DaemonSnapshot:
    """What the daemon said to ``status``, and how the question went."""

    live: bool
    starting: bool
    status: dict[str, Any] | None
    latency_ms: int
    checked_at: float
    error: str | None = None

    @property
    def current_run(self) -> str | None:
        current = (self.status or {}).get("current") or {}
        run = current.get("run_id")
        return str(run) if run else None


def probe_daemon(
    ctl: CtlClient, *, now: float, timeout_s: float = STATUS_TIMEOUT_S
) -> DaemonSnapshot:
    t0 = time.monotonic()
    try:
        reply = ctl.submit("status", timeout_s=timeout_s)
    except Exception as exc:
        return DaemonSnapshot(False, False, None, 0, now, error=str(exc))
    latency = int((time.monotonic() - t0) * 1000)
    if reply is None:
        return DaemonSnapshot(False, False, None, latency, now)
    if reply.stale:
        return DaemonSnapshot(False, True, None, latency, now)
    return DaemonSnapshot(bool(reply.ok), False, reply.status, latency, now)


@dataclass(frozen=True)
class RunsSnapshot:
    runs: tuple[RunRecord, ...]
    item_by_run: dict[str, str]
    repo_by_item: dict[str, str]
    last_event_by_run: dict[str, float]

    def item_for(self, run_id: str) -> str | None:
        return self.item_by_run.get(run_id)

    def repo_for(self, run_id: str) -> str | None:
        item = self.item_by_run.get(run_id)
        return self.repo_by_item.get(item) if item else None


def build_runs(mailbox: MailboxClient, *, limit: int = 200) -> RunsSnapshot:
    runs = tuple(mailbox.runs(limit=limit))
    items = mailbox.items()
    item_by_run: dict[str, str] = {}
    repo_by_item: dict[str, str] = {}
    for item in items:
        if item.repo:
            repo_by_item[item.item_id] = item.repo
        if item.run_id:
            item_by_run[item.run_id] = item.item_id
    for record in runs:
        if record.run_id not in item_by_run:
            found = mailbox.item_for_run(record.run_id)
            if found:
                item_by_run[record.run_id] = found
    last: dict[str, float] = {}
    for record in runs[:30]:
        ts = mailbox.last_event_ts(record.run_id)
        if ts is not None:
            last[record.run_id] = ts
    return RunsSnapshot(runs, item_by_run, repo_by_item, last)


@dataclass(frozen=True)
class ItemsSnapshot:
    queued: tuple[WorkItem, ...]
    items: tuple[WorkItem, ...]
    gates: tuple[MergeGate, ...]
    holds: tuple[ReviewHold, ...]


def build_items(mailbox: MailboxClient) -> ItemsSnapshot:
    items = tuple(mailbox.items())
    queued = tuple(sorted((i for i in items if i.state == "queued"), key=lambda i: i.created_at))
    return ItemsSnapshot(queued, items, tuple(mailbox.gates()), tuple(mailbox.holds()))


@dataclass(frozen=True)
class RunDetail:
    record: RunRecord
    tasks: tuple[TaskRecord, ...]
    phases: tuple[sqlite3.Row, ...]
    item: WorkItem | None
    gate: MergeGate | None
    hold: ReviewHold | None
    thread: ChatThread | None
    last_event_ts: float | None
    landing_events: tuple[Event, ...]


def build_run_detail(mailbox: MailboxClient, run_id: str) -> RunDetail | None:
    record = mailbox.run(run_id)
    if record is None:
        return None
    item_id = mailbox.item_for_run(run_id)
    item = mailbox.item(item_id) if item_id else None
    gate = next((g for g in mailbox.gates(("open", "approving")) if g.run_id == run_id), None)
    hold = next((h for h in mailbox.holds() if h.run_id == run_id), None)
    landing: list[Event] = []
    for prefix in LANDING_PREFIXES:
        last: Event | None = None
        for _seq, event in mailbox.events(run_id, type_prefix=prefix):
            last = event
        if last is not None:
            landing.append(last)
    landing.sort(key=lambda e: e.ts)
    return RunDetail(
        record=record,
        tasks=tuple(mailbox.tasks(run_id)),
        phases=tuple(mailbox.phase_attempts(run_id)),
        item=item,
        gate=gate,
        hold=hold,
        thread=mailbox.thread_for_run(run_id),
        last_event_ts=mailbox.last_event_ts(run_id),
        landing_events=tuple(landing),
    )


class EventTail:
    """A cursor over a run's persisted events, the same ``seq`` tail
    ``sbxloop logs --follow`` uses."""

    def __init__(
        self, mailbox: MailboxClient, run_id: str, *, type_prefix: str | None = None
    ) -> None:
        self.mailbox = mailbox
        self.run_id = run_id
        self.type_prefix = type_prefix
        self.after_seq = 0

    def pull(self, *, limit: int = 2000) -> list[tuple[int, Event]]:
        out: list[tuple[int, Event]] = []
        for seq, event in self.mailbox.events(
            self.run_id, after_seq=self.after_seq, type_prefix=self.type_prefix
        ):
            out.append((seq, event))
            self.after_seq = seq
            if len(out) >= limit:
                break
        return out

    def reset(self, *, type_prefix: str | None = None) -> None:
        self.type_prefix = type_prefix
        self.after_seq = 0


@dataclass
class ConsoleState:
    """Everything the screens render, replaced snapshot by snapshot."""

    daemon: DaemonSnapshot | None = None
    runs: RunsSnapshot | None = None
    items: ItemsSnapshot | None = None
    heartbeat: float | None = None
    daemon_started_at: float | None = None
    version: str = ""
    read_only: bool = False
    refreshed_at: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def bridge_alive(self) -> bool:
        return self.heartbeat is not None and time.time() - self.heartbeat <= 15.0


def build_state(mailbox: MailboxClient, previous: ConsoleState, *, now: float) -> ConsoleState:
    """One refresh: every list the screens share, from one worker pass."""
    state = ConsoleState(
        daemon=previous.daemon,
        version=previous.version,
        read_only=previous.read_only,
    )
    try:
        state.runs = build_runs(mailbox)
        state.items = build_items(mailbox)
        state.heartbeat = mailbox.heartbeat()
        state.daemon_started_at = mailbox.daemon_started_at()
    except Exception as exc:
        state.errors.append(str(exc))
        state.runs = previous.runs
        state.items = previous.items
    state.refreshed_at = now
    return state


RefreshHook = Callable[[ConsoleState], None]


def iter_events(mailbox: MailboxClient, run_id: str) -> Iterator[Event]:
    for _seq, event in mailbox.events(run_id):
        yield event


__all__ = [
    "LANDING_PREFIXES",
    "STATUS_TIMEOUT_S",
    "ConsoleState",
    "CtlClient",
    "DaemonSnapshot",
    "EventTail",
    "ItemsSnapshot",
    "RunDetail",
    "RunsSnapshot",
    "build_items",
    "build_run_detail",
    "build_runs",
    "build_state",
    "probe_daemon",
]
