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
