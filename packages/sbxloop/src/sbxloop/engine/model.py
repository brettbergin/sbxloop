"""Engine domain models: runs, tasks, plans, verdicts, the task graph."""

from __future__ import annotations

from graphlib import CycleError, TopologicalSorter
from pathlib import Path, PurePosixPath
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
    # Workspace subtrees (relative paths) this task claims exclusive write
    # access to. Only tasks that declare disjoint `owns` may execute in the
    # same parallel wave; writes outside the declared subtrees fail the task
    # at harvest time. Empty (the default) means undeclared — the task then
    # always runs alone, preserving sequential semantics.
    owns: list[str] = Field(default_factory=list)

    @field_validator("owns")
    @classmethod
    def _check_owns(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in value:
            path = PurePosixPath(raw)
            if path.is_absolute():
                raise ValueError(f"owns path must be relative, got {raw!r}")
            parts = path.parts
            if not parts or any(part in ("..", ".") for part in parts):
                raise ValueError(f"owns path must be a plain relative path, got {raw!r}")
            normalized.append(str(PurePosixPath(*parts)))
        return normalized


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


class PlanModel(_Model):
    """The agent's plan for one task."""

    steps: list[str]
    expected_artifacts: list[str] = Field(default_factory=list)
    verify_commands: list[str] = Field(default_factory=list)


class Issue(_Model):
    severity: Literal["low", "medium", "high"] = "medium"
    detail: str


class Verdict(_Model):
    """Critic output: scrutinize uses pass/revise, validate uses accept/reject."""

    verdict: Literal["pass", "revise", "accept", "reject"]
    issues: list[Issue] = Field(default_factory=list)
    feedback: str = ""


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


class RunResult(_Model):
    """What LoopEngine.start()/resume() returns to callers."""

    run_id: str
    state: RunState
    tasks: list[TaskRecord] = Field(default_factory=list)
    workspace: Path | None = None
    mounted: bool = False

    @property
    def succeeded(self) -> bool:
        return self.state == "completed"


def _owns_overlap(a: str, b: str) -> bool:
    """Whether one owns path contains (or equals) the other."""
    a_parts = PurePosixPath(a).parts
    b_parts = PurePosixPath(b).parts
    shorter = min(len(a_parts), len(b_parts))
    return a_parts[:shorter] == b_parts[:shorter]


def owns_disjoint(a: list[str], b: list[str]) -> bool:
    """Whether two declared ownership sets can never write the same path."""
    return not any(_owns_overlap(x, y) for x in a for y in b)


def pack_parallel_batch(ready: list[TaskRecord], max_parallel: int) -> list[TaskRecord]:
    """Select the next wave from the ready tasks (dependencies satisfied).

    Safety by construction: a task joins a multi-task wave only when it
    declares ``owns`` disjoint from every task already in the wave. A task
    with no declared ownership runs alone — undeclared writes cannot be
    attributed in advance, so it gets sequential semantics.
    """
    assert ready
    first = ready[0]
    batch = [first]
    if not first.spec.owns:
        return batch
    for candidate in ready[1:]:
        if len(batch) >= max_parallel:
            break
        if not candidate.spec.owns:
            continue
        if all(owns_disjoint(candidate.spec.owns, member.spec.owns) for member in batch):
            batch.append(candidate)
    return batch


def artifact_files(root: Path) -> list[Path]:
    """Regular files under root, hidden files/dirs excluded (an agent's .git
    would otherwise swamp listings and deliveries), sorted for stable output."""
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)
    )


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
