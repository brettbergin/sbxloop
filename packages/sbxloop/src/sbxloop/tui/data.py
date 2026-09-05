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

from sbxloop.config import TUI_CONTROL_CHANNEL
from sbxloop.daemon.control import CommandReply
from sbxloop.daemon.mailbox import MailboxClient
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import ChatThread, MergeGate, ReviewHold, dispatch_eligible_at
from sbxloop.daemon.usage import RunUsage, usage_for_run
from sbxloop.engine.model import RunRecord, TaskRecord
from sbxloop.events import HostEventTypes
from sbxloop_worker.protocol import Event

#: A ``status`` question to the daemon: cheap (read-only verb), and the
#: ctl server refuses a request older than its start, so a short budget
#: says "no daemon" quickly without ever mis-executing.
STATUS_TIMEOUT_S = 3.0
#: The event types the Landing tab shows, newest of each — the host's own
#: names, each `land.*` kind on its own so a draft hold is not hidden
#: behind a later update.
LANDING_KINDS: tuple[str, ...] = (
    HostEventTypes.RUN_DELIVER,
    HostEventTypes.REVIEW_VERDICT,
    HostEventTypes.REVIEW_RECONCILED,
    HostEventTypes.FIX_ROUND,
    HostEventTypes.FIX_UNANSWERED,
    HostEventTypes.CI_STATUS,
    HostEventTypes.LANDING_CHECKS,
    HostEventTypes.LAND_UNDRAFT,
    HostEventTypes.LAND_HELD_BY_DRAFT,
    HostEventTypes.LAND_UPDATE,
    HostEventTypes.LAND_ENQUEUED,
    HostEventTypes.LAND_DEQUEUED,
    HostEventTypes.LAND_HUMAN_ACK,
    HostEventTypes.LAND_HUMAN_ACK_CAPPED,
    HostEventTypes.LAND_BOT_STANDING,
    HostEventTypes.RUN_GATED,
    HostEventTypes.RUN_AWAITING_REVIEW,
    HostEventTypes.RUN_MERGED,
    HostEventTypes.RUN_FOLLOWUPS,
    HostEventTypes.RUN_BLOCKED,
    HostEventTypes.RUN_RECONCILED,
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
    """Ask the daemon ``status``. ``None`` is no daemon; a stale refusal is
    a daemon still starting; a *pending* reply is a daemon that took the
    request but was too busy to answer in time — alive, not down (the
    misread the ctl queue's own docstring warns about)."""
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
    if reply.pending:
        return DaemonSnapshot(
            True, False, None, latency, now, error="busy: status not answered in time"
        )
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
    """Every run with its item and repository — a few queries, not one per
    run: the daemon's ledger maps runs to items, and an item's pinned run
    wins for the item it currently belongs to."""
    runs = tuple(mailbox.runs(limit=limit))
    items = mailbox.items()
    item_by_run = mailbox.run_items()
    repo_by_item: dict[str, str] = {}
    for item in items:
        if item.repo:
            repo_by_item[item.item_id] = item.repo
        if item.run_id:
            item_by_run[item.run_id] = item.item_id
    last = mailbox.last_event_ts_many([r.run_id for r in runs[:30]])
    return RunsSnapshot(runs, item_by_run, repo_by_item, last)


@dataclass(frozen=True)
class ItemsSnapshot:
    queued: tuple[WorkItem, ...]  # in the daemon's dispatch order
    eligible_at: dict[str, float]  # item id -> when the dispatch rule lets it go
    items: tuple[WorkItem, ...]
    gates: tuple[MergeGate, ...]
    holds: tuple[ReviewHold, ...]


def build_items(mailbox: MailboxClient, *, retry_backoff_s: float) -> ItemsSnapshot:
    """The queue as the daemon will take it: its own order and its own
    eligibility rule (`dispatch_eligible_at`), never re-derived here."""
    items = tuple(mailbox.items())
    queued = tuple(mailbox.queued_in_order())
    eligible = {i.item_id: dispatch_eligible_at(i, retry_backoff_s) for i in queued}
    return ItemsSnapshot(queued, eligible, items, tuple(mailbox.gates()), tuple(mailbox.holds()))


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
    usage: RunUsage | None = None


def build_run_detail(
    mailbox: MailboxClient, run_id: str, *, previous: RunDetail | None = None
) -> RunDetail | None:
    """``previous`` is the last snapshot: its usage fold (a scan of every
    ``agent.usage`` event) is reused while the run's event tail has not
    moved, so a refresh tick costs a few indexed reads, not a scan."""
    record = mailbox.run(run_id)
    if record is None:
        return None
    item_id = mailbox.item_for_run(run_id)
    item = mailbox.item(item_id) if item_id else None
    gate = next((g for g in mailbox.gates(("open", "approving")) if g.run_id == run_id), None)
    hold = next((h for h in mailbox.holds() if h.run_id == run_id), None)
    landing: list[Event] = []
    for kind in LANDING_KINDS:
        last = mailbox.last_event(run_id, kind)
        if last is not None:
            landing.append(last)
    landing.sort(key=lambda e: e.ts)
    last_event_ts = mailbox.last_event_ts(run_id)
    if (
        previous is not None
        and previous.usage is not None
        and previous.last_event_ts == last_event_ts
    ):
        usage = previous.usage
    else:
        with mailbox.read_engine() as engine:
            usage = usage_for_run(engine, run_id)
    return RunDetail(
        record=record,
        tasks=tuple(mailbox.tasks(run_id)),
        phases=tuple(mailbox.phase_attempts(run_id)),
        item=item,
        gate=gate,
        hold=hold,
        thread=mailbox.thread_for_run(run_id),
        last_event_ts=last_event_ts,
        landing_events=tuple(landing),
        usage=usage,
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
        """Rows after the cursor. The cursor moves only in :meth:`commit`,
        once the rows were rendered: a worker superseded mid-pull must not
        advance it and lose its rows."""
        out: list[tuple[int, Event]] = []
        for seq, event in self.mailbox.events(
            self.run_id, after_seq=self.after_seq, type_prefix=self.type_prefix
        ):
            out.append((seq, event))
            if len(out) >= limit:
                break
        return out

    def commit(self, rows: list[tuple[int, Event]]) -> None:
        if rows:
            self.after_seq = max(self.after_seq, rows[-1][0])

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
    # Control-channel rows newer than the one the Chat screen last showed.
    control_unread: int = 0

    @property
    def bridge_alive(self) -> bool:
        return self.heartbeat is not None and time.time() - self.heartbeat <= 15.0


def build_state(
    mailbox: MailboxClient,
    previous: ConsoleState,
    *,
    now: float,
    retry_backoff_s: float = 900.0,
    control_seen: int = 0,
) -> ConsoleState:
    """One refresh: every list the screens share, from one worker pass."""
    state = ConsoleState(
        daemon=previous.daemon,
        version=previous.version,
        read_only=previous.read_only,
    )
    try:
        state.runs = build_runs(mailbox)
        state.items = build_items(mailbox, retry_backoff_s=retry_backoff_s)
        state.heartbeat = mailbox.heartbeat()
        state.daemon_started_at = mailbox.daemon_started_at()
        state.control_unread = mailbox.count_after(TUI_CONTROL_CHANNEL, control_seen)
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
    "LANDING_KINDS",
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
