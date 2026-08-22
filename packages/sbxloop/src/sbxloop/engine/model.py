"""Engine domain models: runs, tasks, plans, verdicts, the task graph."""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RunState = Literal[
    "created",
    "provisioning",
    "decomposing",
    "running",
    "finalizing",
    "completed",
    "failed",
    "cancelled",
]

TaskState = Literal[
    "pending",
    "planning",
    "executing",
    "scrutinizing",
    "verifying",
    "validating",
    "done",
    "failed",
    "skipped",
]

TERMINAL_TASK_STATES: frozenset[str] = frozenset({"done", "failed", "skipped"})
TERMINAL_RUN_STATES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
RESUMABLE_RUN_STATES: frozenset[str] = frozenset(
    # 'cancelled' is terminal for reporting/liveness purposes (nothing is in
    # flight, so `list_runs` must not show it as active) yet an operator may
    # still `sbxloop resume` a run they cancelled mid-flight — the same
    # terminal+resumable combination 'failed' already has.
    {"created", "provisioning", "decomposing", "running", "finalizing", "failed", "cancelled"}
)

Phase = Literal["decompose", "plan", "execute", "scrutinize", "verify", "validate"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskSpec(_Model):
    """One unit of the decomposed outcome (agent-authored, engine-validated)."""

    id: str
    title: str
    description: str = ""
    depends_on: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    verify_commands: list[str] = Field(default_factory=list)


class TaskGraph(_Model):
    tasks: list[TaskSpec]

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


class EgressSpec(_Model):
    """One plan-declared network need: a domain EXECUTE must reach, and why.

    Granted to the agent sandbox just before EXECUTE — but only within the
    operator's ``[policy]`` bounds (checked at plan time, enforced again at
    grant time). ``domain`` is a bare domain or ``*.domain`` wildcard; no
    scheme, path, or port, and never the bare ``"*"``.
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


class PlanModel(_Model):
    """The agent's plan for one task."""

    steps: list[str]
    expected_artifacts: list[str] = Field(default_factory=list)
    verify_commands: list[str] = Field(default_factory=list)
    # External domains EXECUTE needs beyond the baseline, with justification.
    egress: list[EgressSpec] = Field(default_factory=list)


class Issue(_Model):
    severity: Literal["low", "medium", "high"] = "medium"
    detail: str


class Verdict(_Model):
    """Critic output: scrutinize uses pass/revise, validate uses accept/reject.

    ``verify_suspect`` is the scrutinizer's ruling on the *check* rather
    than the work (#231): a verify command can be portable and runnable and
    still assert the wrong thing (field failure r567rsm4e — an ``od``
    column layout that never matches). The executor cannot edit verify
    commands and the mechanical verify phase has no opinion, so the
    scrutinizer — which sees the failing command next to the passing code
    — is the only stage placed to say "this check itself is wrong". The
    engine turns a ``pass`` + ``verify_suspect`` into an immediate replan
    instead of letting revisions burn against a check no revision can fix.
    """

    verdict: Literal["pass", "revise", "accept", "reject"]
    issues: list[Issue] = Field(default_factory=list)
    feedback: str = ""
    verify_suspect: bool = False
    verify_suspect_reason: str = ""

    @model_validator(mode="after")
    def _check_verify_suspect(self) -> Verdict:
        # The reason is the whole payload: it is what the planner is told
        # about why the old check was wrong. A bare flag would spend a
        # replan on "no reason given", so it is retried like a steer
        # without guidance.
        if self.verify_suspect and not self.verify_suspect_reason.strip():
            raise ValueError(
                "`verify_suspect: true` requires a non-empty `verify_suspect_reason` "
                "saying concretely what the check asserts wrongly"
            )
        return self


SteerAction = Literal["continue", "steer_task", "steer_run"]


class SteerVerdict(_Model):
    """STEER output: a user-facing reply to a chat message, plus the course
    change the message requires (if any).

    - ``continue``: the message changes nothing (a question, a status check);
      only ``reply`` is used.
    - ``steer_task``: the current task must be done differently — it is
      re-planned with ``guidance`` as feedback (without spending its replan
      budget: this is user direction, not a failure).
    - ``steer_run``: the whole remaining run changes direction — ``guidance``
      becomes standing guidance injected into every later plan/execute
      prompt, persisted so a resumed run keeps it.
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


class TaskRecord(_Model):
    """Persisted per-task state."""

    spec: TaskSpec
    state: TaskState = "pending"
    revisions: int = 0
    replans: int = 0
    last_feedback: str = ""
    session_id: str | None = None
    plan: PlanModel | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_TASK_STATES


class RunRecord(_Model):
    run_id: str
    outcome: str
    state: RunState
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
    # orphaned run, or cancellation attribution. None for runs still in
    # flight or terminated through the ordinary in-process path.
    reason: str | None = None


class RunResult(_Model):
    """What LoopEngine.start()/resume() returns to callers."""

    run_id: str
    state: RunState
    tasks: list[TaskRecord] = Field(default_factory=list)
    workspace: Path | None = None
    mounted: bool = False
    # Sandbox names deliberately left alive (keep_sandboxes/keep_on_failure),
    # so callers can point the user at `sbxloop shell`.
    kept_sandboxes: list[str] = Field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.state == "completed"


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
    """Regular files under root, sorted for stable output, partitioned into
    kept files and per-entry counts of files whose path contains an excluded
    component (at any depth, so vendored nested .git dirs are caught too).

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
    candidates = [p for p in sorted(root.rglob("*")) if p.is_file()]
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


def artifacts_dir(run: RunRecord | RunResult, state_dir: Path) -> Path | None:
    """The single host directory holding a run's artifacts.

    Mounted runs write straight into the workspace; unmounted runs are
    harvested to ``runs/<run>/artifacts``. Every reader (run summary,
    ``sbxloop artifacts``, delivery) resolves through here. None means the
    run never got as far as provisioning a workspace.
    """
    if run.workspace is None:
        return None
    if run.mounted:
        return run.workspace
    return state_dir / "runs" / run.run_id / "artifacts"
