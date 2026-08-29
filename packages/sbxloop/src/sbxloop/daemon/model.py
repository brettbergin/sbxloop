"""Daemon data models: the work item, what a finished run reports, and the
notices the loop narrates to a human channel."""

from __future__ import annotations

from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

from sbxloop.engine.model import RunState

# ``cancelled`` is an operator's decision (``!sbx cancel``), not a failure:
# it is terminal for the daemon (no retry, no breaker count) while the run
# itself stays resumable from the CLI. ``blocked`` is the run having cleared
# its own bar and GitHub refusing to finish the PR — terminal for the daemon
# (a human has to look), retryable by hand. ``failed`` covers both the
# attempt budget running out and an operator abandoning the item.
ItemState = Literal["queued", "running", "done", "failed", "blocked", "cancelled"]
# A decision the source has not been told about yet. ``abandoned`` /
# ``requeued`` are operator decisions from another process (the row-only
# CLI cannot report); ``merged`` / ``blocked`` are the run's own outcome,
# owed until the issue close / label actually landed on GitHub.
PendingReport = Literal["abandoned", "requeued", "merged", "blocked"]


class WorkItem(BaseModel):
    """One unit of discovered work and its dispatch bookkeeping.

    ``item_id`` is stable across polls (``gh:<issue#>``) so repeated
    discovery of the same issue dedups on it. ``claimed`` records that the
    source-side claim (label swap) already happened — a crash between claim
    and run start must not re-claim. ``requested_by`` is the Discord user
    who asked for the work through the concierge, when known, so the run's
    finish can ping them.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str
    source_key: str
    title: str
    body: str = ""
    url: str = ""
    state: ItemState = "queued"
    attempts: int = 0
    claimed: bool = False
    run_id: str | None = None
    last_error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    pending_report: PendingReport | None = None
    requested_by: str | None = None


class RunReport(NamedTuple):
    """What the daemon tells the source and the humans about a finished
    run — read from the engine's run record, which carries the PR."""

    run_id: str
    state: RunState
    task_summary: str
    # The pull request the run delivered: (number, url).
    pr: tuple[int, str] | None = None
    branch: str | None = None
    # Fix rounds spent (review + CI/gate/conflict/human).
    rounds: int = 0
    # Why the run stopped short of merged, when it did.
    reason: str | None = None
    workspace: str | None = None
    # Who asked for the cancel (``state`` is then ``cancelled`` from the
    # daemon's point of view even though the persisted run is still
    # resumable — the finish card tells the human how to continue it).
    cancelled_by: str | None = None
    # ``!sbx cancel --retry``: the item went straight back to the queue.
    requeued: bool = False

    @property
    def succeeded(self) -> bool:
        return self.state == "merged"


TickOutcome = Literal[
    "done",
    "retry",
    "failed",
    "blocked",
    "interrupted",
    "cancelled",
    "requeued",
]
IdleKind = Literal["paused", "breaker", "daily_cap", "backoff", "no_work"]


class TickResult(NamedTuple):
    """What one poll+dispatch cycle did, for logs and tests."""

    discovered: int = 0
    dispatched: str | None = None  # item_id that ran this tick
    outcome: TickOutcome | None = None
    idle_kind: IdleKind | None = None
    # Human detail for the idle kind (e.g. "3 queued; next eligible in 42s").
    idle_detail: str | None = None

    @property
    def idle_reason(self) -> str | None:
        if self.idle_kind is None:
            return None
        return f"{self.idle_kind} ({self.idle_detail})" if self.idle_detail else self.idle_kind


# What the loop narrates. Daemon-scoped kinds belong in the control channel;
# run-scoped kinds carry a ``run_id`` and belong in that run's thread, the
# terminal ones mirrored to the channel so a human who is not reading the
# thread still sees how it ended.
NoticeKind = Literal[
    "daemon.started",
    "daemon.stopped",
    "daemon.daily_cap",
    "daemon.gc",
    "daemon.state_archived",
    "daemon.version_drift",
    "breaker.opened",
    "breaker.half_open",
    "source.poll_recovered",
    "workspace.refreshed",
    "workspace.refresh_failed",
    "item.queued",
    "item.claim_failed",
    "item.abandoned",
    "item.requeued",
    "item.abandon_cancelling",
    "item.requeue_cancelling",
    "item.requeue_unpinned",
    "run.resuming",
    "run.resume_budget_exhausted",
    "run.done",
    "run.failed",
    "run.abandoned",
    "run.blocked",
    "run.cancelled",
    "run.requeued",
    "recovery.requeued",
    "recovery.settling",
    "recovery.resume_pending",
    "recovery.run_reconciled",
    "recovery.offline_abandon",
    "recovery.offline_requeue",
    "recovery.stale_sandbox_removed",
]

# Kinds a frontend should mirror to the control channel even when the
# notice has a run thread: the run's fate, which a human not reading the
# thread still needs to see.
TERMINAL_NOTICE_KINDS: frozenset[str] = frozenset(
    {
        "run.done",
        "run.failed",
        "run.abandoned",
        "run.blocked",
        "run.cancelled",
        "run.requeued",
        "item.abandoned",
    }
)

NoticeLevel = Literal["info", "warning", "error"]


class DaemonNotice(NamedTuple):
    """One thing the loop wants a human to know, addressed well enough for
    a frontend to route it (to a run's thread, or the control channel)."""

    kind: NoticeKind
    text: str
    item_id: str | None = None
    run_id: str | None = None
    url: str | None = None
    level: NoticeLevel = "info"
