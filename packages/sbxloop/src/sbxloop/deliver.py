"""Deliver a completed run's artifacts as a GitHub pull request.

Everything goes through the :class:`GithubOps` facade, i.e. runs as
``github.op`` jobs inside the github-ops sandbox — the only environment
holding ``GH_TOKEN``; the credential split is preserved. Files are committed
atomically through the git data API (blobs → tree → commit → ref) rather
than per-file contents PUTs: one commit regardless of file count, and
base64 blobs carry binary content.

Two ways of building that tree (#248):

- **git diff** — when the workspace is a git checkout (the per-run clone
  from ``hostgit.clone_for_run``, or an in-place checkout), only what the
  run changed relative to its base commit is committed: added/modified
  files as blobs with their real mode (``100755`` kept), deletions as
  ``sha: null`` tree entries. A snapshot overlay could never delete or
  rename a file and flipped every executable to ``100644`` — silently
  wrong PRs against an existing repository, the one failure a reviewer
  may not notice.
- **snapshot** — every kept file layered onto the base tree; the right
  answer for greenfield workspaces (no git history to diff against), and
  the fallback when a checkout carries no usable base commit.

Scaffold status: this is a real, unit-tested code path (stubbed GithubOps),
but per the project pattern — unverified external behaviors get a seam and
an e2e check, never a confident default — it is NOT field-proven until the
real-sbx e2e workflow exercises it. Known e2e-validation items (each marker
names the open issue or e2e step that will retire it — the audit in
``tests/unit/test_e2e_markers.py`` fails on a marker without one, #226):

- TODO(e2e): branch-name collisions — handled in code (a 422 on the refs
  POST force-updates the existing ``sbxloop/<run>`` branch, and a 422 on
  the PR create reuses the open PR for that head), because ``sbxloop
  deliver <run>`` re-runs delivery against a repository a prior partial
  attempt may already have touched (#223) — but the 422 shapes are taken
  from the GitHub API docs, not yet observed in the field
- TODO(e2e): ``sha: null`` deletions and ``100755``/``120000`` modes in
  the git-diff tree against real GitHub (documented Git Data API
  behavior, not yet exercised end to end)
- TODO(e2e): empty repositories — handled in code (contents-API bootstrap
  commit when the base ref is missing) but not yet exercised against real
  GitHub
- TODO(e2e): repository creation (`ensure_repository` with create=True;
  org-owned targets in particular)

Blob creation is batched (#66): the whole file manifest ships to the github
sandbox as ``blobs.create_many`` jobs — chunked only by payload size, so a
delivery issues O(total bytes / chunk cap) worker jobs, not one per file.
The worker performs the per-file blob POSTs in-sandbox and streams
``gh.op_progress`` events so long deliveries stay visible in the TUI.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sbxloop import hostgit
from sbxloop.engine.model import DEFAULT_ARTIFACT_EXCLUDES, exclusion_hit, scan_artifacts
from sbxloop.errors import DeliveryError, GithubOpsError
from sbxloop.gh.ops import GithubOps, PrRef
from sbxloop.ids import branch_name as branch_name  # re-export; shared with hostgit isolation

FILE_MODE = "100644"
BODY_FILE_LIST_CAP = 50
TITLE_CLIP = 72
# Cap on base64 payload bytes per blobs.create_many job. The manifest rides
# inside the job JSON (staged into the sandbox with one `sbx cp`), so this
# bounds the staged file size; a single oversized file still gets its own
# chunk rather than failing.
BLOB_BATCH_MAX_B64_BYTES = 4 * 1024 * 1024
STATUS_MARKER = {"added": "A", "modified": "M", "deleted": "D"}


def _is_ref_collision(exc: GithubOpsError) -> bool:
    """Whether a refs POST failed because the branch already exists —
    GitHub's documented answer is HTTP 422 "Reference already exists"."""
    text = str(exc)
    return "HTTP 422" in text and "already exists" in text.lower()


def _is_pr_collision(exc: GithubOpsError) -> bool:
    """Whether a PR create failed because that head already has an open PR
    (HTTP 422 "A pull request already exists for owner:branch")."""
    text = str(exc)
    return "HTTP 422" in text and "pull request already exists" in text.lower()


def ensure_repository(
    ops: GithubOps, repo: str, *, create: bool = False, public: bool = False
) -> bool:
    """Probe the delivery repository; create it when explicitly allowed.

    Returns True when the repository was created. Creation is opt-in
    (``create``) rather than automatic on 404 so a typo'd ``--repo`` fails
    loudly instead of silently delivering into a brand-new repository. New
    repositories are private unless ``public``, and ``auto_init`` so the
    default branch exists and the normal PR delivery path applies unchanged.

    The probe asks the worker for "missing" as data rather than catching a
    404: three field runs showed the expected miss painted as a red error
    panel in the transcript before this code got to say it was fine (#222).
    """
    if ops.repo_lookup(repo) is not None:
        return False
    if not create:
        raise DeliveryError(
            f"repository {repo} does not exist — create it first, or pass "
            "--create-repo (config: [github] create_repo = true) to have "
            "sbxloop create it"
        )
    owner, name = repo.split("/", 1)
    user = ops.raw("GET", "/user")
    login = str(user.get("login", "")) if isinstance(user, dict) else ""
    body = {"name": name, "private": not public, "auto_init": True}
    if login.lower() == owner.lower():
        ops.raw("POST", "/user/repos", body)
    else:
        ops.raw("POST", f"/orgs/{owner}/repos", body)
    return True


@dataclass
class DeliveryPlan:
    """What one delivery commits.

    ``uploads`` (relative path -> content) become blobs; ``entries`` are the
    tree entries without blob shas (deletions already carry ``sha: None``,
    which is how the Git Data API removes a path from a base tree).
    ``lines`` is the PR-body listing and ``note`` says how the plan was
    derived, so a reviewer can tell a diff-based PR from a snapshot.
    """

    mode: str  # "git-diff" | "snapshot"
    entries: list[dict[str, Any]]
    uploads: dict[str, bytes]
    lines: list[str]
    excluded_note: str | None = None
    note: str | None = None

    @property
    def count(self) -> int:
        return len(self.entries)


def deliver_workspace(
    ops: GithubOps,
    repo: str,
    *,
    run_id: str,
    outcome: str,
    source_dir: Path,
    base: str | None = None,
    draft: bool = False,
    exclude: Sequence[str] = DEFAULT_ARTIFACT_EXCLUDES,
) -> PrRef:
    """Publish source_dir as one commit on a new branch and open a PR."""
    plan: DeliveryPlan | None = None
    if not _is_checkout_root(source_dir):
        # Fail before any API call when there is nothing to send; the
        # git-diff plan can only decide that once the base commit is known.
        plan = _plan_snapshot(source_dir, exclude)

    if base is None:
        base = str(ops.repo_get(repo).get("default_branch") or "main")
    base_sha = _base_commit_sha(ops, repo, base)
    if base_sha is None:
        # An existing-but-empty repository has a default branch name and no
        # ref behind it (GitHub answers 409; an absent branch on a non-empty
        # repo — unusual explicit `base` — answers 404; the worker folds
        # both into "missing"). Bootstrap an initial commit so the normal PR
        # path (branch off base, open a PR against it) applies unchanged.
        _bootstrap_empty_repo(ops, repo, base, run_id=run_id, outcome=outcome)
        base_sha = _base_commit_sha(ops, repo, base)
        if base_sha is None:
            raise DeliveryError(f"base branch {base!r} of {repo} still missing after bootstrap")
    base_tree = _commit_tree_sha(ops, repo, base_sha)

    if plan is None:
        plan = _plan_git_diff(source_dir, base_sha, exclude)
    if plan is None:
        # A checkout with nothing to diff against (the agent git-init-ed
        # the workspace itself): the snapshot is still the right delivery,
        # said out loud in the PR body rather than silently.
        plan = _plan_snapshot(source_dir, exclude)
        plan.note = "delivered as a workspace snapshot: no base commit to diff against"

    shas = _create_blobs(ops, repo, plan.uploads)
    entries = [
        {**entry, "sha": shas[entry["path"]]} if entry["path"] in shas else entry
        for entry in plan.entries
    ]
    tree = _sha(
        ops.raw("POST", f"/repos/{repo}/git/trees", {"base_tree": base_tree, "tree": entries}),
        f"tree for {repo}",
    )
    commit = _sha(
        ops.raw(
            "POST",
            f"/repos/{repo}/git/commits",
            {
                "message": f"sbxloop run {run_id}: deliver artifacts\n\nOutcome: {outcome}",
                "tree": tree,
                "parents": [base_sha],
            },
        ),
        f"commit for {repo}",
    )
    branch = branch_name(run_id)
    _point_branch(ops, repo, branch, commit)

    try:
        return ops.pr_create(
            repo,
            base=base,
            head=branch,
            title=_title(outcome),
            body=_body(run_id, outcome, plan),
            draft=draft,
        )
    except GithubOpsError as exc:
        if not _is_pr_collision(exc):
            raise
        existing = _find_open_pr(ops, repo, branch)
        if existing is None:
            raise
        return existing


def _point_branch(ops: GithubOps, repo: str, branch: str, commit: str) -> None:
    """Create the delivery branch at ``commit`` — or, when a prior attempt
    for the same run already created it, force-move it there.

    The branch name is a pure function of the run id, so a re-delivery
    (``sbxloop deliver <run>`` after a failed first attempt, #223) collides
    with whatever the earlier attempt left. Force-updating rather than
    suffixing keeps one branch (and one PR) per run: the newer commit is
    built from the same artifacts and supersedes the old one.
    """
    try:
        ops.raw("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
    except GithubOpsError as exc:
        if not _is_ref_collision(exc):
            raise
        ops.raw("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": commit, "force": True})


def _find_open_pr(ops: GithubOps, repo: str, branch: str) -> PrRef | None:
    """The open PR whose head is ``branch`` — a re-delivery's earlier PR,
    which the force-moved branch has just refreshed."""
    owner = repo.split("/", 1)[0]
    pulls = ops.raw("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{branch}")
    if not isinstance(pulls, list):
        return None
    for pull in pulls:
        if isinstance(pull, dict) and pull.get("number"):
            return PrRef(number=int(pull["number"]), url=str(pull.get("html_url", "")))
    return None


def _is_checkout_root(source_dir: Path) -> bool:
    """Whether source_dir is the working-tree root of a git checkout — the
    only shape the git-diff plan handles (a subtree of a checkout is never
    a run workspace: provisioning refuses it). Without a git binary there
    is no diff to take, so the snapshot path applies as before."""
    if hostgit.find_git() is None or not (source_dir / ".git").exists():
        return False
    root = hostgit.repo_toplevel(source_dir)
    return root is not None and root == source_dir.resolve()


def _plan_snapshot(source_dir: Path, exclude: Sequence[str]) -> DeliveryPlan:
    scan = scan_artifacts(source_dir, exclude)
    if not scan.files:
        raise DeliveryError(f"nothing to deliver: no files in {source_dir}")
    rel = [f.relative_to(source_dir).as_posix() for f in scan.files]
    return DeliveryPlan(
        mode="snapshot",
        entries=[{"path": path, "mode": FILE_MODE, "type": "blob"} for path in rel],
        uploads={path: file.read_bytes() for path, file in zip(rel, scan.files, strict=True)},
        lines=[f"- `{path}`" for path in rel],
        excluded_note=scan.excluded_note,
    )


def _plan_git_diff(source_dir: Path, base_sha: str, exclude: Sequence[str]) -> DeliveryPlan | None:
    """The run's changes as tree entries, or None when the checkout has no
    base commit to measure against (the caller falls back to a snapshot).

    The exclude denylist still applies: an agent that builds a ``.venv``
    inside an un-ignored checkout must not have it delivered any more than
    the snapshot path would.
    """
    diff_base = hostgit.resolve_diff_base(source_dir, base_sha)
    if diff_base is None:
        return None
    excluded: dict[str, int] = {}
    kept: list[hostgit.WorkspaceChange] = []
    for change in hostgit.changes_since(source_dir, diff_base):
        hit = exclusion_hit(change.path.split("/"), exclude)
        if hit is None:
            kept.append(change)
        else:
            excluded[hit] = excluded.get(hit, 0) + 1
    if not kept:
        raise DeliveryError(
            f"nothing to deliver: {source_dir} has no changes relative to {diff_base[:12]}"
        )
    entries: list[dict[str, Any]] = []
    uploads: dict[str, bytes] = {}
    for change in kept:
        if change.status == "deleted":
            entries.append({"path": change.path, "mode": FILE_MODE, "type": "blob", "sha": None})
            continue
        entries.append({"path": change.path, "mode": change.mode, "type": "blob"})
        full = source_dir / change.path
        uploads[change.path] = (
            str(full.readlink()).encode() if change.mode == "120000" else full.read_bytes()
        )
    excluded_note = (
        f"{sum(excluded.values())} file(s) excluded ({', '.join(sorted(excluded))})"
        if excluded
        else None
    )
    return DeliveryPlan(
        mode="git-diff",
        entries=entries,
        uploads=uploads,
        lines=[f"- {STATUS_MARKER[c.status]} `{c.path}`" for c in kept],
        excluded_note=excluded_note,
        note=f"delivered as the workspace's git diff against `{diff_base[:12]}`",
    )


def _bootstrap_empty_repo(
    ops: GithubOps, repo: str, base: str, *, run_id: str, outcome: str
) -> None:
    """Give an empty repository its initial commit on ``base``.

    The contents API is the one endpoint that works with no ref to build
    on — the PUT creates the branch and its first commit together. The
    README it writes is superseded by the delivery PR whenever the
    workspace ships its own.
    """
    readme = f"# {repo.split('/', 1)[1]}\n\nInitialized by sbxloop run {run_id}.\n"
    ops.raw(
        "PUT",
        f"/repos/{repo}/contents/README.md",
        {
            "message": f"sbxloop run {run_id}: initialize repository\n\nOutcome: {outcome}",
            "content": base64.b64encode(readme.encode()).decode(),
            "branch": base,
        },
    )


def _base_commit_sha(ops: GithubOps, repo: str, base: str) -> str | None:
    """The commit sha behind ``base``, or None when there is no such ref
    (missing branch or empty repository) — a state delivery bootstraps
    around, so it arrives as data, not as an exception."""
    return ops.ref_lookup(repo, f"heads/{base}")


def _commit_tree_sha(ops: GithubOps, repo: str, commit_sha: str) -> str:
    commit = ops.raw("GET", f"/repos/{repo}/git/commits/{commit_sha}")
    try:
        return str(commit["tree"]["sha"])
    except (TypeError, KeyError) as exc:
        raise DeliveryError(f"cannot read base commit {commit_sha} of {repo}") from exc


def _create_blobs(ops: GithubOps, repo: str, uploads: dict[str, bytes]) -> dict[str, str]:
    """Create all blobs via batched worker jobs; returns relative path -> sha."""
    shas: dict[str, str] = {}
    for chunk in _manifest_chunks(uploads):
        shas.update(ops.blobs_create_many(repo, chunk))
    missing = [path for path in uploads if path not in shas]
    if missing:
        raise DeliveryError(f"GitHub returned no blob sha for: {', '.join(missing[:5])}")
    return shas


def _manifest_chunks(uploads: dict[str, bytes]) -> list[list[dict[str, str]]]:
    """Split the file manifest into payload-size-capped job chunks."""
    chunks: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    current_bytes = 0
    for path, raw in uploads.items():
        content = base64.b64encode(raw).decode("ascii")
        if current and current_bytes + len(content) > BLOB_BATCH_MAX_B64_BYTES:
            chunks.append(current)
            current, current_bytes = [], 0
        current.append({"path": path, "content_b64": content})
        current_bytes += len(content)
    if current:
        chunks.append(current)
    return chunks


def _sha(response: Any, what: str) -> str:
    sha = response.get("sha") if isinstance(response, dict) else None
    if not sha:
        raise DeliveryError(f"GitHub returned no sha creating {what}: {response!r}")
    return str(sha)


def _title(outcome: str) -> str:
    title = f"sbxloop: {outcome}"
    return title if len(title) <= TITLE_CLIP else title[: TITLE_CLIP - 1] + "…"


def _body(run_id: str, outcome: str, plan: DeliveryPlan) -> str:
    listed = plan.lines[:BODY_FILE_LIST_CAP]
    if plan.count > BODY_FILE_LIST_CAP:
        listed.append(f"- … +{plan.count - BODY_FILE_LIST_CAP} more")
    heading = "Changes" if plan.mode == "git-diff" else "Files"
    body = (
        f"Artifacts produced by sbxloop run `{run_id}`.\n\n"
        f"**Outcome:** {outcome}\n\n"
        f"**{heading} ({plan.count}):**\n" + "\n".join(listed) + "\n"
    )
    if plan.excluded_note:
        body += f"\n**Not delivered:** {plan.excluded_note}\n"
    if plan.note:
        body += f"\n_{plan.note}_\n"
    return body
