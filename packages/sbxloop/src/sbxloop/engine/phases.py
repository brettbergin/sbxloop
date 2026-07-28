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
- STEER (interactive chat) runs as a fresh read-only session, like the
  critics: it may inspect the workspace to answer the user accurately but
  must not "helpfully" edit anything — direction changes flow back through
  the engine as re-plans or standing guidance, never as direct edits.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal, NamedTuple, TypeVar

from pydantic import BaseModel

from sbxloop.config import Config
from sbxloop.engine.model import Issue, PlanModel, SteerVerdict, TaskGraph, TaskRecord, Verdict
from sbxloop.engine.prompts import bullet_list, render
from sbxloop.errors import WorkerError
from sbxloop.ids import new_job_id
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import BatchCommandResult, JobRequest, JobResult, SessionHealth

EVIDENCE_COMMANDS: tuple[tuple[str, str], ...] = (
    ("git status", "git status --short 2>&1 | head -50"),
    ("git diff stat", "git diff --stat 2>&1 | head -50"),
    ("recent files", "find . -type f -newer /tmp -not -path './.git/*' 2>/dev/null | head -30"),
)

OUTPUT_CLIP = 6_000

# Persona label per phase prompt: stamped onto the job's agent.* events (via
# WorkerClient.submit) so the transcript header says WHO is responding
# (planner, executor, ...) instead of a generic "agent".
AGENT_NAMES = {
    "decompose": "decomposer",
    "plan": "planner",
    "execute": "executor",
    "scrutinize": "scrutinizer",
    "validate": "validator",
    "steer": "steering",
}

ModelT = TypeVar("ModelT", bound=BaseModel)


class VerifyOutcome(NamedTuple):
    """VERIFY's result: pass/fail, failure feedback for the executor, and
    the full command transcript — persisted on the phase row so VALIDATE
    (including a resumed one) judges with the same evidence."""

    passed: bool
    feedback: str
    results: str


class CriticOutcome(NamedTuple):
    """A critic phase's verdict plus the tooling health of the session that
    produced it, so the engine can persist how blind the critic actually was
    (#123). ``downgraded`` is True when a clean verdict was replaced because
    the session had lost its inspection tooling."""

    verdict: Verdict
    health: SessionHealth | None
    downgraded: bool


def clip(text: str | None, limit: int = OUTPUT_CLIP) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return f"...(clipped)...\n{text[-limit:]}"


class PhaseRunner:
    """Runs the six phases for one run against the agent sandbox's worker."""

    def __init__(
        self,
        agent: WorkerClient,
        config: Config,
        run_id: str,
        outcome: str,
        *,
        workdir: str | None = None,
    ) -> None:
        self.agent = agent
        self.config = config
        self.run_id = run_id
        self.outcome = outcome
        # Canonical in-VM working directory for every job in this run: the
        # discovered workspace mount, or the harvest dir. Evidence and verify
        # commands must run where the executor wrote its files.
        self.workdir = workdir
        # Standing chat guidance (steer_run verdicts), injected into every
        # later plan/execute prompt. The engine appends live entries and
        # replays persisted ones on resume.
        self.user_guidance: list[str] = []

    def add_guidance(self, text: str) -> None:
        self.user_guidance.append(text)

    def _guidance(self) -> str:
        return bullet_list(self.user_guidance)

    # -- job plumbing ------------------------------------------------------

    def _agent_job(
        self,
        prompt: str,
        *,
        agent_name: str,
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
            cwd=self.workdir,
            timeout_s=self.config.budgets.per_job_timeout_s,
        )
        result = self.agent.submit(job, agent=agent_name)
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
        check: Callable[[ModelT], None] | None = None,
        preamble: str = "",
    ) -> tuple[ModelT, JobResult]:
        """Run a JSON-expecting agent job; one retry with what went wrong.

        Retryable failures: schema mismatch (ValidationError), semantic
        rejection by ``check`` (host-side validation on the parsed model;
        raise ValueError to reject — pydantic's ValidationError is a
        ValueError subclass, so both share the retry path), and a reply
        containing no JSON at all (ExpectedJsonMissing — the field failure
        that used to kill whole runs on one chatty reply). Anything else
        raises immediately.

        ``preamble`` is injected into the template's ``$retry_context`` slot
        on every attempt (validation feedback appends to it, never replaces
        it) — used by the degraded-critic guard to confront a re-run critic
        with its predecessor's tooling failures.

        Returns the validated model together with the raw JobResult, whose
        session-health tally the critic phases inspect.
        """
        retry_context = preamble
        last_error: Exception | None = None
        for _ in range(2):
            prompt = render(
                prompt_name,
                languages=self.config.sandbox.effective_languages,
                retry_context=retry_context,
                **context,
            )
            try:
                result = self._agent_job(
                    prompt,
                    agent_name=AGENT_NAMES[prompt_name],
                    permission_mode=permission_mode,
                    expect="json",
                )
            except WorkerError as exc:
                if "ExpectedJsonMissing" not in str(exc):
                    raise
                last_error = exc
                retry_context = preamble + (
                    "\n## Previous attempt was invalid\n\n"
                    "Your previous response contained no parseable JSON. Respond "
                    "with ONLY one fenced ```json block in the format above — no "
                    "prose before or after it."
                )
                continue
            try:
                model = model_cls.model_validate(result.output_json)
                if check is not None:
                    check(model)
                return model, result
            except ValueError as exc:  # includes pydantic's ValidationError
                last_error = exc
                retry_context = preamble + (
                    "\n## Previous attempt was invalid\n\n"
                    "Your previous response failed validation with:\n\n"
                    f"```\n{exc}\n```\n\nFix the structure and respond again."
                )
        raise WorkerError(f"{prompt_name} produced invalid output twice: {last_error}")

    def shell_batch(
        self, commands: Sequence[str], *, cwd: str | None = None
    ) -> list[BatchCommandResult]:
        """Run mechanical shell commands as ONE worker job (#125).

        Every job pays a fixed round-trip cost (stage the job JSON, boot a
        cold interpreter under ``sbx exec``, fetch the result file) that
        dwarfs what verify/evidence commands actually do, so they ride
        together. Per-command semantics are preserved: each command still
        gets the per-job timeout, and the job budget covers the worst case
        of all of them — matching what N sequential jobs cost before.
        """
        per_command = self.config.budgets.per_job_timeout_s
        job = JobRequest(
            job_id=new_job_id(),
            run_id=self.run_id,
            kind="shell.batch",
            commands=list(commands),
            command_timeout_s=per_command,
            timeout_s=per_command * len(commands),
            cwd=cwd or self.workdir,
        )
        result = self.agent.submit(job)
        if result.status != "ok":
            assert result.error is not None
            raise WorkerError(f"shell batch failed ({result.error.type}): {result.error.message}")
        return [BatchCommandResult.model_validate(item) for item in result.output_json or []]

    # -- phases ------------------------------------------------------------

    def decompose(self) -> TaskGraph:
        graph, _ = self._agent_json(
            TaskGraph,
            "decompose",
            {
                "outcome": self.outcome,
                "max_tasks": str(self.config.budgets.max_tasks),
            },
        )
        return graph

    def plan(self, task: TaskRecord) -> PlanModel:
        plan, _ = self._agent_json(
            PlanModel,
            "plan",
            {
                "outcome": self.outcome,
                "task_id": task.spec.id,
                "task_title": task.spec.title,
                "task_description": task.spec.description or "(no further description)",
                "acceptance_criteria": bullet_list(task.spec.acceptance_criteria),
                "feedback": task.last_feedback or "(none — first attempt)",
                "user_guidance": self._guidance(),
            },
            check=self._check_plan_egress,
        )
        return plan

    def _check_plan_egress(self, plan: PlanModel) -> None:
        """Reject plans declaring egress outside the operator's bounds.

        Feeds the retry loop, so the planner gets one chance to drop the
        domain (or find a baseline-reachable alternative) before the run
        fails — the "grant only within operator-set limits" guardrail.
        """
        from sbxloop.policy import effective_egress_bounds, egress_rejection

        allow, deny = effective_egress_bounds(self.config)
        problems = [
            f"- {egress.domain}: {rejection}"
            for egress in plan.egress
            if (rejection := egress_rejection(egress.domain, allow, deny)) is not None
        ]
        if problems:
            raise ValueError(
                "plan-declared egress is outside the operator's bounds:\n"
                + "\n".join(problems)
                + "\nDrop these domains from `egress` (prefer baseline-reachable hosts: "
                "PyPI, GitHub, apt mirrors — or the well-known package registries, "
                "which are always declarable). Only the operator can extend the "
                "bounds, via [policy] allow in sbxloop.toml."
            )

    def execute(self, task: TaskRecord, plan: PlanModel) -> JobResult:
        prompt = render(
            "execute",
            languages=self.config.sandbox.effective_languages,
            outcome=self.outcome,
            task_id=task.spec.id,
            task_title=task.spec.title,
            task_description=task.spec.description or "(no further description)",
            plan_steps=bullet_list(plan.steps),
            expected_artifacts=bullet_list(plan.expected_artifacts),
            feedback=task.last_feedback or "(none — first attempt)",
            user_guidance=self._guidance(),
        )
        return self._agent_job(
            prompt, agent_name=AGENT_NAMES["execute"], permission_mode="auto", expect="text"
        )

    def steer(
        self, message: str, *, tasks: Sequence[TaskRecord], task: TaskRecord | None
    ) -> SteerVerdict:
        """Answer one interactive chat message and rule on its course change.

        ``task`` is the task the engine is currently driving (None between
        tasks); ``tasks`` is the whole board, so the agent can speak to
        overall progress.
        """
        board = bullet_list(
            [f"{t.spec.id} [{t.state}] {t.spec.title}" for t in tasks],
            empty="(the outcome has not been decomposed into tasks yet)",
        )
        if task is None:
            current = "(no task is active right now — the run is between tasks)"
        else:
            plan_steps = bullet_list(task.plan.steps) if task.plan else "(not yet planned)"
            current = (
                f"Task {task.spec.id}: {task.spec.title} (state: {task.state}, "
                f"revisions: {task.revisions}, replans: {task.replans})\n\n"
                f"{task.spec.description or '(no further description)'}\n\n"
                f"Plan steps:\n{plan_steps}\n\n"
                f"Prior feedback:\n{task.last_feedback or '(none)'}"
            )
        verdict, _ = self._agent_json(
            SteerVerdict,
            "steer",
            {
                "outcome": self.outcome,
                "tasks_summary": board,
                "current_task": current,
                "user_guidance": self._guidance(),
                "user_message": message,
            },
            permission_mode="read_only",
        )
        return verdict

    def scrutinize(self, task: TaskRecord, plan: PlanModel, executor_report: str) -> CriticOutcome:
        try:
            evidence = self.shell_batch([command for _, command in EVIDENCE_COMMANDS])
        except WorkerError:
            # Evidence is best-effort context for the critic, never fatal.
            evidence = []
        evidence_parts: list[str] = []
        for (label, _), result in zip(EVIDENCE_COMMANDS, evidence, strict=False):
            output = clip(result.output, 1_500).strip()
            if output:
                evidence_parts.append(f"### {label}\n```\n{output}\n```")
        return self._critic_json(
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
            allowed=("pass", "revise"),
        )

    def _critic_json(
        self,
        prompt_name: str,
        context: dict[str, str],
        *,
        allowed: tuple[str, str],
    ) -> CriticOutcome:
        """Run a critic phase with the degraded-tooling guard (#123).

        ``allowed`` is (clean, dirty) for the phase's verdict vocabulary. A
        clean verdict from a session whose tool calls failed is not trusted:
        the phase re-runs once in a fresh session that is confronted with
        the failures and must account for the reduced coverage (a transient
        crash gets its second chance here). If the re-run is also degraded
        and still claims clean, the verdict is downgraded to the dirty one —
        a critic that could not inspect the work must not green-light it.
        Permission denials never trigger the guard: a read-only critic
        probing ``shell`` is the barrier working as designed.
        """
        clean, dirty = allowed
        verdict, result = self._critic_attempt(prompt_name, context, allowed)
        health = result.health
        if verdict.verdict != clean or health is None or not health.degraded:
            return CriticOutcome(verdict, health, False)
        preamble = (
            "\n## Degraded tooling warning\n\n"
            "A previous review session lost part of its inspection tooling "
            f"({health.summary()}) and still claimed {clean!r} — that verdict "
            "was discarded. Verify the work with the tools that DO function; "
            f"if you cannot actually inspect it, respond {dirty!r} and say "
            "which checks you could not perform. Do not claim verification "
            "you could not carry out."
        )
        verdict, result = self._critic_attempt(prompt_name, context, allowed, preamble=preamble)
        health = result.health
        if verdict.verdict != clean or health is None or not health.degraded:
            return CriticOutcome(verdict, health, False)
        detail = (
            f"{prompt_name} session had degraded tooling ({health.summary()}) "
            f"and could not reliably verify the work; its {clean!r} was "
            f"downgraded to {dirty!r}"
        )
        return CriticOutcome(
            Verdict(
                verdict=dirty,  # type: ignore[arg-type]
                issues=[*verdict.issues, Issue(severity="high", detail=detail)],
                feedback=(
                    "the reviewer's session lost part of its inspection tooling "
                    f"({health.summary()}), so the work could not be verified and "
                    "must be treated as unreviewed. Re-check the acceptance "
                    "criteria yourself and report concrete evidence (exact file "
                    "paths and the relevant contents) so the work is verifiable "
                    "even by file reads alone."
                ),
            ),
            health,
            True,
        )

    def _critic_attempt(
        self,
        prompt_name: str,
        context: dict[str, str],
        allowed: tuple[str, str],
        *,
        preamble: str = "",
    ) -> tuple[Verdict, JobResult]:
        verdict, result = self._agent_json(
            Verdict,
            prompt_name,
            context,
            permission_mode="read_only",
            preamble=preamble,
        )
        if verdict.verdict not in allowed:
            raise WorkerError(f"{prompt_name} returned invalid verdict {verdict.verdict!r}")
        return verdict, result

    def verify(self, task: TaskRecord, plan: PlanModel) -> VerifyOutcome:
        """Run every verify command; the transcript rides on the outcome."""
        commands = list(dict.fromkeys(task.spec.verify_commands + plan.verify_commands))
        failures: list[str] = []
        results: list[str] = []
        for result in self.shell_batch(commands) if commands else []:
            output = clip(result.output, 1_500)
            results.append(f"$ {result.command}\n(exit {result.exit_code})\n{output}")
            if result.exit_code != 0:
                failures.append(
                    f"verify command failed: `{result.command}` (exit {result.exit_code})\n{output}"
                )
        return VerifyOutcome(
            passed=not failures,
            feedback="\n\n".join(failures),
            results="\n\n".join(results) or "(no verify commands)",
        )

    def validate(self, task: TaskRecord, verify_results: str) -> CriticOutcome:
        return self._critic_json(
            "validate",
            {
                "outcome": self.outcome,
                "task_id": task.spec.id,
                "task_title": task.spec.title,
                "task_description": task.spec.description or "(no further description)",
                "acceptance_criteria": bullet_list(task.spec.acceptance_criteria),
                "verify_results": verify_results,
            },
            allowed=("accept", "reject"),
        )
