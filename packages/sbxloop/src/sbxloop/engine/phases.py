"""Phase handlers: each turns engine state into one or more worker jobs.

Session strategy per phase (a deliberate design decision):

- DECOMPOSE / PLAN / EXECUTE run with full ("auto") permissions — the
  microVM is the security boundary. DECOMPOSE and PLAN are always fresh;
  EXECUTE is the one phase that continues, resuming its own previous attempt
  on a revision so the work already done is not re-derived (a replan clears
  it — the approach that session holds is the one being discarded).
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

import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, NamedTuple, TypeVar

from pydantic import BaseModel

from sbxloop.config import Config
from sbxloop.engine.model import Issue, PlanModel, SteerVerdict, TaskGraph, TaskRecord, Verdict
from sbxloop.engine.prompts import bullet_list, render
from sbxloop.errors import WorkerError
from sbxloop.ids import new_job_id
from sbxloop.log import get_logger
from sbxloop.verifylint import UV_LOCKFILE, gate_rule, lint_verify_commands, project_gate
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import (
    BatchCommandResult,
    JobRequest,
    JobResult,
    SessionHealth,
    Usage,
)


class EvidenceCommand(NamedTuple):
    """One mechanical command whose output is handed to the critic, with the
    character budget its output gets in the prompt."""

    label: str
    command: str
    limit: int = 1_500


# The diff gets an order of magnitude more room than the other evidence
# because it replaces work the critic would otherwise do by hand. Handing
# over ~5k tokens of patch once is far cheaper than the ten-plus tool-call
# turns it takes to rediscover the same thing: every turn re-sends the whole
# session context (~22k tokens of fixed overhead measured in the field), so
# a turn saved is worth vastly more than the bytes it costs to save it.
DIFF_CLIP = 20_000

EVIDENCE_COMMANDS: tuple[EvidenceCommand, ...] = (
    EvidenceCommand("git status", "git status --short 2>&1 | head -50"),
    # `git diff HEAD` (not bare `git diff`) so staged work is included — the
    # executor is free to `git add` and frequently does, and a critic shown
    # an empty diff concludes nothing was done.
    EvidenceCommand("git diff", "git diff HEAD 2>&1", DIFF_CLIP),
    EvidenceCommand(
        "recent files",
        "find . -type f -newer /tmp -not -path './.git/*' 2>/dev/null | head -30",
    ),
)

OUTPUT_CLIP = 6_000
# Verify output keeps head + tail (#253): a pytest run over hundreds of
# tests prints the failing assertions in the middle/top of its output and
# only a "N failed" summary at the bottom, so a tail-only clip handed the
# critic a summary with no assertion text. The head is the first failure's
# traceback; the tail is the summary and the last failure.
VERIFY_HEAD_CLIP = 2_000
VERIFY_TAIL_CLIP = 4_000

# Every verify failure fed back to the executor starts with this; the
# engine counts occurrences to headline "N more" in the live stream. It is
# a display convention only — provenance decisions read the persisted
# verify attempt, never this text (critic feedback is agent-authored).
VERIFY_FAILURE_PREFIX = "verify command failed:"

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

log = get_logger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


class VerifyOutcome(NamedTuple):
    """VERIFY's result: pass/fail, failure feedback for the executor, and
    the full command transcript — persisted on the phase row so VALIDATE
    (including a resumed one) judges with the same evidence."""

    passed: bool
    feedback: str
    results: str


class PhaseSpend(NamedTuple):
    """Model usage accumulated since the last drain — the token bill for the
    phase attempt the engine is about to record."""

    usage: Usage | None
    turns: int | None


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


def clip_head_tail(
    text: str | None, head: int = VERIFY_HEAD_CLIP, tail: int = VERIFY_TAIL_CLIP
) -> str:
    """Keep the first ``head`` and last ``tail`` characters, eliding the
    middle with a marker that says how much was dropped."""
    text = text or ""
    if len(text) <= head + tail:
        return text
    dropped = len(text) - head - tail
    return f"{text[:head]}\n...(clipped {dropped} chars)...\n{text[-tail:]}"


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
        workspace: Path | None = None,
    ) -> None:
        self.agent = agent
        self.config = config
        self.run_id = run_id
        self.outcome = outcome
        # Canonical in-VM working directory for every job in this run: the
        # discovered workspace mount, or the harvest dir. Evidence and verify
        # commands must run where the executor wrote its files.
        self.workdir = workdir
        # The host-side workspace directory (the run's clone), consulted for
        # project-shape facts the verify-command lint keys on — a `uv.lock`
        # at the root flips the Python convention (#250). Host-side because
        # the lint runs at JSON acceptance, where a round trip into the VM
        # per retry would cost more than the check is worth; the workspace
        # is mounted identically in the common case, and an unmounted run
        # still starts from this clone.
        self.workspace = workspace
        # Standing chat guidance (steer_run verdicts), injected into every
        # later plan/execute prompt. The engine appends live entries and
        # replays persisted ones on resume.
        self.user_guidance: list[str] = []
        # Running usage tally across agent jobs, drained by the engine when
        # it records a phase attempt (drain_spend) so retries and critic
        # re-runs bill to the phase row they served.
        self._spend_usage = Usage()
        self._spend_turns = 0

    def add_guidance(self, text: str) -> None:
        self.user_guidance.append(text)

    def drain_spend(self) -> PhaseSpend:
        """Usage accumulated since the last drain (every agent job, retries
        included), then reset. A phase that fails before being recorded leaks
        its spend into the next drained row — accepted: the columns serve
        aggregate per-phase accounting, not billing."""
        spend = PhaseSpend(
            usage=self._spend_usage if self._spend_usage != Usage() else None,
            turns=self._spend_turns or None,
        )
        self._spend_usage = Usage()
        self._spend_turns = 0
        return spend

    def _guidance(self) -> str:
        return bullet_list(self.user_guidance)

    def _lint_verify_commands(self, commands: Sequence[str]) -> list[str]:
        """Verify-command lint under this run's toolchains and project shape.

        Re-checks the lockfile and the project gate every time rather than
        once at construction: on a mounted workspace the executor may have
        created ``uv.lock`` — or a Makefile — in an earlier task, and later
        plans should be held to the convention the workspace now has.
        """
        uv_project = self.workspace is not None and (self.workspace / UV_LOCKFILE).is_file()
        return lint_verify_commands(
            commands, self.config.sandbox.effective_languages, uv_project=uv_project
        )

    # -- job plumbing ------------------------------------------------------

    def _agent_job(
        self,
        prompt: str,
        *,
        phase: str,
        permission_mode: Literal["auto", "read_only"],
        expect: Literal["text", "json"],
        resume_session_id: str | None = None,
    ) -> JobResult:
        agent_name = AGENT_NAMES[phase]
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
            max_tool_calls=self.config.budgets.max_tool_calls_per_phase or None,
            # Only EXECUTE ever passes one. The critics are fresh by design
            # (module docstring): a reviewer that inherited the executor's
            # session would inherit its conclusions with it, and that
            # independence is the loop's integrity check.
            resume_session_id=resume_session_id,
        )
        started = time.monotonic()
        log.info(
            "phase.agent_call",
            run=self.run_id,
            job=job.job_id,
            agent=agent_name,
            model=self.config.model,
            permission_mode=permission_mode,
            expect=expect,
            prompt_chars=len(prompt),
            resumed=bool(resume_session_id),
        )
        result = self.agent.submit(job, agent=agent_name)
        usage = result.usage
        if usage is not None:
            self._spend_usage = self._spend_usage.merged(usage)
        if result.turns:
            self._spend_turns += result.turns
        log.info(
            "phase.agent_done",
            run=self.run_id,
            job=job.job_id,
            agent=agent_name,
            status=result.status,
            duration_s=round(time.monotonic() - started, 1),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            error=result.error.message[:200] if result.error is not None else None,
        )
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
            prompt = render(prompt_name, retry_context=retry_context, **context)
            try:
                result = self._agent_job(
                    prompt,
                    phase=prompt_name,
                    permission_mode=permission_mode,
                    expect="json",
                )
            except WorkerError as exc:
                if "ExpectedJsonMissing" not in str(exc):
                    raise
                last_error = exc
                log.warning(
                    "phase.retry",
                    run=self.run_id,
                    prompt=prompt_name,
                    reason="reply contained no JSON",
                )
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
                log.warning(
                    "phase.retry",
                    run=self.run_id,
                    prompt=prompt_name,
                    reason="output failed validation",
                    error=str(exc)[:300],
                )
                retry_context = preamble + (
                    "\n## Previous attempt was invalid\n\n"
                    "Your previous response failed validation with:\n\n"
                    f"```\n{exc}\n```\n\nFix the structure and respond again."
                )
        log.warning(
            "phase.invalid_twice", run=self.run_id, prompt=prompt_name, error=str(last_error)[:300]
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
        started = time.monotonic()
        log.debug(
            "phase.shell_batch",
            run=self.run_id,
            job=job.job_id,
            commands=len(commands),
            cwd=cwd or self.workdir,
        )
        result = self.agent.submit(job)
        log.debug(
            "phase.shell_batch_done",
            run=self.run_id,
            job=job.job_id,
            status=result.status,
            duration_s=round(time.monotonic() - started, 1),
        )
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
                "project_gate": gate_rule(self.project_gate()),
            },
            check=self._check_taskgraph_verify_commands,
        )
        return graph

    def project_gate(self) -> str | None:
        """This project's own gate, honouring the operator's override.

        Re-derived per call rather than cached: a run may create the
        makefile (or the package.json) that declares it, and later plans
        should be held to the convention the workspace now has.
        """
        return project_gate(self.workspace, self.config.sandbox.gate_command)

    def _check_taskgraph_verify_commands(self, graph: TaskGraph) -> None:
        """Reject task verify commands that violate toolchain conventions,
        and require the graph as a whole to run the project's own gate.

        The executor cannot edit verify commands, so a bare `python -m
        pytest` from the decomposer costs a revision cycle plus an in-VM
        workaround at verify time (field failure r12ygfd7t); rejecting at
        JSON acceptance costs one retry with the rule quoted.

        The gate is checked **across the graph, not per task**. A delivered
        PR (#389) failed `mdformat` and `security` — both plain `make check`
        targets — because nothing in the run ran what CI enforces. But
        demanding the gate of every task would run a multi-minute check once
        per task for no extra signal, so one task carrying it is the
        requirement; decompositions already tend to end with a "everything
        green" task, which is exactly where it belongs.
        """
        problems = [
            f"- task {task.id}: {message}"
            for task in graph.tasks
            for message in self._lint_verify_commands(task.verify_commands)
        ]
        gate = self.project_gate()
        if gate:
            every_command = [c for task in graph.tasks for c in task.verify_commands]
            problems += [
                f"- {message}" for message in lint_verify_commands(every_command, (), gate=gate)
            ]
        if problems:
            raise ValueError(
                "verify commands violate the sandbox's toolchain conventions:\n"
                + "\n".join(problems)
            )

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
            check=self._check_plan,
        )
        return plan

    def _check_plan(self, plan: PlanModel) -> None:
        """Semantic plan validation: egress bounds + verify-command lint."""
        self._check_plan_egress(plan)
        problems = self._lint_verify_commands(plan.verify_commands)
        if problems:
            raise ValueError(
                "verify commands violate the sandbox's toolchain conventions:\n"
                + "\n".join(f"- {message}" for message in problems)
            )

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

    def execute(
        self,
        task: TaskRecord,
        plan: PlanModel,
        *,
        prior_report: str = "",
        resume_session_id: str | None = None,
    ) -> JobResult:
        """Do the work for one task.

        ``prior_report`` is what the previous attempt on this task said it
        did, and ``resume_session_id`` continues that attempt's own agent
        session where it still exists. Both exist to stop a revision
        re-establishing what the last attempt already knew — the engine
        holds that context either way and used to withhold it, so five
        executor sessions on one task each re-ran the same setup and the
        same gate from scratch (field failure rrhb28j7n/t5).
        """
        prompt = render(
            "execute",
            outcome=self.outcome,
            task_id=task.spec.id,
            task_title=task.spec.title,
            task_description=task.spec.description or "(no further description)",
            plan_steps=bullet_list(plan.steps),
            expected_artifacts=bullet_list(plan.expected_artifacts),
            feedback=task.last_feedback or "(none — first attempt)",
            prior_attempt=clip(prior_report) or "(none — this is the first attempt)",
            user_guidance=self._guidance(),
        )
        return self._agent_job(
            prompt,
            phase="execute",
            permission_mode="auto",
            expect="text",
            resume_session_id=resume_session_id,
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

    @staticmethod
    def verify_commands(task: TaskRecord, plan: PlanModel) -> list[str]:
        """The exact command list VERIFY will run: spec-level checks first,
        then the plan's, deduplicated. Shown to the scrutinizer verbatim so
        it judges the same checks the mechanical phase runs (#231)."""
        return list(dict.fromkeys(task.spec.verify_commands + plan.verify_commands))

    def scrutinize(self, task: TaskRecord, plan: PlanModel, executor_report: str) -> CriticOutcome:
        try:
            evidence = self.shell_batch([item.command for item in EVIDENCE_COMMANDS])
        except WorkerError:
            # Evidence is best-effort context for the critic, never fatal.
            log.warning(
                "phase.evidence_failed",
                run=self.run_id,
                task=task.spec.id,
                hint="the critic judges without repo evidence",
                exc_info=True,
            )
            evidence = []
        evidence_parts: list[str] = []
        for item, result in zip(EVIDENCE_COMMANDS, evidence, strict=False):
            # Head+tail rather than tail-only: a long diff's first hunk is as
            # load-bearing as its last, and the same clip is a no-op for the
            # short outputs, so one rule covers every evidence command.
            output = clip_head_tail(
                result.output, head=item.limit // 3, tail=item.limit - item.limit // 3
            ).strip()
            if output:
                evidence_parts.append(f"### {item.label}\n```\n{output}\n```")
        return self._critic_json(
            "scrutinize",
            {
                "task_id": task.spec.id,
                "task_title": task.spec.title,
                "task_description": task.spec.description or "(no further description)",
                "acceptance_criteria": bullet_list(task.spec.acceptance_criteria),
                "plan_steps": bullet_list(plan.steps),
                "prior_feedback": clip(task.last_feedback) or "(none — first attempt)",
                "executor_report": clip(executor_report) or "(executor produced no report)",
                "evidence": "\n\n".join(evidence_parts) or "(no evidence gathered)",
                "verify_commands": bullet_list(
                    self.verify_commands(task, plan), empty="(no verify commands)"
                ),
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
        commands = self.verify_commands(task, plan)
        failures: list[str] = []
        results: list[str] = []
        for result in self.shell_batch(commands) if commands else []:
            output = clip_head_tail(result.output)
            results.append(f"$ {result.command}\n(exit {result.exit_code})\n{output}")
            if result.exit_code != 0:
                failures.append(
                    f"{VERIFY_FAILURE_PREFIX} `{result.command}` "
                    f"(exit {result.exit_code})\n{output}"
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
