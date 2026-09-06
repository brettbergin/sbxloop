"""Daemon data models: the work item, what a finished run reports, and the
notices the loop narrates to a human channel."""

from __future__ import annotations

from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, field_validator

from sbxloop.engine.model import Published, RunKind, RunState
from sbxloop.ghids import normalize_item_id

# ``cancelled`` is an operator's decision (``!sbx cancel``), not a failure:
# it is terminal for the daemon (no retry, no breaker count) while the run
# itself stays resumable from the CLI. ``blocked`` is the run having cleared
# its own bar and GitHub refusing to finish the PR — terminal for the daemon
# (a human has to look), retryable by hand. ``failed`` covers both the
# attempt budget running out and an operator abandoning the item. ``gated``
# is the opt-in merge gate (``[landing] merge_gate``): the run cleared every
# bar and parked awaiting one merge approval — a *waiting* state, not a
# terminal one (never superseded by rediscovery, invisible to dispatch),
# resolved by ``approve_merge`` or ``abandon`` — and the same row state
# holds a workload parked at publishing by `publish = "hold"` (#760), its
# gate row of kind ``publish``, released by ``approve_merge`` re-queueing
# the item with its run pinned. ``awaiting_review`` (#675)
# is the same shape for a base that requires an approving review the loop
# cannot give: the run stays pinned, the daemon polls the PR slowly, and a
# person on GitHub ends it. ``paused_review`` is that wait past
# ``[landing] review_wait_s``: nothing polls, the run stays pinned, and
# ``resume <item>`` puts the wait back up.
ItemState = Literal[
    "queued",
    "running",
    "done",
    "failed",
    "blocked",
    "cancelled",
    "gated",
    "awaiting_review",
    "paused_review",
]
# A decision the source has not been told about yet. ``abandoned`` /
# ``requeued`` are operator decisions from another process (the row-only
# CLI cannot report); ``merged`` / ``blocked`` are the run's own outcome,
# owed until the issue close / label actually landed on GitHub.
# ``gated`` is the park announcement: the issue gets the awaiting-merge
# label and the how-to-approve comment.
# ``completed`` is a workload's outcome (#760): its result published, the
# source told where it went. ``held`` is a workload parked at publishing by
# its profile's `publish = "hold"`: the source hears how to release it.
PendingReport = Literal["abandoned", "requeued", "merged", "blocked", "gated", "completed", "held"]


class WorkItem(BaseModel):
    """One unit of discovered work and its dispatch bookkeeping.

    ``item_id`` is stable across polls so repeated discovery of the same
    issue dedups on it. GitHub ids are *typed* — ``gh:issue:<n>`` for an
    issue, ``gh:pr:<n>`` for a pull request referenced as a work-item
    resource — and the grammar lives entirely in :mod:`sbxloop.ghids`
    (``format_gh_id`` / ``parse_gh_id``). The legacy bare form ``gh:<n>``
    is still accepted on read and normalised to ``gh:issue:<n>``; nothing
    new is ever written in that form. Ids from other sources (e.g.
    ``inbox:x.md``) pass through untouched. ``claimed`` records that the
    source-side claim (label swap) already happened — a crash between claim
    and run start must not re-claim. ``requested_by`` is the Discord user
    who asked for the work through the concierge, when known, so the run's
    finish can ping them. ``repo`` is the ``owner/name`` the item came from;
    it is optional so items persisted before multi-repo support still load,
    and readers fall back to the daemon's sole configured repository.
    ``kind`` is the run the item becomes (#760): a ``code`` run for a
    labelled issue, a ``workload`` for a chat ask or an issue carrying the
    workload label, under ``profile`` (a ``[[workloads]]`` name, or None
    for the configured default). Rows written before there were two kinds
    read as ``code``.
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
    repo: str | None = None
    # Earliest dispatch time for a queued item, when one applies: the retry
    # backoff of an exhausted run waiting to resume its own PR (#523). None
    # means the ordinary rules (attempt backoff; pinned resumes go first).
    not_before: float | None = None
    # The token of the claim comment this daemon posted (or is about to
    # post) for the item — persisted *before* the comment goes up (#530),
    # so a process that dies between the two leaves a row recovery can
    # settle: finish the claim if the comment landed, forget it if not.
    claim_token: str | None = None
    # What a previous attempt left on the GitHub origin (#600), carried
    # across a restart-by-label so the new run continues that branch/PR
    # instead of redoing the work. All None for an item that never ran.
    prior_run_id: str | None = None
    prior_branch: str | None = None
    prior_pr_number: int | None = None
    kind: RunKind = "code"
    profile: str | None = None

    @property
    def restarted(self) -> bool:
        """Whether this item is a previous attempt picked back up (#600)."""
        return bool(self.prior_run_id or self.prior_branch or self.prior_pr_number)

    @field_validator("item_id")
    @classmethod
    def _normalize_item_id(cls, value: str) -> str:
        """Legacy ``gh:<n>`` becomes ``gh:issue:<n>``; other ids pass through.

        Normalising here means a row loaded from a store written before the
        typed-id migration surfaces as a typed item without a schema
        migration, and every id the model hands out is canonical.
        """
        return normalize_item_id(value)


class TaskOutcome(NamedTuple):
    """One workload task on the finish card (#757): what it produced and
    what the judge made of it."""

    task_id: str
    title: str
    state: str
    summary: str
    files: int
    # ``passed`` / ``failed — unmet: …`` from the judge's last verdict, or
    # None for a task that was never judged (skipped, or the run died first).
    verdict: str | None


class RunReport(NamedTuple):
    """What the daemon tells the source and the humans about a finished
    run — read from the engine's run record, which carries the PR (a
    ``code`` run) or the tasks' outputs (a ``workload``)."""

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
    # Which run shape the cards render (#757): a workload's finish card
    # shows its tasks' outputs and verdicts where a code run's shows the PR.
    kind: RunKind = "code"
    outputs: tuple[TaskOutcome, ...] = ()
    # A workload's closing line (`engine.model.workload_summary`).
    summary: str | None = None
    # Where a workload's result went (#759), one entry per sink.
    published: tuple[Published, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.state == "merged"


TickOutcome = Literal[
    "done",
    "retry",
    "failed",
    "blocked",
    "gated",
    "awaiting_review",
    "held",
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
    "daemon.paused",
    "daemon.resumed",
    "daemon.daily_cap",
    "daemon.gc",
    "daemon.state_archived",
    "daemon.repoless_items_stranded",
    "daemon.version_drift",
    "daemon.schedule_fired",
    "daemon.schedule_skipped",
    "daemon.schedule_paused",
    "daemon.schedule_resumed",
    "breaker.opened",
    "breaker.half_open",
    "source.poll_recovered",
    "source.repo_suspended",
    "source.repo_recovered",
    "source.repo_resumed",
    "workspace.refreshed",
    "workspace.refresh_failed",
    "workspace.cloned",
    "item.queued",
    "item.claim_failed",
    "recovery.claim_settled",
    "item.abandoned",
    "item.requeued",
    "item.abandon_cancelling",
    "item.requeue_cancelling",
    "item.requeue_unpinned",
    "run.resuming",
    "run.resume_budget_exhausted",
    "run.exhausted",
    "run.rounds_granted",
    "run.done",
    "run.failed",
    "run.abandoned",
    "run.blocked",
    "run.gated",
    "run.awaiting_review",
    "run.held",
    "run.released",
    "run.review_paused",
    "run.review_resumed",
    "run.cancelled",
    "run.requeued",
    "gate.approved",
    "gate.merge_failed",
    "gate.dismissed",
    "review.approved",
    "review.ready",
    "review.reparked",
    "review.changes_requested",
    "review.merge_failed",
    "review.dismissed",
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
        "run.exhausted",
        "run.abandoned",
        "run.blocked",
        "run.gated",
        "run.awaiting_review",
        "run.held",
        "run.review_paused",
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
    # Chat user ids this notice is addressed to (#675): the frontend spells
    # each as a mention in front of the text and lets it ping.
    mention_ids: tuple[str, ...] = ()
