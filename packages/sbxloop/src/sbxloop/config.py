"""Configuration loading with layered precedence.

Precedence (highest wins): ``SBXLOOP_*`` environment variables >
``./sbxloop.toml`` > ``pyproject.toml [tool.sbxloop]`` > built-in defaults.

Nested keys use ``__`` in environment variables, e.g.
``SBXLOOP_BUDGETS__MAX_TASKS=5``. Values are parsed as TOML scalars where
possible (so ``true``, ``42``, ``1.5`` get real types) and fall back to
strings.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from sbxloop.errors import ConfigError

ENV_PREFIX = "SBXLOOP_"

# SBXLOOP_-prefixed variables consumed by the *worker process* rather than
# host configuration; the env config layer must not treat them as settings.
RESERVED_ENV_KEYS = frozenset({"worker_backend", "echo_script"})

WorkerTransport = Literal["stream", "poll"]
SecretStrategy = Literal["proxy", "plain-env"]


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SandboxConfig(_ConfigModel):
    template: str | None = None
    workspace: Path | None = None
    extra_allow_domains: list[str] = Field(default_factory=list)


class GithubConfig(_ConfigModel):
    """The GitHub integration. ``repo`` is the gate: unset (the default)
    disables GitHub entirely — no github sandbox is provisioned, no GH_TOKEN
    is required, and repo-facing features (progress reporting, delivery)
    refuse to run. Setting it makes ``repo`` the one repository sbxloop is
    allowed to work with; behavior toggles like ``report`` act on it."""

    repo: str | None = None
    report: bool = False
    # Publish a completed run's artifacts as a PR to `repo`.
    deliver: bool = False
    deliver_base: str | None = None  # base branch; None → the repo's default
    deliver_draft: bool = False

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[\w.-]+/[\w.-]+", value):
            raise ValueError(f"github.repo must be owner/name, got {value!r}")
        return value

    @property
    def enabled(self) -> bool:
        return self.repo is not None


class RunSettings(_ConfigModel):
    """Execution shape of a run.

    ``max_parallel`` is the upper bound on agent sandboxes executing tasks
    concurrently. The default of 1 preserves the strictly sequential loop
    exactly. Values above 1 only fan out tasks that are independent AND
    declare disjoint ``owns`` subtrees (see docs/parallel-execution.md);
    every extra slot is a full microVM plus provisioning cost.
    """

    max_parallel: int = Field(default=1, ge=1)


class Budgets(_ConfigModel):
    max_revisions_per_task: int = 2
    max_replans_per_task: int = 1
    max_tasks: int = 20
    max_wall_clock_s: float = 7200.0
    per_job_timeout_s: float = 900.0


class Config(_ConfigModel):
    model: str = "auto"
    # sbx --app-name. Empty (the default) shares the user's normal sbx
    # application state, so their `sbx login` and `sbx policy init balanced`
    # apply directly. Setting a name isolates sbxloop state, but the isolated
    # app-state needs its own `sbx --app-name <name> login` and policy init.
    app_name: str = ""
    state_dir: Path = Path(".sbxloop")
    keep_sandboxes: bool = False
    worker_transport: WorkerTransport = "stream"
    secret_strategy: SecretStrategy = "proxy"
    # Advanced: in-sandbox interpreter for the worker, and whether to run the
    # install flow. Overridden in tests/e2e via SBXLOOP_WORKER_PYTHON /
    # SBXLOOP_INSTALL_WORKERS.
    worker_python: str = "/home/agent/.sbxloop/venv/bin/python"
    install_workers: bool = True
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    github: GithubConfig = Field(default_factory=GithubConfig)
    budgets: Budgets = Field(default_factory=Budgets)
    run: RunSettings = Field(default_factory=RunSettings)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def _pyproject_layer(cwd: Path) -> dict[str, Any]:
    path = cwd / "pyproject.toml"
    if not path.is_file():
        return {}
    data = _read_toml(path)
    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        return {}
    section = tool.get("sbxloop", {})
    return section if isinstance(section, dict) else {}


def _sbxloop_toml_layer(cwd: Path) -> dict[str, Any]:
    path = cwd / "sbxloop.toml"
    if not path.is_file():
        return {}
    return _read_toml(path)


def _parse_env_value(raw: str) -> Any:
    """Parse an env var as a TOML scalar; fall back to the raw string."""
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return raw


def _env_layer(env: Mapping[str, str]) -> dict[str, Any]:
    layer: dict[str, Any] = {}
    for key, raw in env.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = key[len(ENV_PREFIX) :].lower().split("__")
        if not all(path) or path[0] in RESERVED_ENV_KEYS:
            continue
        node = layer
        for part in path[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ConfigError(f"conflicting env overrides at {key}")
        node[path[-1]] = _parse_env_value(raw)
    return layer


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{dotted}."))
        else:
            flat[dotted] = value
    return flat


def load_dotenv_file(cwd: Path | None = None) -> Path | None:
    """Load ``<cwd>/.env`` into the process environment, if present.

    Real environment variables always win (``override=False``), so a ``.env``
    file is a convenience layer for the two PATs and ``SBXLOOP_*`` settings —
    never a way to silently shadow explicit exports. Returns the loaded path,
    or None when there is no file.
    """
    from dotenv import load_dotenv

    path = (cwd or Path.cwd()) / ".env"
    if not path.is_file():
        return None
    load_dotenv(path, override=False)
    return path


def load_config_with_sources(
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[Config, dict[str, str]]:
    """Load config and report, per dotted key, which layer supplied it."""
    cwd = cwd or Path.cwd()
    if env is None:
        # Only consult .env when reading the real environment; explicit env
        # mappings (tests, embedders) stay hermetic.
        load_dotenv_file(cwd)
    env = os.environ if env is None else env

    layers: list[tuple[str, dict[str, Any]]] = [
        ("pyproject.toml", _pyproject_layer(cwd)),
        ("sbxloop.toml", _sbxloop_toml_layer(cwd)),
        ("env", _env_layer(env)),
    ]

    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for name, layer in layers:
        merged = _deep_merge(merged, layer)
        for dotted in _flatten(layer):
            sources[dotted] = name

    try:
        config = Config.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"invalid sbxloop configuration: {exc}") from exc

    for dotted in _flatten(config.model_dump()):
        sources.setdefault(dotted, "default")
    return config, sources


def load_config(cwd: Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Load the effective sbxloop configuration for ``cwd``."""
    config, _ = load_config_with_sources(cwd=cwd, env=env)
    return config
