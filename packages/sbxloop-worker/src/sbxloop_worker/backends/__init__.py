"""Agent backends: the pluggable engine that runs one agent session."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sbxloop_worker.protocol import Event, JobRequest, SessionHealth, Usage

# emit("agent.message", content="...") -> Event
EmitFn = Callable[..., Event]

BACKEND_ENV = "SBXLOOP_WORKER_BACKEND"


class BackendUnavailableError(RuntimeError):
    """The requested backend cannot run in this environment."""


@dataclass
class BackendResult:
    output_text: str = ""
    output_json: dict[str, Any] | list[Any] | None = None
    session_id: str | None = None
    usage: Usage | None = None
    turns: int | None = None
    health: SessionHealth | None = None
    artifacts: list[str] = field(default_factory=list)


class AgentBackend(Protocol):
    name: str

    def ensure_available(self) -> None: ...

    def run_session(self, job: JobRequest, emit: EmitFn) -> BackendResult: ...


def get_backend(name: str | None = None) -> AgentBackend:
    """Resolve a backend by name (default: $SBXLOOP_WORKER_BACKEND or copilot)."""
    resolved = name or os.environ.get(BACKEND_ENV) or "copilot"
    if resolved == "echo":
        from sbxloop_worker.backends.echo import EchoBackend

        return EchoBackend()
    if resolved == "copilot":
        from sbxloop_worker.backends.copilot import CopilotBackend

        return CopilotBackend()
    if resolved == "claude":
        from sbxloop_worker.backends.claude import ClaudeBackend

        return ClaudeBackend()
    raise BackendUnavailableError(f"unknown agent backend {resolved!r}")


def ensure_available(name: str | None = None) -> None:
    """Raise :class:`BackendUnavailableError` unless ``name`` can run here.

    The precondition each backend's ``run_session`` opens with — its SDK
    importable, and for claude the Claude Code CLI on PATH — reachable
    *without* starting a session, so a caller can ask "is this sandbox
    equipped for this backend?" before committing a job to it.

    The host asks exactly that of a long-lived sandbox it is about to reuse
    (:meth:`sbxloop.worker.client.WorkerClient.backend_ready`): the worker
    is installed with the configured backend's extra, so a box built under
    one backend is not equipped for another, and the worker version — all
    the reuse gate used to check — is identical either way.
    """
    get_backend(name).ensure_available()
