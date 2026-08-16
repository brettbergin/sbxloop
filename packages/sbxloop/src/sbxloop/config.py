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

ENV_PREFIX = "SBXLOOP_"

# SBXLOOP_-prefixed variables consumed by the *worker process* rather than
# host configuration; the env config layer must not treat them as settings.
RESERVED_ENV_KEYS = frozenset({"worker_backend", "echo_script"})

WorkerTransport = Literal["stream", "poll"]
SecretStrategy = Literal["proxy", "plain-env"]
HarvestMode = Literal["per-task", "final"]
WorkspaceIsolation = Literal["auto", "clone", "in-place"]


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

    ``workspace_isolation`` governs what happens when ``workspace`` points
    at an existing git checkout. ``auto`` (default): each run works in a
    self-contained per-run clone on branch ``sbxloop/<run_id>``, leaving the
    checkout and its branches untouched; a dirty source tree refuses the run
    (uncommitted changes would silently not travel). ``clone``: same
    isolation, but a dirty tree proceeds from committed HEAD with a warning,
    and a non-git workspace is an error. ``in-place``: pre-isolation
    behavior — runs mutate the workspace directly, no git involved.
    """

    template: str | None = None
    workspace: Path | None = None
    workspace_isolation: WorkspaceIsolation = "auto"
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
    # Create `repo` when it does not exist (probed right after provisioning,
    # so a missing repo fails the run before any work). Opt-in so a typo'd
    # repo errors instead of silently delivering into a fresh repository;
    # needs a token that may create repositories for the owner.
    create_repo: bool = False
    create_public: bool = False  # created repositories are private by default

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
    a file is excluded when any component of its path matches. Entries with
    glob metacharacters match components via fnmatch (``*.egg-info``).
    Setting it replaces the default list wholesale rather than adding to it.

    The default drops run/VCS state (``.git``, ``.sbxloop``) plus the
    machine-generated dependency and build trees of the supported languages
    — ``node_modules``, ``__pycache__``, ``.venv``/``venv``, ``target``,
    ``obj``, ``.gradle``, ``.bundle``, ``CMakeFiles`` and the Python tool
    caches. Those are regenerable, can reach hundreds of MB, and are exactly
    what nobody wants in an ``sbxloop artifacts`` listing or a ``--deliver``
    PR diff. Dot-path artifacts like ``.github/`` or ``.gitignore`` are still
    delivered.

    Generic names that mean build output in one ecosystem and hand-written
    content in another — ``bin``, ``build``, ``dist``, ``out``, ``lib``,
    ``vendor`` — are deliberately *not* excluded; add them here if your
    project wants them dropped. On top of this list, files the workspace's
    own ``.gitignore`` rules ignore are dropped too (tallied as
    ``gitignored``): the project knows its build byproducts better than any
    generic list can. Exclusions are always counted and surfaced, never
    silent.

    The list covers every supported language regardless of
    ``[sandbox] languages``: that key governs which toolchains are
    pre-installed, not which ones the agent ends up using, and it defaults to
    Python alone — see ``engine.model.DEFAULT_ARTIFACT_EXCLUDES``.

    ``harvest_mode`` controls when unmounted-run artifacts are copied to the
    host.  ``"per-task"`` (default) copies after every task boundary plus the
    final sweep — narrowing the loss window on long runs.  ``"final"`` skips
    the mid-run copies and only performs the authoritative sweep at the end,
    which is cheaper for runs with large workspaces.
    """

    # Mirrors engine.model.DEFAULT_ARTIFACT_EXCLUDES (kept literal here —
    # importing engine.model from config would be a circular import). See
    # that module for why each entry is in and why "bin"/"build"/"vendor"
    # are out; test_config_default_mirrors_model_default pins the two.
    exclude: list[str] = Field(
        default_factory=lambda: [
            ".git",
            ".sbxloop",
            ".mypy_cache",
            ".nox",
            ".pytest_cache",
            ".ruff_cache",
            ".tox",
            ".venv",
            "*.egg-info",
            "__pycache__",
            "venv",
            "node_modules",
            "target",
            ".gradle",
            "obj",
            ".bundle",
            "CMakeFiles",
        ]
    )
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
    # Per-phase tool-call ceiling (#228): an agent session past this many
    # tool calls has further calls turned away with a nudge to stop
    # investigating and report. 0 disables.
    max_tool_calls_per_phase: int = Field(default=40, ge=0)


BacklogMode = Literal["off", "github", "inbox"]


class DaemonConfig(_ConfigModel):
    """``sbxloop daemon`` — the always-on outer loop.

    The daemon discovers work (GitHub issues carrying ``trigger_label`` in
    the configured repo, and ``.md`` files in ``inbox_dir``), runs each item
    as a full inner-loop run, and reports back to the source. It is fully
    autonomous — a label or a file alone starts a run — so the spend
    guardrails here are the only thing standing between a mislabeled issue
    and an empty Copilot budget: a rolling daily run cap, a per-item retry
    cap, and a consecutive-failure circuit breaker.

    ``backlog`` lets the inner agent file follow-up work it discovers
    (written to ``.sbxloop/backlog/*.md`` in the run workspace) into either
    source. Agent-filed items land in triage (the ``backlog_label`` / the
    inbox ``triage/`` dir) and never run until a human promotes them, unless
    ``backlog_auto_trigger`` is set — a self-feeding queue is the failure
    mode that flag guards.

    ``close_on_success`` / ``tracking_issue`` shape how loudly a delivered
    run touches the issue tracker. The defaults suit a task queue where the
    PR is the reviewable object: close the source issue, open a per-run
    tracking issue. Pointed at a repo whose issues are design/discussion
    items (sbxloop's own tracker, #251) that auto-closes a design issue the
    moment a *draft* PR appears and doubles issue volume with self-closing
    tracking issues — set both to false there: the source issue gets the
    summary comment plus ``delivered_label`` and stays open for the human
    who merges the PR.

    Unattended runs need a different workspace posture from a one-shot
    ``sbxloop run`` (#255). ``workspace_isolation`` replaces the
    ``[sandbox]`` setting for daemon runs whenever ``[sandbox] workspace``
    is a git checkout: the default ``clone`` proceeds from committed HEAD
    with a warning where ``auto`` would refuse a dirty tree — a refusal no
    human is present to answer, which would otherwise fail every issue
    while someone has uncommitted work in that checkout. ``refresh_workspace``
    fetches and fast-forwards the checkout before each fresh run so runs
    start from current ``origin/<branch>`` rather than a stale local HEAD.
    ``state_dir`` anchors the daemon's state to an absolute path outside the
    workspace; unset resolves to ``$XDG_STATE_HOME/sbxloop/<project>``
    (``~/.local/state/...``) unless the top-level ``state_dir`` was set
    explicitly or a legacy ``./.sbxloop/state.db`` already exists — see
    :func:`sbxloop.daemon.paths.resolve_state_dir`.
    """

    inbox_dir: str = ".sbxloop/inbox"  # "" disables the inbox source
    workspace_isolation: WorkspaceIsolation = "clone"
    refresh_workspace: bool = True
    state_dir: Path | None = None
    # Must be positive: Event.wait(<= 0) returns immediately and the loop spins.
    poll_interval_s: float = Field(default=60.0, gt=0)
    trigger_label: str = "sbxloop:run"
    in_progress_label: str = "sbxloop:in-progress"
    failed_label: str = "sbxloop:failed"
    backlog_label: str = "sbxloop:backlog"
    delivered_label: str = "sbxloop:delivered"
    close_on_success: bool = True
    tracking_issue: bool = True
    max_runs_per_day: int = 12
    max_attempts_per_item: int = 2
    # Resumes (after a restart/crash) are not attempts, but each one gets a
    # fresh engine wall clock; past this many per item the interrupted run is
    # settled as a failed attempt instead of resumed (#234). 0 = never resume.
    max_resumes_per_item: int = Field(default=2, ge=0)
    retry_backoff_s: float = 900.0
    max_consecutive_failures: int = 3
    breaker_cooldown_s: float = 3600.0
    # Keep below the service manager's stop timeout. Cancellation is honored
    # only at task-phase boundaries and interrupted runs are resumable, so
    # this is a courtesy wait, not a correctness requirement.
    shutdown_grace_s: float = 60.0
    backlog: BacklogMode = "off"
    backlog_max_per_run: int = 5
    backlog_auto_trigger: bool = False
    # Autonomous PRs arrive as drafts unless the operator says otherwise.
    deliver_draft: bool = True
    # Retention for runs/<run_id>/ on disk (workspace clone + harvested
    # artifacts). Swept on daemon start and daily; 0 disables. The SQLite
    # rows are never removed. See sbxloop.gc for what is exempt.
    prune_runs_after_days: float = Field(default=14.0, ge=0)

    @model_validator(mode="after")
    def _check(self) -> DaemonConfig:
        labels = [
            self.trigger_label,
            self.in_progress_label,
            self.failed_label,
            self.backlog_label,
            self.delivered_label,
        ]
        if any(not label.strip() for label in labels):
            raise ValueError("daemon labels must be non-empty")
        # GitHub label names are case-insensitive: "sbxloop:run" and
        # "SBXLOOP:RUN" are the same label, so they cannot mark two states.
        if len({label.strip().casefold() for label in labels}) != len(labels):
            raise ValueError("daemon labels must be distinct (case-insensitively)")
        for name in (
            "max_runs_per_day",
            "max_attempts_per_item",
            "max_consecutive_failures",
            "backlog_max_per_run",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"daemon.{name} must be >= 1")
        return self


ChronologyLevel = Literal["quiet", "normal", "verbose"]


class DiscordConfig(_ConfigModel):
    """The daemon's human channel: a gateway bot posting each run's
    chronology (agent messages, tool lines, issue/PR links) into a thread
    under a control channel, and relaying replies typed in that thread to
    the running agent as steering. Unset ``channel_id`` disables it. The bot
    token comes from ``DISCORD_BOT_TOKEN`` in the environment / .env, never
    from this file. Anyone who can post in the channel can steer — restrict
    the channel accordingly."""

    channel_id: int | None = None
    command_prefix: str = "!sbx"
    thread_per_run: bool = True
    # quiet: lifecycle + links + chat; normal: plus agent messages, with each
    # burst of tool calls digested into one line edited in place (#235:
    # streaming every call drowned the channel); verbose: every call.
    chronology_level: ChronologyLevel = "normal"
    # Discord's hard cap is 2000; leave headroom for wrappers.
    max_message_chars: int = Field(default=1900, ge=200, le=2000)
    # Rich output: embed cards for the run headline, finished report and
    # `!sbx status`; a per-run status message edited in place as tasks
    # progress; at the verbose level, consecutive tool calls batched into
    # one code block of at most tool_batch_lines.
    embeds: bool = True
    status_line: bool = True
    tool_batch_lines: int = Field(default=8, ge=1, le=40)

    @property
    def enabled(self) -> bool:
        return self.channel_id is not None


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
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)


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
