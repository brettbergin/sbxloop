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

Clone size (#632)
-----------------
Every run clone is ``--single-branch --no-tags`` (:data:`CLONE_OPTIONS`):
a run works on one branch, and on a repository with years of release tags
and hundreds of branches the rest is dead weight copied per run. This is
safe because nothing downstream reads another branch out of the clone —
:func:`merge_from_base` fetches the base branch *explicitly* before every
fix round, and :func:`resolve_diff_base` then finds the merge base through
that fetch, the ``CLONE_BASE_REF`` pin, or ``origin/HEAD``.

What is deliberately NOT done:

* ``--depth 1``. A shallow clone has no history for ``git merge-base`` to
  walk, so the delivery diff could not be anchored (the base the branch
  forked from is exactly what a shallow clone throws away) and
  ``merge_from_base`` would have nothing to merge against.
* ``--filter=blob:none`` by default. A partial clone keeps history but
  fetches blobs *lazily*, from inside the sandbox VM, the moment the agent
  touches a file whose blob is missing (``git checkout``, ``git diff``,
  ``git log -p`` …). The VM holds no git credential, so on a private
  remote that is a mid-task git failure with no useful message; even on a
  public one every such blob is a network round trip in the agent's
  critical path. It is therefore opt-in — ``[sandbox] clone_filter =
  "blob:none"`` — and applies only to :func:`clone_from_remote`, the
  credential-free public-remote path where lazy fetches can succeed at
  all (git ignores filters on local path clones anyway). A git too old to
  know ``--filter`` falls back to an unfiltered clone with a log line
  rather than failing the run.
"""

from __future__ import annotations

import contextlib
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
from sbxloop.log import get_logger

log = get_logger(__name__)

# Ref pinning the commit a run clone was cut from. Lives inside the clone's
# own .git so it survives resumes and travels nowhere else (refs outside
# heads/tags are not pushed or listed as branches).
CLONE_BASE_REF = "refs/sbxloop/base"

# Options every run clone carries; see "Clone size" in the module docstring.
CLONE_OPTIONS = ("--single-branch", "--no-tags")

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


def normalise_repo_url(url: str | None) -> str | None:
    """Canonical lowercased ``owner/name`` for a GitHub remote URL, or None.

    Accepts scp-style ssh (``git@github.com:owner/name.git``), https/git
    scheme URLs with or without embedded credentials, a trailing slash or a
    ``.git`` suffix, and a bare ``owner/name`` slug. Anything that does not
    yield exactly an owner and a name is None.
    """
    if url is None:
        return None
    text = url.strip()
    if not text:
        return None
    _scheme, sep, rest = text.partition("://")
    if sep:
        _authority, _slash, path = rest.partition("/")
    else:
        _userinfo, at, remainder = text.rpartition("@")
        path = remainder.partition(":")[2] if at else text
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) != 2:
        return None
    owner, name = parts
    return f"{owner.lower()}/{name.lower()}"


def origin_matches_repo(path: Path, repo: str) -> bool | None:
    """Whether ``path``'s origin names ``repo``; None when it cannot be told.

    None means the path is not a git checkout, has no origin, or either URL
    is not a recognisable ``owner/name`` remote.
    """
    origin = normalise_repo_url(origin_url(path))
    if origin is None:
        return None
    expected = normalise_repo_url(repo)
    if expected is None:
        return None
    return origin == expected


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
        with Repo.clone_from(str(source), str(target), multi_options=list(CLONE_OPTIONS)) as clone:
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


def clone_from_remote(
    repo_url: str,
    target: Path,
    branch: str,
    *,
    existing: bool = False,
    clone_filter: str | None = None,
) -> str:
    """Clone a *remote* URL into ``target`` on a fresh ``branch``; return sha.

    The no-local-checkout path of per-repo workspace resolution: a repo
    entry configured without a workspace has no host tree to clone, so the
    run's tree comes from the remote itself. Only credential-free (public)
    remotes can work here — the host deliberately holds no git credential
    (#46) — so the clone runs with terminal and credential-helper prompting
    disabled and fails loudly rather than hanging on an auth prompt.

    With ``existing`` the clone is cut *on* ``branch`` (``--branch``): under
    ``--single-branch`` that is the only way ``origin/<branch>`` exists in
    the clone at all, and a branch the remote does not have fails the clone
    — the right outcome for a fix round, which must not rebuild from the
    default branch (see :func:`clone_existing_branch`).

    ``clone_filter`` is a git partial-clone filter (``blob:none``); read the
    module docstring before reaching for it. A git that does not know
    ``--filter`` gets one retry without it, logged as
    ``workspace.clone_filter_unsupported``.

    ``ProvisionError`` on any failure; a half-created target is removed so a
    retry starts clean. Never falls back to another tree.
    """
    env = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GCM_INTERACTIVE": "never",
    }
    options = list(CLONE_OPTIONS)
    if existing:
        options.append(f"--branch={branch}")
    try:
        with _clone_remote(repo_url, target, env, options, clone_filter) as clone:
            if existing:
                # A fix round continuing its own pull request: start from what
                # the remote branch actually has.
                clone.git.checkout("-B", branch, f"origin/{branch}")
            else:
                clone.git.checkout("-b", branch)
            sha = clone.head.commit.hexsha
            clone.git.update_ref(CLONE_BASE_REF, sha)
            return sha
    except (GitCommandError, NoSuchPathError, OSError, ValueError) as exc:
        if target.exists() and not (target / ".git").is_dir():
            shutil.rmtree(target, ignore_errors=True)
        raise ProvisionError(
            f"cloning {public_remote_url(repo_url)} into {target} failed: {_describe(exc)}"
        ) from exc


def _clone_remote(
    repo_url: str, target: Path, env: dict[str, str], options: list[str], clone_filter: str | None
) -> Repo:
    if clone_filter is None:
        return Repo.clone_from(repo_url, str(target), env=env, multi_options=options)
    try:
        return Repo.clone_from(
            repo_url, str(target), env=env, multi_options=[*options, f"--filter={clone_filter}"]
        )
    except GitCommandError as exc:
        if not _unknown_filter_option(exc):
            raise
        # A server that cannot filter merely warns and sends everything; only
        # a client too old for --filter refuses outright. Either way the run
        # gets a full clone, never an error.
        log.warning(
            "workspace.clone_filter_unsupported",
            repo=public_remote_url(repo_url),
            clone_filter=clone_filter,
            detail=_describe(exc),
            hint="git does not support --filter; cloning without it",
        )
        if target.exists() and not (target / ".git").is_dir():
            shutil.rmtree(target, ignore_errors=True)
        return Repo.clone_from(repo_url, str(target), env=env, multi_options=options)


def _unknown_filter_option(exc: GitCommandError) -> bool:
    text = _describe(exc).lower()
    return "unknown option" in text and "filter" in text


def clone_existing_branch(source: Path, target: Path, branch: str) -> str:
    """Clone ``source`` into ``target`` checked out on an existing ``branch``.

    The counterpart to :func:`clone_for_run`, which starts fresh work: this
    *continues* work that already exists on a branch — a fix round against
    its own pull request. It checks out ``origin/<branch>`` (the state the
    PR actually has, not whatever a stale local ref says) onto a local
    branch of the same name, so a later delivery under that name updates
    the same PR rather than opening a second one.

    Raises :class:`ProvisionError` when the branch is not on the remote.
    That refusal is the point: a fix round that silently fell back to the
    default branch would rebuild from scratch, and its delivery would then
    force-update the PR's branch with a tree that never contained the PR's
    work — destroying it. Failing to provision is recoverable; that is not.
    """
    raw_upstream = origin_url(source)
    upstream = public_remote_url(raw_upstream) if raw_upstream is not None else None
    remote_ref = f"origin/{branch}"
    try:
        with Repo.clone_from(str(source), str(target), multi_options=list(CLONE_OPTIONS)) as clone:
            if remote_ref not in {r.name for r in clone.refs}:
                # A single-branch clone of a clone carries only the source's
                # HEAD branch as `origin/<head>`, never its remote-tracking
                # refs — and the branch a PR lives on is only ever
                # remote-tracking in the daemon's checkout (it fetched it; it
                # never checked it out). Ask the source for that exact ref
                # rather than mutating it; a local branch of that name on the
                # source (a hand-made checkout) is the second place to look.
                _fetch_branch_from_source(clone, source, branch)
            clone.git.checkout("-B", branch, remote_ref)
            if upstream is not None:
                clone.git.remote("set-url", "origin", upstream)
            sha = clone.head.commit.hexsha
            clone.git.update_ref(CLONE_BASE_REF, sha)
            return sha
    except ProvisionError:
        # The clone itself succeeded — it is the branch that is missing — so
        # `.git` exists and the usual "only clean a half-clone" guard would
        # leave a complete but useless checkout behind, which a resume would
        # then reuse (`_clone_workspace` never re-clones over one). Remove
        # it: nothing here is worth keeping.
        shutil.rmtree(target, ignore_errors=True)
        raise
    except (GitCommandError, NoSuchPathError, OSError, ValueError) as exc:
        if target.exists() and not (target / ".git").is_dir():
            shutil.rmtree(target, ignore_errors=True)
        raise ProvisionError(
            f"cloning workspace {source} into {target} on branch {branch} failed: {_describe(exc)}"
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
            log.debug("gitignore.in_place_probe_failed", root=str(root), exc_info=True)
    try:
        with tempfile.TemporaryDirectory(prefix="sbxloop-gitignore-") as tmp:
            subprocess.run(  # nosec B603 - list argv, git binary, no shell
                [git, "init", "-q", tmp], check=True, capture_output=True, env=env
            )
            env["GIT_DIR"] = str(Path(tmp) / ".git")
            env["GIT_WORK_TREE"] = str(root)
            return _ls_files(argv, root, env)
    except (subprocess.CalledProcessError, OSError):
        log.warning(
            "gitignore.probe_failed",
            root=str(root),
            hint="ignore rules not applied",
            exc_info=True,
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


def diff_text(repo_path: Path, remote_base_sha: str | None) -> str | None:
    """The run's changes as a unified diff for a reviewer, or None when the
    checkout has no base to measure against.

    Tracked paths come from ``git diff <base>`` (working tree included,
    committed or not — the agent may do either) and untracked-but-not-
    ignored files are appended as ``--no-index`` diffs against ``/dev/null``
    so a new file the agent never ``git add``-ed is still reviewed. A
    ``--stat`` header opens the text. Unclipped: the caller decides how
    much of it a prompt may carry.
    """
    base = resolve_diff_base(repo_path, remote_base_sha)
    git = find_git()
    if base is None or git is None:
        return None
    try:
        with Repo(repo_path) as repo:
            stat = repo.git.diff("--stat", "--no-color", base)
            body = repo.git.diff("--no-color", base)
            untracked = [
                path
                for path in repo.git.ls_files("--others", "--exclude-standard", "-z").split("\0")
                if path
            ]
    except (InvalidGitRepositoryError, NoSuchPathError, GitCommandError) as exc:
        raise DeliveryError(f"git diff failed in {repo_path}: {exc}") from exc
    parts = [stat.strip(), body]
    for path in untracked:
        # `--no-index` exits 1 whenever the files differ, which they always
        # do against /dev/null; the output is the diff either way.
        proc = subprocess.run(  # nosec B603 - list argv, git binary, no shell
            [
                git,
                "-C",
                str(repo_path),
                "diff",
                "--no-color",
                "--no-index",
                "--",
                "/dev/null",
                path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        parts.append(proc.stdout or f"diff --git a/{path} b/{path}\n(new file, unreadable)\n")
    return "\n".join(part for part in parts if part)


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


def _fetch_branch_from_source(clone: Repo, source: Path, branch: str) -> None:
    last: GitCommandError | None = None
    for src_ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
        try:
            clone.git.fetch("origin", f"{src_ref}:refs/remotes/origin/{branch}")
            return
        except GitCommandError as exc:
            last = exc
    raise ProvisionError(
        f"branch {branch!r} is not on {source} (neither a local branch "
        f"nor a fetched origin ref): {_describe(last) if last else 'no ref'}. Refusing to "
        "continue work on a branch that is not there — a fix round "
        "starting from the default branch would deliver a tree that "
        "never contained the pull request's work, and updating the "
        "branch with it destroys that work"
    ) from last


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


@dataclass(frozen=True)
class MergeResult:
    """What :func:`merge_from_base` did to a run's clone.

    ``merged`` is True when the base is now contained in the branch (a clean
    merge, or nothing to merge); ``conflicts`` are the paths left carrying
    conflict markers for the fixer to resolve when it is not.
    """

    merged: bool
    conflicts: tuple[str, ...]
    message: str


# Identity for the commits the host makes in a run clone. Nothing about
# them is published: delivery squashes the working tree through the Git Data
# API, so these only exist to give git a committer.
_HOST_GIT_ENV = {
    "GIT_AUTHOR_NAME": "sbxloop",
    "GIT_AUTHOR_EMAIL": "sbxloop@localhost",
    "GIT_COMMITTER_NAME": "sbxloop",
    "GIT_COMMITTER_EMAIL": "sbxloop@localhost",
}


def merge_from_base(repo_path: Path, base_branch: str, *, remote: str = "origin") -> MergeResult:
    """Bring the current base branch into a run's clone before a fix round.

    A pull request that conflicts with its base cannot be fixed by editing
    the run's files alone: delivery overlays the working tree onto the
    *current* base tree, so the conflicting hunks would simply be overwritten
    with the run's version. Merging ``<remote>/<base>`` into the clone first
    makes the conflict concrete — the fixer sees the markers in its working
    tree, resolves them, and the next delivery diffs against a base the
    branch now contains.

    Uncommitted work is checkpointed as a commit first (git refuses to merge
    over local edits, and the agent may or may not have committed); local
    commits are invisible to delivery, which squashes the tree. A merge that
    conflicts is left in progress on purpose, with the conflicted paths
    reported, so the fixer finishes it (``git add -A && git commit``). Fetch
    failures raise :class:`ProvisionError`.
    """
    try:
        with Repo(repo_path) as repo, repo.git.custom_environment(**_HOST_GIT_ENV):
            if remote not in {r.name for r in repo.remotes}:
                return MergeResult(False, (), f"{repo_path}: no {remote} remote")
            try:
                # An explicit refspec: the clone is single-branch (#632), so
                # its configured fetch refspec covers only the branch it was
                # cut on, and a bare `fetch origin <base>` would update
                # FETCH_HEAD but never `<remote>/<base>`.
                repo.git.fetch(
                    remote, f"+refs/heads/{base_branch}:refs/remotes/{remote}/{base_branch}"
                )
            except GitCommandError as exc:
                raise ProvisionError(
                    f"git fetch {remote} {base_branch} failed in {repo_path}: {_describe(exc)}"
                ) from exc
            ref = f"{remote}/{base_branch}"
            target = repo.commit(ref)
            if repo.git.status("--porcelain").strip():
                repo.git.add("-A")
                repo.git.commit("-m", f"sbxloop: checkpoint before merging {ref}", "--no-verify")
            if repo.is_ancestor(target, repo.head.commit):
                return MergeResult(True, (), f"{repo_path}: already contains {ref}")
            try:
                repo.git.merge("--no-edit", ref)
            except GitCommandError as exc:
                conflicts = tuple(
                    path
                    for path in repo.git.diff("--name-only", "--diff-filter=U").split("\n")
                    if path
                )
                if not conflicts:
                    # Not a content conflict — leave the tree as it was.
                    with contextlib.suppress(GitCommandError):
                        repo.git.merge("--abort")
                    raise ProvisionError(
                        f"merging {ref} into {repo_path} failed: {_describe(exc)}"
                    ) from exc
                return MergeResult(
                    False,
                    conflicts,
                    f"{repo_path}: merging {ref} left {len(conflicts)} conflicted file(s) "
                    "for the fixer to resolve",
                )
            return MergeResult(True, (), f"{repo_path}: merged {ref} ({target.hexsha[:12]})")
    except (InvalidGitRepositoryError, NoSuchPathError, ValueError) as exc:
        raise ProvisionError(f"cannot merge into {repo_path}: {exc}") from exc


def _describe(exc: Exception) -> str:
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, str) and stderr.strip():
        return stderr.strip().splitlines()[-1]
    return str(exc)


def current_branch(repo_path: Path) -> str | None:
    """The branch a checkout is on, or None (detached HEAD, unborn HEAD, not
    a repository). Used to tell whether a run's clone actually landed on the
    branch it was pinned to (#600)."""
    try:
        with Repo(repo_path) as repo:
            if repo.head.is_detached:
                return None
            return str(repo.active_branch.name)
    except (InvalidGitRepositoryError, NoSuchPathError, TypeError, ValueError):
        return None
