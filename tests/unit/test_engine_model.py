"""TaskGraph and model validation tests."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from sbxloop.engine.model import (
    DEFAULT_ARTIFACT_EXCLUDES,
    RunRecord,
    SteerVerdict,
    TaskGraph,
    TaskSpec,
    Verdict,
    artifact_files,
    artifacts_dir,
    scan_artifacts,
)


def spec(id: str, deps: list[str] | None = None) -> dict[str, object]:
    return {"id": id, "title": id.upper(), "depends_on": deps or []}


class TestTaskGraph:
    def test_valid_graph_topo_order(self) -> None:
        graph = TaskGraph.model_validate(
            {"tasks": [spec("t3", ["t1", "t2"]), spec("t1"), spec("t2", ["t1"])]}
        )
        assert [t.id for t in graph.topo_order()] == ["t1", "t2", "t3"]

    def test_topo_order_stable_for_independent_tasks(self) -> None:
        graph = TaskGraph.model_validate({"tasks": [spec("b"), spec("a"), spec("c")]})
        assert [t.id for t in graph.topo_order()] == ["b", "a", "c"]  # authored order

    def test_empty_graph_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one task"):
            TaskGraph.model_validate({"tasks": []})

    def test_duplicate_ids_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate task ids"):
            TaskGraph.model_validate({"tasks": [spec("t1"), spec("t1")]})

    def test_unknown_dependency_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unknown tasks"):
            TaskGraph.model_validate({"tasks": [spec("t1", ["ghost"])]})

    def test_self_dependency_rejected(self) -> None:
        with pytest.raises(ValidationError, match="depends on itself"):
            TaskGraph.model_validate({"tasks": [spec("t1", ["t1"])]})

    def test_cycle_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cycle"):
            TaskGraph.model_validate({"tasks": [spec("t1", ["t2"]), spec("t2", ["t1"])]})


class TestVerdict:
    def test_verdict_literals(self) -> None:
        assert Verdict(verdict="pass").issues == []
        with pytest.raises(ValidationError):
            Verdict(verdict="maybe")  # type: ignore[arg-type]

    def test_task_spec_defaults(self) -> None:
        t = TaskSpec(id="t1", title="X")
        assert t.acceptance_criteria == []
        assert t.verify_commands == []


class TestArtifactFiles:
    """The exclusion is a targeted denylist, not "anything dot-prefixed":
    dot-path artifacts agents produce on purpose (.github/, .gitignore) must
    survive listings and delivery (#67)."""

    def make_workspace(self, tmp_path: Path) -> Path:
        root = tmp_path / "ws"
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
        (root / ".gitignore").write_text("*.pyc\n")
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("pass\n")
        (root / ".git" / "refs").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text("ref\n")
        (root / ".git" / "refs" / "x").write_text("sha\n")
        return root

    def test_dot_path_artifacts_kept_git_excluded(self, tmp_path: Path) -> None:
        root = self.make_workspace(tmp_path)
        rels = [p.relative_to(root).as_posix() for p in artifact_files(root)]
        assert rels == [".github/workflows/ci.yml", ".gitignore", "src/main.py"]

    def test_scan_counts_exclusions_per_entry(self, tmp_path: Path) -> None:
        root = self.make_workspace(tmp_path)
        scan = scan_artifacts(root)
        assert scan.excluded == {".git": 2}
        assert scan.excluded_total == 2
        assert scan.excluded_note == "2 file(s) excluded (.git)"

    def test_nested_excluded_dir_is_caught(self, tmp_path: Path) -> None:
        root = self.make_workspace(tmp_path)
        (root / "vendor" / ".git").mkdir(parents=True)
        (root / "vendor" / ".git" / "config").write_text("x\n")
        scan = scan_artifacts(root)
        assert scan.excluded == {".git": 3}
        assert all(".git" not in p.parts for p in scan.files)

    def test_sbxloop_state_dir_excluded_by_default(self, tmp_path: Path) -> None:
        root = self.make_workspace(tmp_path)
        (root / ".sbxloop").mkdir()
        (root / ".sbxloop" / "state.db").write_text("db\n")
        scan = scan_artifacts(root)
        assert scan.excluded == {".git": 2, ".sbxloop": 1}
        assert scan.excluded_note == "3 file(s) excluded (.git, .sbxloop)"

    def test_custom_exclude_list(self, tmp_path: Path) -> None:
        root = self.make_workspace(tmp_path)
        scan = scan_artifacts(root, exclude=[".git", "src"])
        assert [p.relative_to(root).as_posix() for p in scan.files] == [
            ".github/workflows/ci.yml",
            ".gitignore",
        ]
        assert scan.excluded == {".git": 2, "src": 1}

    def test_no_exclusions_has_no_note(self, tmp_path: Path) -> None:
        root = tmp_path / "clean"
        root.mkdir()
        (root / "a.txt").write_text("a")
        scan = scan_artifacts(root)
        assert scan.excluded == {}
        assert scan.excluded_note is None

    def test_config_default_mirrors_model_default(self) -> None:
        # config.py keeps a literal copy (importing engine.model there would
        # be circular); this pins the two against drift.
        from sbxloop.config import ArtifactsConfig

        assert tuple(ArtifactsConfig().exclude) == DEFAULT_ARTIFACT_EXCLUDES


class TestArtifactsDir:
    def test_mounted_run_uses_workspace(self) -> None:
        record = RunRecord(
            run_id="r1",
            outcome="x",
            state="completed",
            created_at=1.0,
            updated_at=1.0,
            workspace=Path("/tmp/ws"),
            mounted=True,
        )
        assert artifacts_dir(record, Path("/state")) == Path("/tmp/ws")

    def test_unmounted_run_uses_harvest_dir(self) -> None:
        record = RunRecord(
            run_id="r1",
            outcome="x",
            state="completed",
            created_at=1.0,
            updated_at=1.0,
            workspace=Path("/tmp/ws"),
            mounted=False,
        )
        assert artifacts_dir(record, Path("/state")) == Path("/state/runs/r1/artifacts")

    def test_never_provisioned_run_has_none(self) -> None:
        record = RunRecord(
            run_id="r1", outcome="x", state="created", created_at=1.0, updated_at=1.0
        )
        assert artifacts_dir(record, Path("/state")) is None


class TestSteerVerdict:
    def test_continue_needs_no_guidance(self) -> None:
        verdict = SteerVerdict(reply="all fine")
        assert verdict.action == "continue"
        assert verdict.guidance == ""

    def test_steer_actions_require_guidance(self) -> None:
        with pytest.raises(ValidationError, match="guidance"):
            SteerVerdict(reply="ok", action="steer_task")
        with pytest.raises(ValidationError, match="guidance"):
            SteerVerdict(reply="ok", action="steer_run", guidance="   ")
        verdict = SteerVerdict(reply="ok", action="steer_run", guidance="use Go")
        assert verdict.guidance == "use Go"

    def test_unknown_action_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SteerVerdict(reply="ok", action="abort_everything", guidance="g")
