"""Engine domain models: runs, tasks, plans, verdicts, the task graph."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sbxloop.paths import SbxloopHome

# What a run is for (#755). `code` is the developer loop: a task graph that
# ends in a pull request. `workload` runs the operator persona through the
# same run shape — sandboxes, store, events, thread — but its stages are
# plan → execute → judge → publish, and its result is whatever the ask
# named, not a PR. Persisted with the run; a resume never re-derives it.
RunKind = Literal["code", "workload"]

RunState = Literal[
    # the task graph
    "created",
    "provisioning",
    "decomposing",
    "building",
    # the pipeline: one run carries its work all the way to a merged PR
    "gating",
    "delivering",
    "reviewing",
    "fixing",
    "awaiting_ci",
    "landing",
    # the workload stages (#755): the operator's plan, its execution, the
    # judgment against the ask, and the publication of the result
    "planning",
    "executing",
    "judging",
    "publishing",
    # terminal
    "merged",
    "completed",
    "failed",
    "blocked",
    "cancelled",
    "gated",
    "awaiting_review",
    "held",
]

# The post-build stages in order. `runs.stage` records the last non-terminal
# state a run entered, so `resume` re-enters the pipeline where it stopped
# rather than at the task graph.
PIPELINE_STAGES: tuple[str, ...] = (
    "gating",
    "delivering",
    "reviewing",
    "fixing",
    "awaiting_ci",
    "landing",
)

# A `workload` run's stages in order (#755). `planning` and `executing` are
# the task graph under other names — a resume from either re-enters the
# graph where it stopped, exactly as `decomposing`/`building` do for a code
# run; `judging` and `publishing` re-enter themselves.
WORKLOAD_STAGES: tuple[str, ...] = (
    "planning",
    "executing",
    "judging",
    "publishing",
)

TaskState = Literal[
    "pending",
    "executing",
    "verifying",
    "done",
    "failed",
    "skipped",
]

TERMINAL_TASK_STATES: frozenset[str] = frozenset({"done", "failed", "skipped"})
# `merged` is the end of a run that delivered to a repository; `completed`
# the end of one with no repository to deliver to (a local `sbxloop run`
# stops after the gate). `blocked` means the run cleared its own bar but
# GitHub would not finish the PR — a protection rule, a draft that would not
# clear, CI that never reported — so a human has to look; it is terminal for
# liveness and resumable once the cause is fixed. `failed` and `cancelled`
# are terminal for reporting (nothing is in flight, so `list_runs` must not
# show them as active) yet an operator may still `sbxloop resume` them.
# `gated` is the opt-in merge gate (`[landing] merge_gate`): the run
# cleared every bar and parked awaiting one human approval; the daemon's
# approve path completes the landing with gh ops alone — no engine is
# resurrected — so the state is terminal and deliberately NOT resumable.
# `awaiting_review` (#675) is the run parked on a base that requires an
# approving review the loop cannot give its own PR: every bar it can clear
# is cleared, no sandbox is kept, and a person on GitHub ends the wait.
# Terminal for liveness; resumable, because a reviewer's changes-requested
# is a fix round the engine runs (`resume` re-enters the landing stage).
# `held` is a workload parked at its publishing stage by a profile's
# `publish = "hold"` (#760): the result is judged and persisted, no sandbox
# is kept, and a person releases it. Terminal for liveness; resumable,
# because the release IS a `resume` at the publishing stage.
TERMINAL_RUN_STATES: frozenset[str] = frozenset(
    {"merged", "completed", "failed", "blocked", "cancelled", "gated", "awaiting_review", "held"}
)
RESUMABLE_RUN_STATES: frozenset[str] = frozenset(
    {
        "created",
        "provisioning",
        "decomposing",
        "building",
        *PIPELINE_STAGES,
        *WORKLOAD_STAGES,
        "failed",
        "blocked",
        "cancelled",
        "awaiting_review",
        "held",
    }
)

Phase = Literal[
    "decompose",
    "build",
    "verify",
    "gate",
    "review",
    "steer",
    "followup",
    # A workload's own phases: the operator's plan and execute rows, and
    # the judge — per task (with a task_id) as the LLM verdict on one
    # task's work, and once at the end (task_id None) as the mechanical
    # re-check over the finished workspace.
    "plan",
    "execute",
    "judge",
]

# What a fix round is for. `review` rounds are charged to the review budget;
# everything else — a red gate, red CI, a base conflict, a human objecting on
# the PR — to the CI budget, because those are the rounds red CI would cost.
FixKind = Literal["review", "gate", "ci", "conflict", "human", "bot"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EgressSpec(_Model):
    """One declared network need: a domain BUILD must reach, and why.

    Declared by the decomposer per task and granted to the agent sandbox
    just before BUILD — but only within the operator's ``[policy]`` bounds
    (checked at graph acceptance, enforced again at grant time). ``domain``
    is a bare domain or ``*.domain`` wildcard; no scheme, path, or port,
    and never the bare ``"*"``.
    """

    domain: str
    reason: str = ""

    @field_validator("domain")
    @classmethod
    def _check_domain(cls, value: str) -> str:
        from sbxloop.policy import valid_pattern

        value = value.strip().lower()
        if not valid_pattern(value):
            raise ValueError(
                f"egress domain must be a domain or *.domain wildcard "
                f"(no scheme/path/port), got {value!r}"
            )
        return value


class TaskNeeds(_Model):
    """What a workload task declares it will need, by name — the operator's
    plan asking, never taking.

    ``hosts`` are domains the task will reach (the shape of ``egress``);
    ``credentials`` are catalogue names, each a request for a grant on the
    service sandbox — a credential is never a value in the agent's box;
    ``sink`` names where the result should go and ``repo`` a repository it
    needs. The plan records the needs; whether they are met is decided by
    the run's profile, which is a later concern than the plan.
    """

    hosts: list[str] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list)
    sink: str | None = None
    repo: str | None = None

    @field_validator("hosts")
    @classmethod
    def _check_hosts(cls, value: list[str]) -> list[str]:
        from sbxloop.policy import valid_pattern

        hosts = [host.strip().lower() for host in value]
        bad = [host for host in hosts if not valid_pattern(host)]
        if bad:
            raise ValueError(
                f"needs.hosts must be domains or *.domain wildcards (no scheme/path/port), "
                f"got {bad!r}"
            )
        return hosts

    @property
    def empty(self) -> bool:
        return not (self.hosts or self.credentials or self.sink or self.repo)


class TaskSpec(_Model):
    """One unit of the decomposed outcome (agent-authored, engine-validated)."""

    id: str
    title: str
    description: str = ""
    depends_on: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    verify_commands: list[str] = Field(default_factory=list)
    # External domains this task's BUILD needs beyond the baseline, with
    # justification. Decomposer-authored, like the verify commands.
    egress: list[EgressSpec] = Field(default_factory=list)
    # A workload task's declared needs (operator-authored); a code run's
    # tasks leave it empty.
    needs: TaskNeeds = Field(default_factory=TaskNeeds)


class TaskGraph(_Model):
    tasks: list[TaskSpec]
    # The pull request's title in the repository's own commit-subject
    # style (#621), decomposer-authored; None leaves the outcome as the
    # title. One line, whitespace-folded; an empty answer is None.
    pr_title: str | None = None

    @field_validator("pr_title")
    @classmethod
    def _fold_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        folded = " ".join(str(value).split())
        return folded or None

    @model_validator(mode="after")
    def _check_graph(self) -> TaskGraph:
        if not self.tasks:
            raise ValueError("task graph must contain at least one task")
        ids = [t.id for t in self.tasks]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate task ids: {ids}")
        known = set(ids)
        for task in self.tasks:
            unknown = [d for d in task.depends_on if d not in known]
            if unknown:
                raise ValueError(f"task {task.id} depends on unknown tasks: {unknown}")
            if task.id in task.depends_on:
                raise ValueError(f"task {task.id} depends on itself")
        try:
            self.topo_order()
        except CycleError as exc:
            raise ValueError(f"task graph contains a cycle: {exc.args}") from exc
        return self

    def topo_order(self) -> list[TaskSpec]:
        """Dependency order; ties broken by authored order (depth, position)."""
        by_id = {t.id: t for t in self.tasks}
        # TopologicalSorter validates the cycle; the actual ordering is
        # deterministic: dependency depth first, authored position second.
        list(TopologicalSorter({t.id: set(t.depends_on) for t in self.tasks}).static_order())
        position = {t.id: i for i, t in enumerate(self.tasks)}
        return sorted(
            self.tasks,
            key=lambda t: (self._depth(t, by_id), position[t.id]),
        )

    def _depth(self, task: TaskSpec, by_id: dict[str, TaskSpec]) -> int:
        if not task.depends_on:
            return 0
        return 1 + max(self._depth(by_id[d], by_id) for d in task.depends_on)


class WorkloadPlan(TaskGraph):
    """The operator's plan: the same task graph the engine schedules, under
    a run title instead of a pull request's, each task carrying its
    acceptance criteria as the judge's whole exam and its declared needs."""

    title: str | None = None

    @field_validator("title")
    @classmethod
    def _fold_run_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        folded = " ".join(str(value).split())
        return folded or None


class JudgeVerdict(_Model):
    """The judge's answer on one task: whether every acceptance criterion
    is met, the ones that are not (quoted, so the next attempt knows what
    to fix), and its notes. ``unmet`` empty with ``passed`` false is a
    verdict that names nothing to fix — rejected, so a judge cannot fail
    work without saying why."""

    passed: bool
    unmet: list[str] = Field(default_factory=list)
    notes: str = ""

    @field_validator("unmet")
    @classmethod
    def _strip_unmet(cls, value: list[str]) -> list[str]:
        return [" ".join(str(item).split()) for item in value if str(item).strip()]

    @model_validator(mode="after")
    def _check_verdict(self) -> JudgeVerdict:
        if not self.passed and not self.unmet:
            raise ValueError("a failing verdict must name at least one unmet criterion")
        return self


SteerAction = Literal["continue", "steer_task", "steer_run"]


class SteerVerdict(_Model):
    """STEER output: a user-facing reply to a chat message, plus the course
    change the message requires (if any).

    - ``continue``: the message changes nothing (a question, a status check);
      only ``reply`` is used.
    - ``steer_task``: the current task must be done differently — its build
      session is discarded and restarted with ``guidance`` as feedback
      (without spending its replan budget: this is user direction, not a
      failure).
    - ``steer_run``: the whole remaining run changes direction — ``guidance``
      becomes standing guidance injected into every later build prompt,
      persisted so a resumed run keeps it.
    """

    reply: str
    action: SteerAction = "continue"
    guidance: str = ""

    @model_validator(mode="after")
    def _check_guidance(self) -> SteerVerdict:
        if self.action != "continue" and not self.guidance.strip():
            raise ValueError(
                f"action {self.action!r} requires non-empty `guidance` for the planner"
            )
        return self


# How many of a task's files an output lists by path; the rest are counted.
MAX_OUTPUT_FILES = 200
MAX_SUMMARY_CHARS = 200
# The heading operator_execute.md asks the report to end with; matched as a
# line so a "## Result" the prose mentions inline does not cut the text.
_RESULT_HEADING = re.compile(r"^#{1,6}\s*results?\b[^\n]*\n", re.IGNORECASE | re.MULTILINE)


class TaskOutput(_Model):
    """What one workload task produced (#757): the operator's own account
    of the result and the files it left in the data directory.

    ``text`` is the report's ``## Result`` section (the whole report when
    the operator wrote none), ``summary`` its first line; ``files`` are the
    data-directory paths the task's attempts touched, listed mechanically
    rather than as the report names them, so the sinks (#759) publish what
    is there and not what was claimed. Replaced on every attempt: the task
    holds one output, its latest.
    """

    summary: str = ""
    text: str = ""
    files: list[str] = Field(default_factory=list)
    # Files past MAX_OUTPUT_FILES: counted, never silently dropped (#67).
    more_files: int = 0
    # Reserved for structured results a sink can carry whole (#759).
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def file_count(self) -> int:
        return len(self.files) + self.more_files

    @classmethod
    def from_report(
        cls, report: str, *, files: Sequence[str] = (), more_files: int = 0
    ) -> TaskOutput:
        """Cut the output from the operator's report: the text under its
        last ``## Result`` heading (the whole report when it wrote none —
        the judge decides what that is worth, not the parser), the first
        non-empty line of that as the summary."""
        text = report.strip()
        match = _RESULT_HEADING.split(text)
        if len(match) > 1:
            text = match[-1].strip()
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        summary = " ".join(first.split())
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[: MAX_SUMMARY_CHARS - 1] + "…"
        return cls(summary=summary, text=text, files=list(files), more_files=more_files)


class Published(_Model):
    """One place a workload's result went (#759): the sink and where it
    landed — an issue's URL, the artifacts directory, ``chat`` for the
    reply posted where the run was asked for (the thread, the terminal).
    Persisted on the run row, so a resume at publishing skips what already
    landed and the record says where a result is."""

    sink: str
    location: str
    # What the sink carried, for the record: task ids and a file count.
    tasks: list[str] = Field(default_factory=list)
    files: int = 0


class TaskRecord(_Model):
    """Persisted per-task state."""

    spec: TaskSpec
    state: TaskState = "pending"
    revisions: int = 0
    replans: int = 0
    last_feedback: str = ""
    session_id: str | None = None
    # Fingerprints (command + normalised output) of every verify failure
    # seen on this task, across revisions AND replans. A repeat means the
    # identical check failed the identical way again (#387).
    verify_fingerprints: list[str] = Field(default_factory=list)
    # Set once a fingerprint repeats: the check, not the code, is the thing
    # to change, and no further identical revision is spent.
    verify_suspect: bool = False
    # A workload task's result (#757); None for a code run's tasks, whose
    # result is the tree the build left behind.
    output: TaskOutput | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_TASK_STATES


class RunRecord(_Model):
    run_id: str
    outcome: str
    state: RunState
    # `code` for every run the developer loop drives — and for every row a
    # database from before #755 holds, which the migration reads back as
    # such; `workload` for a run of the operator persona.
    kind: RunKind = "code"
    created_at: float
    updated_at: float
    # Host workspace directory and whether it was live-mounted into the
    # agent VM (False → artifacts are harvested to runs/<run>/artifacts).
    workspace: Path | None = None
    mounted: bool = False
    # Why this run's sandboxes were deliberately left alive ("debug",
    # "manual"); None means normal teardown applied. `sandbox prune`
    # excludes kept runs unless asked to include them.
    kept_reason: str | None = None
    # Why this run reached its terminal state: reconciliation of an
    # orphaned run, cancellation attribution, an exhausted budget, or what
    # GitHub refused. None for runs still in flight or that ended merged.
    reason: str | None = None
    # Pipeline bookkeeping. `stage` is the last non-terminal state the run
    # entered — a terminal state overwrites `state` but leaves this, so a
    # resume of a failed/blocked run knows where to re-enter. The PR fields
    # are set at the first delivery; `head_sha` moves with every re-delivery
    # and is what CI checks and the merge are judged against. The round
    # counters are the budgets spent (see `[landing]`); `update_head` is the
    # head an update-branch was requested at, so a later poll can tell an
    # update still in flight from one that landed.
    stage: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    pr_node_id: str | None = None
    branch: str | None = None
    head_sha: str | None = None
    # The plan's own PR title (#621), moved by a fix round that retitled;
    # None → the outcome names the PR.
    pr_title: str | None = None
    review_rounds: int = 0
    ci_rounds: int = 0
    update_attempts: int = 0
    update_head: str | None = None
    last_verdict: str | None = None
    # Which fix-round budget the run ran out of (`review` / `ci`), set when a
    # round exhaustion ends the run and cleared by `grant_rounds` — so the
    # daemon can tell a run that stopped one round short from one that broke
    # (#523). `granted_rounds` extends both `[landing]` budgets for this run:
    # the daemon grants `retry_rounds` once, an operator grants more.
    exhausted: str | None = None
    granted_rounds: int = 0
    # The `[[credentials]]` names this run is granted (#765): what its
    # service sandbox holds and what `call_service` may name. Persisted so
    # a resume re-provisions the same sandbox; empty for every run that
    # asked for none — which is every `code` run today.
    credentials: list[str] = Field(default_factory=list)
    # Where a workload's result went (#759), sink by sink, as it lands;
    # empty for a code run (its result is the pull request) and for a
    # workload that has not published yet.
    published: list[Published] = Field(default_factory=list)


class RunResult(_Model):
    """What LoopEngine.start()/resume() returns to callers."""

    run_id: str
    state: RunState
    kind: RunKind = "code"
    tasks: list[TaskRecord] = Field(default_factory=list)
    workspace: Path | None = None
    mounted: bool = False
    # Sandbox names deliberately left alive (keep_sandboxes/keep_on_failure),
    # so callers can point the user at `sbxloop shell`.
    kept_sandboxes: list[str] = Field(default_factory=list)
    # The delivered pull request, when the run got that far, and why the run
    # stopped short of `merged` when it did.
    pr_number: int | None = None
    pr_url: str | None = None
    reason: str | None = None
    # The fix-round budget that ran out, when that is why the run failed.
    exhausted: str | None = None
    # A workload's closing line (#757), composed from its tasks' outputs;
    # None for a code run, whose result is the pull request.
    summary: str | None = None
    # Where the result went (#759), for a workload that published.
    published: list[Published] = Field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.state in ("merged", "completed")

    @property
    def outputs(self) -> list[tuple[str, TaskOutput]]:
        """(task id, output) for every task that produced one."""
        return [(t.spec.id, t.output) for t in self.tasks if t.output is not None]


def workload_summary(tasks: Sequence[TaskRecord], title: str | None = None) -> str:
    """A workload's closing line (#757): what was asked, how many tasks
    the judge passed, and what each produced — composed from the tasks'
    persisted outputs so the engine's result and the daemon's report read
    the same store row the same way, with no extra model turn."""
    done = sum(1 for t in tasks if t.state == "done")
    head = f"{done}/{len(tasks)} task(s) passed the judge" if tasks else "no tasks ran"
    if title:
        head = f"{title} — {head}"
    lines = [head]
    for t in tasks:
        if t.output is None:
            continue
        line = f"{t.spec.id}: {t.output.summary or '(no result reported)'}"
        if count := t.output.file_count:
            line += f" ({count} file{'s' if count != 1 else ''})"
        lines.append(line)
    return "\n".join(lines)


# Where the agent leaves the pull request's description (#678): under the
# workspace's `.sbxloop/`, which delivery never ships (it is the first
# exclude below), read by the engine before the delivery — the body's twin
# of `engine.review.PR_TITLE_FILE`.
PR_BODY_FILE = ".sbxloop/pr-body"

# Path components excluded from artifact listings, harvest and delivery by
# default. A denylist, not "anything dot-prefixed": agents legitimately
# produce .github/workflows, .gitignore, .env.example and friends — only
# machine-generated dependency and build trees are dropped. An agent's .git
# or node_modules would otherwise swamp listings, balloon an unmounted run's
# harvest, and land in a delivery PR diff. Overridable via [artifacts]
# exclude (which replaces this list wholesale).
#
# The list is static rather than derived from [sandbox] languages, which
# selects what gets PRE-INSTALLED, not what the workspace ends up holding.
# That key defaults to toolchains.DEFAULT_LANGUAGES ("python",) and setting
# it replaces rather than extends, so deriving from it would leave every
# non-Python default run harvesting node_modules/target/obj — the exact bug
# this list exists to fix — and would drop __pycache__ back into listings for
# anyone who set languages = ["rust"]. Provisioning is only a head start
# besides: agents self-heal missing toolchains mid-run, so a Rust-configured
# run that npm-installs a docs site still grows a node_modules. A name that
# never occurs costs nothing to carry, so the narrowing buys nothing either.
# Entries earn a place only if they are (i) conventionally
# gitignored in their ecosystem and (ii) implausible as hand-written content
# in any other. That rule deliberately keeps out the generic names —
# "bin", "build", "dist", "out", "lib", "vendor" — which are build output in
# one ecosystem and checked-in scripts or deps in the next; a project that
# wants those dropped can say so in [artifacts] exclude. Exclusions are
# always counted and surfaced, never silent (#67), so a wrong call here is
# visible and recoverable rather than a quiet truncation.
DEFAULT_ARTIFACT_EXCLUDES = (
    # Run/VCS state, any ecosystem.
    ".git",
    ".sbxloop",
    # Python: virtualenvs, bytecode, tool caches, packaging metadata (the
    # egg-info directory is named after the project, so only a glob can
    # catch it).
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "*.egg-info",
    "__pycache__",
    "venv",
    # JavaScript / TypeScript.
    "node_modules",
    # Rust (cargo) and Java (Maven) both name their build tree "target".
    "target",
    # Java (Gradle's project-local cache; "build" is deliberately kept).
    ".gradle",
    # C# / .NET intermediate objects ("bin" is deliberately kept — too many
    # ecosystems check hand-written scripts into it).
    "obj",
    # Ruby (bundler's project-local config/cache).
    ".bundle",
    # C / C++ (CMake scratch; Go needs no entry — GOCACHE and the module
    # cache live under $HOME, outside the harvested workspace).
    "CMakeFiles",
)


@dataclass(frozen=True)
class ArtifactScan:
    """artifact_files plus what was excluded, so callers can surface it —
    silent truncation of deliveries is exactly the bug (#67)."""

    files: list[Path]
    excluded: dict[str, int]  # exclude entry -> count of files dropped under it

    @property
    def excluded_total(self) -> int:
        return sum(self.excluded.values())

    @property
    def excluded_note(self) -> str | None:
        """Human-readable note like '3 file(s) excluded (.git)', or None."""
        if not self.excluded:
            return None
        return f"{self.excluded_total} file(s) excluded ({', '.join(sorted(self.excluded))})"


# Tally key for files dropped by the tree's own .gitignore rules — the one
# exclusion that is not an entry of the exclude list.
GITIGNORED = "gitignored"


def scan_artifacts(
    root: Path, exclude: Sequence[str] = DEFAULT_ARTIFACT_EXCLUDES, *, gitignore: bool = True
) -> ArtifactScan:
    """Regular files and symlinks under root, sorted for stable output,
    partitioned into kept files and per-entry counts of files whose path
    contains an excluded component (at any depth, so vendored nested .git
    dirs are caught too). A symlink is kept as the link itself — whatever
    it points at, resolving or not — because that is what git tracks and
    what a snapshot delivery must reproduce (#695); nothing is followed
    into a symlinked directory.

    Entries containing a glob metacharacter match components via fnmatch
    (``*.egg-info`` — dynamically named directories that no exact name can
    cover); the exclusion tally reports the pattern, not each matched name.

    With ``gitignore`` (the default) files the tree's own ``.gitignore``
    rules ignore are dropped too, tallied under ``GITIGNORED`` — the
    exclude list is a cross-ecosystem denylist that cannot know a project's
    ``dist/`` or generated ``_version.py`` are byproducts, but its
    ``.gitignore`` does (#249). Name-based entries take precedence in the
    tally; ``exclude`` stays the operator's override on top of both.
    """
    candidates = [p for p in sorted(root.rglob("*")) if p.is_symlink() or p.is_file()]
    ignored: frozenset[str] = frozenset()
    if gitignore and ((root / ".git").exists() or any(p.name == ".gitignore" for p in candidates)):
        # Local import: hostgit pulls in GitPython, which this pure model
        # module must not require at import time.
        from sbxloop.hostgit import gitignored_files

        ignored = gitignored_files(root) or frozenset()
    files: list[Path] = []
    excluded: dict[str, int] = {}
    for p in candidates:
        rel = p.relative_to(root)
        hit = exclusion_hit(rel.parts, exclude)
        if hit is None and rel.as_posix() in ignored:
            hit = GITIGNORED
        if hit is None:
            files.append(p)
        else:
            excluded[hit] = excluded.get(hit, 0) + 1
    return ArtifactScan(files=files, excluded=excluded)


def exclusion_hit(parts: Sequence[str], exclude: Sequence[str]) -> str | None:
    """The exclude entry (name or glob) matching any component of a relative
    path, or None when the path is kept. Shared by the workspace scan and
    the git-diff delivery path so both apply one denylist."""
    names = frozenset(entry for entry in exclude if not _is_glob(entry))
    patterns = [entry for entry in exclude if _is_glob(entry)]
    for part in parts:
        if part in names:
            return part
        pattern = next((p for p in patterns if fnmatch.fnmatch(part, p)), None)
        if pattern is not None:
            return pattern
    return None


def _is_glob(entry: str) -> bool:
    return any(ch in entry for ch in "*?[")


def artifact_files(root: Path, exclude: Sequence[str] = DEFAULT_ARTIFACT_EXCLUDES) -> list[Path]:
    """The kept files of scan_artifacts, for callers that need no exclusion note."""
    return scan_artifacts(root, exclude).files


def artifacts_dir(run: RunRecord | RunResult, home: SbxloopHome) -> Path | None:
    """The single host directory holding a run's artifacts.

    Mounted runs write straight into the workspace; unmounted runs are
    harvested to ``runs/<run>/artifacts``. Every reader (run summary,
    ``sbxloop artifacts``, delivery) resolves through here. None means the
    run never got as far as provisioning a workspace.

    A workload's artifacts are what its ``artifact`` sink delivered (#759)
    — the files its tasks declared, copied to ``runs/<run>/artifacts``
    mounted or not — never its whole data directory, so the listing is
    the result and not the working state around it.
    """
    if run.workspace is None:
        return None
    if run.kind == "workload":
        return home.run_artifacts(run.run_id)
    if run.mounted:
        return run.workspace
    return home.run_artifacts(run.run_id)
