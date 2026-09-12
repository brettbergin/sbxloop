"""Repository builders for tests, behind one hermetic git environment.

Building goes through GitPython — the typed objects the code under test
itself uses — so a fixture that needs a different shape takes a parameter
rather than growing another argv wrapper. Assertions in the tests keep
reading the result with the git binary (:func:`git`): they check the
conversion, not GitPython against itself.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from git import Actor, Repo


def git_env() -> dict[str, str]:
    """The environment every test-side git process runs under: no user or
    system config, a fixed identity, and — read at call time, since a test
    sets it while a private HTTPS server is up — the test CA."""
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_SSL_CAINFO": os.environ.get("GIT_SSL_CAINFO", ""),
    }


IDENTITY = Actor("t", "t@example.com")


def git(*argv: str, cwd: Path) -> str:
    """The git binary, hermetically; stdout stripped. For assertions, and
    for the operations a builder does not model."""
    return subprocess.run(  # nosec B603 B607 - fixed argv, test-only
        ["git", *argv], cwd=cwd, check=True, capture_output=True, text=True, env=git_env()
    ).stdout.strip()


def open_repo(root: Path) -> Repo:
    """A :class:`Repo` on ``root`` whose commands run under :func:`git_env`."""
    repo = Repo(root)
    repo.git.update_environment(**git_env())
    return repo


def init_repo(root: Path, branch: str = "main") -> Repo:
    """A fresh repository on ``branch`` with no commits. The init itself is
    the binary under :func:`git_env` — ``Repo.init`` would run under the
    inherited environment, user config and templates included."""
    root.mkdir(parents=True, exist_ok=True)
    git("init", "-b", branch, cwd=root)
    return open_repo(root)


def commit_files(repo: Repo, files: Mapping[str, str], message: str) -> str:
    """Write ``files`` (relative path → text), stage them and commit; the
    new commit's sha."""
    root = Path(str(repo.working_tree_dir))
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    repo.index.add(list(files))
    return repo.index.commit(message, author=IDENTITY, committer=IDENTITY).hexsha


def make_repo(tmp_path: Path, name: str = "src", files: Mapping[str, str] | None = None) -> Path:
    """``tmp_path/name``: one commit on ``main`` carrying ``files``
    (default ``hello.txt``)."""
    root = tmp_path / name
    with init_repo(root) as repo:
        commit_files(repo, dict(files) if files is not None else {"hello.txt": "hi\n"}, "init")
    return root
