"""Daemon data models: the work item and what a finished run reports."""

from __future__ import annotations

from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

from sbxloop.engine.model import RunState

ItemSource = Literal["github", "inbox"]
ItemState = Literal["queued", "running", "done", "failed", "abandoned"]


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

    @property
    def succeeded(self) -> bool:
        return self.state == "completed" and self.delivery_error is None


TickOutcome = Literal["done", "retry", "abandoned", "delivery_failed", "interrupted"]
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
