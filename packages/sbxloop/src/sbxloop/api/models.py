"""The public shapes: what a client sends and what it reads back.

Every resource carries ``id``, ``workspace_id`` and RFC 3339 UTC
timestamps. Nothing here exposes a host path, a database row, or a
credential. ``available_actions`` on a read is advice for a UI; the server
rechecks eligibility when the action arrives.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from sbxloop.daemon.controls.operations import Operation
from sbxloop.daemon.controls.principal import WORKSPACE_ID, Capability


def rfc3339(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Health(ApiModel):
    status: Literal["ok"] = "ok"


class Readiness(ApiModel):
    ready: bool
    generation: str | None = None
    #: Rows the public chronology is behind the engine's own; ``None`` until
    #: the projection exists.
    projection_lag: int | None = None


class Limits(ApiModel):
    page_default: int
    page_max: int
    max_body_bytes: int
    max_stream_clients: int
    auth_failures_per_minute: int
    auth_lockout_s: int


class Retention(ApiModel):
    replay_s: int
    idempotency_s: int
    operation_deadline_s: int


class Capabilities(ApiModel):
    contract_version: int = 1
    server_version: str
    workspace_id: str = WORKSPACE_ID
    features: list[str]
    run_kinds: list[str] = Field(default_factory=lambda: ["code", "workload", "tool"])
    capabilities: list[str]
    limits: Limits
    retention: Retention


class Actor(ApiModel):
    kind: str
    id: str
    display: str | None = None
    via: str


class Target(ApiModel):
    kind: str
    id: str


class OperationOut(ApiModel):
    id: str
    workspace_id: str = WORKSPACE_ID
    action: str
    target: Target
    state: str
    effect: str
    actor: Actor
    request: dict[str, Any]
    accepted_at: str
    claimed_at: str | None = None
    finished_at: str | None = None
    expires_at: str | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_detail: str | None = None

    @classmethod
    def from_operation(cls, op: Operation) -> OperationOut:
        actor = op.actor
        return cls(
            id=op.id,
            workspace_id=str(actor.get("workspace_id") or WORKSPACE_ID),
            action=op.action,
            target=Target(kind=op.target_kind, id=op.target_key),
            state=op.state,
            effect=op.effect,
            actor=Actor(
                kind=str(actor.get("kind", "operator")),
                id=str(actor.get("id", "")),
                display=actor.get("display"),
                via=str(actor.get("via", "")),
            ),
            request=dict(op.request),
            accepted_at=rfc3339(op.accepted_at) or "",
            claimed_at=rfc3339(op.claimed_at),
            finished_at=rfc3339(op.finished_at),
            expires_at=rfc3339(op.expires_at),
            result=op.result,
            error_code=op.error_code,
            error_detail=op.error_detail,
        )


class CurrentRun(ApiModel):
    item_id: str
    run_id: str
    title: str
    kind: str
    profile: str | None = None


class Hold(ApiModel):
    name: str
    owner: str | None = None
    via: str = ""
    reason: str = ""
    created_at: str | None = None


class RepoHealth(ApiModel):
    model_config = ConfigDict(extra="allow")

    repo: str
    state: str


class Status(ApiModel):
    """The daemon's live state, observed at ``observed_at``."""

    workspace_id: str = WORKSPACE_ID
    observed_at: str
    generation: str | None = None
    version: str
    current: CurrentRun | None = None
    claiming: str | None = None
    queued: int
    runs_today: int
    max_runs_per_day: int
    run_cap_timezone: str
    resumes_today: int = 0
    breaker_open: bool
    paused: bool
    holds: list[Hold]
    stopping: bool = False
    restarting: bool = False
    provider_hold: str | None = None
    source: str | None = None
    source_failures: int = 0
    source_retry_in_s: float = 0.0
    repos: list[RepoHealth] = Field(default_factory=list)
    #: The public chronology's high-water mark, for a client that reads a
    #: snapshot and then subscribes from it; ``None`` until it exists.
    watermark: int | None = None

    @classmethod
    def from_status(cls, status: dict[str, Any], *, now: float) -> Status:
        current = status.get("current")
        details = status.get("hold_details")
        if isinstance(details, list) and details:
            holds = [
                Hold(
                    name=str(h.get("name")),
                    owner=h.get("owner"),
                    via=str(h.get("via") or ""),
                    reason=str(h.get("reason") or ""),
                    created_at=rfc3339(h.get("created_at")),
                )
                for h in details
            ]
        else:
            holds = [Hold(name=str(name)) for name in status.get("holds") or []]
        return cls(
            observed_at=rfc3339(now) or "",
            generation=status.get("generation"),
            version=str(status.get("version", "")),
            current=CurrentRun(
                item_id=str(current["item_id"]),
                run_id=str(current["run_id"]),
                title=str(current.get("title", "")),
                kind=str(current.get("kind", "code")),
                profile=current.get("profile"),
            )
            if current
            else None,
            claiming=status.get("claiming"),
            queued=int(status.get("queued", 0)),
            runs_today=int(status.get("runs_today", 0)),
            max_runs_per_day=int(status.get("max_runs_per_day", 0)),
            run_cap_timezone=str(status.get("run_cap_timezone", "UTC")),
            resumes_today=int(status.get("resumes_today", 0)),
            breaker_open=bool(status.get("breaker_open", False)),
            paused=bool(status.get("paused", False)),
            holds=holds,
            stopping=bool(status.get("stopping", False)),
            restarting=bool(status.get("restarting", False)),
            provider_hold=status.get("provider_hold"),
            source=status.get("source"),
            source_failures=int(status.get("source_failures", 0)),
            source_retry_in_s=float(status.get("source_retry_in_s", 0.0)),
            repos=[
                RepoHealth.model_validate(r)
                for r in status.get("repos") or []
                if isinstance(r, dict) and "repo" in r and "state" in r
            ],
        )


# -- auth -------------------------------------------------------------------------


class TokenRequest(ApiModel):
    grant_type: Literal["client_credentials", "refresh_token"]
    client_id: str | None = None
    client_secret: str | None = None
    refresh_token: str | None = None


class TokenResponse(ApiModel):
    token_type: Literal["Bearer"] = "Bearer"
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_in: int
    scope: str
    client_id: str


class RevokeRequest(ApiModel):
    #: The refresh token whose family to revoke, besides the access token
    #: this request was made with.
    refresh_token: str | None = None


class Me(ApiModel):
    client_id: str
    name: str
    capabilities: list[Capability]
    workspace_id: str = WORKSPACE_ID
    token_expires_at: str


# -- work items, runs, the queue (#1036) ------------------------------------------

OriginKind = Literal["issue", "chat", "schedule", "api", "other"]


class Origin(ApiModel):
    """Where a work item came from. ``ref`` is the id the daemon's own
    surfaces (ctl, chat, the console) know the item by."""

    kind: OriginKind
    repository_id: str | None = None
    repository: str | None = None
    number: int | None = None
    url: str | None = None
    ref: str


class Item(ApiModel):
    id: str
    workspace_id: str = WORKSPACE_ID
    kind: str
    state: str
    title: str
    origin: Origin
    profile: str | None = None
    recipe: str | None = None
    recipe_target: str | None = None
    attempts: int = 0
    run_id: str | None = None
    last_error: str | None = None
    pending_report: str | None = None
    not_before: str | None = None
    created_at: str
    updated_at: str
    revision: int = 0
    available_actions: list[str] = Field(default_factory=list)


class ItemDetail(Item):
    body: str = ""
    #: Every run the item was dispatched under, oldest first.
    runs: list[str] = Field(default_factory=list)
    #: Who admitted the item through a recorded operation, when one did.
    admitted_by: Actor | None = None


class PullRequest(ApiModel):
    number: int | None = None
    url: str | None = None
    branch: str | None = None
    head_sha: str | None = None
    title: str | None = None


class Rounds(ApiModel):
    review: int = 0
    ci: int = 0
    granted: int = 0
    #: Which budget the run ran out of (``review`` / ``ci``), or ``None``.
    exhausted: str | None = None


class PublishedOut(ApiModel):
    sink: str
    location: str
    tasks: list[str] = Field(default_factory=list)
    files: int = 0


class GateSummary(ApiModel):
    kind: str
    state: str
    revision: int = 0


class Run(ApiModel):
    id: str
    workspace_id: str = WORKSPACE_ID
    kind: str
    state: str
    stage: str | None = None
    outcome: str
    reason: str | None = None
    item_id: str | None = None
    created_at: str
    updated_at: str
    pull_request: PullRequest | None = None
    rounds: Rounds = Field(default_factory=Rounds)
    published: list[PublishedOut] = Field(default_factory=list)
    gate: GateSummary | None = None
    review_wait: str | None = None
    revision: int = 0
    available_actions: list[str] = Field(default_factory=list)


class TaskOutputOut(ApiModel):
    summary: str = ""
    files: list[str] = Field(default_factory=list)


class Task(ApiModel):
    id: str
    title: str
    description: str = ""
    state: str
    depends_on: list[str] = Field(default_factory=list)
    revisions: int = 0
    replans: int = 0
    verify_suspect: bool = False
    verify_reauthors: int = 0
    output: TaskOutputOut | None = None


class QueueEntry(ApiModel):
    position: int
    item: Item
    #: When dispatch's own rule lets the item go; ``None`` means now.
    eligible_at: str | None = None
    eligible: bool = True
    reason: str | None = None


class QueuePage(ApiModel):
    """The queue in dispatch order, with what stands in its way."""

    data: list[QueueEntry]
    next_cursor: str | None = None
    has_more: bool = False
    observed_at: str
    paused: bool = False
    breaker_open: bool = False


class Repository(ApiModel):
    id: str
    workspace_id: str = WORKSPACE_ID
    repository: str
    forge: str
    enabled: bool = True
    deliver_base: str | None = None
    trigger_label: str
    workload_label: str
    health: RepoHealth | None = None


class Profile(ApiModel):
    id: str
    name: str
    description: str = ""
    sinks: list[str] = Field(default_factory=list)
    publish: str = "auto"
    repo: bool = False
    default: bool = False


class Recipe(ApiModel):
    id: str
    name: str
    parameters: list[str] = Field(default_factory=list)
    enabled: bool = True


# -- intake and item commands ------------------------------------------------------


class IssueIntake(ApiModel):
    kind: Literal["issue"]
    #: The repository by its public id or its ``owner/name``; one of the two.
    repository_id: str | None = None
    repository: str | None = None
    number: int = Field(ge=1)
    run_kind: Literal["code", "workload"] = "code"


class WorkloadIntake(ApiModel):
    kind: Literal["workload"]
    ask: str = Field(min_length=1, max_length=65536)
    profile: str | None = None
    sink: str | None = None


class ToolIntake(ApiModel):
    kind: Literal["tool"]
    recipe: str = Field(min_length=1, max_length=64)
    parameters: dict[str, str] = Field(default_factory=dict)


IntakeRequest = Annotated[IssueIntake | WorkloadIntake | ToolIntake, Field(discriminator="kind")]


class Admitted(ApiModel):
    item: Item
    operation: OperationOut
    #: ``False`` when the same request (or a poll) had already queued it.
    created: bool


class ItemCommand(ApiModel):
    reason: str | None = Field(default=None, max_length=2000)
    expected_revision: int | None = Field(default=None, ge=0)


class ItemCommandResult(ApiModel):
    item: Item
    operation: OperationOut


# -- the public chronology (#1037) -------------------------------------------------


class EventOut(ApiModel):
    """One public event. ``id`` is the replay cursor (``evt_<seq>``);
    ``native_seq`` is the engine's own sequence when the event is a
    projection of the run's chronology; ``actor`` is absent where nothing
    truthful can be said."""

    id: str
    schema_version: int = 1
    type: str
    occurred_at: str
    recorded_at: str
    workspace_id: str = WORKSPACE_ID
    run_id: str | None = None
    item_id: str | None = None
    operation_id: str | None = None
    actor: Actor | None = None
    causation_id: str | None = None
    native_seq: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)
