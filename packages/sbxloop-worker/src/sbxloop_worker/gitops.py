"""Git mutations in the agent sandbox, where repository code may execute."""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MergeResult:
    merged: bool
    conflicts: tuple[str, ...]
    message: str


class GitMergeError(RuntimeError):
    pass


def merge_from_base(
    repo_path: Path,
    base_branch: str,
    *,
    remote: str = "origin",
    timeout_s: float = 300,
    base_sha: str | None = None,
    bundle_path: Path | None = None,
) -> MergeResult:
    """Checkpoint the run's edits, fetch its base, and leave any conflicts.

    This function is dispatched to the agent worker, never called by the
    host: add/commit/fetch/merge can execute repository-defined commands.
    """
    deadline = time.monotonic() + timeout_s
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "sbxloop",
        "GIT_AUTHOR_EMAIL": "sbxloop@localhost",
        "GIT_COMMITTER_NAME": "sbxloop",
        "GIT_COMMITTER_EMAIL": "sbxloop@localhost",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(["git", *args], timeout_s)
        proc = subprocess.run(  # nosec B603 B607 - repository commands run in the agent VM
            ["git", *args],
            cwd=repo_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
        if check and proc.returncode:
            raise GitMergeError(proc.stderr.strip() or f"git {args[0]} exited {proc.returncode}")
        return proc

    try:
        remotes = git("remote").stdout.splitlines()
    except (GitMergeError, OSError) as exc:
        raise GitMergeError(f"cannot merge into {repo_path}: {exc}") from exc
    if remote not in remotes and base_sha is None:
        return MergeResult(False, (), f"{repo_path}: no {remote} remote")
    try:
        if base_sha is not None:
            if bundle_path is not None:
                git(
                    "fetch",
                    "--no-recurse-submodules",
                    str(bundle_path),
                    "refs/sbxloop/fetched-base",
                )
            git("update-ref", f"refs/remotes/{remote}/{base_branch}", base_sha)
        else:
            git("fetch", remote, f"+refs/heads/{base_branch}:refs/remotes/{remote}/{base_branch}")
    except GitMergeError as exc:
        raise GitMergeError(
            f"git fetch {remote} {base_branch} failed in {repo_path}: {exc}"
        ) from exc
    ref = f"{remote}/{base_branch}"
    target = git("rev-parse", "--verify", ref).stdout.strip()
    if git("status", "--porcelain").stdout.strip():
        git("add", "-A")
        git("commit", "-m", f"sbxloop: checkpoint before merging {ref}", "--no-verify")
    ancestry = git("merge-base", "--is-ancestor", target, "HEAD", check=False)
    if ancestry.returncode == 0:
        return MergeResult(True, (), f"{repo_path}: already contains {ref}")
    if ancestry.returncode != 1:
        raise GitMergeError(ancestry.stderr.strip())
    try:
        git("merge", "--no-edit", ref)
    except GitMergeError as exc:
        conflicts = tuple(git("diff", "--name-only", "--diff-filter=U").stdout.splitlines())
        if not conflicts:
            with contextlib.suppress(GitMergeError):
                git("merge", "--abort")
            raise GitMergeError(f"merging {ref} into {repo_path} failed: {exc}") from exc
        return MergeResult(
            False,
            conflicts,
            f"{repo_path}: merging {ref} left {len(conflicts)} conflicted file(s) "
            "for the fixer to resolve",
        )
    return MergeResult(True, (), f"{repo_path}: merged {ref} ({target[:12]})")
