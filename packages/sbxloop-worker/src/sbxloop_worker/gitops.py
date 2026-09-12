"""Git mutations in the agent sandbox, where repository code may execute."""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from pathlib import Path

from git import GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Reference, Repo

# Identity for the checkpoint and merge commits, and no prompting: a
# credential the sandbox lacks fails the fetch instead of hanging it.
_ENV = {
    "GIT_AUTHOR_NAME": "sbxloop",
    "GIT_AUTHOR_EMAIL": "sbxloop@localhost",
    "GIT_COMMITTER_NAME": "sbxloop",
    "GIT_COMMITTER_EMAIL": "sbxloop@localhost",
    "GIT_TERMINAL_PROMPT": "0",
}


@dataclass(frozen=True)
class MergeResult:
    merged: bool
    conflicts: tuple[str, ...]
    message: str


class GitMergeError(RuntimeError):
    pass


def _reason(exc: GitCommandError) -> str:
    """git's own words for a failure, which the fixer round relays. The
    sandbox holds no credential, so there is nothing in them to redact."""
    text = str(exc.stderr or "").strip().removeprefix("stderr: ").strip("'")
    return text or f"git exited {exc.status}"


def _paths(listing: bytes) -> tuple[str, ...]:
    """NUL-separated paths read as bytes: a name git cannot decode is still
    a path, and GitPython's index objects would refuse the whole index."""
    return tuple(part.decode("utf-8", "surrogateescape") for part in listing.split(b"\0") if part)


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

    Refs, remotes and ancestry are typed (`Repo`, `Reference`,
    `is_ancestor`); the work-tree operations — status, add, commit, merge
    and the conflict listing — stay git commands on purpose. GitPython's
    `IndexFile` decodes every path strictly, so `index.commit`,
    `unmerged_blobs` and `is_dirty` all raise on a repository with one
    filename that is not valid UTF-8; the commands read the same bytes and
    carry on.
    """
    deadline = time.monotonic() + timeout_s

    def budget() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitMergeError(f"merging into {repo_path} timed out after {timeout_s:.0f}s")
        return remaining

    try:
        repo = Repo(repo_path)
    except (InvalidGitRepositoryError, NoSuchPathError, OSError) as exc:
        raise GitMergeError(f"cannot merge into {repo_path}: {exc}") from exc
    with repo, repo.git.custom_environment(**_ENV):
        if base_sha is None and remote not in {r.name for r in repo.remotes}:
            return MergeResult(False, (), f"{repo_path}: no {remote} remote")
        ref = f"{remote}/{base_branch}"
        try:
            if base_sha is not None:
                if bundle_path is not None:
                    repo.git.fetch(
                        "--no-recurse-submodules",
                        str(bundle_path),
                        "refs/sbxloop/fetched-base",
                        kill_after_timeout=budget(),
                    )
                Reference.create(repo, f"refs/remotes/{ref}", base_sha, force=True)
            else:
                repo.git.fetch(
                    remote,
                    f"+refs/heads/{base_branch}:refs/remotes/{ref}",
                    kill_after_timeout=budget(),
                )
            target = repo.commit(f"refs/remotes/{ref}")
        except (GitCommandError, ValueError) as exc:
            detail = _reason(exc) if isinstance(exc, GitCommandError) else str(exc)
            raise GitMergeError(
                f"git fetch {remote} {base_branch} failed in {repo_path}: {detail}"
            ) from exc
        try:
            status: bytes = repo.git.status("--porcelain", "-z", stdout_as_string=False)
            if status.strip(b"\0"):
                repo.git.add(A=True)
                # No hooks: the checkpoint is sbxloop's, and a repository's
                # pre-commit hook must not decide whether it happens.
                repo.git.commit(m=f"sbxloop: checkpoint before merging {ref}", no_verify=True)
            if repo.is_ancestor(target, repo.head.commit):
                return MergeResult(True, (), f"{repo_path}: already contains {ref}")
        except GitCommandError as exc:
            raise GitMergeError(_reason(exc)) from exc
        try:
            repo.git.merge("--no-edit", ref, kill_after_timeout=budget())
        except GitCommandError as exc:
            unmerged: bytes = repo.git.diff(
                "--name-only", "--diff-filter=U", "-z", stdout_as_string=False
            )
            conflicts = _paths(unmerged)
            if not conflicts:
                with contextlib.suppress(GitCommandError):
                    repo.git.merge("--abort")
                raise GitMergeError(
                    f"merging {ref} into {repo_path} failed: {_reason(exc)}"
                ) from exc
            return MergeResult(
                False,
                conflicts,
                f"{repo_path}: merging {ref} left {len(conflicts)} conflicted file(s) "
                "for the fixer to resolve",
            )
        return MergeResult(True, (), f"{repo_path}: merged {ref} ({target.hexsha[:12]})")
