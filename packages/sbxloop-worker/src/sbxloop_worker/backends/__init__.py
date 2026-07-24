"""Agent backends: the pluggable engine that runs one agent session."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sbxloop_worker.protocol import Event, JobRequest, Usage

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
    artifacts: list[str] = field(default_factory=list)


class AgentBackend(Protocol):
    name: str

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
    raise BackendUnavailableError(f"unknown agent backend {resolved!r}")
