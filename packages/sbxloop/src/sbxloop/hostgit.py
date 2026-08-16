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
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
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


def is_dirty(repo_path: Path, *, ignore: Sequence[str] = ()) -> bool:
    """Whether the checkout has uncommitted changes.

    Untracked files count: a clone takes committed HEAD, so anything this
    reports would silently not travel into the run workspace. ``ignore``
    names top-level entries excluded from the check — sbxloop's own state
    directory must never trip the isolation refusal (running any sbxloop
    command from inside a checkout drops a relative ``.sbxloop`` there,
    which is run state, not user content — field failure r5a1d9m9c).
    """
    try:
        with Repo(repo_path) as repo:
            pathspecs = [f":!{name}" for name in ignore]
            output = repo.git.status("--porcelain", "--", *pathspecs)
            return bool(output.strip())
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


def gitignored_files(root: Path) -> frozenset[str] | None:
    """Relative POSIX paths under ``root`` that the tree's own ``.gitignore``
    rules ignore, or None when git is unavailable or the probe fails.

    Backs the artifact scan (#249): the name-based exclude list cannot know
    that *this* project's ``dist/``, ``_vendor/*.whl`` or generated
    ``_version.py`` are build byproducts, but the project's ``.gitignore``
    does — and any tree after a build/sync carries them, so without this
    they land in the delivered PR.

    Two modes, both ``git ls-files --others --ignored`` (untracked *and*
    ignored — a force-added tracked file stays a deliverable):

    * ``root`` is itself a checkout (the per-run clone, or an in-place
      workspace): run in that repo, so the index decides what is tracked.
    * otherwise (harvested copies never carry ``.git``; plain workspaces):
      point git at a throwaway empty repo with ``root`` as the work tree,
      so every file is untracked and only the ignore rules matter.

    Only in-tree ``.gitignore`` files apply (``--exclude-per-directory``,
    not ``--exclude-standard``): a delivery must not depend on the
    operator's global excludes file, and the throwaway repo has no
    ``info/exclude`` anyway. Never searches parent directories — a harvest
    dir under a checkout's ``.sbxloop/`` would otherwise inherit that
    checkout's rules and see *itself* as ignored, dropping everything.
    """
    git = find_git()
    if git is None:
        return None
    argv = [git, "ls-files", "--others", "--ignored", "--exclude-per-directory=.gitignore", "-z"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if (root / ".git").exists():
        try:
            return _ls_files(argv, root, env)
        except (subprocess.CalledProcessError, OSError):
            # Not a usable repo (a bare `.git` marker, corrupt HEAD, …):
            # fall through to the self-contained probe.
            logger.debug("in-place gitignore probe failed in %s", root, exc_info=True)
    try:
        with tempfile.TemporaryDirectory(prefix="sbxloop-gitignore-") as tmp:
            subprocess.run(  # nosec B603 - list argv, git binary, no shell
                [git, "init", "-q", tmp], check=True, capture_output=True, env=env
            )
            env["GIT_DIR"] = str(Path(tmp) / ".git")
            env["GIT_WORK_TREE"] = str(root)
            return _ls_files(argv, root, env)
    except (subprocess.CalledProcessError, OSError):
        logger.warning(
            "gitignore probe failed in %s; ignore rules not applied", root, exc_info=True
        )
        return None


def _ls_files(argv: Sequence[str], cwd: Path, env: dict[str, str]) -> frozenset[str]:
    proc = subprocess.run(  # nosec B603 - list argv, git binary, no shell
        list(argv), cwd=cwd, env=env, check=True, capture_output=True
    )
    return frozenset(
        part.decode("utf-8", "surrogateescape") for part in proc.stdout.split(b"\0") if part
    )


def _describe(exc: Exception) -> str:
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, str) and stderr.strip():
        return stderr.strip().splitlines()[-1]
    return str(exc)
