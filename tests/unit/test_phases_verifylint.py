"""PhaseRunner's verify-command lint keys on the workspace's project shape
(#250): a ``uv.lock`` at the host workspace root flips the Python
convention from ``.venv/bin/...`` to ``uv run``, and it is re-read on every
check so a lockfile the builder creates mid-run is honored by later
decompositions (a fix round's graph is re-checked against the workspace as
it stands)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop.config import Config
from sbxloop.engine.model import TaskGraph, TaskSpec, VerifyReauthor
from sbxloop.engine.phases import PhaseRunner


def runner(workspace: Path | None) -> PhaseRunner:
    return PhaseRunner(None, Config(), "r1", "ship it", workspace=workspace)  # type: ignore[arg-type]


def graph(*verify: str, egress: list[dict[str, str]] | None = None) -> TaskGraph:
    return TaskGraph(
        tasks=[
            TaskSpec.model_validate(
                {
                    "id": "t1",
                    "title": "Build",
                    "verify_commands": list(verify),
                    "egress": egress or [],
                }
            )
        ]
    )


def test_no_workspace_keeps_the_venv_convention() -> None:
    phases = runner(None)
    phases._check_taskgraph(graph(".venv/bin/pytest -q"))
    with pytest.raises(ValueError, match=r"\.venv/bin/pytest"):
        phases._check_taskgraph(graph("pytest -q"))


def test_lockfile_in_workspace_requires_uv_run(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("version = 1\n")
    phases = runner(tmp_path)
    phases._check_taskgraph(graph("uv run pytest -q"))
    with pytest.raises(ValueError, match="uv run"):
        phases._check_taskgraph(graph(".venv/bin/pytest -q"))


def test_lockfile_is_reread_per_check(tmp_path: Path) -> None:
    # A mounted workspace gains uv.lock when the builder runs `uv init`/
    # `uv lock` in an early task; the next graph check is held to the
    # convention the workspace has NOW, not at construction.
    phases = runner(tmp_path)
    phases._check_taskgraph(graph(".venv/bin/pytest -q"))
    (tmp_path / "uv.lock").write_text("version = 1\n")
    with pytest.raises(ValueError, match=r"uv\.lock"):
        phases._check_taskgraph(graph(".venv/bin/pytest -q"))


def test_taskgraph_check_names_the_offending_task(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("version = 1\n")
    phases = runner(tmp_path)
    bad = graph(".venv/bin/pytest -q")
    with pytest.raises(ValueError, match=r"task t1: .*uv run"):
        phases._check_taskgraph(bad)


def test_out_of_bounds_egress_is_rejected_with_the_bounds_message() -> None:
    """Task-declared egress is checked at graph acceptance (the plan phase
    that used to do this is gone); the rejection feeds the retry with the
    operator-bounds explanation."""
    phases = runner(None)
    bad = graph("test -f README.md", egress=[{"domain": "evil.example.com", "reason": "api"}])
    with pytest.raises(ValueError, match=r"outside the operator's bounds"):
        phases._check_taskgraph(bad)


def test_declarable_registry_egress_is_accepted() -> None:
    phases = runner(None)
    ok = graph(
        "test -f README.md", egress=[{"domain": "registry.npmjs.org", "reason": "npm install"}]
    )
    phases._check_taskgraph(ok)


# -- re-author guards ---------------------------------------------------
#
# The re-author phase is the only actor allowed to change the exam rather
# than the work, and it is asked to do so having just been told a check is
# in the way. These are what stop "fix the check" becoming "delete it".


def _answer(verdict: str, command: str = "") -> VerifyReauthor:
    return VerifyReauthor(verdict=verdict, command=command, reason="r")  # type: ignore[arg-type]


def test_a_replacement_is_held_to_the_same_lint_as_a_decomposition() -> None:
    phases = runner(None)
    phases._check_reauthor(_answer("replace", ".venv/bin/pytest -q"), suspect_command="x")
    with pytest.raises(ValueError, match=r"\.venv/bin/pytest"):
        phases._check_reauthor(_answer("replace", "pytest -q"), suspect_command="x")


def test_a_replacement_may_not_reach_the_network_or_kill_by_pattern() -> None:
    phases = runner(None)
    with pytest.raises(ValueError, match="not the network"):
        phases._check_reauthor(
            _answer("replace", "curl -sf https://example.com/"), suspect_command="x"
        )
    with pytest.raises(ValueError, match="by pattern or name"):
        phases._check_reauthor(_answer("replace", "pkill -f server"), suspect_command="x")


@pytest.mark.parametrize("command", ["true", "echo ok", ": ", "true && echo done"])
def test_a_replacement_that_cannot_fail_is_refused(command: str) -> None:
    """A check that passes whatever the workspace contains is a deleted
    check wearing the shape of one."""
    phases = runner(None)
    with pytest.raises(ValueError, match="cannot fail"):
        phases._check_reauthor(_answer("replace", command), suspect_command="x")


def gated_runner(workspace: Path) -> PhaseRunner:
    """A runner whose resolved toolchains include the gate's own (#624):
    a detector whose command the sandbox could not run is not consulted."""
    return PhaseRunner(  # type: ignore[arg-type]
        None, Config(), "r1", "ship it", workspace=workspace, languages=("python", "make")
    )


def test_the_command_carrying_the_project_gate_cannot_be_dropped(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("check:\n\tpytest\n")
    phases = gated_runner(tmp_path)
    assert phases.project_gate() == "make check"
    with pytest.raises(ValueError, match="cannot be dropped"):
        phases._check_reauthor(_answer("drop"), suspect_command="make check")
    # ... and a replacement for it must still run it
    with pytest.raises(ValueError, match="must run it too"):
        phases._check_reauthor(_answer("replace", "test -f out"), suspect_command="make check")
    phases._check_reauthor(_answer("replace", "make check ARGS=1"), suspect_command="make check")


def test_dropping_an_ordinary_check_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("check:\n\tpytest\n")
    phases = gated_runner(tmp_path)
    phases._check_reauthor(_answer("drop"), suspect_command="test -f dist/index.html")


def test_keep_is_never_rejected() -> None:
    runner(None)._check_reauthor(_answer("keep"), suspect_command="anything at all")
