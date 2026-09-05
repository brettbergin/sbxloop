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
that fetch, the ``CLONE_BASE_REF`` pin, or ``origin/HEAD``. The one thing
a clone does read from tags is its own version — see "Tags" below.

What is deliberately NOT done:

* ``--depth 1``. A shallow clone has no history for ``git merge-base`` to
  walk, so the delivery diff could not be anchored (the base the branch
  forked from is exactly what a shallow clone throws away) and
  ``merge_from_base`` would have nothing to merge against.
* ``--filter=blob:none`` by default. A partial clone keeps history but
  fetches blobs *lazily*, from inside the sandbox VM, the moment the agent
  touches a file whose blob is missing (``git checkout``, ``git diff``,
  ``git log -p`` …). The VM holds no git credential — the run's token
  authenticates the *host* clone only (#683) — so on a private remote
  that is a mid-task git failure with no useful message; even on a public
  one every such blob is a network round trip in the agent's critical
  path. It is therefore opt-in — ``[sandbox] clone_filter = "blob:none"``
  — and applies only to :func:`clone_from_remote`, the remote path where
  lazy fetches can succeed at all (git ignores filters on local path
  clones anyway), and only sensibly on a public repository. A git too old
  to know ``--filter`` falls back to an unfiltered clone with a log line
  rather than failing the run.

Cloning a private remote (#683)
-------------------------------
:func:`clone_from_remote` takes the run's GitHub token — the same PAT or
App installation token the github sandbox delivers with — and hands it to
git through a one-shot ``credential.helper`` set in the *environment*
(``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_n``, git ≥ 2.31). The token never
appears on the command line, never lands in ``.git/config`` or the remote
URL, and is gone once the clone returns; any helper the host user has
configured is switched off for that one process so the clone can only ever
authenticate as the run. ``x-access-token`` is the username GitHub accepts
for both credential kinds.

Submodules (#692)
-----------------
A run clone is populated after it is cut (:func:`populate_submodules`),
never through ``--recurse-submodules``: git would clone every submodule
from its ``.gitmodules`` URL, which for a repository the host has checked
out is a needless network round trip — and on a private remote a failing
one. Each submodule is instead cloned from the host checkout's own copy
when that copy holds the recorded commit, falling back to the remote with
the run's token otherwise; the remote path is the only path a repository
without a host checkout has. Nested submodules populate the same way,
level by level. Populating happens only on a *fresh* clone: a resume
re-entering a clone must never ``submodule update`` over a commit the
agent moved a submodule to.

Delivery reads modes from ``git diff --raw`` rather than the filesystem,
so a submodule — a gitlink, mode ``160000``, which ``stat`` sees as a
plain directory — is delivered as the commit it now points at, not as the
deletion of the directory. What a run changed *inside* a submodule is not
delivered at all: the pull request is against the superproject, and the
submodule's own repository is not the one the run is for. Such changes
are named in the delivery (see ``notes`` on :func:`changes_since`) rather
than silently dropped.

Git LFS (#693)
--------------
Every clone runs with ``GIT_LFS_SKIP_SMUDGE=1``: a host whose git has the
LFS filters installed would otherwise try to download every LFS object
during the checkout — from the host checkout's path, which serves none —
and a host without them writes pointer files anyway. The clone therefore
always starts as pointers, and :func:`populate_lfs` turns them into the
objects on purpose: the LFS filters are configured in the clone itself
(``git lfs install --local``, so ``git status`` in the sandbox compares
cleaned content and an untouched asset is not "modified"), the objects the
host checkout already has are hard-linked out of its store, and whatever
is still missing is fetched from the repository's LFS endpoint with the
run's token through the same one-shot helper the clone uses. That needs
``git-lfs`` on the host, and a repository that uses LFS fails to provision
without it — said with the package name — rather than starting a run on
pointer files. ``[sandbox] clone_lfs = false`` is the deliberate way to run
on pointers.

Delivery does not push LFS objects. A file the run added or changed under
an ``filter=lfs`` attribute is left out of the pull request and named in
its **Not delivered** line (:func:`lfs_tracked`): committing the bytes as a
plain blob is exactly the mistake such repositories forbid.

Tags (#694)
-----------
A ``--no-tags`` clone has no tags for a build that derives its version from
them — ``setuptools_scm``, ``hatch-vcs``, ``GitVersion``, a Makefile's
``git describe`` — and such a build fails or, worse, quietly reports
``0.0.0``. :func:`fetch_tags` brings the repository's tags into a fresh
clone when :func:`sbxloop.toolchains.tag_version_markers` finds such a
marker in the workspace's manifests (or ``[sandbox] fetch_tags = "always"``
says so): from the host checkout when it has tags — its tags are consistent
with the clone's history and cost no network — else ``git fetch --tags
origin`` under the run's credential. A tag that points off the branch's
history brings its objects along; that is the price of a correct
``git describe`` and still far short of a full clone.
"""

from __future__ import annotations

import contextlib
import os
import re
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


GITLINK_MODE = "160000"
SYMLINK_MODE = "120000"
EXECUTABLE_MODE = "100755"
REGULAR_MODE = "100644"
_NULL_SHA = "0" * 40


def tree_mode(full: Path) -> str:
    """The git tree mode ``git add`` would record for a path on disk:
    ``120000`` for a symlink (whatever it points at, or whether it resolves),
    ``100755`` for an executable, ``100644`` otherwise. Shared by the
    git-diff and snapshot delivery plans (#695) so neither flattens what
    the other keeps."""
    if full.is_symlink():
        return SYMLINK_MODE
    return EXECUTABLE_MODE if full.stat().st_mode & 0o111 else REGULAR_MODE


def blob_content(full: Path, mode: str) -> bytes:
    """What the blob for ``full`` uploads: a symlink's target string (git
    stores the link, not what it points at), a file's bytes otherwise."""
    return str(full.readlink()).encode() if mode == SYMLINK_MODE else full.read_bytes()


@dataclass(frozen=True)
class WorkspaceChange:
    """One path the run changed relative to its base commit. ``mode`` is the
    git tree mode to commit (``100644``/``100755``/``120000``, or
    ``160000`` for a submodule gitlink); empty for a deleted file. ``sha``
    is set only for a gitlink: the submodule commit the path now points at
    (a gitlink has no content to upload — the sha *is* the entry)."""

    path: str
    status: ChangeStatus
    mode: str
    sha: str = ""

    @property
    def is_gitlink(self) -> bool:
        return self.mode == GITLINK_MODE


@dataclass(frozen=True)
class Submodule:
    """One ``.gitmodules`` entry: its config ``name``, the ``path`` it is
    checked out at (relative, POSIX) and the ``url`` it is fetched from as
    written — possibly relative to the superproject's own remote."""

    name: str
    path: str
    url: str


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


def is_tracked(repo_root: Path, path: Path) -> bool | None:
    """Whether ``path`` (inside the checkout at ``repo_root``) is in git's
    index — a file the repository carries, as opposed to one the operator
    dropped into the tree (``sbxloop init``, an ignored ``sbxloop.toml``).

    None when git is unavailable or the probe fails, so the caller decides
    what "could not tell" means for it. The checkout may be one a sandbox
    agent has written to, so its config is untrusted: ``core.fsmonitor``
    (a hook git would run) is forced off on the command line, and inherited
    ``GIT_*`` variables are dropped so they cannot point git elsewhere.
    ``ls-files`` never executes anything else.
    """
    git = find_git()
    if git is None:
        return None
    try:
        relative = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        proc = subprocess.run(  # nosec B603 - list argv, git binary, no shell
            [git, "-c", "core.fsmonitor=false", "ls-files", "--error-unmatch", "--", str(relative)],
            cwd=repo_root,
            env=env,
            capture_output=True,
            check=False,
        )
    except OSError:
        log.debug("git.is_tracked_probe_failed", root=str(repo_root), exc_info=True)
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    log.debug(
        "git.is_tracked_probe_failed",
        root=str(repo_root),
        rc=proc.returncode,
        stderr=proc.stderr.decode("utf-8", "replace")[-400:],
    )
    return None


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
        with Repo.clone_from(
            str(source), str(target), env=_local_clone_env(), multi_options=list(CLONE_OPTIONS)
        ) as clone:
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
    token: str | None = None,
) -> str:
    """Clone a *remote* URL into ``target`` on a fresh ``branch``; return sha.

    The no-local-checkout path of per-repo workspace resolution: a repo
    entry configured without a workspace has no host tree to clone, so the
    run's tree comes from the remote itself. The host holds no git
    credential of its own (#46): the clone runs with terminal and
    credential-helper prompting disabled and fails loudly rather than
    hanging on an auth prompt. ``token`` — the run's GitHub credential —
    authenticates this one clone through an environment-scoped helper (see
    the module docstring, #683); without it only a public remote can work.

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
    env = _clone_env(token)
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


# The environment variable the one-shot credential helper reads the run's
# token from. It exists only in the clone's process environment.
CLONE_TOKEN_ENV = "SBXLOOP_GIT_TOKEN"  # nosec B105 - env var name, not a secret
_CLONE_CREDENTIAL_HELPER = (
    '!f() { echo username=x-access-token; echo "password=$' + CLONE_TOKEN_ENV + '"; }; f'
)


# Git LFS objects are never fetched by a clone's checkout (#693): the host
# populates them afterwards, deliberately — see the module docstring.
_SKIP_LFS_SMUDGE = {"GIT_LFS_SKIP_SMUDGE": "1"}


def _local_clone_env() -> dict[str, str]:
    """The environment a clone of a host checkout runs under."""
    return dict(_SKIP_LFS_SMUDGE)


def _clone_env(token: str | None) -> dict[str, str]:
    """The environment a remote clone runs under.

    Prompting is off in every form so a missing or rejected credential
    fails the clone instead of hanging. With ``token`` git additionally
    gets two config entries through ``GIT_CONFIG_*`` — an empty
    ``credential.helper`` that clears whatever helpers the host user has
    (a keychain must not answer for the run), then the one-shot helper
    that answers with the token from :data:`CLONE_TOKEN_ENV`. Neither the
    helper nor the token touches argv, ``.git/config`` or the URL.
    """
    env = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GCM_INTERACTIVE": "never",
        **_SKIP_LFS_SMUDGE,
    }
    if token:
        env.update(
            {
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "",
                "GIT_CONFIG_KEY_1": "credential.helper",
                "GIT_CONFIG_VALUE_1": _CLONE_CREDENTIAL_HELPER,
                CLONE_TOKEN_ENV: token,
            }
        )
    return env


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
        with Repo.clone_from(
            str(source), str(target), env=_local_clone_env(), multi_options=list(CLONE_OPTIONS)
        ) as clone:
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


_SUBMODULE_CONFIG_RE = re.compile(r"^submodule\.(.+)\.(path|url) (.*)$")


def list_submodules(repo_path: Path) -> list[Submodule]:
    """The submodules ``repo_path``'s ``.gitmodules`` declares, in file
    order; empty when there is no such file. A checkout is not needed —
    the file is read with ``git config -f`` — so this also answers for a
    workspace whose submodules were never populated."""
    modules = repo_path / ".gitmodules"
    if not modules.is_file():
        return []
    try:
        with Repo(repo_path) as repo:
            listing = repo.git.config(
                "-f", str(modules), "--get-regexp", r"^submodule\..*\.(path|url)$"
            )
    except GitCommandError as exc:
        if exc.status == 1:
            return []  # no matching keys
        raise ProvisionError(f"reading {modules} failed: {_describe(exc)}") from exc
    except (InvalidGitRepositoryError, NoSuchPathError, ValueError) as exc:
        raise ProvisionError(f"reading {modules} failed: {exc}") from exc
    found: dict[str, dict[str, str]] = {}
    for line in listing.splitlines():
        match = _SUBMODULE_CONFIG_RE.match(line)
        if match:
            name, key, value = match.groups()
            found.setdefault(name, {})[key] = value.strip()
    return [
        Submodule(name=name, path=entry["path"], url=entry.get("url", ""))
        for name, entry in found.items()
        if entry.get("path")
    ]


def populate_submodules(
    clone: Path, *, source: Path | None, token: str | None
) -> list[tuple[str, str]]:
    """Check out every submodule of a freshly cut run ``clone`` (#692),
    nested ones included; returns ``(path, how)`` per submodule populated,
    ``how`` being ``"local"`` or ``"remote"``.

    With ``source`` — the host checkout the clone was cut from — each
    submodule is first cloned from the source's own populated copy of it,
    which costs no network and needs no credential. That copy is usable
    only when it holds the commit the superproject records; when it does
    not (the host checkout's submodule is stale or was never initialised),
    or is absent, the submodule comes from its ``.gitmodules`` URL, with the
    run's ``token`` answering a credential challenge exactly as the
    superproject's remote clone does. Either way the submodule's ``origin``
    ends up at the ``.gitmodules`` URL (``git submodule sync``), so nothing
    in the clone points back at a host path.

    Only for a fresh clone: a resumed run's clone must keep whatever commit
    the agent moved a submodule to. ``ProvisionError`` names the submodule
    and its URL when neither route can populate it — a run that started on
    an empty directory where a dependency should be would fail later and
    worse.
    """
    populated: list[tuple[str, str]] = []
    for sub in list_submodules(clone):
        if not _is_gitlink_in_index(clone, sub.path):
            # A ``.gitmodules`` entry whose gitlink was removed from the tree
            # (a half-finished ``git rm``): nothing to check out, and
            # ``submodule update`` would refuse the pathspec.
            log.info(
                "workspace.submodule_not_in_tree",
                submodule=sub.path,
                detail=f".gitmodules names {sub.path} but the tree has no gitlink there",
            )
            continue
        local = source / sub.path if source is not None else None
        how = "remote"
        if local is not None and (local / ".git").exists():
            try:
                _submodule_update(clone, sub, local_source=local)
            except GitCommandError as exc:
                log.info(
                    "workspace.submodule_local_source_unusable",
                    submodule=sub.path,
                    source=str(local),
                    detail=_describe(exc),
                    fallback=f"cloning it from {public_remote_url(sub.url)}",
                )
                _discard_half_populated(clone, sub.path)
            else:
                how = "local"
        if how == "remote":
            try:
                _submodule_update(clone, sub, token=token)
            except GitCommandError as exc:
                raise ProvisionError(
                    f"populating submodule {sub.path} of {clone} from "
                    f"{public_remote_url(sub.url)} failed: {_describe(exc)}. The run's "
                    "GitHub credential must be able to read that repository too; "
                    "set [sandbox] clone_submodules = false to run without submodules"
                ) from exc
        populated.append((sub.path, how))
        # A submodule's own submodules: same routes, one level down.
        nested = populate_submodules(
            clone / sub.path, source=local if how == "local" else None, token=token
        )
        populated.extend((f"{sub.path}/{path}", nested_how) for path, nested_how in nested)
    return populated


def _is_gitlink_in_index(clone: Path, path: str) -> bool:
    entry: str = Repo(clone).git.ls_files("--stage", "--", path)
    return entry.startswith(f"{GITLINK_MODE} ")


def _submodule_update(
    clone: Path, sub: Submodule, *, local_source: Path | None = None, token: str | None = None
) -> None:
    """``git submodule update --init`` for one submodule. With
    ``local_source`` the clone reads that path instead of the ``.gitmodules``
    URL — an override given on the command line so it is never written to
    the clone's config — and ``submodule sync`` then points the submodule's
    ``origin`` at the URL ``.gitmodules`` names, as a remote populate would
    have. Without it the remote is used, under the run's credential."""
    with Repo(clone) as repo:
        if local_source is not None:
            repo.git(
                c=[
                    f"submodule.{sub.name}.url={local_source}",
                    "protocol.file.allow=always",
                ]
            ).submodule("update", "--init", "--", sub.path, env=_clone_env(None))
            # `init` under the override wrote no URL to the clone's config
            # (the override already answered), and `sync` only touches a
            # registered submodule: register it now, from .gitmodules, then
            # sync so the submodule's origin follows.
            repo.git.submodule("init", "--", sub.path)
            repo.git.submodule("sync", "--", sub.path)
        else:
            repo.git.submodule("update", "--init", "--", sub.path, env=_clone_env(token))


def _discard_half_populated(clone: Path, path: str) -> None:
    """Undo a failed local populate so the remote attempt starts clean: the
    submodule's working tree and the ``.git/modules`` store git made for
    it. The superproject's own config entry is left — ``submodule update
    --init`` rewrites it."""
    with contextlib.suppress(OSError):
        shutil.rmtree(clone / path)
        (clone / path).mkdir(parents=True)
    with Repo(clone) as repo:
        name = next((s.name for s in list_submodules(clone) if s.path == path), path)
        store = Path(repo.git_dir) / "modules" / name
        shutil.rmtree(store, ignore_errors=True)
        with contextlib.suppress(GitCommandError):
            repo.git.config("--unset", f"submodule.{name}.url")


def submodule_hosts(repo_path: Path) -> list[str]:
    """The hosts ``repo_path``'s submodules are fetched from, for the run's
    egress allow list (#692): the agent may need to fetch inside a
    submodule (``git submodule update`` after a bump, a ``git fetch`` to
    look at upstream). Scheme URLs and scp-style ssh URLs contribute their
    host; a relative URL (``../lib.git``) resolves against the checkout's
    ``origin`` and so contributes *its* host; a local path contributes
    nothing. Deduped, first occurrence winning; empty when there is no
    ``.gitmodules`` or it cannot be read."""
    try:
        subs = list_submodules(repo_path)
    except ProvisionError:
        return []
    origin = origin_url(repo_path)
    hosts: list[str] = []
    for sub in subs:
        url = sub.url
        if url.startswith(("./", "../")):
            if origin is None:
                continue
            url = origin
        host = url_host(url)
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def url_host(url: str) -> str | None:
    """The host of a git URL — scheme (``https://user@host:443/o/r``) or
    scp-style (``git@host:o/r``) — or None for a local path."""
    _scheme, sep, rest = url.partition("://")
    if sep:
        authority = rest.partition("/")[0]
    else:
        userinfo, at, remainder = url.rpartition("@")
        if not at or "/" in userinfo or ":" not in remainder:
            return None  # a local path, or something no fetch would resolve
        authority = remainder.partition(":")[0]
    _userinfo, _at, hostport = authority.rpartition("@")
    if hostport.startswith("["):  # bracketed IPv6 literal
        return hostport[1:].partition("]")[0] or None
    return hostport.partition(":")[0] or None


# ---------------------------------------------------------------------------
# Git LFS (#693)


@dataclass(frozen=True)
class LfsPopulation:
    """What :func:`populate_lfs` did: LFS-tracked files in the checkout,
    how many objects came out of the host checkout's store, and how many
    were fetched from the repository's LFS endpoint."""

    files: int
    linked: int
    fetched: int


def lfs_version() -> str | None:
    """The host's ``git lfs version`` line, or None when git-lfs is not
    installed (git then does not know the subcommand)."""
    git = find_git()
    if git is None:
        return None
    try:
        proc = subprocess.run(  # nosec B603 - list argv, git binary, no shell
            [git, "lfs", "version"], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def lfs_endpoint(repo_url: str) -> str:
    """The Git LFS batch endpoint for a GitHub-style https clone URL:
    ``<url>.git/info/lfs`` — where git-lfs itself would look for a remote
    at that URL, given here explicitly because a clone cut from a host
    checkout has the host path as its origin."""
    url = repo_url.rstrip("/")
    if not url.endswith(".git"):
        url += ".git"
    return f"{url}/info/lfs"


def populate_lfs(
    clone: Path, *, source: Path | None, lfs_url: str | None, token: str | None
) -> LfsPopulation:
    """Turn the pointer files of a freshly cut run ``clone`` into their
    LFS objects (#693), and configure the clone so git treats them as LFS
    from then on.

    ``source`` is the host checkout the clone was cut from: every object
    its LFS store holds is hard-linked into the clone's (a copy when the
    two are on different filesystems), which is the whole population for a
    checkout that was itself pulled — no network, no credential, and
    nothing written to the source. What is still a pointer afterwards is
    fetched from ``lfs_url`` (see :func:`lfs_endpoint`) with ``token``
    answering the credential challenge exactly as a remote clone does.

    ``ProvisionError`` when git-lfs is not installed on the host, when
    objects are missing and there is no endpoint to fetch them from, or
    when the fetch fails or leaves pointers behind — each naming the fix. A
    run that started on pointer files would fail later and worse. Only for
    a fresh clone: a resumed clone keeps whatever the agent did.
    """
    if lfs_version() is None:
        raise ProvisionError(
            f"{clone} uses Git LFS (.gitattributes routes files through filter=lfs) but "
            "git-lfs is not installed on the host: install it (apt install git-lfs / "
            "brew install git-lfs) or set [sandbox] clone_lfs = false to run on the "
            "pointer files"
        )
    repo = Repo(clone)
    # The clean/smudge filters in the clone's own config — not the host's
    # global one, which the run must not depend on — and no hooks: the
    # pre-push hook pushes objects, and nothing pushes from a run clone.
    repo.git.lfs("install", "--local", "--skip-repo")
    pointers = _lfs_pointers(repo)
    if not pointers:
        return LfsPopulation(len(_lfs_files(repo)), 0, 0)
    linked = 0
    if source is not None:
        linked = _link_lfs_objects(source, clone, [oid for oid, _path in pointers])
        if linked:
            repo.git.lfs("checkout", env=_local_clone_env())
            pointers = _lfs_pointers(repo)
    fetched = 0
    if pointers:
        if lfs_url is None:
            raise ProvisionError(
                f"{len(pointers)} Git LFS object(s) of {clone} are not in the host checkout's "
                f"LFS store ({', '.join(path for _oid, path in pointers[:5])}"
                f"{', …' if len(pointers) > 5 else ''}) and the run has no GitHub repository "
                "to fetch them from: `git lfs pull` in the host checkout, or set "
                "[sandbox] clone_lfs = false to run on the pointer files"
            )
        try:
            repo.git(c=[f"lfs.url={lfs_url}"]).lfs("pull", env=_clone_env(token))
        except GitCommandError as exc:
            raise ProvisionError(
                f"fetching {len(pointers)} Git LFS object(s) for {clone} from {lfs_url} "
                f"failed: {_describe(exc)}. The run's GitHub credential must be able to "
                "read the repository's LFS store; set [sandbox] clone_lfs = false to run "
                "on the pointer files"
            ) from exc
        fetched = len(pointers)
        remaining = _lfs_pointers(repo)
        if remaining:
            raise ProvisionError(
                f"{len(remaining)} Git LFS object(s) of {clone} are still pointer files "
                f"after `git lfs pull` from {lfs_url}: "
                + ", ".join(path for _oid, path in remaining[:5])
            )
    return LfsPopulation(len(_lfs_files(repo)), linked, fetched)


def _lfs_files(repo: Repo) -> list[tuple[str, str, str]]:
    """``(oid, marker, path)`` for every LFS-tracked file in the checkout —
    the marker ``*`` when the object is checked out, ``-`` for a pointer."""
    out: str = repo.git.lfs("ls-files", "--long")
    files: list[tuple[str, str, str]] = []
    for line in out.splitlines():
        oid, _sp, rest = line.partition(" ")
        marker, _sp, path = rest.partition(" ")
        if oid and marker in ("*", "-") and path:
            files.append((oid, marker, path))
    return files


def _lfs_pointers(repo: Repo) -> list[tuple[str, str]]:
    return [(oid, path) for oid, marker, path in _lfs_files(repo) if marker == "-"]


def _lfs_store(repo_path: Path) -> Path:
    """The checkout's LFS object store (``lfs/objects`` under the common
    git dir — a linked worktree shares its main checkout's)."""
    common: str = Repo(repo_path).git.rev_parse("--git-common-dir")
    return (repo_path / common).resolve() / "lfs" / "objects"


def _link_lfs_objects(source: Path, clone: Path, oids: Sequence[str]) -> int:
    """Hard-link the objects ``source``'s store has among ``oids`` into the
    clone's store (copying where linking is refused); returns how many."""
    try:
        store = _lfs_store(source)
    except (GitCommandError, InvalidGitRepositoryError, NoSuchPathError):
        return 0
    target_store = clone / ".git" / "lfs" / "objects"
    linked = 0
    for oid in oids:
        obj = store / oid[:2] / oid[2:4] / oid
        if not obj.is_file():
            continue
        dest = target_store / oid[:2] / oid[2:4] / oid
        if dest.exists():
            linked += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(obj, dest)
        except OSError:
            shutil.copy2(obj, dest)
        linked += 1
    return linked


def lfs_tracked(repo_path: Path, paths: Sequence[str]) -> list[str]:
    """Those of ``paths`` the checkout's attributes route through Git LFS
    (``filter=lfs``), from ``git check-attr`` — the working tree's
    ``.gitattributes`` included, so a pattern the run itself added counts."""
    if not paths:
        return []
    out: str = Repo(repo_path).git.check_attr("-z", "filter", "--", *paths)
    fields = out.split("\0")
    tracked: list[str] = []
    for i in range(0, len(fields) - 2, 3):
        path, _attr, value = fields[i], fields[i + 1], fields[i + 2]
        if value == "lfs" and path not in tracked:
            tracked.append(path)
    return tracked


# ---------------------------------------------------------------------------
# Tags (#694)


@dataclass(frozen=True)
class TagFetch:
    """What :func:`fetch_tags` did: how many tags the clone holds now and
    where they came from — ``local`` (the host checkout), ``remote`` (the
    clone's origin under the run's credential)."""

    tags: int
    source: str


def fetch_tags(clone: Path, *, source: Path | None, token: str | None) -> TagFetch:
    """Give a ``--no-tags`` run ``clone`` the repository's tags (#694).

    Local first: when ``source`` — the host checkout the clone was cut
    from — has tags, they are fetched from it (no network, no credential;
    its tags are consistent with the history the clone has). Otherwise
    from the clone's ``origin``, which :func:`clone_for_run` points at the
    upstream URL, with ``token`` answering the credential challenge as a
    remote clone does. Tags whose commits the single branch lacks bring
    those objects along; that is the price of a build that reads
    ``git describe``, and paid only by projects that do.

    A remote with no tags is not an error — a project on setuptools-scm
    before its first release is legitimately ``0.1.dev``. A fetch that
    fails is ``ProvisionError``: the build that needs the tags would fail
    later and worse, and ``[sandbox] fetch_tags = "never"`` is the opt-out.
    """
    try:
        with Repo(clone) as repo:
            if source is not None and tag_count(source):
                repo.git.fetch("--tags", str(source), env=_local_clone_env())
                return TagFetch(tag_count(clone), "local")
            repo.git.fetch("--tags", "origin", env=_clone_env(token))
            return TagFetch(tag_count(clone), "remote")
    except (GitCommandError, InvalidGitRepositoryError, NoSuchPathError, OSError) as exc:
        raise ProvisionError(
            f"fetching tags into {clone} failed: {_describe(exc)}. The workspace derives its "
            'version from git tags; set [sandbox] fetch_tags = "never" to run without them'
        ) from exc


def tag_count(repo_path: Path) -> int:
    """How many tags ``repo_path`` holds."""
    with Repo(repo_path) as repo:
        listing: str = repo.git.tag("--list")
    return len(listing.split()) if listing else 0


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

    ``root`` is made absolute first: a caller may hand in a relative
    artifact root (a test, an embedder), and git
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


def changes_since(
    repo_path: Path, base: str, *, notes: list[str] | None = None
) -> list[WorkspaceChange]:
    """What the working tree changed relative to ``base``: tracked paths
    from ``git diff`` (committed or not — the agent may do either) plus
    untracked-but-not-ignored files, sorted by path.

    Renames are reported as delete + add on purpose: the consumer builds a
    git tree, which has no rename concept. Modes come from the working tree
    (what ``git add`` itself would record) so an executable script keeps
    ``100755``; symlinks are ``120000`` with the link target as content.

    Submodules (#692) are told apart by the mode git reports (``160000``),
    never by looking at the filesystem, where a gitlink is just a directory.
    A moved gitlink comes back with the commit it now points at — read from
    the submodule's HEAD when the agent moved it without staging, the one
    case where git's own diff shows no new sha. What cannot be delivered is
    *skipped*, and each skip is explained in ``notes`` when the caller
    passes a list: work inside a submodule (its own tree is not the
    superproject's), and a gitlink pointing at a commit the submodule's
    remote does not have (a pointer nobody else could resolve).
    """
    changes: dict[str, WorkspaceChange] = {}
    skipped = notes if notes is not None else []
    try:
        with Repo(repo_path) as repo:
            listing = repo.git.diff("--raw", "--no-renames", "--abbrev=40", "-z", base)
            for meta, path in _pairs(listing):
                change = _change_from_raw(repo_path, path, meta, skipped)
                if change is not None:
                    changes[path] = change
            untracked = repo.git.ls_files("--others", "--exclude-standard", "-z")
            for path in untracked.split("\0"):
                if path:
                    changes[path] = _describe_change(repo_path, path, "A")
    except (InvalidGitRepositoryError, NoSuchPathError, GitCommandError) as exc:
        raise DeliveryError(f"git diff failed in {repo_path}: {exc}") from exc
    return [changes[path] for path in sorted(changes)]


def _change_from_raw(
    repo_path: Path, path: str, meta: str, notes: list[str]
) -> WorkspaceChange | None:
    """One ``git diff --raw`` record — ``:<old mode> <new mode> <old sha>
    <new sha> <status>`` — as a change, or None for a submodule change that
    is not deliverable (explained in ``notes``)."""
    fields = meta.lstrip(":").split()
    if len(fields) < 5:
        raise DeliveryError(f"unexpected git diff --raw record in {repo_path}: {meta!r}")
    old_mode, new_mode, old_sha, new_sha, status = fields[:5]
    if GITLINK_MODE not in (old_mode, new_mode):
        return _describe_change(repo_path, path, status)
    if new_mode != GITLINK_MODE:
        # The submodule is gone: removed outright (the tree drops the path)
        # or replaced by a file, which git reports as one type-change record
        # — the file is then an ordinary blob at the same path.
        if new_mode == "000000":
            return WorkspaceChange(path=path, status="deleted", mode=GITLINK_MODE)
        return _describe_change(repo_path, path, status)
    sub = repo_path / path
    dirty = submodule_is_dirty(sub)
    if new_sha == _NULL_SHA:
        # Moved in the working tree without `git add`: the diff shows no
        # commit, the submodule's HEAD is where it points now.
        new_sha = head_commit(sub) or ""
    if not new_sha or new_sha == old_sha:
        # Nothing to point the superproject at that it does not already
        # point at: the only change is inside the submodule.
        if dirty or not new_sha:
            notes.append(f"changes inside submodule `{path}` are not delivered")
        return None
    if dirty:
        notes.append(f"changes inside submodule `{path}` are not delivered")
    if not _commit_is_published(sub, new_sha):
        notes.append(
            f"submodule `{path}` points at commit {new_sha[:12]}, which its remote "
            "does not have; that gitlink is not delivered (push the submodule first)"
        )
        return None
    change_status: ChangeStatus = "added" if status.startswith("A") else "modified"
    return WorkspaceChange(path=path, status=change_status, mode=GITLINK_MODE, sha=new_sha)


def submodule_is_dirty(sub: Path) -> bool:
    """Whether a populated submodule's working tree has changes of its own
    (tracked edits or untracked files). An unpopulated or unreadable
    submodule is not dirty: there is nothing in it to lose."""
    try:
        with Repo(sub) as repo:
            return bool(repo.git.status("--porcelain").strip())
    except (InvalidGitRepositoryError, NoSuchPathError, GitCommandError, ValueError):
        return False


def _commit_is_published(sub: Path, sha: str) -> bool:
    """Whether ``sha`` is reachable from any remote-tracking ref or tag of
    the submodule — the evidence that it exists somewhere other than this
    clone. A submodule whose repository cannot be read is taken at its
    word: the gitlink is delivered rather than second-guessed."""
    try:
        with Repo(sub) as repo:
            listing = repo.git.for_each_ref(
                f"--contains={sha}", "refs/remotes", "refs/tags", "--format=%(refname)"
            )
            return bool(listing.strip())
    except (InvalidGitRepositoryError, NoSuchPathError, GitCommandError, ValueError):
        return True


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
    """``git_status`` is git's name-status letter (A/M/D/T...). For blobs
    only — a gitlink never reaches this (see :func:`_change_from_raw`)."""
    if git_status.startswith("D"):
        return WorkspaceChange(path=path, status="deleted", mode="")
    status: ChangeStatus = "added" if git_status.startswith("A") else "modified"
    full = repo_path / path
    if not full.is_symlink() and not full.is_file():
        # Gone from disk but still in the index (rm without git rm): git
        # diff against a commit still lists it; the tree must drop it.
        return WorkspaceChange(path=path, status="deleted", mode="")
    return WorkspaceChange(path=path, status=status, mode=tree_mode(full))


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
    if isinstance(stderr, str):
        # GitPython hands stderr over as "\n  stderr: '<text>'"; the last
        # non-empty line inside the quotes is the reason git gave.
        text = stderr.strip()
        if text.startswith("stderr: '") and text.endswith("'"):
            text = text[len("stderr: '") : -1]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            return lines[-1]
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


def exclude_from_git(root: Path, pattern: str) -> bool:
    """Add ``pattern`` to ``root``'s ``.git/info/exclude`` — the checkout's
    private ignore list, never committed — so a directory the loop keeps
    inside a workspace (the dependency cache, #766) is not something the
    agent can ``git add``. Idempotent; a no-op (False) when ``root`` is not
    a checkout with a ``.git`` directory of its own."""
    info = root / ".git" / "info"
    if not (root / ".git").is_dir():
        return False
    exclude = info / "exclude"
    text = exclude.read_text() if exclude.is_file() else ""
    if pattern in text.splitlines():
        return True
    info.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as fh:
        if text and not text.endswith("\n"):
            fh.write("\n")
        fh.write(pattern + "\n")
    return True
