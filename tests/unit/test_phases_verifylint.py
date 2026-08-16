"""PhaseRunner's verify-command lint keys on the workspace's project shape
(#250): a ``uv.lock`` at the host workspace root flips the Python
convention from ``.venv/bin/...`` to ``uv run``, and it is re-read on every
check so a lockfile the executor creates mid-run is honored by later plans."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop.config import Config
from sbxloop.engine.model import PlanModel, TaskGraph, TaskSpec
from sbxloop.engine.phases import PhaseRunner


def runner(workspace: Path | None) -> PhaseRunner:
    return PhaseRunner(None, Config(), "r1", "ship it", workspace=workspace)  # type: ignore[arg-type]


def plan(*verify: str) -> PlanModel:
    return PlanModel(steps=["do"], verify_commands=list(verify))


def test_no_workspace_keeps_the_venv_convention() -> None:
    phases = runner(None)
    phases._check_plan(plan(".venv/bin/pytest -q"))
    with pytest.raises(ValueError, match=r"\.venv/bin/pytest"):
        phases._check_plan(plan("pytest -q"))


def test_lockfile_in_workspace_requires_uv_run(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("version = 1\n")
    phases = runner(tmp_path)
    phases._check_plan(plan("uv run pytest -q"))
    with pytest.raises(ValueError, match="uv run"):
        phases._check_plan(plan(".venv/bin/pytest -q"))


def test_lockfile_is_reread_per_check(tmp_path: Path) -> None:
    # A mounted workspace gains uv.lock when the executor runs `uv init`/
    # `uv lock` in an early task; the plan for the next task is held to the
    # convention the workspace has NOW, not at construction.
    phases = runner(tmp_path)
    phases._check_plan(plan(".venv/bin/pytest -q"))
    (tmp_path / "uv.lock").write_text("version = 1\n")
    with pytest.raises(ValueError, match=r"uv\.lock"):
        phases._check_plan(plan(".venv/bin/pytest -q"))


def test_taskgraph_check_uses_the_same_project_shape(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("version = 1\n")
    phases = runner(tmp_path)
    graph = TaskGraph(
        tasks=[TaskSpec(id="t1", title="Build", verify_commands=[".venv/bin/pytest -q"])]
    )
    with pytest.raises(ValueError, match=r"task t1: .*uv run"):
        phases._check_taskgraph_verify_commands(graph)
