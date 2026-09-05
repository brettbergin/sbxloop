"""Models for the sbx CLI layer."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SandboxRole = Literal["agent", "github", "service"]


class SecretSpec(BaseModel):
    """A secret to inject into a sandbox.

    Either a built-in sbx secret *service* (e.g. ``github``, which injects
    ``GH_TOKEN``/``GITHUB_TOKEN`` scoped to github.com hosts), or a *custom*
    secret bound to one host + env var via ``sbx secret set-custom``.
    Values are never stored on this model; they are passed at provision time.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["service", "custom"]
    service: str | None = None
    host: str | None = None
    env: str | None = None

    @model_validator(mode="after")
    def _check_kind(self) -> SecretSpec:
        if self.kind == "service":
            if not self.service:
                raise ValueError("service secret requires a service name")
            if self.host or self.env:
                raise ValueError("service secret must not set host/env")
        else:
            if not self.host or not self.env:
                raise ValueError("custom secret requires host and env")
            if self.service:
                raise ValueError("custom secret must not set service")
        return self


class SandboxSpec(BaseModel):
    """Everything needed to create and configure one sandbox."""

    model_config = ConfigDict(extra="forbid")

    name: str
    role: SandboxRole
    workspace: Path
    agent: str = "shell"
    template: str | None = None
    policy_allows: list[str] = Field(default_factory=list)
    secrets: list[SecretSpec] = Field(default_factory=list)
    # Plain environment: may sit in the in-VM env file under any strategy.
    persistent_env: dict[str, str] = Field(default_factory=dict)
    # The service sandbox's secrets (`[[credentials]]` values and the
    # registries' `auth_env`, #765/#766): values that travel only the
    # credential's non-proxy road — per-job stdin, or the 0600 env file —
    # and never an `sbx` argument, an event, or a log line. Empty for the
    # agent role: its only credential is the agent's own, in `secrets`.
    secret_env: dict[str, str] = Field(default_factory=dict)
    # Client files for the configured registries (`[[registries]]`, #680):
    # in-VM path → contents, delivered with `sbx cp` and chmod 600. The
    # `.netrc` kinds carry a credential in the text, so a spec is as
    # sensitive as its secret_env.
    files: dict[str, str] = Field(default_factory=dict)


class SandboxInfo(BaseModel):
    """One row of ``sbx ls`` (parsed tolerantly; unknown columns dropped)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    agent: str | None = None
    status: str | None = None
    workspace: str | None = None


class ExecResult(BaseModel):
    """Outcome of one sbx invocation (or an exec'd command inside a sandbox)."""

    model_config = ConfigDict(extra="forbid")

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0
