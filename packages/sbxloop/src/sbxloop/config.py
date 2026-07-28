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

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from sbxloop.errors import ConfigError
from sbxloop.toolchains import DEFAULT_LANGUAGES, normalize_language, supported_languages
from sbxloop.toolchains import build_dirs as toolchain_build_dirs
from sbxloop.toolchains import resolve as resolve_toolchains

ENV_PREFIX = "SBXLOOP_"

# SBXLOOP_-prefixed variables consumed by the *worker process* rather than
# host configuration; the env config layer must not treat them as settings.
RESERVED_ENV_KEYS = frozenset({"worker_backend", "echo_script"})

WorkerTransport = Literal["stream", "poll"]
SecretStrategy = Literal["proxy", "plain-env"]
HarvestMode = Literal["per-task", "final"]


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SandboxConfig(_ConfigModel):
    """Sandbox provisioning: which template, where the workspace lives, what
    egress every run gets, and which language toolchains the agent sandbox is
    made dev-ready for.

    ``languages`` selects entries from ``sbxloop.toolchains`` — installed
    before the agent's first turn so it does not spend revision budget
    bootstrapping its own compiler. Unset means ``["python"]``, which is the
    head start Python has had since 0.4.0; setting it REPLACES that default
    rather than adding to it, so nothing is provisioned for a language the
    operator did not ask for. Provisioning is probe-first (a template that
    already ships the toolchain costs nothing) and never fatal.
    """

    template: str | None = None
    workspace: Path | None = None
    extra_allow_domains: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)

    @field_validator("languages")
    @classmethod
    def _check_languages(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        bad: list[str] = []
        for entry in value:
            key = normalize_language(entry)
            if key is None:
                bad.append(entry)
            elif key not in normalized:
                normalized.append(key)
        if bad:
            raise ValueError(
                f"unsupported sandbox.languages entries {bad}: "
                f"choose from {list(supported_languages())}"
            )
        return normalized

    @property
    def effective_languages(self) -> tuple[str, ...]:
        """Languages to provision, applying the default for an unset list."""
        return tuple(self.languages) or DEFAULT_LANGUAGES


class PolicyConfig(_ConfigModel):
    """Operator bounds for plan-declared egress.

    The PLAN phase may request extra network allows for EXECUTE (each with a
    justification). Requests are auto-granted just before EXECUTE only when
    they match ``allow`` and no ``deny`` pattern; everything is event-logged
    (``sbxloop logs RUN --type policy.``). Patterns are exact domains,
    ``*.example.com`` wildcards (the domain and all subdomains), or ``*``
    (everything). Empty ``allow`` — the default — means plans may only use
    the always-reachable baseline (Copilot/GitHub hosts, the language
    registry baseline in ``policy.BASELINE_REGISTRY_DOMAINS``, apt mirrors)
    plus the well-known package registries (see
    ``policy.WELL_KNOWN_REGISTRY_DOMAINS``), which are in-bounds to declare
    without configuration. ``deny`` overrides all of it — including the
    always-reachable tier, which provisioning seeds through
    ``policy.baseline_allows`` rather than unconditionally.
    """

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)

    @field_validator("allow", "deny")
    @classmethod
    def _check_patterns(cls, value: list[str]) -> list[str]:
        from sbxloop.policy import valid_pattern

        value = [p.strip().lower() for p in value]
        bad = [p for p in value if not valid_pattern(p, operator=True)]
        if bad:
            raise ValueError(
                f"invalid egress pattern(s) {bad}: use a domain, *.domain wildcard, or *"
            )
        return value


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


class ArtifactsConfig(_ConfigModel):
    """What artifact listings and delivery leave out, and how harvesting works.

    ``exclude`` entries are single path components matched at any depth;
    a file is excluded when any component of its path matches. The default
    drops only genuinely noisy state dirs (``.git``, ``.sbxloop``) — dot-path
    artifacts like ``.github/`` or ``.gitignore`` are delivered. Exclusions
    are always counted and surfaced, never silent.

    ``harvest_mode`` controls when unmounted-run artifacts are copied to the
    host.  ``"per-task"`` (default) copies after every task boundary plus the
    final sweep — narrowing the loss window on long runs.  ``"final"`` skips
    the mid-run copies and only performs the authoritative sweep at the end,
    which is cheaper for runs with large workspaces.
    """

    # Mirrors engine.model.DEFAULT_ARTIFACT_EXCLUDES (kept literal here —
    # importing engine.model from config would be a circular import).
    exclude: list[str] = Field(default_factory=lambda: [".git", ".sbxloop"])
    harvest_mode: HarvestMode = "per-task"

    @field_validator("exclude")
    @classmethod
    def _check_exclude(cls, value: list[str]) -> list[str]:
        value = [e.strip() for e in value]
        bad = [e for e in value if not e or "/" in e or "\\" in e or e in (".", "..")]
        if bad:
            raise ValueError(
                f"invalid artifacts.exclude entries {bad}: each must be a bare "
                f'directory/file name (no path separators), e.g. ".git"'
            )
        return value


class Limits(_ConfigModel):
    """Sandbox resource guardrails, sampled in-VM on the worker heartbeat.

    Thresholds are percent-used of the workspace filesystem / memory; 0
    disables a guardrail. Crossing a warn threshold emits a prominent
    ``sandbox.resources_warning`` event and escalates the TUI gauge;
    crossing ``disk_abort`` fails the current task with an explicit
    "sandbox disk exhausted" error instead of letting in-VM tooling fail
    confusingly on a full disk.
    """

    disk_warn: float = 85.0
    disk_abort: float = 95.0
    mem_warn: float = 90.0

    @model_validator(mode="after")
    def _check_thresholds(self) -> Limits:
        for name in ("disk_warn", "disk_abort", "mem_warn"):
            value = getattr(self, name)
            if value < 0 or value > 100:
                raise ValueError(f"limits.{name} must be a percentage in 0..100, got {value}")
        if 0 < self.disk_abort <= self.disk_warn:
            raise ValueError(
                f"limits.disk_abort ({self.disk_abort}) must be greater than "
                f"limits.disk_warn ({self.disk_warn})"
            )
        return self


class Budgets(_ConfigModel):
    max_revisions_per_task: int = 2
    max_replans_per_task: int = 1
    max_tasks: int = 20
    max_wall_clock_s: float = 7200.0
    per_job_timeout_s: float = 1800.0


class Config(_ConfigModel):
    model: str = "auto"
    # sbx --app-name. Empty (the default) shares the user's normal sbx
    # application state, so their `sbx login` and `sbx policy init balanced`
    # apply directly. Setting a name isolates sbxloop state, but the isolated
    # app-state needs its own `sbx --app-name <name> login` and policy init.
    app_name: str = ""
    state_dir: Path = Path(".sbxloop")
    keep_sandboxes: bool = False
    # Keep the pair alive only when a run fails, so the evidence (worker
    # stderr, install leftovers, workspace state) survives for
    # `sbxloop shell <run>`. Kept runs are marked in the state DB and
    # eventually collectable via `sbxloop sandbox prune --include-kept`.
    keep_on_failure: bool = False
    worker_transport: WorkerTransport = "stream"
    secret_strategy: SecretStrategy = "proxy"
    # Advanced: in-sandbox interpreter for the worker, and whether to run the
    # install flow. Overridden in tests/e2e via SBXLOOP_WORKER_PYTHON /
    # SBXLOOP_INSTALL_WORKERS.
    worker_python: str = "/home/agent/.sbxloop/venv/bin/python"
    install_workers: bool = True
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    github: GithubConfig = Field(default_factory=GithubConfig)
    artifacts: ArtifactsConfig = Field(default_factory=ArtifactsConfig)
    budgets: Budgets = Field(default_factory=Budgets)
    limits: Limits = Field(default_factory=Limits)

    @property
    def artifact_excludes(self) -> list[str]:
        """``[artifacts] exclude`` plus the selected languages' build output.

        Listing, harvest and delivery all resolve their exclusions through
        here rather than reading ``artifacts.exclude`` directly, so the two
        can never disagree about what an artifact is.

        Build output is bulky, reproducible from source, and never what the
        user asked for — but which directory holds it is entirely a function
        of the ecosystem, which is precisely what ``[sandbox] languages``
        already declares. Deriving it means a Rust run stops delivering a
        several-hundred-megabyte ``target/`` tree without anyone having to
        remember to configure it.

        Explicit ``exclude`` entries win by being kept: this only ever adds.
        An operator who wants build output delivered anyway can select no
        language, or use the per-run artifact commands on the raw workspace.
        """
        derived = toolchain_build_dirs(resolve_toolchains(list(self.sandbox.effective_languages)))
        merged = list(self.artifacts.exclude)
        for name in derived:
            if name not in merged:
                merged.append(name)
        return merged


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
