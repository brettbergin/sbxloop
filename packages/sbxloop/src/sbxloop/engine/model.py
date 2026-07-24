"""Engine domain models: runs, tasks, plans, verdicts, the task graph."""

from __future__ import annotations

from graphlib import CycleError, TopologicalSorter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class RunResult(_Model):
    """What LoopEngine.start()/resume() returns to callers."""

    run_id: str
    state: RunState
    tasks: list[TaskRecord] = Field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.state == "completed"
