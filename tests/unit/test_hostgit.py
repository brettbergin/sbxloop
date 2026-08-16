"""hostgit tests against real git repos built in tmp_path.

The helpers wrap GitPython (which drives the system git binary); tests
arrange fixtures with plain subprocess git so the arrangement is
independent of the code under test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sbxloop import hostgit
from sbxloop.errors import ProvisionError


def git(*argv: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *argv],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )


def make_repo(tmp_path: Path, name: str = "src") -> Path:
    root = tmp_path / name
    root.mkdir()
    git("init", "-b", "main", cwd=root)
    (root / "hello.txt").write_text("hi\n")
    git("add", ".", cwd=root)
    git("commit", "-m", "init", cwd=root)
    return root


class TestRepoToplevel:
    def test_root_returns_itself(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        assert hostgit.repo_toplevel(root) == root.resolve()

    def test_subdir_returns_root(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        sub = root / "sub"
        sub.mkdir()
        assert hostgit.repo_toplevel(sub) == root.resolve()

    def test_plain_dir_is_none(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert hostgit.repo_toplevel(plain) is None


class TestDirtyAndHead:
    def test_clean_repo_is_not_dirty(self, tmp_path: Path) -> None:
        assert hostgit.is_dirty(make_repo(tmp_path)) is False

    def test_modified_file_is_dirty(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        (root / "hello.txt").write_text("changed\n")
        assert hostgit.is_dirty(root) is True

    def test_untracked_file_is_dirty(self, tmp_path: Path) -> None:
        # Untracked files would silently not travel into a clone; they must
        # trip the dirty refusal like modifications do.
        root = make_repo(tmp_path)
        (root / "new.txt").write_text("x\n")
        assert hostgit.is_dirty(root) is True

    def test_ignored_names_do_not_count(self, tmp_path: Path) -> None:
        """sbxloop's own state dir dropped inside a checkout is run state,
        not user content (field failure r5a1d9m9c)."""
        root = make_repo(tmp_path)
        (root / ".sbxloop").mkdir()
        (root / ".sbxloop" / "state.db").write_text("db\n")
        assert hostgit.is_dirty(root) is True  # counted without ignore
        assert hostgit.is_dirty(root, ignore=[".sbxloop"]) is False

    def test_real_changes_still_count_alongside_ignored_names(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        (root / ".sbxloop").mkdir()
        (root / ".sbxloop" / "state.db").write_text("db\n")
        (root / "hello.txt").write_text("changed\n")
        assert hostgit.is_dirty(root, ignore=[".sbxloop"]) is True

    def test_head_commit_sha(self, tmp_path: Path) -> None:
        sha = hostgit.head_commit(make_repo(tmp_path))
        assert sha is not None and len(sha) == 40

    def test_unborn_head_is_none(self, tmp_path: Path) -> None:
        root = tmp_path / "empty"
        root.mkdir()
        git("init", "-b", "main", cwd=root)
        assert hostgit.head_commit(root) is None


class TestCloneForRun:
    def test_clone_is_self_contained_on_branch(self, tmp_path: Path) -> None:
        source = make_repo(tmp_path)
        target = tmp_path / "state" / "runs" / "r1" / "workspace"
        target.parent.mkdir(parents=True)

        sha = hostgit.clone_for_run(source, target, "sbxloop/r1")

        assert sha == hostgit.head_commit(source)
        assert (target / "hello.txt").read_text() == "hi\n"
        # self-contained: a real .git directory, no alternates into the source
        assert (target / ".git").is_dir()
        assert not (target / ".git" / "objects" / "info" / "alternates").exists()
        # on the run branch
        head = (target / ".git" / "HEAD").read_text().strip()
        assert head.endswith("refs/heads/sbxloop/r1")
        # the source repo is untouched: no new branch, same HEAD
        assert not (source / ".git" / "refs" / "heads" / "sbxloop").exists()
        assert hostgit.head_commit(source) == sha

    def test_clone_failure_raises_provision_error(self, tmp_path: Path) -> None:
        with pytest.raises(ProvisionError, match="cloning workspace"):
            hostgit.clone_for_run(tmp_path / "nope", tmp_path / "target", "sbxloop/r1")

    def test_find_git_missing_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hostgit.shutil, "which", lambda name: None)
        assert hostgit.find_git() is None


def touch(root: Path, *rels: str) -> None:
    for rel in rels:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")


class TestGitignoredFiles:
    """Build byproducts a project gitignores (wheels, dist/, generated
    _version.py) must not reach a delivered PR (#249)."""

    def test_checkout_uses_its_index(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        (root / ".gitignore").write_text("dist/\n_vendor/\n_version.py\n")
        touch(root, "dist/a.whl", "pkg/_vendor/w.whl", "pkg/_version.py", "src/m.py")
        # A force-added ignored file is tracked, hence a deliverable.
        git("add", "-f", "pkg/_version.py", cwd=root)
        assert hostgit.gitignored_files(root) == {"dist/a.whl", "pkg/_vendor/w.whl"}

    def test_plain_tree_honours_in_tree_gitignore(self, tmp_path: Path) -> None:
        """Harvested copies carry .gitignore but never .git."""
        root = tmp_path / "harvest"
        root.mkdir()
        (root / ".gitignore").write_text("dist/\n")
        (root / "pkg").mkdir()
        (root / "pkg" / ".gitignore").write_text("_version.py\n")
        touch(root, "dist/a.whl", "pkg/_version.py", "pkg/m.py", "_version.py")
        assert hostgit.gitignored_files(root) == {"dist/a.whl", "pkg/_version.py"}

    def test_broken_dot_git_falls_back_to_plain_probe(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        (root / ".git").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text("ref\n")
        (root / ".gitignore").write_text("dist/\n")
        touch(root, "dist/a.whl", "m.py")
        assert hostgit.gitignored_files(root) == {"dist/a.whl"}

    def test_parent_checkout_rules_do_not_leak(self, tmp_path: Path) -> None:
        """A harvest dir under a checkout's .sbxloop/ must not see itself as
        ignored through the enclosing repo's rules."""
        outer = make_repo(tmp_path)
        (outer / ".gitignore").write_text(".sbxloop/\n")
        root = outer / ".sbxloop" / "runs" / "r1" / "artifacts"
        touch(root, "app.py", "dist/a.whl")
        assert hostgit.gitignored_files(root) == frozenset()

    def test_no_git_binary_is_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hostgit, "find_git", lambda: None)
        assert hostgit.gitignored_files(tmp_path) is None
