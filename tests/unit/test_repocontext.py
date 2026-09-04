"""The repository's own instruction files reach every phase prompt (#688).

A repo that says "run make lint before committing" in AGENTS.md said it
to nobody: the planner and the reviewer run as their own sessions with
their own prompts, and the builder saw the file only where its CLI read
it by convention. Now the loop reads the files itself, once per render,
and hands each phase the same capped block under one heading.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.engine.model import TaskRecord, TaskSpec
from sbxloop.engine.phases import PhaseRunner
from sbxloop.engine.repocontext import (
    CONVENTION_FILES,
    HEADING,
    RepoContext,
    read_repo_context,
    repo_conventions,
)
from sbxloop_worker.protocol import JobRequest, JobResult

RULE = "Run `make lint` before committing; never touch `generated/`."


def write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class TestReadRepoContext:
    def test_nothing_declared_is_nothing_said(self, tmp_path: Path) -> None:
        assert read_repo_context(tmp_path, max_chars=12_000) == RepoContext("", (), False)
        assert read_repo_context(None, max_chars=12_000) == RepoContext("", (), False)
        assert repo_conventions(tmp_path, max_chars=12_000) == ""

    def test_every_convention_file_is_read_in_order_under_its_own_heading(
        self, tmp_path: Path
    ) -> None:
        for index, name in enumerate(CONVENTION_FILES):
            write(tmp_path, name, f"rule {index}\n")
        context = read_repo_context(tmp_path, max_chars=12_000)
        assert context.files == CONVENTION_FILES
        assert not context.clipped
        headings = [line for line in context.conventions.splitlines() if line.startswith("### ")]
        assert headings == [f"### {name}" for name in CONVENTION_FILES]
        assert context.conventions.index("rule 0") < context.conventions.index("rule 9")

    def test_a_symlinked_or_copied_file_is_rendered_once_under_both_names(
        self, tmp_path: Path
    ) -> None:
        write(tmp_path, "AGENTS.md", RULE + "\n")
        (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")
        write(tmp_path, "CONTRIBUTING.md", RULE + "\n")  # a copy, not a link
        context = read_repo_context(tmp_path, max_chars=12_000)
        assert context.conventions.count(RULE) == 1
        assert context.conventions.startswith("### AGENTS.md (also CLAUDE.md, CONTRIBUTING.md)\n")
        assert context.files == ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md")

    def test_empty_and_unreadable_files_are_skipped(self, tmp_path: Path) -> None:
        write(tmp_path, "AGENTS.md", "  \n\n")
        (tmp_path / "CLAUDE.md").mkdir()  # a directory of that name, not a file
        write(tmp_path, "CONTRIBUTING.md", RULE + "\n")
        context = read_repo_context(tmp_path, max_chars=12_000)
        assert context.files == ("CONTRIBUTING.md",)

    def test_a_latin1_byte_does_not_fail_the_read(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_bytes(b"Maintainer: Ren\xe9\n" + RULE.encode())
        context = read_repo_context(tmp_path, max_chars=12_000)
        assert RULE in context.conventions and "Ren�" in context.conventions

    def test_clipping_at_the_budget_says_so(self, tmp_path: Path) -> None:
        write(tmp_path, "AGENTS.md", "x" * 500)
        context = read_repo_context(tmp_path, max_chars=100)
        assert context.clipped
        assert context.conventions.endswith("\n\n(clipped at 100 chars)")
        body = context.conventions.removesuffix("\n\n(clipped at 100 chars)")
        assert len(body) <= 100
        assert context.files == ("AGENTS.md",)

    def test_a_block_within_the_budget_is_not_clipped(self, tmp_path: Path) -> None:
        write(tmp_path, "AGENTS.md", RULE)
        context = read_repo_context(tmp_path, max_chars=len(f"### AGENTS.md\n\n{RULE}"))
        assert not context.clipped and "clipped" not in context.conventions

    def test_a_zero_budget_reads_nothing(self, tmp_path: Path) -> None:
        write(tmp_path, "AGENTS.md", RULE)
        assert read_repo_context(tmp_path, max_chars=0) == RepoContext("", (), False)

    def test_the_prompt_section_opens_with_the_heading(self, tmp_path: Path) -> None:
        write(tmp_path, "AGENTS.md", RULE)
        section = repo_conventions(tmp_path, max_chars=12_000)
        assert section.startswith(HEADING + "\n\n### AGENTS.md\n\n")
        assert "follow them over the defaults below" in HEADING


class PromptAgent:
    """Records every prompt it is handed and answers from a script."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.prompts: list[tuple[str, str]] = []  # (agent persona, prompt)

    def submit(self, job: JobRequest, *, agent: str | None = None) -> JobResult:
        assert job.prompt is not None
        self.prompts.append((agent or "", job.prompt))
        answer = self.responses.pop(0)
        return JobResult(
            job_id=job.job_id,
            status="ok",
            output_json=answer if isinstance(answer, dict) else None,
            output_text=answer if isinstance(answer, str) else json.dumps(answer),
        )


GRAPH = {"tasks": [{"id": "t1", "title": "Do it", "verify_commands": [".venv/bin/pytest -q"]}]}
VERDICT = {"verdict": "approve", "summary": "fine", "findings": []}


class TestEveryPhaseCarriesTheConventions:
    """#688 acceptance: a workspace with AGENTS.md produces decompose,
    build and review prompts carrying its text under the heading."""

    @pytest.fixture
    def phases(self, tmp_path: Path) -> tuple[PhaseRunner, PromptAgent]:
        write(tmp_path, "AGENTS.md", RULE + "\n")
        agent = PromptAgent([GRAPH, "done", VERDICT])
        runner = PhaseRunner(agent, Config(), "r1", "ship it", workspace=tmp_path)  # type: ignore[arg-type]
        return runner, agent

    def test_decompose_build_and_review(self, phases: tuple[PhaseRunner, PromptAgent]) -> None:
        runner, agent = phases
        runner.decompose()
        task = TaskRecord(spec=TaskSpec(id="t1", title="Do it"))
        runner.build(task)
        runner.review(diff="+x", pr_number=1, round=1, tasks=[task], history="", refuted=set())
        assert [persona for persona, _ in agent.prompts] == ["decomposer", "builder", "reviewer"]
        for phase, prompt in agent.prompts:
            assert HEADING in prompt, phase
            assert prompt.index(HEADING) < prompt.index("### AGENTS.md") < prompt.index(RULE), phase

    def test_the_budget_is_the_operators(self, tmp_path: Path) -> None:
        write(tmp_path, "AGENTS.md", "x" * 500)
        config = Config.model_validate({"budgets": {"repo_context_max_chars": 64}})
        agent = PromptAgent([GRAPH])
        PhaseRunner(agent, config, "r1", "ship it", workspace=tmp_path).decompose()  # type: ignore[arg-type]
        assert "(clipped at 64 chars)" in agent.prompts[0][1]
        assert "x" * 100 not in agent.prompts[0][1]

    def test_the_files_are_reread_per_render(self, tmp_path: Path) -> None:
        agent = PromptAgent([GRAPH, GRAPH])
        runner = PhaseRunner(agent, Config(), "r1", "ship it", workspace=tmp_path)  # type: ignore[arg-type]
        runner.decompose()
        assert HEADING not in agent.prompts[0][1]
        write(tmp_path, "AGENTS.md", RULE)  # a task wrote it; the next plan is held to it
        runner.decompose()
        assert RULE in agent.prompts[1][1]

    def test_no_workspace_renders_no_heading(self) -> None:
        agent = PromptAgent([GRAPH])
        PhaseRunner(agent, Config(), "r1", "ship it").decompose()  # type: ignore[arg-type]
        assert HEADING not in agent.prompts[0][1]
