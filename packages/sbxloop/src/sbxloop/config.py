"""Configuration loading with layered precedence.

Precedence (highest wins): ``SBXLOOP_*`` environment variables >
``./sbxloop.toml`` > ``pyproject.toml [tool.sbxloop]`` >
``~/.config/sbxloop/sbxloop.toml`` (``$XDG_CONFIG_HOME`` honoured) >
built-in defaults.

The user-level file is the lowest layer so a project can always override it;
it exists for settings that follow the operator rather than the checkout
(``model``, ``app_name``, ``[discord]``). It is located from the *env mapping*
handed to the loader — never from ``os.environ`` behind the caller's back — so
hermetic callers (tests, embedders passing ``env={}``) never read the real
home directory.

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
from sbxloop.log import LogFormat, LogLevel, get_logger
from sbxloop.toolchains import DEFAULT_LANGUAGES, normalize_language, supported_languages

log = get_logger(__name__)

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
    crossing ``disk_abort`` / ``mem_abort`` fails the current task with an
    explicit "sandbox disk/memory exhausted" error instead of letting in-VM
    tooling fail confusingly on a full disk or under the OOM killer.

    ``mem_abort`` is off by default (#253): sbxloop cannot size the microVM,
    and a healthy parallel test run on a large repo legitimately spikes
    MemAvailable for a heartbeat or two — pressure that resolves itself,
    unlike a full disk. Opt in when an in-VM OOM has been surfacing as a
    confusing test failure.
    """

    disk_warn: float = 85.0
    disk_abort: float = 95.0
    mem_warn: float = 90.0
    mem_abort: float = 0.0

    @model_validator(mode="after")
    def _check_thresholds(self) -> Limits:
        for name in ("disk_warn", "disk_abort", "mem_warn", "mem_abort"):
            value = getattr(self, name)
            if value < 0 or value > 100:
                raise ValueError(f"limits.{name} must be a percentage in 0..100, got {value}")
        for warn, abort in (("disk_warn", "disk_abort"), ("mem_warn", "mem_abort")):
            warn_value, abort_value = getattr(self, warn), getattr(self, abort)
            if 0 < abort_value <= warn_value:
                raise ValueError(
                    f"limits.{abort} ({abort_value}) must be greater than "
                    f"limits.{warn} ({warn_value})"
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
    # Discovery lane: an issue carrying this label is a charter — the run
    # investigates and files findings as backlog issues, never a PR.
    audit_label: str = "sbxloop:audit"
    # When a patch item is abandoned (or delivers nothing), file a post-mortem
    # charter carrying the plan, verify transcripts and failure events, so the
    # discovery lane turns the daemon's own failures into evidenced findings.
    # Never for audit items (no recursion); capped per rolling day.
    postmortems: bool = True
    postmortems_per_day: int = 3
    # Scheduled area audits: charters versioned in the target repo under
    # ``audit_dir`` (front-matter `every: 7d`), opened as audit issues when
    # due. Off by default — a repo opts in by carrying charters AND the
    # operator flipping this on.
    audits: bool = False
    audit_dir: Path = Path(".github/sbxloop/audits")
    # After a run delivers a PR, open a review audit of that PR — the loop
    # evaluating the code it just wrote (defects, missing edge cases, scope
    # drift) and filing findings for a human to promote.
    review_deliveries: bool = True
    reviews_per_day: int = 5
    # Where findings ABOUT THE TOOL (sbxloop's planner, prompts, lint,
    # delivery) go — the tool's own tracker, never the project's. Unset:
    # such findings are only noted in the closing comment.
    tool_repo: str | None = None
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
    # Liveness safety net for phantom active runs (#374). When the daemon has
    # no run executing, any non-terminal run whose last activity (engine
    # chronology, falling back to the run row's updated timestamp) is older
    # than this is reconciled to a terminal state, so list_runs and
    # `!sbx status` agree on what is active. The in-flight run is never
    # considered stale. 0 disables the sweep.
    run_stale_after_s: float = Field(default=21600.0, ge=0)
    # The daemon's own log stream (stderr → journald under systemd). INFO is
    # the lifecycle tier: startup summary, claims, run dispatch/finish, task
    # and phase transitions, operator commands; DEBUG adds every tool call,
    # sbx invocation and poll. ``json`` renders one object per line for log
    # shippers; ``console`` is key=value for humans and journalctl.
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "console"

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("log_format", mode="before")
    @classmethod
    def _lower_format(cls, value: Any) -> Any:
        return value.lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _check(self) -> DaemonConfig:
        labels = [
            self.trigger_label,
            self.in_progress_label,
            self.failed_label,
            self.backlog_label,
            self.delivered_label,
            self.audit_label,
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
            "postmortems_per_day",
            "reviews_per_day",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"daemon.{name} must be >= 1")
        if self.tool_repo is not None and not re.fullmatch(r"[\w.-]+/[\w.-]+", self.tool_repo):
            raise ValueError(f"daemon.tool_repo must be owner/name, got {self.tool_repo!r}")
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


class ConciergeConfig(_ConfigModel):
    """The control channel's agent: an LLM session that answers @mentions
    in the Discord control channel, operates the daemon (every ``!sbx``
    verb), enqueues new work and explains runs, PRs and diffs. It runs in
    a long-lived agent-role sandbox and reaches the daemon only through
    host tools. Effective only when ``[discord]`` is enabled; needs
    ``COPILOT_GITHUB_TOKEN`` on the daemon host like any agent session.
    It acts with the same authority as ``!sbx`` — anyone who can mention
    the bot can drive the daemon; restrict the channel accordingly."""

    enabled: bool = True
    # None → the top-level ``model``.
    model: str | None = None
    # One message's wall-clock budget (the whole tool loop).
    timeout_s: float = Field(default=180.0, ge=30, le=900)
    # Replies longer than this are clipped before being split into
    # Discord messages.
    max_reply_chars: int = Field(default=4000, ge=500, le=8000)
    # What one host tool may hand back to the model.
    max_tool_result_chars: int = Field(default=6000, ge=1000, le=20000)
    max_tool_calls: int = Field(default=16, ge=1, le=64)
    # The SDK session is resumed message after message; after this many
    # turns a fresh session is started so context does not grow forever.
    session_turns: int = Field(default=40, ge=1, le=500)
    # Expose the read-only GitHub tool (PR/issue/diff/file reads through
    # the daemon's github-ops sandbox) when GitHub is configured.
    github_tools: bool = True
    # Let the concierge write to issues in the configured repo: file them
    # (backlog label), list them, comment on them, label one for a run, and
    # close one. Every act that starts a run or closes an issue happens only
    # after the person explicitly says so.
    create_issues: bool = True


USER_CONFIG_SUBPATH = Path("sbxloop") / "sbxloop.toml"


def default_state_dir() -> Path:
    """``~/.sbxloop`` — one per user, wherever the shell happens to stand.

    The default used to be the *relative* ``.sbxloop``, i.e. "cwd at the
    time": ``sbxloop status`` from another directory showed an empty world,
    and any command run from inside a checkout dropped a state dir into it
    (field run ``r5a1d9m9c`` — the isolation probe then refused the next run
    as dirty). Project-scoped state remains available by setting a relative
    ``state_dir`` explicitly.
    """
    return Path.home() / ".sbxloop"


def _home_dir(env: Mapping[str, str]) -> Path:
    """The home the *loader* should trust: ``env["HOME"]`` when the mapping
    names one, else the process home. Keeps the default ``state_dir`` and
    ``~`` expansion consistent with ``user_config_path`` for hermetic
    callers (``env={"HOME": ...}``), which would otherwise read the user
    config from the mapped home but resolve state into the real one."""
    home = env.get("HOME")
    return Path(home) if home else Path.home()


def _expand_home(value: str, home: Path) -> str:
    """``expanduser`` against an explicit ``home`` (bare ``~`` and ``~/…``
    only; ``~user`` is left to the field validator)."""
    if value == "~":
        return str(home)
    if value.startswith("~/"):
        return str(home / value[2:])
    return value


class Config(_ConfigModel):
    model: str = "auto"
    # sbx --app-name. Empty (the default) shares the user's normal sbx
    # application state, so their `sbx login` and `sbx policy init balanced`
    # apply directly. Setting a name isolates sbxloop state, but the isolated
    # app-state needs its own `sbx --app-name <name> login` and policy init.
    app_name: str = ""
    # A relative value is anchored at the config discovery root (the cwd
    # ``load_config`` was given), not at the process cwd at first use.
    state_dir: Path = Field(default_factory=default_state_dir)
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
    concierge: ConciergeConfig = Field(default_factory=ConciergeConfig)

    @field_validator("state_dir", mode="after")
    @classmethod
    def _expand_home(cls, value: Path) -> Path:
        # `state_dir = "~/.sbxloop"` in TOML must mean the home directory,
        # not a literal "~" directory under the project.
        return value.expanduser()


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


def user_config_path(env: Mapping[str, str]) -> Path | None:
    """``$XDG_CONFIG_HOME/sbxloop/sbxloop.toml`` (``~/.config/...`` when
    unset), or None when ``env`` names neither — the hermetic case."""
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / USER_CONFIG_SUBPATH
    home = env.get("HOME")
    if home:
        return Path(home) / ".config" / USER_CONFIG_SUBPATH
    return None


def _user_config_layer(env: Mapping[str, str]) -> dict[str, Any]:
    path = user_config_path(env)
    if path is None or not path.is_file():
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
        ("user config", _user_config_layer(env)),
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

    # Resolve the home-relative parts of ``state_dir`` against the loader's
    # HOME (not the process environment) before validation, so ``env`` fully
    # determines where state lands.
    home = _home_dir(env)
    if "state_dir" not in merged:
        merged["state_dir"] = str(home / ".sbxloop")
    elif isinstance(merged["state_dir"], str):
        merged["state_dir"] = _expand_home(merged["state_dir"], home)

    try:
        config = Config.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"invalid sbxloop configuration: {exc}") from exc

    if not config.state_dir.is_absolute():
        # An explicit relative state_dir means project-scoped state: pin it
        # to the directory the config was discovered in so its meaning
        # cannot drift with a later chdir.
        config = config.model_copy(update={"state_dir": cwd / config.state_dir})

    for dotted in _flatten(config.model_dump()):
        sources.setdefault(dotted, "default")
    # Which layer set which key — never the values (tokens live in env).
    overridden = {k: v for k, v in sources.items() if v != "default"}
    log.debug(
        "config.loaded",
        cwd=str(cwd),
        state_dir=str(config.state_dir),
        layers={name: len(_flatten(layer)) for name, layer in layers},
        overrides=overridden,
    )
    return config, sources


def load_config(cwd: Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Load the effective sbxloop configuration for ``cwd``."""
    config, _ = load_config_with_sources(cwd=cwd, env=env)
    return config
