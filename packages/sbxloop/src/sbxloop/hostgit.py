"""Host-side git helpers for per-run workspace isolation, on GitPython.

When ``[sandbox] workspace`` points at an existing checkout, provisioning
clones it into the per-run workspace instead of mutating it in place (see
``Provisioner._resolve_workspace``). GitPython drives the system git
binary, so semantics are exact-git: a plain path-source clone hardlinks
objects on the same filesystem and degrades to copying across filesystems
automatically. ``--shared``/``--reference`` are deliberately NOT used:
their ``objects/info/alternates`` would point at a host path that is
invisible inside the sandbox VM, breaking git for the agent; a plain clone
is self-contained.

``find_git`` gates the whole feature: ``auto`` isolation falls back to
in-place when no git binary exists rather than erroring, so the import of
GitPython's ``Repo`` (which raises at *use* when git is missing) stays
behind that probe.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from git import GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo

from sbxloop.errors import ProvisionError

logger = logging.getLogger(__name__)


def find_git() -> str | None:
    """Absolute path of the git binary, or None when git is not installed."""
    return shutil.which("git")


def repo_toplevel(path: Path) -> Path | None:
    """The working-tree root of the checkout containing ``path``, or None
    when ``path`` is not inside a git checkout (bare repos included: they
    have no working tree to isolate)."""
    try:
        with Repo(path, search_parent_directories=True) as repo:
            top = repo.working_tree_dir
    except (InvalidGitRepositoryError, NoSuchPathError):
        return None
    return Path(top) if top else None


def head_commit(repo_path: Path) -> str | None:
    """The commit sha of HEAD, or None on an unborn HEAD (no commits yet)."""
    try:
        with Repo(repo_path) as repo:
            return repo.head.commit.hexsha
    except (InvalidGitRepositoryError, NoSuchPathError, ValueError):
        return None


def is_dirty(repo_path: Path) -> bool:
    """Whether the checkout has uncommitted changes.

    Untracked files count: a clone takes committed HEAD, so anything this
    reports would silently not travel into the run workspace.
    """
    try:
        with Repo(repo_path) as repo:
            return repo.is_dirty(untracked_files=True)
    except (InvalidGitRepositoryError, NoSuchPathError, GitCommandError) as exc:
        raise ProvisionError(f"git status failed in {repo_path}: {exc}") from exc


def clone_for_run(source: Path, target: Path, branch: str) -> str:
    """Clone ``source`` into ``target`` on a fresh ``branch``; return HEAD sha.

    Self-contained by construction (no alternates); on failure any
    half-created target is removed so a retry starts clean.
    """
    try:
        with Repo.clone_from(str(source), str(target)) as clone:
            clone.git.checkout("-b", branch)
            return clone.head.commit.hexsha
    except (GitCommandError, NoSuchPathError, OSError, ValueError) as exc:
        if target.exists() and not (target / ".git").is_dir():
            shutil.rmtree(target, ignore_errors=True)
        raise ProvisionError(
            f"cloning workspace {source} into {target} failed: {_describe(exc)}"
        ) from exc


def _describe(exc: Exception) -> str:
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, str) and stderr.strip():
        return stderr.strip().splitlines()[-1]
    return str(exc)
