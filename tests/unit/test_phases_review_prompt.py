"""The review prompt is written for a reader, not a builder (#690): the
project's gate reaches it as a result to weigh — or as "there is none" —
never as the decomposer's instruction to run it, and a diff the budget
clipped says so where the cut is rather than passing as unchanged."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sbxloop.config import Config
from sbxloop.engine.model import TaskRecord, TaskSpec
from sbxloop.engine.phases import PhaseRunner, clip_diff
from sbxloop_worker.protocol import JobRequest, JobResult

VERDICT = {"verdict": "approve", "summary": "fine", "findings": []}
TASK = TaskRecord(spec=TaskSpec(id="t1", title="Do it"))


class ReviewAgent:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def submit(
        self, job: JobRequest, *, agent: str | None = None, tool_handler: Any = None
    ) -> JobResult:
        assert job.prompt is not None
        self.prompts.append(job.prompt)
        return JobResult(
            job_id=job.job_id, status="ok", output_json=VERDICT, output_text=json.dumps(VERDICT)
        )


def review(
    workspace: Path, diff: str = "+x", languages: tuple[str, ...] | None = None, **config: Any
) -> str:
    agent = ReviewAgent()
    runner = PhaseRunner(
        agent,  # type: ignore[arg-type]
        Config.model_validate(config),
        "r1",
        "ship it",
        workspace=workspace,
        languages=languages,
    )
    runner.review(diff=diff, pr_number=1, round=1, tasks=[TASK], history="", refuted=set())
    return agent.prompts[0]


def test_review_prompt_treats_the_gate_as_evidence(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("check:\n\t./ci.sh\n")
    # Gate detection is bounded by the resolved toolchains (#624).
    prompt = review(tmp_path, languages=("python", "make"))
    assert "This repository's gate is `make check`." in prompt
    assert "treat its result as evidence about the diff" in prompt
    assert "do not re-run it" in prompt
    # The decomposer's instruction never reaches the reviewer.
    assert "MUST run" not in prompt
    assert "verify_commands` MUST" not in prompt


def test_review_prompt_says_when_there_is_no_gate(tmp_path: Path) -> None:
    prompt = review(tmp_path)
    assert "This repository declares no single gate command" in prompt
    assert "judge the diff on its own" in prompt
    assert "you must run" not in prompt.lower()
    assert "none is required" not in prompt


class TestClippedDiff:
    def test_a_short_diff_passes_through(self) -> None:
        assert clip_diff("+one\n+two\n", 10_000) == "+one\n+two\n"
        assert clip_diff(None, 10_000) == ""

    def test_the_cut_is_marked_in_the_diffs_own_terms(self) -> None:
        lines = [f"+line {index:04d}" for index in range(2_000)]
        diff = "\n".join(lines)
        clipped = clip_diff(diff, 10_000)
        assert clipped.startswith("+line 0000")
        assert clipped.endswith("+line 1999")
        marker = next(line for line in clipped.splitlines() if line.startswith("[diff clipped"))
        assert marker.startswith("[diff clipped at 10000 chars — ")
        assert "lines not shown; do not assume they are unchanged" in marker
        assert "read those files from the working tree" in marker
        hidden = len(diff) - (len(clipped) - len(marker) - 2)
        assert f"{hidden} chars" in marker

    def test_the_reviewer_is_told_where_the_cut_is(self, tmp_path: Path) -> None:
        diff = "\n".join(f"+line {index:04d}" for index in range(3_000))
        prompt = review(tmp_path, diff=diff, landing={"review_diff_max_chars": 10_000})
        assert "[diff clipped at 10000 chars — " in prompt
        assert "never treat the gap as unchanged" in " ".join(prompt.split())
        assert "Anything not shown here is unchanged" not in prompt
