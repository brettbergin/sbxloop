"""Shared host/worker protocol models.

These models are the contract between the sbxloop host orchestrator and the
in-sandbox worker. Both sides import this exact module (the host depends on
``sbxloop-worker``), so drift is a type error rather than a runtime surprise.

Every object carries a protocol version ``v``; host and worker versions are
kept in lockstep, so models reject unknown fields outright.
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION = 1

JobKind = Literal["agent.session", "shell.check", "shell.batch", "github.op"]
JobStatus = Literal["ok", "error", "timeout"]
PermissionMode = Literal["auto", "read_only"]
ExpectMode = Literal["text", "json"]


class ProtocolModel(BaseModel):
    """Base for all wire models: strict, no silent extra fields."""

    model_config = ConfigDict(extra="forbid")


class Usage(ProtocolModel):
    """Model/token usage reported by an agent backend for one job."""

    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost: float | None = None

    def merged(self, other: Usage) -> Usage:
        """Accumulate another usage sample into this one (None-safe sums)."""

        def add(a: int | None, b: int | None) -> int | None:
            if a is None and b is None:
                return None
            return (a or 0) + (b or 0)

        cost: float | None = None
        if self.cost is not None or other.cost is not None:
            cost = (self.cost or 0.0) + (other.cost or 0.0)
        return Usage(
            model=other.model or self.model,
            input_tokens=add(self.input_tokens, other.input_tokens),
            output_tokens=add(self.output_tokens, other.output_tokens),
            cache_read_tokens=add(self.cache_read_tokens, other.cache_read_tokens),
            cache_write_tokens=add(self.cache_write_tokens, other.cache_write_tokens),
            cost=cost,
        )


class SessionHealth(ProtocolModel):
    """Tooling health observed during one agent session.

    ``permission_denials`` maps a denied permission kind to how many times it
    was rejected — expected in read-only sessions (the allowlist doing its
    job), but worth an audit trail. ``tool_refusals`` maps a tool name to how
    many of its calls the Copilot CLI's own command validator declined to run
    (e.g. ``kill`` without a literal numeric PID) — like denials, these are
    policy at work, and the agent can rephrase and retry. ``tool_failures``
    maps a tool name to how many of its calls actually ran and failed;
    failures mean the session could not run its intended inspection, which is
    what the engine's degraded-critic guard reacts to (#123): a critic that
    lost its tooling must not emit a clean verdict as if it had verified
    anything.
    """

    permission_denials: dict[str, int] = Field(default_factory=dict)
    tool_failures: dict[str, int] = Field(default_factory=dict)
    tool_refusals: dict[str, int] = Field(default_factory=dict)
    # Tool calls the per-phase ceiling turned away (#228). Bounded spend,
    # not lost tooling — a separate tally so it never reads as degraded.
    tool_calls: int = 0
    tool_cap_denials: int = 0

    @property
    def degraded(self) -> bool:
        """True when the session lost tooling it tried to use. Denials and
        validator refusals do not count: a read-only critic probing ``write``,
        or having a ``kill $(cat pid)`` declined by the CLI's validator, is
        policy working as designed, not a broken session."""
        return bool(self.tool_failures)

    def summary(self) -> str:
        """Human-readable tally for prompts, feedback, and event messages."""

        def tally(counts: dict[str, int]) -> str:
            return ", ".join(f"{name} x{count}" for name, count in sorted(counts.items()))

        parts = []
        if self.tool_failures:
            parts.append(f"tool failures: {tally(self.tool_failures)}")
        if self.permission_denials:
            parts.append(f"permission denials: {tally(self.permission_denials)}")
        if self.tool_refusals:
            parts.append(f"tool refusals: {tally(self.tool_refusals)}")
        if self.tool_cap_denials:
            parts.append(
                f"tool-call ceiling hit: {self.tool_cap_denials} call(s) turned away "
                f"after {self.tool_calls - self.tool_cap_denials}"
            )
        return "; ".join(parts) or "healthy"


class ErrorInfo(ProtocolModel):
    """Structured error carried in a JobResult or error event."""

    type: str
    message: str
    detail: str | None = None
    # HTTP status of a failed github.op, so the host can branch on the code
    # rather than on message wording (#221). None for non-HTTP failures.
    http_status: int | None = None


class JobRequest(ProtocolModel):
    """One unit of work the host submits to a worker.

    Exactly one job kind is used per request; kind-specific fields for other
    kinds must be left at their defaults (validated below).
    """

    v: int = PROTOCOL_VERSION
    job_id: str
    run_id: str
    kind: JobKind
    timeout_s: float = 1800.0

    # kind == "agent.session"
    prompt: str | None = None
    system_message: str | None = None
    model: str | None = None
    resume_session_id: str | None = None
    permission_mode: PermissionMode = "auto"
    expect: ExpectMode = "text"
    # Per-phase tool-call ceiling (#228): calls past it are turned away with
    # an in-session nudge to stop investigating and report. None = unbounded.
    max_tool_calls: int | None = None

    # kind == "shell.check"
    argv: list[str] | None = None

    # kind == "shell.batch": shell command strings, each run via ``sh -c``
    # sequentially inside ONE worker process. Every job pays a fixed
    # round-trip cost (stage job JSON, exec a cold interpreter, fetch the
    # result) that dwarfs a mechanical command's real work, so verify and
    # evidence commands ride together instead of one job each.
    commands: list[str] | None = None
    # Per-command timeout for shell.batch (defaults to timeout_s, which
    # always bounds the job as a whole).
    command_timeout_s: float | None = None

    # agent.session + shell.check: in-sandbox working directory. The worker
    # process chdirs here (via --cwd) so agent sessions and shell commands
    # run in the run's canonical workspace.
    cwd: str | None = None

    # kind == "github.op"
    op: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_kind_fields(self) -> JobRequest:
        if self.kind == "agent.session":
            if not self.prompt:
                raise ValueError("agent.session requires a non-empty prompt")
            if self.argv is not None or self.commands is not None or self.op is not None:
                raise ValueError("agent.session must not set argv, commands, or op")
        elif self.kind == "shell.check":
            if not self.argv:
                raise ValueError("shell.check requires a non-empty argv")
            if self.prompt is not None or self.commands is not None or self.op is not None:
                raise ValueError("shell.check must not set prompt, commands, or op")
        elif self.kind == "shell.batch":
            if not self.commands:
                raise ValueError("shell.batch requires non-empty commands")
            if self.prompt is not None or self.argv is not None or self.op is not None:
                raise ValueError("shell.batch must not set prompt, argv, or op")
        elif self.kind == "github.op":
            if not self.op:
                raise ValueError("github.op requires an op name")
            if self.prompt is not None or self.argv is not None or self.commands is not None:
                raise ValueError("github.op must not set prompt, argv, or commands")
        return self


class BatchCommandResult(ProtocolModel):
    """Per-command outcome of a shell.batch job, carried in JobResult.output_json.

    A nonzero exit_code is still a successful *job* — the host owns the
    verification decision, exactly as for shell.check.
    """

    command: str
    exit_code: int
    output: str = ""


class JobResult(ProtocolModel):
    """Authoritative outcome of a job, written to the result file by the worker.

    The event stream is best-effort telemetry; this file is the source of truth.
    """

    v: int = PROTOCOL_VERSION
    job_id: str
    status: JobStatus
    output_text: str | None = None
    output_json: dict[str, Any] | list[Any] | None = None
    session_id: str | None = None
    usage: Usage | None = None
    # Tooling health of an agent session (None when nothing degraded, or for
    # non-agent jobs / backends that do not report it).
    health: SessionHealth | None = None
    exit_code: int | None = None
    error: ErrorInfo | None = None
    artifacts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_status(self) -> JobResult:
        if self.status != "ok" and self.error is None:
            raise ValueError(f"status={self.status!r} requires an error")
        return self


class Event(ProtocolModel):
    """One line of the JSONL event stream (worker- or host-emitted)."""

    v: int = PROTOCOL_VERSION
    ts: float
    run_id: str
    job_id: str | None = None
    type: str
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def now(
        cls,
        type: str,
        run_id: str,
        job_id: str | None = None,
        **data: Any,
    ) -> Event:
        return cls(ts=time.time(), run_id=run_id, job_id=job_id, type=type, data=data)

    def to_json_line(self) -> str:
        """Serialize to a single JSONL line (no trailing newline)."""
        return json.dumps(self.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json_line(cls, line: str) -> Event:
        return cls.model_validate(json.loads(line))


# Well-known event types. Kept as plain strings on the wire; these constants
# exist so emitters and consumers share one spelling.
class EventTypes:
    WORKER_START = "worker.start"
    WORKER_HEARTBEAT = "worker.heartbeat"
    WORKER_STDOUT = "worker.stdout"
    WORKER_RESULT = "worker.result"
    WORKER_ERROR = "worker.error"
    WORKER_END = "worker.end"

    AGENT_MESSAGE = "agent.message"
    AGENT_MESSAGE_DELTA = "agent.message_delta"
    AGENT_TOOL_START = "agent.tool_start"
    AGENT_TOOL_END = "agent.tool_end"
    AGENT_USAGE = "agent.usage"
    AGENT_PERMISSION_DENIED = "agent.permission_denied"
    # The per-phase tool-call ceiling was reached; further calls are turned
    # away with a nudge (#228). Emitted once per session.
    AGENT_TOOL_CAP = "agent.tool_cap"

    GH_OP_START = "gh.op_start"
    GH_OP_PROGRESS = "gh.op_progress"
    GH_OP_END = "gh.op_end"

    # Resource telemetry sampled on the heartbeat cadence. Worker-emitted,
    # but sandbox-scoped: the host enriches these with the sandbox role.
    SANDBOX_RESOURCES = "sandbox.resources"
    SANDBOX_RESOURCES_WARNING = "sandbox.resources_warning"
    # Worker-emitted, sandbox-scoped: the sandbox runtime degrades or
    # reroutes an agent tool (e.g. the bundled-ripgrep page-size fallback).
    SANDBOX_TOOLING_WARNING = "sandbox.tooling_warning"
