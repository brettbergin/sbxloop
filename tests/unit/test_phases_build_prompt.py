"""The build prompt names where the builder is and what it has (#689):
the checkout path and the run's resolved toolchain set with versions, from
the run — not guessed from the task."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sbxloop import toolchains
from sbxloop.config import Config
from sbxloop.engine.model import TaskRecord, TaskSpec
from sbxloop.engine.phases import PhaseRunner
from sbxloop_worker.protocol import JobRequest, JobResult


class RecordingAgent:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def submit(
        self, job: JobRequest, *, agent: str | None = None, tool_handler: Any = None
    ) -> JobResult:
        assert job.prompt is not None
        self.prompts.append(job.prompt)
        return JobResult(job_id=job.job_id, status="ok", output_text="done")


TASK = TaskRecord(spec=TaskSpec(id="t1", title="Do it"))


def test_the_resolved_set_and_versions_reach_the_builder(tmp_path: Path) -> None:
    agent = RecordingAgent()
    versions = {"javascript": toolchains.ToolchainVersion("22", ".nvmrc", "22")}
    PhaseRunner(
        agent,  # type: ignore[arg-type]
        Config(),
        "r1",
        "ship it",
        workdir="/work/repo",
        workspace=tmp_path,
        languages=("typescript", "go"),
        versions=versions,
    ).build(TASK)
    prompt = " ".join(agent.prompts[0].split())
    assert "checked out at `/work/repo`." in prompt
    assert "Never write outside `/work/repo`." in prompt
    assert (
        "Resolved toolchains for this repository: javascript 22 (from .nvmrc), typescript, go."
        in prompt
    )


def test_without_a_given_set_the_workspace_pins_are_read(tmp_path: Path) -> None:
    """An embedder that names the languages but not the versions gets the
    same answer provisioning would have read (#627)."""
    (tmp_path / "go.mod").write_text("module m\n\ngo 1.22\n")
    (tmp_path / ".python-version").write_text("3.13\n")
    agent = RecordingAgent()
    PhaseRunner(agent, Config(), "r1", "ship it", workspace=tmp_path).build(TASK)  # type: ignore[arg-type]
    prompt = " ".join(agent.prompts[0].split())
    assert "checked out at the current working directory." in prompt
    assert "Resolved toolchains for this repository: python 3.13 (from .python-version)." in prompt
