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

JobKind = Literal["agent.session", "shell.check", "github.op"]
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


class ErrorInfo(ProtocolModel):
    """Structured error carried in a JobResult or error event."""

    type: str
    message: str
    detail: str | None = None


class JobRequest(ProtocolModel):
    """One unit of work the host submits to a worker.

    Exactly one job kind is used per request; kind-specific fields for other
    kinds must be left at their defaults (validated below).
    """

    v: int = PROTOCOL_VERSION
    job_id: str
    run_id: str
    kind: JobKind
    timeout_s: float = 900.0

    # kind == "agent.session"
    prompt: str | None = None
    system_message: str | None = None
    model: str | None = None
    resume_session_id: str | None = None
    permission_mode: PermissionMode = "auto"
    expect: ExpectMode = "text"

    # kind == "shell.check"
    argv: list[str] | None = None
    cwd: str | None = None

    # kind == "github.op"
    op: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_kind_fields(self) -> JobRequest:
        if self.kind == "agent.session":
            if not self.prompt:
                raise ValueError("agent.session requires a non-empty prompt")
            if self.argv is not None or self.op is not None:
                raise ValueError("agent.session must not set argv or op")
        elif self.kind == "shell.check":
            if not self.argv:
                raise ValueError("shell.check requires a non-empty argv")
            if self.prompt is not None or self.op is not None:
                raise ValueError("shell.check must not set prompt or op")
        elif self.kind == "github.op":
            if not self.op:
                raise ValueError("github.op requires an op name")
            if self.prompt is not None or self.argv is not None:
                raise ValueError("github.op must not set prompt or argv")
        return self


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

    GH_OP_START = "gh.op_start"
    GH_OP_END = "gh.op_end"
