"""Engine domain models: runs, tasks, plans, verdicts, the task graph."""

from __future__ import annotations

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
    {"created", "provisioning", "decomposing", "running", "finalizing", "failed"}
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
    """Critic output: scrutinize uses pass/revise, validate uses accept/reject."""

    verdict: Literal["pass", "revise", "accept", "reject"]
    issues: list[Issue] = Field(default_factory=list)
    feedback: str = ""


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


# Path components excluded from artifact listings and delivery by default.
# A denylist, not "anything dot-prefixed": agents legitimately produce
# .github/workflows, .gitignore, .env.example and friends — only genuinely
# noisy state dirs are dropped (an agent's .git would otherwise swamp
# listings and deliveries). Overridable via [artifacts] exclude.
DEFAULT_ARTIFACT_EXCLUDES = (".git", ".sbxloop")


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


def scan_artifacts(root: Path, exclude: Sequence[str] = DEFAULT_ARTIFACT_EXCLUDES) -> ArtifactScan:
    """Regular files under root, sorted for stable output, partitioned into
    kept files and per-entry counts of files whose path contains an excluded
    component (at any depth, so vendored nested .git dirs are caught too)."""
    names = frozenset(exclude)
    files: list[Path] = []
    excluded: dict[str, int] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        hit = next((part for part in p.relative_to(root).parts if part in names), None)
        if hit is None:
            files.append(p)
        else:
            excluded[hit] = excluded.get(hit, 0) + 1
    return ArtifactScan(files=files, excluded=excluded)


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
