"""Phase handlers: each turns engine state into one or more worker jobs.

Session strategy per phase (a deliberate design decision):

- DECOMPOSE / PLAN / EXECUTE run as fresh agent sessions with full ("auto")
  permissions — the microVM is the security boundary.
- SCRUTINIZE / VALIDATE run as **fresh sessions in the same agent sandbox
  with read-only permissions**: a fresh session removes conversational
  anchoring to the executor's claims, read-only permissions stop the critic
  from "fixing" things itself, and reusing the sandbox preserves the
  workspace state the critic must inspect (a fresh sandbox per critic would
  cost minutes and lose it).
- VERIFY is mechanical — shell commands, no LLM, no opinions.
"""

from __future__ import annotations

from typing import Literal, TypeVar

from pydantic import BaseModel, ValidationError

from sbxloop.config import Config
from sbxloop.engine.model import PlanModel, TaskGraph, TaskRecord, Verdict
from sbxloop.engine.prompts import bullet_list, render
from sbxloop.errors import WorkerError
from sbxloop.ids import new_job_id
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import JobRequest, JobResult

EVIDENCE_COMMANDS: tuple[tuple[str, str], ...] = (
    ("git status", "git status --short 2>&1 | head -50"),
    ("git diff stat", "git diff --stat 2>&1 | head -50"),
    ("recent files", "find . -type f -newer /tmp -not -path './.git/*' 2>/dev/null | head -30"),
)

OUTPUT_CLIP = 6_000

ModelT = TypeVar("ModelT", bound=BaseModel)


def clip(text: str | None, limit: int = OUTPUT_CLIP) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return f"...(clipped)...\n{text[-limit:]}"


class PhaseRunner:
    """Runs the six phases for one run against the agent sandbox's worker."""

    def __init__(self, agent: WorkerClient, config: Config, run_id: str, outcome: str) -> None:
        self.agent = agent
        self.config = config
        self.run_id = run_id
        self.outcome = outcome

    # -- job plumbing ------------------------------------------------------

    def _agent_job(
        self,
        prompt: str,
        *,
        permission_mode: Literal["auto", "read_only"],
        expect: Literal["text", "json"],
    ) -> JobResult:
        job = JobRequest(
            job_id=new_job_id(),
            run_id=self.run_id,
            kind="agent.session",
            prompt=prompt,
            model=self.config.model,
            permission_mode=permission_mode,
            expect=expect,
            timeout_s=self.config.budgets.per_job_timeout_s,
        )
        result = self.agent.submit(job)
        if result.status != "ok":
            assert result.error is not None
            raise WorkerError(f"agent job failed ({result.error.type}): {result.error.message}")
        return result

    def _agent_json(
        self,
        model_cls: type[ModelT],
        prompt_name: str,
        context: dict[str, str],
        *,
        permission_mode: Literal["auto", "read_only"] = "auto",
    ) -> ModelT:
        """Run a JSON-expecting agent job; one retry with the validation error."""
        retry_context = ""
        last_error: Exception | None = None
        for _ in range(2):
            prompt = render(prompt_name, retry_context=retry_context, **context)
            result = self._agent_job(prompt, permission_mode=permission_mode, expect="json")
            try:
                return model_cls.model_validate(result.output_json)
            except ValidationError as exc:
                last_error = exc
                retry_context = (
                    "\n## Previous attempt was invalid\n\n"
                    "Your previous response failed validation with:\n\n"
                    f"```\n{exc}\n```\n\nFix the structure and respond again."
                )
        raise WorkerError(f"{prompt_name} produced invalid output twice: {last_error}")

    def shell(self, command: str, *, cwd: str | None = None) -> JobResult:
        job = JobRequest(
            job_id=new_job_id(),
            run_id=self.run_id,
            kind="shell.check",
            argv=["sh", "-c", command],
            cwd=cwd,
            timeout_s=self.config.budgets.per_job_timeout_s,
        )
        result = self.agent.submit(job)
        if result.status != "ok":
            assert result.error is not None
            raise WorkerError(f"shell job failed ({result.error.type}): {result.error.message}")
        return result

    # -- phases ------------------------------------------------------------

    def decompose(self) -> TaskGraph:
        return self._agent_json(
            TaskGraph,
            "decompose",
            {
                "outcome": self.outcome,
                "max_tasks": str(self.config.budgets.max_tasks),
            },
        )

    def plan(self, task: TaskRecord) -> PlanModel:
        return self._agent_json(
            PlanModel,
            "plan",
            {
                "outcome": self.outcome,
                "task_id": task.spec.id,
                "task_title": task.spec.title,
                "task_description": task.spec.description or "(no further description)",
                "acceptance_criteria": bullet_list(task.spec.acceptance_criteria),
                "feedback": task.last_feedback or "(none — first attempt)",
            },
        )

    def execute(self, task: TaskRecord, plan: PlanModel) -> JobResult:
        prompt = render(
            "execute",
            outcome=self.outcome,
            task_id=task.spec.id,
            task_title=task.spec.title,
            task_description=task.spec.description or "(no further description)",
            plan_steps=bullet_list(plan.steps),
            expected_artifacts=bullet_list(plan.expected_artifacts),
            feedback=task.last_feedback or "(none — first attempt)",
        )
        return self._agent_job(prompt, permission_mode="auto", expect="text")

    def scrutinize(self, task: TaskRecord, plan: PlanModel, executor_report: str) -> Verdict:
        evidence_parts: list[str] = []
        for label, command in EVIDENCE_COMMANDS:
            try:
                result = self.shell(command)
            except WorkerError:
                continue
            output = clip(result.output_text, 1_500).strip()
            if output:
                evidence_parts.append(f"### {label}\n```\n{output}\n```")
        verdict = self._agent_json(
            Verdict,
            "scrutinize",
            {
                "task_id": task.spec.id,
                "task_title": task.spec.title,
                "task_description": task.spec.description or "(no further description)",
                "acceptance_criteria": bullet_list(task.spec.acceptance_criteria),
                "plan_steps": bullet_list(plan.steps),
                "executor_report": clip(executor_report) or "(executor produced no report)",
                "evidence": "\n\n".join(evidence_parts) or "(no evidence gathered)",
            },
            permission_mode="read_only",
        )
        if verdict.verdict not in ("pass", "revise"):
            raise WorkerError(f"scrutinize returned invalid verdict {verdict.verdict!r}")
        return verdict

    def verify(self, task: TaskRecord, plan: PlanModel) -> tuple[bool, str]:
        """Run every verify command; returns (all_passed, failure_feedback)."""
        commands = list(dict.fromkeys(task.spec.verify_commands + plan.verify_commands))
        failures: list[str] = []
        results: list[str] = []
        for command in commands:
            result = self.shell(command)
            output = clip(result.output_text, 1_500)
            results.append(f"$ {command}\n(exit {result.exit_code})\n{output}")
            if result.exit_code != 0:
                failures.append(
                    f"verify command failed: `{command}` (exit {result.exit_code})\n{output}"
                )
        self._last_verify_results = "\n\n".join(results) or "(no verify commands)"
        return (not failures, "\n\n".join(failures))

    _last_verify_results: str = "(verification not run)"

    def validate(self, task: TaskRecord) -> Verdict:
        verdict = self._agent_json(
            Verdict,
            "validate",
            {
                "outcome": self.outcome,
                "task_id": task.spec.id,
                "task_title": task.spec.title,
                "task_description": task.spec.description or "(no further description)",
                "acceptance_criteria": bullet_list(task.spec.acceptance_criteria),
                "verify_results": self._last_verify_results,
            },
            permission_mode="read_only",
        )
        if verdict.verdict not in ("accept", "reject"):
            raise WorkerError(f"validate returned invalid verdict {verdict.verdict!r}")
        return verdict
