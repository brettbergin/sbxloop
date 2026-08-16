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
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from git import GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo

from sbxloop.errors import DeliveryError, ProvisionError

logger = logging.getLogger(__name__)

# Ref pinning the commit a run clone was cut from. Lives inside the clone's
# own .git so it survives resumes and travels nowhere else (refs outside
# heads/tags are not pushed or listed as branches).
CLONE_BASE_REF = "refs/sbxloop/base"

ChangeStatus = Literal["added", "modified", "deleted"]


@dataclass(frozen=True)
class WorkspaceChange:
    """One path the run changed relative to its base commit. ``mode`` is the
    git tree mode to commit (``100644``/``100755``/``120000``); empty for
    deletions."""

    path: str
    status: ChangeStatus
    mode: str


@dataclass(frozen=True)
class RefreshResult:
    """What :func:`refresh_from_origin` did to a checkout.

    ``advanced`` is True only when HEAD moved; ``message`` is always a
    human-readable one-liner (the daemon relays it as a chronology event).
    ``before``/``after`` are HEAD shas (equal when nothing moved).
    """

    advanced: bool
    before: str | None
    after: str | None
    message: str


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


def origin_url(repo_path: Path) -> str | None:
    """The URL of the checkout's ``origin`` remote, or None when it has none."""
    try:
        with Repo(repo_path) as repo:
            if "origin" not in {r.name for r in repo.remotes}:
                return None
            url = repo.remotes.origin.url
    except (InvalidGitRepositoryError, NoSuchPathError, GitCommandError, ValueError):
        return None
    return url or None


def public_remote_url(url: str) -> str:
    """``url`` with any embedded userinfo removed.

    Git remotes routinely carry a credential in the URL itself
    (``https://x-access-token:ghp_...@github.com/o/r``); copying such a URL
    into a per-run clone would persist that token in the clone's
    ``.git/config``, which the agent sandbox can read. Only scheme URLs can
    carry userinfo worth hiding — the ``git@`` in an scp-style
    ``git@github.com:o/r`` is a login name, not a secret, and is left alone.
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    authority, slash, path = rest.partition("/")
    _userinfo, at, host = authority.rpartition("@")
    return f"{scheme}://{host if at else authority}{slash}{path}"


def clone_for_run(source: Path, target: Path, branch: str) -> str:
    """Clone ``source`` into ``target`` on a fresh ``branch``; return HEAD sha.

    Self-contained by construction (no alternates); on failure any
    half-created target is removed so a retry starts clean. The commit the
    clone started from is pinned under ``CLONE_BASE_REF`` so delivery can
    later diff the run's work against it (#248) even after the agent has
    committed, rebased or moved the branch — HEAD alone no longer says
    where the run began.


    A path clone's ``origin`` is the *host path* of the source, which is
    meaningless inside the sandbox VM and misleading to anyone reading
    ``git remote -v`` in the run workspace afterwards. When the source has
    its own ``origin`` (the GitHub remote, typically) the clone's origin is
    re-pointed at that URL — metadata only: userinfo is stripped first (see
    :func:`public_remote_url`) so no credential travels, and nothing in
    sbxloop pushes from the workspace (delivery goes through the GitHub API
    from the github sandbox).
    """
    raw_upstream = origin_url(source)
    upstream = public_remote_url(raw_upstream) if raw_upstream is not None else None
    try:
        with Repo.clone_from(str(source), str(target)) as clone:
            clone.git.checkout("-b", branch)
            if upstream is not None:
                clone.git.remote("set-url", "origin", upstream)
            sha = clone.head.commit.hexsha
            clone.git.update_ref(CLONE_BASE_REF, sha)
            return sha
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

    The in-place mode opens a ``.git`` the sandbox agent could write to
    (the clone is mounted into the VM), so its config is untrusted:
    ``core.fsmonitor`` names a hook git runs while scanning the work tree,
    which would turn this scan into host code execution. It is forced off
    on the command line, which outranks every config file. Nothing else
    ``ls-files`` consults can execute code (no hooks, filters, or pager
    with captured output).

    ``root`` is made absolute first: unmounted artifact roots derive from
    the relative default ``state_dir`` (``.sbxloop/runs/...``), and git
    resolves ``GIT_WORK_TREE`` against its cwd -- which is ``root`` -- so
    a relative value would point at ``root/root`` and ignore nothing.
    """
    git = find_git()
    if git is None:
        return None
    root = root.absolute()
    argv = [
        git,
        "-c",
        "core.fsmonitor=false",
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-per-directory=.gitignore",
        "-z",
    ]
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


def resolve_diff_base(repo_path: Path, remote_base_sha: str | None) -> str | None:
    """The commit a run's changes should be measured against, or None when
    the checkout carries no anchor at all (a repo the agent ``git init``-ed
    itself, say).

    Preference order:

    1. The merge base with the PR's target commit when that commit is
       known locally — the run then delivers exactly what ``git diff
       base...HEAD`` (plus the working tree) shows, including local
       commits the checkout was ahead by.
    2. The commit the run clone was cut from (``CLONE_BASE_REF``).
    3. ``origin/HEAD``'s merge base, for clones made before the pin
       existed.
    """
    try:
        with Repo(repo_path) as repo:
            head = repo.head.commit.hexsha
            if remote_base_sha:
                base = _merge_base(repo, remote_base_sha, head)
                if base:
                    return base
            for ref in (CLONE_BASE_REF, "origin/HEAD"):
                base = _merge_base(repo, ref, head)
                if base:
                    return base
    except (InvalidGitRepositoryError, NoSuchPathError, ValueError):
        return None
    return None


def _merge_base(repo: Repo, ref: str, head: str) -> str | None:
    try:
        return str(repo.git.merge_base(ref, head)).strip() or None
    except GitCommandError:
        return None


def changes_since(repo_path: Path, base: str) -> list[WorkspaceChange]:
    """What the working tree changed relative to ``base``: tracked paths
    from ``git diff`` (committed or not — the agent may do either) plus
    untracked-but-not-ignored files, sorted by path.

    Renames are reported as delete + add on purpose: the consumer builds a
    git tree, which has no rename concept. Modes come from the working tree
    (what ``git add`` itself would record) so an executable script keeps
    ``100755``; symlinks are ``120000`` with the link target as content.
    """
    changes: dict[str, WorkspaceChange] = {}
    try:
        with Repo(repo_path) as repo:
            listing = repo.git.diff("--name-status", "--no-renames", "-z", base)
            for status, path in _pairs(listing):
                changes[path] = _describe_change(repo_path, path, status)
            untracked = repo.git.ls_files("--others", "--exclude-standard", "-z")
            for path in untracked.split("\0"):
                if path:
                    changes[path] = _describe_change(repo_path, path, "A")
    except (InvalidGitRepositoryError, NoSuchPathError, GitCommandError) as exc:
        raise DeliveryError(f"git diff failed in {repo_path}: {exc}") from exc
    return [changes[path] for path in sorted(changes)]


def _pairs(listing: str) -> list[tuple[str, str]]:
    fields = listing.split("\0")
    return [(fields[i], fields[i + 1]) for i in range(0, len(fields) - 1, 2) if fields[i]]


def _describe_change(repo_path: Path, path: str, git_status: str) -> WorkspaceChange:
    """``git_status`` is git's name-status letter (A/M/D/T...)."""
    if git_status.startswith("D"):
        return WorkspaceChange(path=path, status="deleted", mode="")
    status: ChangeStatus = "added" if git_status.startswith("A") else "modified"
    full = repo_path / path
    if full.is_symlink():
        return WorkspaceChange(path=path, status=status, mode="120000")
    if not full.is_file():
        # Gone from disk but still in the index (rm without git rm): git
        # diff against a commit still lists it; the tree must drop it.
        return WorkspaceChange(path=path, status="deleted", mode="")
    executable = bool(full.stat().st_mode & 0o111)
    return WorkspaceChange(path=path, status=status, mode="100755" if executable else "100644")
def refresh_from_origin(repo_path: Path) -> RefreshResult:
    """``git fetch`` and fast-forward the checked-out branch to its
    upstream, so an unattended run starts from current ``<remote>/<branch>``
    rather than whatever HEAD the checkout was last left at (#255).

    The remote fetched is the one the branch tracks (``upstream/main`` in a
    fork layout, not necessarily ``origin``); a branch with no upstream
    falls back to ``origin/<branch>``, the convention everyone expects.

    Strictly non-destructive: fast-forward only. A detached HEAD, a branch
    with no upstream, a diverged local branch, or a working tree whose
    local edits collide with the update all leave the checkout exactly as
    found and say so in the result. Fetch failures (network down, remote
    gone) raise :class:`ProvisionError`; the caller decides whether that is
    fatal — for the daemon it is not, a stale HEAD beats no run.
    """
    try:
        with Repo(repo_path) as repo:
            remotes = {r.name for r in repo.remotes}
            before = repo.head.commit.hexsha if repo.head.is_valid() else None
            on_branch = before is not None and not repo.head.is_detached
            tracking = repo.active_branch.tracking_branch() if on_branch else None
            # Fetch the remote that owns the ref we will fast-forward to;
            # fetching origin while the branch tracks another remote would
            # leave that ref stale and call the checkout "up to date".
            remote_name = tracking.remote_name if tracking is not None else "origin"
            if remote_name not in remotes:
                return RefreshResult(False, before, before, f"{repo_path}: no {remote_name} remote")
            try:
                repo.remote(remote_name).fetch()
            except GitCommandError as exc:
                raise ProvisionError(
                    f"git fetch {remote_name} failed in {repo_path}: {_describe(exc)}"
                ) from exc
            if not on_branch:
                why = "unborn HEAD" if before is None else "detached HEAD"
                return RefreshResult(False, before, before, f"{repo_path}: {why}; fetched only")
            branch = repo.active_branch
            if tracking is None:
                # No upstream configured (a plain `git clone` sets one, a
                # hand-built checkout may not): fall back to the same-named
                # origin branch.
                candidate = f"origin/{branch.name}"
                if candidate not in {r.name for r in repo.remotes.origin.refs}:
                    return RefreshResult(
                        False, before, before, f"{repo_path}: {branch.name} has no origin branch"
                    )
                remote_ref = candidate
            else:
                remote_ref = tracking.name
            remote_commit = repo.commit(remote_ref)
            if remote_commit.hexsha == before:
                return RefreshResult(
                    False,
                    before,
                    before,
                    f"{repo_path}: {branch.name} up to date with {remote_ref}",
                )
            if not repo.is_ancestor(repo.head.commit, remote_commit):
                return RefreshResult(
                    False,
                    before,
                    before,
                    f"{repo_path}: {branch.name} has diverged from {remote_ref}; "
                    "left as is (local commits would need a merge or rebase)",
                )
            try:
                repo.git.merge("--ff-only", remote_ref)
            except GitCommandError as exc:
                return RefreshResult(
                    False,
                    before,
                    before,
                    f"{repo_path}: could not fast-forward {branch.name} to {remote_ref}: "
                    f"{_describe(exc)}",
                )
            after = repo.head.commit.hexsha
            return RefreshResult(
                True,
                before,
                after,
                f"{repo_path}: fast-forwarded {branch.name} "
                f"{(before or '')[:12]} → {after[:12]} ({remote_ref})",
            )
    except (InvalidGitRepositoryError, NoSuchPathError, ValueError) as exc:
        raise ProvisionError(f"cannot refresh {repo_path}: {exc}") from exc
    except GitCommandError as exc:
        raise ProvisionError(f"refreshing {repo_path} failed: {_describe(exc)}") from exc


def _describe(exc: Exception) -> str:
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, str) and stderr.strip():
        return stderr.strip().splitlines()[-1]
    return str(exc)
