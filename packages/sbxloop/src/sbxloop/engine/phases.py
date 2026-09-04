"""Phase handlers: each turns engine state into one or more worker jobs.

Session strategy per phase (a deliberate design decision):

- DECOMPOSE / BUILD run with full ("auto") permissions — the microVM is the
  security boundary. DECOMPOSE is always fresh; BUILD plans and executes in
  one session and is the one phase that continues, resuming its own
  previous attempt on a revision so the work already done is not re-derived
  (a replan clears it — the approach that session holds is the one being
  discarded).
- VERIFY is mechanical — shell commands, no LLM, no opinions. Its commands
  are DECOMPOSER-authored only: the agent that does the work must never
  author its own exam (#94), which is also why the builder is shown the
  commands verbatim but cannot edit them.
- STEER (interactive chat) runs as a fresh read-only session: it may
  inspect the workspace to answer the user accurately but must not
  "helpfully" edit anything — direction changes flow back through the
  engine as build restarts or standing guidance, never as direct edits.
- REVIEW runs once per delivery as a fresh read-only session over the
  PR's whole diff. There is no per-task critic: the old SCRUTINIZE/VALIDATE
  stages audited task completion and rubber-stamped it (6/6 pass, 5/5
  accept in the measured baseline) while diff-level defects leaked to the
  PR. One adversarial pass over the assembled diff, driving bounded fix
  rounds, is the critic that earns its turns (see ``engine.review``).
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, NamedTuple, TypeVar

from pydantic import BaseModel

from sbxloop.config import Config
from sbxloop.deliver import pr_conventions
from sbxloop.engine.model import SteerVerdict, TaskGraph, TaskRecord
from sbxloop.engine.prompts import bullet_list, render
from sbxloop.engine.review import ReviewGuard, ReviewVerdict
from sbxloop.errors import WorkerError
from sbxloop.ids import new_job_id
from sbxloop.log import get_logger
from sbxloop.verifylint import (
    UV_LOCKFILE,
    config_override_example,
    gate_problems,
    gate_rule,
    lint_verify_commands,
    project_gate,
)
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import BatchCommandResult, JobRequest, JobResult, Usage

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

# Substitutions applied to verify output before fingerprinting it. A verify
# command that cannot pass fails with the *same* diagnosis every attempt,
# but never byte-identically: pytest prints "in 12.31s", mypy prints a
# duration, tracebacks carry absolute sandbox paths whose run id differs
# per attempt. Normalising those out is what lets the engine recognise "we
# have already seen exactly this failure" without asking a model (#387).
_NORMALISERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Absolute paths (/home/agent/work/<run>/... ) -> the tail component,
    # so the same file under a different run root compares equal.
    (re.compile(r"(?<![\w/])/(?:[\w.+-]+/)+([\w.+-]+)"), r"<path>/\1"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}[\d:.]*\b"), "<ts>"),
    # Durations: "in 12.31s", "took 1.2 sec", "(209s)" — a number carrying a
    # time unit is unambiguous.
    (re.compile(r"\b\d+(?:\.\d+)?\s*(?:s|sec|secs|seconds|ms|m|min|minutes)\b"), "<dur>"),
    # Clock-style durations ("0:00:12") only in an explicit duration
    # context. A bare `\d+:\d{2}` also matches the `line:column` coordinates
    # every compiler and linter prints, and normalising those collapsed two
    # materially different failures (the same error at 10:12 and at 20:15)
    # into one fingerprint — which made the engine call a working check
    # suspect. The keyword prefix is kept so "took" survives the
    # substitution and cannot itself be a filename.
    (
        re.compile(r"(?i)\b(in|took|elapsed|time)(\s+)\d+:\d{2}(?::\d{2})?(?:\.\d+)?\b"),
        r"\1\2<dur>",
    ),
    # Hex ids / memory addresses and bare timestamps.
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<addr>"),
)


def normalise_verify_output(output: str) -> str:
    """Strip the run-to-run noise (timings, absolute paths, addresses) from
    verify output so two attempts of the same failure compare equal."""
    text = output or ""
    for pattern, replacement in _NORMALISERS:
        text = pattern.sub(replacement, text)
    # Collapse trailing whitespace and blank-line drift.
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def verify_fingerprint(command: str, output: str) -> str:
    """Stable identity of one verify failure: the command plus its
    normalised output. Equal fingerprints mean the identical check failed
    the identical way again."""
    payload = f"{(command or '').strip()}\n--\n{normalise_verify_output(output)}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def verify_suspect_feedback(failures: Sequence[VerifyFailure]) -> str:
    """Feedback for a verify command that has now failed identically twice.

    Addressed to the *builder*, because the builder is the only agent this
    signal can reach: the verify commands are decomposer-authored and
    build.md tells the builder they run exactly as written and cannot be
    edited. So this must not order a re-author it is unable to perform. What
    it can ask for is the two things that are in the builder's hands —
    making the work satisfy the command as written (layout, paths, setup),
    or, when the command is genuinely unpassable, saying so plainly in the
    report so a human sees the diagnosis instead of another silent retry.
    """
    quoted = "\n\n".join(
        f"`{failure.command}` (exit {failure.exit_code}) failed again with the "
        f"same output:\n\n{failure.output}"
        for failure in failures
    )
    return (
        "VERIFY COMMAND SUSPECT: the same verify command has now failed twice "
        "with identical output across attempts, so repeating the same change "
        "will not change the result — treat the check itself as suspect.\n\n"
        f"{quoted}\n\n"
        "You cannot edit the verify commands. Do two things instead. First, "
        "work out whether the work can be made to satisfy this command "
        "exactly as written — a different file layout, a path the command "
        "actually looks at, or missing setup the command needs — and if so, "
        "do that. Second, if the command cannot pass however the work is "
        "arranged (for example a config-driven tool given explicit paths that "
        "override its own configured file set, a path that does not exist, or "
        "a command that contradicts the task), stop retrying and state that "
        "plainly in your report, naming the command and why it is unpassable, "
        "so the humans reviewing the run can re-author it."
    )


class VerifyFailure(NamedTuple):
    """One failing verify command, with the fingerprint used to recognise
    it recurring on a later attempt."""

    command: str
    exit_code: int
    output: str

    @property
    def fingerprint(self) -> str:
        return verify_fingerprint(self.command, self.output)


# Persona label per phase prompt: stamped onto the job's agent.* events (via
# WorkerClient.submit) so the transcript header says WHO is responding
# (decomposer, builder, ...) instead of a generic "agent".
AGENT_NAMES = {
    "decompose": "decomposer",
    "build": "builder",
    "steer": "steering",
    "review": "reviewer",
}
# The review prompt carries the PR's diff inline; past this it is clipped
# head+tail (the reviewer still has the tree). Overridden by
# `[landing] review_diff_max_chars`.
REVIEW_DIFF_HEAD_CLIP = 100_000

log = get_logger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


class VerifyOutcome(NamedTuple):
    """VERIFY's result: pass/fail, failure feedback for the builder, and
    the full command transcript — persisted on the phase row so a resumed
    run re-enters with the same evidence."""

    passed: bool
    feedback: str
    results: str
    failures: tuple[VerifyFailure, ...] = ()


class PhaseSpend(NamedTuple):
    """Model usage accumulated since the last drain — the token bill for the
    phase attempt the engine is about to record."""

    usage: Usage | None
    turns: int | None


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
    """Runs the three phases for one run against the agent sandbox's worker."""

    def __init__(
        self,
        agent: WorkerClient,
        config: Config,
        run_id: str,
        outcome: str,
        *,
        workdir: str | None = None,
        workspace: Path | None = None,
        languages: Sequence[str] | None = None,
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
        # The run's resolved toolchain set (#624) — what the sandbox was
        # actually provisioned with, which the verify-command lint keys its
        # per-language rules on. None (embedders, tests) falls back to the
        # config's own answer.
        self.languages: tuple[str, ...] = (
            tuple(languages) if languages is not None else config.sandbox.effective_languages
        )
        # Standing chat guidance (steer_run verdicts), injected into every
        # later build prompt. The engine appends live entries and
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
            commands,
            self.languages,
            uv_project=uv_project,
            workspace=self.workspace,
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
            # Only BUILD ever passes one: a revision continues its own prior
            # attempt's session so the work already done is not re-derived.
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
    ) -> tuple[ModelT, JobResult]:
        """Run a JSON-expecting agent job; one retry with what went wrong.

        Retryable failures: schema mismatch (ValidationError), semantic
        rejection by ``check`` (host-side validation on the parsed model;
        raise ValueError to reject — pydantic's ValidationError is a
        ValueError subclass, so both share the retry path), and a reply
        containing no JSON at all (ExpectedJsonMissing — the field failure
        that used to kill whole runs on one chatty reply). Anything else
        raises immediately.

        Returns the validated model together with the raw JobResult.
        """
        retry_context = ""
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
                retry_context = (
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
                retry_context = (
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
                "config_override_example": config_override_example(self.languages),
                "pr_conventions": pr_conventions(self.workspace),
            },
            check=self._check_taskgraph,
        )
        return graph

    def project_gate(self) -> str | None:
        """This project's own gate, honouring the operator's override.

        Re-derived per call rather than cached: a run may create the
        makefile (or the package.json) that declares it, and later plans
        should be held to the convention the workspace now has. Detection
        is bounded by the run's resolved toolchains (#624): a gate the
        sandbox could not run is not a gate (#625).
        """
        return project_gate(
            self.workspace, self.config.sandbox.gate_command, languages=self.languages
        )

    def _check_taskgraph(self, graph: TaskGraph) -> None:
        """Reject graphs whose verify commands violate toolchain conventions
        or whose egress is outside the operator's bounds, and require the
        graph as a whole to run the project's own gate.

        The builder cannot edit verify commands, so a bare `python -m
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

        Egress bounds are the "grant only within operator-set limits"
        guardrail: the decomposer gets one retry to drop an out-of-bounds
        domain (or find a baseline-reachable alternative) before the run
        fails.
        """
        from sbxloop.policy import effective_egress_bounds, egress_rejection

        problems = [
            f"- task {task.id}: {message}"
            for task in graph.tasks
            for message in self._lint_verify_commands(task.verify_commands)
        ]
        gate = self.project_gate()
        if gate:
            every_command = [c for task in graph.tasks for c in task.verify_commands]
            problems += [f"- {message}" for message in gate_problems(every_command, gate)]
        if problems:
            raise ValueError(
                "verify commands violate the sandbox's toolchain conventions:\n"
                + "\n".join(problems)
            )
        allow, deny = effective_egress_bounds(self.config, self.config.github.repo)
        egress_problems = [
            f"- task {task.id}: {egress.domain}: {rejection}"
            for task in graph.tasks
            for egress in task.egress
            if (rejection := egress_rejection(egress.domain, allow, deny)) is not None
        ]
        if egress_problems:
            raise ValueError(
                "task-declared egress is outside the operator's bounds:\n"
                + "\n".join(egress_problems)
                + "\nDrop these domains from `egress` (prefer baseline-reachable hosts: "
                "PyPI, GitHub, apt mirrors — or the well-known package registries, "
                "which are always declarable). Only the operator can extend the "
                "bounds, via [policy] allow in sbxloop.toml."
            )

    def build(
        self,
        task: TaskRecord,
        *,
        prior_report: str = "",
        resume_session_id: str | None = None,
    ) -> JobResult:
        """Plan and do the work for one task, in one session.

        ``prior_report`` is what the previous attempt on this task said it
        did, and ``resume_session_id`` continues that attempt's own agent
        session where it still exists. Both exist to stop a revision
        re-establishing what the last attempt already knew — the engine
        holds that context either way and used to withhold it, so five
        executor sessions on one task each re-ran the same setup and the
        same gate from scratch (field failure rrhb28j7n/t5).
        """
        prompt = render(
            "build",
            outcome=self.outcome,
            task_id=task.spec.id,
            task_title=task.spec.title,
            task_description=task.spec.description or "(no further description)",
            acceptance_criteria=bullet_list(task.spec.acceptance_criteria),
            verify_commands=bullet_list(
                task.spec.verify_commands, empty="(no verify commands for this task)"
            ),
            feedback=task.last_feedback or "(none — first attempt)",
            prior_attempt=clip(prior_report) or "(none — this is the first attempt)",
            user_guidance=self._guidance(),
        )
        return self._agent_job(
            prompt,
            phase="build",
            permission_mode="auto",
            expect="text",
            resume_session_id=resume_session_id,
        )

    def review(
        self,
        *,
        diff: str | None,
        pr_number: int,
        round: int,
        tasks: Sequence[TaskRecord],
        history: str,
        refuted: set[str],
    ) -> ReviewVerdict:
        """Review the delivered PR: a fresh read-only session over its diff.

        ``history`` is the rendered earlier rounds and ``refuted`` the
        anchors of findings the fixer refuted in them — the reviewer is
        told about both, and :class:`ReviewGuard` sends back, once, a
        verdict that only re-raises refuted findings.
        """
        limit = self.config.landing.review_diff_max_chars
        diff_shown = clip_head_tail(
            diff, head=min(REVIEW_DIFF_HEAD_CLIP, limit * 2 // 3), tail=limit // 3
        )
        board = bullet_list(
            [
                f"{t.spec.id} [{t.state}] {t.spec.title}"
                + (
                    "\n  acceptance: " + "; ".join(t.spec.acceptance_criteria)
                    if t.spec.acceptance_criteria
                    else ""
                )
                for t in tasks
            ],
            empty="(no tasks recorded)",
        )
        verdict, _ = self._agent_json(
            ReviewVerdict,
            "review",
            {
                "outcome": self.outcome,
                "pr_number": str(pr_number),
                "round": str(round),
                "diff": diff_shown
                or "(no diff text available — review the tree in the working directory)",
                "tasks_summary": board,
                "prior_rounds": history,
                "user_guidance": self._guidance(),
                "project_gate": gate_rule(self.project_gate()),
                "config_override_example": config_override_example(self.languages),
            },
            permission_mode="read_only",
            check=ReviewGuard(refuted).check,
        )
        return verdict

    def steer(
        self,
        message: str,
        *,
        tasks: Sequence[TaskRecord],
        task: TaskRecord | None,
        stage: str | None = None,
    ) -> SteerVerdict:
        """Answer one interactive chat message and rule on its course change.

        ``task`` is the task the engine is currently driving (None between
        tasks, and throughout the post-build stages); ``tasks`` is the whole
        board, so the agent can speak to overall progress; ``stage`` names
        where the run is when no task is active ("awaiting CI on PR #12").
        """
        board = bullet_list(
            [f"{t.spec.id} [{t.state}] {t.spec.title}" for t in tasks],
            empty="(the outcome has not been decomposed into tasks yet)",
        )
        if task is None:
            current = (
                f"(no task is active right now — the run is {stage})"
                if stage
                else "(no task is active right now — the run is between tasks)"
            )
        else:
            current = (
                f"Task {task.spec.id}: {task.spec.title} (state: {task.state}, "
                f"revisions: {task.revisions}, replans: {task.replans})\n\n"
                f"{task.spec.description or '(no further description)'}\n\n"
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

    def verify(self, task: TaskRecord) -> VerifyOutcome:
        """Run the task's decomposer-authored verify commands; the transcript
        rides on the outcome."""
        commands = list(dict.fromkeys(task.spec.verify_commands))
        failures: list[VerifyFailure] = []
        results: list[str] = []
        for result in self.shell_batch(commands) if commands else []:
            output = clip_head_tail(result.output)
            results.append(f"$ {result.command}\n(exit {result.exit_code})\n{output}")
            if result.exit_code != 0:
                failures.append(VerifyFailure(result.command, result.exit_code, output))
        return VerifyOutcome(
            passed=not failures,
            feedback="\n\n".join(
                f"{VERIFY_FAILURE_PREFIX} `{failure.command}` "
                f"(exit {failure.exit_code})\n{failure.output}"
                for failure in failures
            ),
            results="\n\n".join(results) or "(no verify commands)",
            failures=tuple(failures),
        )
