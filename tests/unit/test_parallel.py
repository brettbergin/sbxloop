"""Unit tests for the parallel-execution primitives: TaskSpec.owns
validation, wave packing, and the workspace merge helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop.config import Config, load_config
from sbxloop.engine.merge import (
    apply_changes,
    build_seed_dir,
    owns_violations,
    snapshot_tree,
    tree_changes,
)
from sbxloop.engine.model import (
    TaskRecord,
    TaskSpec,
    owns_disjoint,
    pack_parallel_batch,
)


def spec(id: str, owns: list[str] | None = None) -> TaskSpec:
    return TaskSpec(id=id, title=f"Task {id}", owns=owns or [])


def record(id: str, owns: list[str] | None = None) -> TaskRecord:
    return TaskRecord(spec=spec(id, owns))


class TestOwnsValidation:
    def test_normalizes_trailing_slash(self) -> None:
        assert spec("t1", owns=["src/parser/"]).owns == ["src/parser"]

    def test_rejects_absolute(self) -> None:
        with pytest.raises(ValueError, match="must be relative"):
            spec("t1", owns=["/etc"])

    @pytest.mark.parametrize("bad", ["..", "a/../b", ".", ""])
    def test_rejects_traversal_and_empty(self, bad: str) -> None:
        with pytest.raises(ValueError, match="plain relative path"):
            spec("t1", owns=[bad])


class TestOwnsDisjoint:
    def test_disjoint_siblings(self) -> None:
        assert owns_disjoint(["a"], ["b"])
        assert owns_disjoint(["src/a"], ["src/b"])

    def test_nested_overlaps(self) -> None:
        assert not owns_disjoint(["src"], ["src/parser"])
        assert not owns_disjoint(["src/parser"], ["src"])
        assert not owns_disjoint(["a", "c"], ["b", "c/d"])

    def test_prefix_string_is_not_path_overlap(self) -> None:
        # "doc" vs "docs" share a string prefix but are distinct paths.
        assert owns_disjoint(["doc"], ["docs"])


class TestPackParallelBatch:
    def test_undeclared_ownership_runs_alone(self) -> None:
        ready = [record("t1"), record("t2", ["b"])]
        assert [t.spec.id for t in pack_parallel_batch(ready, 4)] == ["t1"]

    def test_disjoint_owns_pack_up_to_max_parallel(self) -> None:
        ready = [record("t1", ["a"]), record("t2", ["b"]), record("t3", ["c"])]
        assert [t.spec.id for t in pack_parallel_batch(ready, 2)] == ["t1", "t2"]
        assert [t.spec.id for t in pack_parallel_batch(ready, 4)] == ["t1", "t2", "t3"]

    def test_overlapping_owns_deferred(self) -> None:
        ready = [record("t1", ["a"]), record("t2", ["a/sub"]), record("t3", ["b"])]
        assert [t.spec.id for t in pack_parallel_batch(ready, 4)] == ["t1", "t3"]

    def test_no_owns_candidate_skipped_not_packed(self) -> None:
        ready = [record("t1", ["a"]), record("t2"), record("t3", ["b"])]
        assert [t.spec.id for t in pack_parallel_batch(ready, 4)] == ["t1", "t3"]


class TestMergeHelpers:
    def test_snapshot_excludes_hidden_paths(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "a/one.txt").write_text("1")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git/HEAD").write_text("ref")
        (tmp_path / "a/.hidden").write_text("x")
        assert set(snapshot_tree(tmp_path)) == {"a/one.txt"}

    def test_snapshot_of_missing_dir_is_empty(self, tmp_path: Path) -> None:
        assert snapshot_tree(tmp_path / "nope") == {}

    def test_tree_changes(self) -> None:
        baseline = {"keep.txt": "h1", "mod.txt": "h2", "gone.txt": "h3"}
        current = {"keep.txt": "h1", "mod.txt": "CHANGED", "new.txt": "h4"}
        assert tree_changes(baseline, current) == {
            "mod.txt": "modified",
            "new.txt": "added",
            "gone.txt": "deleted",
        }

    def test_owns_violations(self) -> None:
        changes = {"a/one.txt": "added", "b/two.txt": "added", "top.txt": "modified"}
        task = spec("t1", owns=["a"])
        assert owns_violations(changes, task) == ["b/two.txt", "top.txt"]
        assert owns_violations(changes, spec("t2", owns=["a", "b", "top.txt"])) == []

    def test_apply_changes_including_deletion(self, tmp_path: Path) -> None:
        staging = tmp_path / "staging"
        merged = tmp_path / "merged"
        (staging / "a").mkdir(parents=True)
        (staging / "a/new.txt").write_text("new")
        merged.mkdir()
        (merged / "old.txt").write_text("old")
        apply_changes(staging, merged, {"a/new.txt": "added", "old.txt": "deleted"})
        assert (merged / "a/new.txt").read_text() == "new"
        assert not (merged / "old.txt").exists()

    def test_build_seed_dir_rebuilds_and_skips_hidden(self, tmp_path: Path) -> None:
        merged = tmp_path / "merged"
        (merged / ".git").mkdir(parents=True)
        (merged / ".git/config").write_text("x")
        (merged / "src").mkdir()
        (merged / "src/app.py").write_text("code")
        seed = tmp_path / "seed"
        (seed / "stale").mkdir(parents=True)
        build_seed_dir(merged, seed)
        assert (seed / "src/app.py").read_text() == "code"
        assert not (seed / ".git").exists()
        assert not (seed / "stale").exists()


class TestRunSettingsConfig:
    def test_default_is_sequential(self) -> None:
        assert Config().run.max_parallel == 1

    def test_toml_and_env_layers(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text("[run]\nmax_parallel = 3\n")
        assert load_config(cwd=tmp_path, env={}).run.max_parallel == 3
        env = {"SBXLOOP_RUN__MAX_PARALLEL": "5"}
        assert load_config(cwd=tmp_path, env=env).run.max_parallel == 5

    def test_rejects_zero(self) -> None:
        with pytest.raises(ValueError, match="max_parallel"):
            Config.model_validate({"run": {"max_parallel": 0}})
