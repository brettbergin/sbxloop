"""Configuration loading with layered precedence.

Precedence (highest wins): ``SBXLOOP_*`` environment variables >
``./sbxloop.toml`` > ``pyproject.toml [tool.sbxloop]`` >
``~/.config/sbxloop/sbxloop.toml`` (``$XDG_CONFIG_HOME`` honoured) >
built-in defaults.

The user-level file is the lowest layer so a project can always override it;
it exists for settings that follow the operator rather than the checkout
(``model``, ``app_name``, ``[discord]`` / ``[slack]``). It is located from the *env mapping*
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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


class _Unset:
    """Sentinel for "argument not supplied", distinct from an explicit None."""


_UNSET = _Unset()


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
    # Continue existing work on this branch instead of starting fresh: the
    # run's clone is checked out at ``origin/<branch>`` and its delivery
    # updates that same branch, so the pull request already open on it is
    # updated rather than a second one opened.
    #
    # Set by the engine when a run is resumed after its workspace clone was
    # pruned — the PR branch is the durable copy of the work; never
    # configured by hand. An absent branch fails provisioning on purpose:
    # a resume that fell back to the default branch would deliver a tree
    # that never contained the PR's work, and force-updating the branch with
    # it destroys that work. A failed provision is recoverable; that is not.
    # The one command that runs everything this project holds itself to.
    # Unset: detected from what the project declares (a `check`/`ci` target in
    # a makefile, justfile or Taskfile; an npm script; tox.ini; noxfile.py) —
    # see `verifylint.GATE_DETECTORS`. Set it for a project whose gate no
    # convention describes, or to `""` to say this project has no gate and
    # switch the requirement off.
    #
    # Whatever it resolves to, one task's verify_commands must run it: that
    # gate is what CI enforces on the pull request, so work that skips it
    # lands red.
    gate_command: str | None = None
    continue_branch: str | None = None
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
    """Operator bounds for task-declared egress.

    DECOMPOSE may declare extra network allows a task needs during BUILD
    (each with a justification). Requests are auto-granted just before BUILD only when
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


_REPO_RE = re.compile(r"[\w.-]+/[\w.-]+")


def _valid_repo(value: str) -> bool:
    return bool(_REPO_RE.fullmatch(value))


class RepoConfig(_ConfigModel):
    """One repository sbxloop works with.

    Declared as ``[[github.repos]]`` entries. Everything here is per-repo;
    the daemon-wide guardrails (daily run cap, per-item retry cap, the
    consecutive-failure circuit breaker and one-run-at-a-time) stay global.
    """

    repo: str
    # The host checkout of *this* repository that runs clone and refresh.
    # None falls back to the legacy ``[sandbox] workspace``, but only when
    # that checkout demonstrably belongs to this repo (see
    # ``Config.workspace_for_repo``) — never another repository's tree.
    workspace: Path | None = None
    deliver_base: str | None = None  # base branch; None → the repo's default
    create_repo: bool = False
    create_public: bool = False
    enabled: bool = True
    # Environment variable holding the token for this repository; None → the
    # daemon-wide GH_TOKEN.
    token_env: str | None = None
    # Per-repo override of the daemon trigger label, plus extra labels applied
    # to issues/PRs for this repository.
    trigger_label: str | None = None
    labels: list[str] = Field(default_factory=list)

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, value: str) -> str:
        if not _valid_repo(value):
            raise ValueError(f"github.repos[].repo must be owner/name, got {value!r}")
        return value

    @property
    def owner(self) -> str:
        return self.repo.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.repo.split("/", 1)[1]


class GithubConfig(_ConfigModel):
    """The GitHub integration. ``repo`` is the gate: unset (the default)
    disables GitHub entirely — no github sandbox is provisioned, no GH_TOKEN
    is required, and the run ends ``completed`` after its gate with nothing
    delivered. Setting it makes ``repo`` the one repository sbxloop works
    with: every run that passes its gate opens a pull request there and
    carries it through review, CI and merge (see ``[landing]``)."""

    repo: str | None = None
    repos: list[RepoConfig] = Field(default_factory=list)
    deliver_base: str | None = None  # base branch; None → the repo's default
    # Create `repo` when it does not exist (probed right after provisioning,
    # so a missing repo fails the run before any work). Opt-in so a typo'd
    # repo errors instead of silently delivering into a fresh repository;
    # needs a token that may create repositories for the owner.
    create_repo: bool = False
    create_public: bool = False  # created repositories are private by default
    # Issue in `repo` this run resolves; rendered as "Closes #N" in the PR
    # body so GitHub links issue and PR and closes the issue on merge even
    # when the daemon is not running. Set per run by the daemon.
    deliver_closes: int | None = Field(default=None, ge=1)
    # How many repositories were enabled in the *un-narrowed* config this was
    # derived from. `for_repo` cuts `repos` down to a single entry, so the
    # list length downstream says nothing about the deployment's shape; the
    # multi-repo guards (workspace resolution, the provision-time origin
    # check) must not be able to disappear just because a run's config was
    # narrowed (#526).
    enabled_repo_count: int | None = None

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, value: str | None) -> str | None:
        if value is not None and not _valid_repo(value):
            raise ValueError(f"github.repo must be owner/name, got {value!r}")
        return value

    @model_validator(mode="after")
    def _normalise_repos(self) -> GithubConfig:
        # A dump/validate round-trip carries both keys; that is consistent (the
        # list contains `repo`) and is accepted as the list form. Genuinely
        # mixing the two forms is not.
        if (
            self.repo is not None
            and self.repos
            and not any(entry.repo.casefold() == self.repo.casefold() for entry in self.repos)
        ):
            raise ValueError(
                "github.repo and github.repos are mutually exclusive: use one or "
                "the other (move the legacy `repo` into a [[github.repos]] entry)"
            )
        if self.repo is not None and not self.repos:
            # Legacy single-repo form: normalise into a one-entry repo list
            # carrying the same effective settings.
            self.repos = [
                RepoConfig(
                    repo=self.repo,
                    deliver_base=self.deliver_base,
                    create_repo=self.create_repo,
                    create_public=self.create_public,
                )
            ]
        elif self.repos:
            seen: dict[str, str] = {}
            for entry in self.repos:
                key = entry.repo.casefold()
                if key in seen:
                    raise ValueError(f"github.repos contains duplicate repository {entry.repo!r}")
                seen[key] = entry.repo
            # Keep the single-repo surface working for callers that read
            # `github.repo`: point it at the first enabled repository.
            primary = next((r for r in self.repos if r.enabled), self.repos[0])
            self.repo = primary.repo
            if self.deliver_base is None:
                self.deliver_base = primary.deliver_base
        elif self._configured_without_repo():
            raise ValueError(
                "github is configured but no repository is set: add `[github] repo` "
                "or at least one [[github.repos]] entry"
            )
        return self

    def _configured_without_repo(self) -> bool:
        """True when the section carries non-default settings but names no
        repository — a config that means to use GitHub yet cannot."""
        # Only delivery settings imply intent to use a repository;
        # `deliver_closes` is set per run by the daemon, not by the operator.
        for name in ("deliver_base", "create_repo", "create_public"):
            field = type(self).model_fields[name]
            if getattr(self, name) != field.get_default(call_default_factory=True):
                return True
        return False

    def repo_list(self) -> list[RepoConfig]:
        """Every configured repository, enabled or not."""
        return list(self.repos)

    def enabled_repos(self) -> list[RepoConfig]:
        return [r for r in self.repos if r.enabled]

    def find_repo(self, selector: str | None) -> RepoConfig | None:
        """Match a repository by full ``owner/name`` or by bare name when
        that name is unambiguous. ``None`` selects the default repository."""
        if selector is None:
            return self.default_repo()
        want = selector.strip().casefold()
        if not want:
            return self.default_repo()
        for entry in self.repos:
            if entry.repo.casefold() == want:
                return entry
        matches = [r for r in self.repos if r.name.casefold() == want]
        if len(matches) == 1:
            return matches[0]
        return None

    def effective_repo(self, repo: str | None = None) -> RepoConfig | None:
        """The repository a run acts on, with daemon-wide defaults folded in.

        ``repo`` is the ``owner/name`` the work item came from; ``None``
        falls back to the default repository (the sole enabled one, else the
        legacy ``github.repo``). Per-repo ``deliver_base``/``create_repo``/
        ``create_public`` win over the global ``[github]`` settings; where a
        repo entry leaves them unset the global value applies.
        """
        entry = self.find_repo(repo)
        if entry is None and repo is None and self.repos:
            entry = self.repos[0]
        if entry is None:
            return None
        return entry.model_copy(
            update={
                "deliver_base": (
                    entry.deliver_base if entry.deliver_base is not None else self.deliver_base
                ),
                "create_repo": entry.create_repo or self.create_repo,
                "create_public": entry.create_public or self.create_public,
            }
        )

    def for_repo(
        self, repo: str | None, *, workspace: Path | _Unset | None = _UNSET
    ) -> GithubConfig:
        """This section narrowed to the one repository a run targets.

        The returned config keeps a single-entry ``repos`` list whose entry
        carries the effective (per-repo overriding global) settings, so
        everything downstream — including a resumed run reading its persisted
        config — sees exactly the repository the work item came from.

        ``workspace`` pins the entry's checkout to the value the caller
        resolved (``Config.workspace_for_repo``), so downstream code sees a
        single authoritative workspace for the run. Passing ``None``
        explicitly pins "this repo has no workspace"; omitting the argument
        entirely leaves the entry's own value alone.
        """
        count = self.enabled_repo_count
        if count is None:
            count = len(self.enabled_repos())
        entry = self.effective_repo(repo)
        if entry is None:
            return self.model_copy(update={"repo": None, "repos": [], "enabled_repo_count": count})
        if not isinstance(workspace, _Unset):
            # Pin unconditionally, including None: leaving the entry's
            # workspace unset would let the narrowed config re-resolve the
            # legacy [sandbox] workspace and hand this run another
            # repository's tree (#526).
            entry = entry.model_copy(update={"workspace": workspace})
        return self.model_copy(
            update={
                "enabled_repo_count": count,
                "repo": entry.repo,
                "repos": [entry],
                "deliver_base": entry.deliver_base,
                "create_repo": entry.create_repo,
                "create_public": entry.create_public,
            }
        )

    @property
    def multi_repo(self) -> bool:
        """Whether the deployment this config came from has several enabled
        repositories — preserved across narrowing by ``enabled_repo_count``."""
        count = self.enabled_repo_count
        if count is None:
            count = len(self.enabled_repos())
        return count > 1

    def default_repo(self) -> RepoConfig | None:
        """The sole enabled repository, or ``None`` when it is ambiguous."""
        enabled = self.enabled_repos()
        if len(enabled) == 1:
            return enabled[0]
        return None

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
    # investigating and report. 0 disables. 60 (was 40) since BUILD merged
    # planning and execution into one session; retune from measured
    # phase_attempts usage once the merged pipeline has field data.
    max_tool_calls_per_phase: int = Field(default=60, ge=0)
    # How many tasks may be in flight at once. A run's wall clock is
    # essentially its turn count times the per-turn latency, and the task
    # loop is otherwise strictly serial even where the task DAG says two
    # tasks are independent.
    #
    # Raising this above 1 is an informed choice, not a free speed-up. Every
    # concurrent task runs in the SAME agent sandbox against the SAME
    # workspace, and ``depends_on`` is agent-authored — nothing guarantees
    # two "independent" tasks touch disjoint files, so overlapping executors
    # can clobber each other. It also multiplies agent runtimes inside a
    # microVM sbxloop cannot size (#253), so raise ``mem_warn``/``mem_abort``
    # awareness with it. Safe today for outcomes whose tasks are genuinely
    # file-disjoint; per-task workspace isolation is what would make it
    # unconditionally safe.
    max_parallel_tasks: int = Field(default=1, ge=1)


# How a landed PR is written onto the base branch. Squash is the default:
# one autonomous PR becomes one commit, so the base branch's history reads
# one line per issue rather than carrying every fix round separately.
MergeMethod = Literal["squash", "merge", "rebase"]

# The one opt-in human touchpoint in an otherwise human-out-of-the-loop
# run: "chat" parks a run that cleared every bar and asks for one approval
# in the run's chat thread before merging; "off" merges unattended.
MergeGate = Literal["off", "chat"]


class LandingConfig(_ConfigModel):
    """What happens to a run's work after its tasks are built: the pull
    request, the loop's own review of it, the fix rounds, the CI wait and
    the merge — all stages of the same run (see ``engine.model.RunState``).
    Effective only with ``[github] repo`` set; without a repository a run
    stops ``completed`` after its gate.

    The PR opens as a draft and is taken out of draft only once the review
    approves and CI is green, so a watching human sees "draft" mean
    "sbxloop is still working on this". Merging is the default and a run
    never ends ``done`` with an unmerged PR: one that cannot land ends
    ``blocked`` with the PR left open for a human. On a repository whose
    merges publish (sbxloop's own does), every merged run is therefore an
    unattended release.

    ``merge_gate`` is the ONE opt-in human touchpoint of the pipeline:
    ``"chat"`` makes a run that cleared every bar — review, CI,
    reconciliation — park ``gated`` instead of merging, with an approval
    prompt in the run's chat thread (the platform comes from ``[chat]
    backend``). A click on the prompt, ``!sbx merge <item>`` in chat, or
    ``sbxloop daemon ctl merge <item>`` on the host completes the landing:
    update-branch if behind, re-checked CI, the same reconciliation gate,
    then the merge — gh-ops only, no sandbox. There is no deadline; the
    park survives daemon restarts, and the daemon moves on to other work
    while it stands. ``"off"`` (the default) merges unattended as before.

    Two round budgets bound the fix loop. ``max_review_rounds`` is how many
    times the review may request changes before the run gives up;
    ``max_ci_rounds`` covers the mechanical failures — a red project gate
    before delivery, red CI, a conflict with the base, a human requesting
    changes on the PR. Each round is a fix task (BUILD + VERIFY) followed
    by a re-delivery onto the same branch, so a round costs real turns; a
    run past either budget ends ``failed`` with the PR still a draft.

    CI is polled every ``ci_poll_interval_s`` for at most ``ci_timeout_s``
    per wait; the wait is not charged to ``[budgets] max_wall_clock_s``,
    which bounds agent work. ``ci_settle_s`` is how long "no check runs
    yet" must persist before it counts as "this repository has no CI":
    Actions registers its check runs a few seconds after a push, and
    merging in that gap would merge before CI started.

    Branch protection commonly requires a PR to be up to date before
    merging, and the base moves; ``merge_update_attempts`` bounds the
    update-branch calls (each is one API call, not a run) before the PR is
    handed over ``blocked``. 0 disables updating.
    """

    deliver_draft: bool = True
    # The opt-in merge gate; see the docstring. Off = fully unattended.
    merge_gate: MergeGate = "off"
    max_review_rounds: int = Field(default=3, ge=0)
    max_ci_rounds: int = Field(default=2, ge=0)
    # A run that exhausts either budget is one round short, not broken: its
    # branch is green and its PR is open. Under the daemon the item's retry
    # resumes that same run with this many more rounds — once — instead of
    # planning from scratch and opening a second PR (#523). 0 hands an
    # exhausted run straight to a human (`ctl grant-rounds` still works).
    retry_rounds: int = Field(default=2, ge=0)
    ci_poll_interval_s: float = Field(default=60.0, gt=0)
    ci_settle_s: float = Field(default=90.0, ge=0)
    ci_timeout_s: float = Field(default=3600.0, gt=0)
    merge_method: MergeMethod = "squash"
    delete_branch_on_merge: bool = True
    merge_update_attempts: int = Field(default=3, ge=0)
    # What becomes of the review's out-of-scope notes and the fix rounds'
    # deferred findings once the PR merges (#517): filed as issues on the
    # repository with `followup_label` — never the trigger label, a human
    # promotes them — listed in one PR comment instead, or dropped. Capped
    # per run so a chatty reviewer cannot fill the tracker.
    followups: Literal["issues", "comment", "off"] = "issues"
    followup_label: str = "sbxloop:follow-up"
    max_followups_per_run: int = Field(default=5, ge=0)
    # The reviewer is shown the PR's diff inline; past this many characters
    # the diff is clipped and the reviewer reads the rest from the tree.
    review_diff_max_chars: int = Field(default=150_000, ge=10_000)


class DaemonConfig(_ConfigModel):
    """``sbxloop daemon`` — the always-on outer loop.

    The daemon discovers work — GitHub issues carrying ``trigger_label`` in
    the configured repo — claims each one, runs it as one full engine run
    (task graph, gate, pull request, review, fix rounds, CI, merge; see
    ``[landing]``) and reports the outcome back on the issue: closed with
    ``completed_label`` when the PR merged, ``failed_label`` when the run
    gave up, ``blocked_label`` when GitHub would not let the loop finish
    and a human has to look. The daemon never files work of its own; only
    a human labelling an issue (directly, or through the Discord concierge)
    starts a run.

    It is fully autonomous — a label alone starts a run — so the spend
    guardrails here are the only thing standing between a mislabeled issue
    and an empty Copilot budget: a calendar-day run cap, a per-item retry
    cap, and a consecutive-failure circuit breaker. The run cap
    (``max_runs_per_day``) is a wall-clock daily gate: it counts the runs
    *started* during the current calendar day in ``run_cap_timezone``
    (default ``UTC``) and resets at 00:00 in that zone.

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

    workspace_isolation: WorkspaceIsolation = "clone"
    refresh_workspace: bool = True
    state_dir: Path | None = None
    # Must be positive: Event.wait(<= 0) returns immediately and the loop spins.
    poll_interval_s: float = Field(default=60.0, gt=0)
    trigger_label: str = "sbxloop:run"
    in_progress_label: str = "sbxloop:in-progress"
    failed_label: str = "sbxloop:failed"
    # Applied when the work actually lands — the PR merged. The durable
    # "sbxloop did this" mark on the issue.
    completed_label: str = "sbxloop:completed"
    # The run cleared its own bar but GitHub would not finish the PR; it is
    # left open, out of the loop's hands, for a human.
    blocked_label: str = "sbxloop:blocked"
    # The run parked behind the opt-in merge gate ([landing] merge_gate):
    # ready to merge, awaiting one human approval in chat (or `ctl merge`).
    gated_label: str = "sbxloop:awaiting-merge"
    max_runs_per_day: int = 12
    # The day boundary for max_runs_per_day. An explicit IANA zone rather
    # than the process's ambient local time; the counter resets at 00:00 here.
    run_cap_timezone: str = "UTC"
    max_attempts_per_item: int = 2
    # Resumes (after a restart/crash) are not attempts, but each one gets a
    # fresh engine wall clock; past this many per item the interrupted run is
    # settled as a failed attempt instead of resumed (#234). 0 = never resume.
    max_resumes_per_item: int = Field(default=2, ge=0)
    # A claim comment from a process that is gone is not a live claim
    # (#530): one from this host whose pid is dead is reclaimed at once, and
    # one from anywhere older than this with no "Run … started" comment
    # after it is reclaimed too. A few poll intervals: a live claimer
    # starts its run within one.
    claim_stale_after_s: float = Field(default=300.0, ge=0)
    # With several repositories, one that keeps failing to poll is backed
    # off on its own (doubling, capped at an hour) and, after this many
    # consecutive failures — or at once when GitHub says it is gone for this
    # token — suspended from polling until `ctl resume-repo` (#516). The
    # healthy repositories are never punished for a bad neighbour.
    repo_suspend_after: int = Field(default=10, ge=1)
    retry_backoff_s: float = 900.0
    max_consecutive_failures: int = 3
    breaker_cooldown_s: float = 3600.0
    # Keep below the service manager's stop timeout. Cancellation is honored
    # only at task-phase boundaries and interrupted runs are resumable, so
    # this is a courtesy wait, not a correctness requirement.
    shutdown_grace_s: float = 60.0
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
            self.completed_label,
            self.blocked_label,
            self.gated_label,
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
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"daemon.{name} must be >= 1")
        try:
            ZoneInfo(self.run_cap_timezone)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            raise ValueError(
                "daemon.run_cap_timezone must be a valid IANA timezone, "
                f"got {self.run_cap_timezone!r}"
            ) from None
        return self


ChronologyLevel = Literal["quiet", "normal", "verbose"]


ChatBackend = Literal["discord", "slack"]
CHAT_BACKENDS: tuple[ChatBackend, ...] = ("discord", "slack")


class ChatBridgeConfig(_ConfigModel):
    """The knobs every chat backend shares — how the daemon's human channel
    renders a run, whatever service carries it. ``[discord]`` and
    ``[slack]`` extend this with their own ``channel_id``; the bridge that
    serves them (``sbxloop.daemon.chat.ChatBridge``) reads only these fields
    plus ``channel_id`` and ``enabled``."""

    command_prefix: str = "!sbx"
    thread_per_run: bool = True
    # quiet: lifecycle + links + chat; normal: plus agent messages, with each
    # burst of tool calls digested into one line edited in place (#235:
    # streaming every call drowned the channel); verbose: every call.
    chronology_level: ChronologyLevel = "normal"
    # Discord's hard cap is 2000 and Slack's block text cap is 3000; the
    # renderer never exceeds 2000 either way, so one ceiling serves both.
    max_message_chars: int = Field(default=1900, ge=200, le=2000)
    # Rich output: embed cards (Discord) / coloured attachments (Slack) for
    # the run headline, finished report and `!sbx status`; a per-run status
    # message edited in place as tasks progress; at the verbose level,
    # consecutive tool calls batched into one code block of at most
    # tool_batch_lines.
    embeds: bool = True
    status_line: bool = True
    tool_batch_lines: int = Field(default=8, ge=1, le=40)
    # How much of a completed tool call's output is echoed into the thread:
    # tail lines for a success (0 = none) and head+tail lines for a failure,
    # which gets the larger budget because that is what a watcher needs to
    # act. Both are upper bounds — the renderer additionally caps the body
    # and clamps the message to the 2000-character limit.
    tool_output_lines: int = Field(default=0, ge=0, le=20)
    tool_fail_output_lines: int = Field(default=20, ge=0, le=60)

    @property
    def enabled(self) -> bool:  # pragma: no cover - overridden
        return False

    @property
    def channel_ref(self) -> str:
        """The control channel id as text, for logs and the store."""
        return ""


class DiscordConfig(ChatBridgeConfig):
    """The daemon's human channel on Discord: a gateway bot posting each
    run's chronology (agent messages, tool lines, issue/PR links) into a
    thread under a control channel, and relaying replies typed in that
    thread to the running agent as steering. Unset ``channel_id`` disables
    it. The bot token comes from ``DISCORD_BOT_TOKEN`` in the environment /
    .env, never from this file. Anyone who can post in the channel can
    steer — restrict the channel accordingly."""

    channel_id: int | None = None

    @property
    def enabled(self) -> bool:
        return self.channel_id is not None

    @property
    def channel_ref(self) -> str:
        return "" if self.channel_id is None else str(self.channel_id)


# A conversation id: C… (a channel, public or private) or the legacy G…
# (private group). Not U… (a user), D… (a DM) or a #name.
_SLACK_CHANNEL_RE = re.compile(r"^[CG][A-Z0-9]{4,}$")


class SlackConfig(ChatBridgeConfig):
    """The same human channel on Slack: a Socket Mode app posting each run's
    headline in a control channel and its chronology in the thread under
    it; @mentioning the app in that thread steers the run. Unset
    ``channel_id`` disables it. Both tokens come from the environment /
    .env — ``SLACK_BOT_TOKEN`` (``xoxb-…``, the Web API) and
    ``SLACK_APP_TOKEN`` (``xapp-…``, the Socket Mode connection) — never
    from this file."""

    # The channel *id* (``C0123ABCDEF``, from the channel's details pane),
    # not its ``#name``: names are renamed, ids are not, and the store keys
    # threads by it.
    channel_id: str | None = None

    @field_validator("channel_id", mode="before")
    @classmethod
    def _channel_id_is_an_id(cls, value: object) -> object:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if not _SLACK_CHANNEL_RE.match(text):
            raise ValueError(
                f"[slack] channel_id must be the channel's id — C… (or legacy G…) as shown "
                f"in the channel details pane, not a user id, a DM or a #name: got {text!r}"
            )
        return text

    @property
    def enabled(self) -> bool:
        return self.channel_id is not None

    @property
    def channel_ref(self) -> str:
        return self.channel_id or ""


class ChatConfig(_ConfigModel):
    """Which service carries the daemon's human channel. ``backend`` names
    the ``[discord]`` or ``[slack]`` section to use; when it is unset the
    one section with a ``channel_id`` is used, and configuring both without
    choosing is an error. Neither configured means the daemon runs headless
    (no chronology, no steering, ``sbxloop daemon ctl`` only)."""

    backend: ChatBackend | None = None


class ConciergeConfig(_ConfigModel):
    """The control channel's agent: an LLM session that answers @mentions
    in the chat control channel, operates the daemon (every ``!sbx``
    verb), enqueues new work and explains runs, PRs and diffs. It runs in
    a long-lived agent-role sandbox and reaches the daemon only through
    host tools. Effective only when a chat backend (``[discord]`` or
    ``[slack]``) is enabled; needs
    ``COPILOT_GITHUB_TOKEN`` on the daemon host like any agent session.
    It acts with the same authority as ``!sbx`` — anyone who can mention
    the bot can drive the daemon; restrict the channel accordingly."""

    enabled: bool = True
    # None → the top-level ``model``.
    model: str | None = None
    # One message's wall-clock budget (the whole tool loop).
    timeout_s: float = Field(default=180.0, ge=30, le=900)
    # Replies longer than this are clipped before being split into
    # chat messages.
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
    # Let the concierge write to issues in the configured repo: file one for
    # a run (it is queued the moment it is filed), list them, comment on
    # them, label an existing one for a run, and close one. Closing is the
    # one act that waits for the person's explicit say-so.
    create_issues: bool = True
    # A filing-blocking clarifying question waits at most this long for the
    # asker; then the concierge is told to proceed and the issue files with
    # its stated assumption — no goal is ever silently dropped. Also the
    # clickable-choice TTL, so buttons and the auto-file expire in step.
    clarify_ttl_s: float = Field(default=900.0, ge=60, le=86400)


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


class AgentConfig(_ConfigModel):
    """Which SDK runs the agent personas inside the agent sandbox (#533).

    ``copilot`` (the default, unchanged behaviour) runs the GitHub Copilot
    SDK and needs ``COPILOT_GITHUB_TOKEN`` on the host. ``claude`` runs the
    Claude Agent SDK — the Claude Code harness — and needs
    ``ANTHROPIC_API_KEY`` on the host; provisioning installs Node and the
    Claude Code CLI into the agent sandbox and allows ``api.anthropic.com``
    egress. Either way the credential is injected into the agent sandbox
    alone, and the top-level ``model`` key names the model the chosen
    backend runs (``"auto"`` lets the backend pick its default).
    An unknown value fails config loading with the accepted choices named.
    """

    backend: Literal["copilot", "claude"] = "copilot"


class Config(_ConfigModel):
    model: str = "auto"
    agent: AgentConfig = Field(default_factory=AgentConfig)
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
    landing: LandingConfig = Field(default_factory=LandingConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    concierge: ConciergeConfig = Field(default_factory=ConciergeConfig)

    @field_validator("state_dir", mode="after")
    @classmethod
    def _expand_home(cls, value: Path) -> Path:
        # `state_dir = "~/.sbxloop"` in TOML must mean the home directory,
        # not a literal "~" directory under the project.
        return value.expanduser()

    @model_validator(mode="after")
    def _chat_backend_is_consistent(self) -> Config:
        """A named backend must have its section configured, and two
        configured sections need a name — both are the operator's mistake
        to fix, reported before the daemon starts rather than as a silently
        headless loop."""
        explicit = self.chat.backend
        if explicit is not None:
            section = self.chat_section(explicit)
            if not section.enabled:
                raise ValueError(
                    f'[chat] backend = "{explicit}" but [{explicit}] channel_id is not set'
                )
        elif self.discord.enabled and self.slack.enabled:
            raise ValueError(
                "both [discord] and [slack] have a channel_id; pick one with "
                '[chat] backend = "discord" | "slack"'
            )
        return self

    def chat_section(self, backend: ChatBackend) -> DiscordConfig | SlackConfig:
        return self.discord if backend == "discord" else self.slack

    @property
    def chat_backend(self) -> ChatBackend | None:
        """The chat service carrying the daemon's human channel: the explicit
        ``[chat] backend``, else the one section with a ``channel_id``, else
        None (headless). Computed on read so a ``model_copy`` that sets a
        channel (the CLI's ``--discord-channel`` / ``--slack-channel``)
        is honoured without re-validating."""
        if self.chat.backend is not None:
            return self.chat.backend
        for backend in CHAT_BACKENDS:
            if self.chat_section(backend).enabled:
                return backend
        return None

    @property
    def chat_settings(self) -> DiscordConfig | SlackConfig | None:
        """The active backend's section, or None when the daemon is headless."""
        backend = self.chat_backend
        return None if backend is None else self.chat_section(backend)

    def workspace_for_repo(self, repo: str | None) -> Path | None:
        """The host checkout runs for ``repo`` clone and refresh from.

        Resolution, in order:

        1. the repo entry's own ``workspace``;
        2. the legacy ``[sandbox] workspace`` — but only when it can be shown
           to belong to this repository: with a single enabled repo (the
           unchanged single-repo deployment) it applies as before; with
           several, only when the checkout's ``origin`` names this entry;
        3. otherwise ``None``.

        It never returns a checkout that belongs to a different repository:
        that is exactly the multi-repo failure this exists to prevent.
        """
        from sbxloop import hostgit

        entry = self.github.find_repo(repo)
        if entry is not None and entry.workspace is not None:
            return entry.workspace.expanduser()
        legacy = self.sandbox.workspace
        if legacy is None:
            return None
        legacy = legacy.expanduser()
        if entry is None:
            # No repo entries at all (GitHub off, or the legacy [github] repo
            # spelling): the single daemon-wide workspace is all there is.
            return legacy if not self.github.enabled_repos() else None
        if not self.github.multi_repo and entry.enabled:
            return legacy
        return legacy if hostgit.origin_matches_repo(legacy, entry.repo) else None


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
