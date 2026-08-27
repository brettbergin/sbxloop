"""Daemon data models: the work item and what a finished run reports."""

from __future__ import annotations

from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

from sbxloop.engine.model import RunState

ItemSource = Literal["github", "inbox"]
# What a run of the item is FOR. ``patch``: change the tree, deliver a PR.
# ``audit``: investigate and file findings as backlog issues — no delivery,
# the tree is disposable, the issues are the output (the discovery lane).
ItemKind = Literal["patch", "audit"]
# ``cancelled`` is an operator's decision (``!sbx cancel``), not a failure:
# it is terminal for the daemon (no retry, no breaker count) while the run
# itself stays resumable from the CLI.
# ``reviewing`` is delivered-but-not-accepted: the run succeeded and opened a
# PR, and the item stays in flight until that PR is green and its review is
# satisfied. It is NOT terminal — settling on "a PR exists" is how a red one
# (#389: mdformat and security failing) got marked done.
ItemState = Literal["queued", "running", "reviewing", "done", "failed", "abandoned", "cancelled"]
# An operator decision the source has not been told about yet: the row-only
# CLI (another process) cannot report, so the loop owes and delivers it.
PendingReport = Literal["abandoned", "requeued"]


class WorkItem(BaseModel):
    """One unit of discovered work and its dispatch bookkeeping.

    ``item_id`` is stable across polls (``gh:<issue#>`` / ``inbox:<name>``)
    so repeated discovery of the same source object dedups on it. ``claimed``
    records that the source-side claim (label swap / file rename) already
    happened — a crash between claim and run start must not re-claim.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str
    source: ItemSource
    source_key: str
    kind: ItemKind = "patch"
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


class ReviewOutcome(NamedTuple):
    """What a review run actually posted to its pull request.

    A review's deliverable is the review, not backlog issues: without this
    the summary layer had only ``filed`` to report on, and a review files
    none — so a ``REQUEST_CHANGES`` with eleven inline comments read as
    "no findings" (#469).

    ``requested_event`` is the verdict the reviewer asked for;
    ``posted_event`` is what GitHub accepted, which is ``COMMENT`` when the
    daemon identity is refused as a reviewer — and then ``gates_merge`` is
    False and nothing on the PR blocks the merge.
    """

    pr_number: int
    url: str
    requested_event: str
    posted_event: str
    comments: int
    gates_merge: bool

    @property
    def approved(self) -> bool:
        return self.requested_event == "APPROVE"


class RunReport(NamedTuple):
    """What the daemon tells the source about a finished run — mined from
    the run's persisted events, the same way the CLI finish summary is."""

    run_id: str
    state: RunState
    task_summary: str
    tracking_issue: tuple[int, str] | None = None
    delivery: tuple[int, str] | None = None
    delivery_error: str | None = None
    workspace: str | None = None
    # Who asked for the cancel (``state`` is then ``cancelled`` from the
    # daemon's point of view even though the persisted run is still
    # resumable — the finish card tells the human how to continue it).
    cancelled_by: str | None = None
    # ``!sbx cancel --retry``: the item went straight back to the queue.
    requeued: bool = False
    # Backlog issues the run filed (``gh:<n>`` refs) — an audit's deliverable.
    filed: tuple[str, ...] = ()
    # Findings addressed to sbxloop itself: filed upstream (``[daemon]
    # tool_repo``) as refs, or merely noted by title in the closing comment.
    tool_filed: tuple[str, ...] = ()
    tool_noted: tuple[str, ...] = ()
    # A review run's deliverable: the review it posted. None for every item
    # that is not a review, so patch and audit reporting is unchanged.
    review: ReviewOutcome | None = None
    # True when this item WAS a review but the post never reached the PR —
    # the review.json was missing or unparseable, no GitHub source was
    # wired, or the POST itself raised (`loop._post_review` swallows that
    # exception on purpose). Distinct from `review is None` on a non-review
    # item: without this, a lost review fell back to the audit lane's
    # "no findings" wording, telling the operator a PR was clean on exactly
    # the runs where a review was requested and never made it (#469 field
    # failure, loop.py:1004).
    review_failed: bool = False

    @property
    def succeeded(self) -> bool:
        return self.state == "completed" and self.delivery_error is None


TickOutcome = Literal[
    "done",
    "retry",
    "abandoned",
    "delivery_failed",
    "interrupted",
    "cancelled",
    "requeued",
    # The run finished and its PR is open; the item is now waiting on that
    # PR's checks and review rather than being done.
    "reviewing",
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
