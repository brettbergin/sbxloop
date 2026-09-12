"""What a control produced, as data.

Outcomes are pydantic models so a later durable record can serialise them
without a second schema; ``after`` (the effect a ``stop`` or ``restart``
defers until the reply is on its way) is excluded from that dump. A
refusal is a :class:`ControlError` with a stable ``code`` and the loop's
own sentence kept verbatim in ``message`` — the prose edge renders it the
way it always did, a JSON edge renders the code.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from sbxloop.daemon.model import WorkItem

ErrorCode = Literal[
    # The target names nothing the daemon knows.
    "unknown_target",
    # The request was well-formed but the target is not in a state the
    # action applies to; ``message`` says which state it is in.
    "not_eligible",
    # The action was refused outright by policy, or an argument is invalid.
    "invalid_argument",
    # A race lost: the effect already happened or is happening.
    "already_terminal",
    "already_in_progress",
    # The run kind never supports this control (a fixed tool recipe has
    # no steering, no fix rounds, no gate).
    "unsupported_for_kind",
    # A version-control backend could not say whether it does what the
    # action needs; "could not tell" is a refusal, not a guess.
    "capability_unknown",
    "capability_unsupported",
    # The principal lacks the capability the action needs.
    "forbidden",
    # No supervisor would start the daemon again.
    "unsupervised",
    # The daemon is not ready to take commands (recovery in progress).
    "daemon_not_ready",
]


class ControlError(Exception):
    """A refusal with a stable code and the sentence to show."""

    def __init__(self, code: ErrorCode, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.detail: dict[str, Any] = detail

    def __str__(self) -> str:
        return self.message


class Outcome(BaseModel):
    """Base of every typed result. Frozen: an outcome is a record.

    ``operation_id`` names the durable operation the effect was recorded
    under, when the surface records one (``None`` for a loop without an
    operation store — a test double)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str | None = None


class StatusOutcome(Outcome):
    status: dict[str, Any]


class PauseOutcome(Outcome):
    hold: str
    holds: list[str]


class ReleaseOutcome(Outcome):
    #: ``None`` when every hold was released (``--all``).
    hold: str | None
    holds: list[str]


class ReviewResumeOutcome(Outcome):
    """``resume <item|run>``: a review wait re-armed or a provider hold
    released — the loop's sentence says which."""

    target: str
    message: str


class CancelOutcome(Outcome):
    #: ``current``: the run in flight was asked to stop at its next
    #: boundary. ``provider``: a run parked on a provider outage was
    #: settled without ever touching a sandbox.
    mode: Literal["current", "provider"]
    retry: bool = False
    target: str | None = None
    message: str | None = None


class QueueOutcome(Outcome):
    items: list[WorkItem]


class ItemsOutcome(Outcome):
    items: list[WorkItem]


class GrantRoundsOutcome(Outcome):
    run_id: str
    rounds: int
    item_id: str


class RepoResumeOutcome(Outcome):
    repo: str
    health: dict[str, Any]


class ScheduleListOutcome(Outcome):
    rows: list[dict[str, Any]]


class ScheduleOutcome(Outcome):
    verb: Literal["add", "remove", "pause", "resume"]
    name: str
    message: str


class LogTailOutcome(Outcome):
    text: str


class StopOutcome(Outcome):
    #: Runs once the reply is on its way; a chat bridge must not be torn
    #: down under its own answer.
    after: Callable[[], None] = Field(exclude=True)


class RestartOutcome(Outcome):
    supervisor: str
    now: bool
    after: Callable[[], None] = Field(exclude=True)


class GateOutcome(Outcome):
    """A merge gate approved or a held result released; ``message`` is
    the loop's own sentence (it names the PR and the person)."""

    target: str
    message: str


class ItemOutcome(Outcome):
    verb: Literal["abandon", "retry", "requeue"]
    item: WorkItem
