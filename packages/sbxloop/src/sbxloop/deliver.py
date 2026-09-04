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

- TODO(e2e #223): branch-name collisions — handled in code (a 422 on the refs
  POST force-updates the existing ``sbxloop/<run>`` branch, and a 422 on
  the PR create reuses the open PR for that head), because ``sbxloop
  deliver <run>`` re-runs delivery against a repository a prior partial
  attempt may already have touched (#223) — but the 422 shapes are taken
  from the GitHub API docs, not yet observed in the field
- TODO(e2e #248): ``sha: null`` deletions and ``100755``/``120000`` modes in
  the git-diff tree against real GitHub (documented Git Data API
  behavior, not yet exercised end to end)
- TODO(e2e #256): empty repositories — handled in code (contents-API bootstrap
  commit when the base ref is missing) but not yet exercised against real
  GitHub
- TODO(e2e #256): repository creation (`ensure_repository` with create=True;
  org-owned targets in particular)

Blob creation is batched (#66): the whole file manifest ships to the github
sandbox as ``blobs.create_many`` jobs — chunked only by payload size, so a
delivery issues O(total bytes / chunk cap) worker jobs, not one per file.
The worker performs the per-file blob POSTs in-sandbox and streams
``gh.op_progress`` events so long deliveries stay visible in the TUI.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from sbxloop import hostgit
from sbxloop.engine.model import (
    DEFAULT_ARTIFACT_EXCLUDES,
    PR_BODY_FILE,
    exclusion_hit,
    scan_artifacts,
)
from sbxloop.errors import DeliveryError, GithubOpsError
from sbxloop.gh.ops import GithubOps, PrRef
from sbxloop.ids import branch_name as branch_name  # re-export; shared with hostgit isolation
from sbxloop.log import get_logger

log = get_logger(__name__)

FILE_MODE = "100644"
BODY_FILE_LIST_CAP = 50
# The places GitHub reads a pull request template from (#678), in the
# order it prefers them; the first that exists is the one a human opening
# a PR in the browser would be handed. A template *directory* holds
# alternatives GitHub only applies by query parameter, so it is not one.
PR_TEMPLATE_PATHS = (
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/pull_request_template.md",
    "PULL_REQUEST_TEMPLATE.md",
    "pull_request_template.md",
    "docs/PULL_REQUEST_TEMPLATE.md",
    "docs/pull_request_template.md",
)
PR_TEMPLATE_CAP = 16 * 1024
# What says a repository lints pull-request titles as conventional commits
# (#678): commitlint's config files, a `commitlint` key in package.json,
# or a workflow running one of the two common title actions.
_COMMITLINT_FILES = (
    "commitlint.config.js",
    "commitlint.config.cjs",
    "commitlint.config.mjs",
    "commitlint.config.ts",
    "commitlint.config.json",
    "commitlint.config.yaml",
    "commitlint.config.yml",
    ".commitlintrc",
    ".commitlintrc.json",
    ".commitlintrc.yaml",
    ".commitlintrc.yml",
    ".commitlintrc.js",
    ".commitlintrc.cjs",
    ".commitlintrc.ts",
)
_TITLE_ACTIONS = ("amannn/action-semantic-pull-request", "wagoid/commitlint-github-action")
# A conventional-commit subject: `type(scope)!: summary`, the type one of
# config-conventional's default `type-enum` — the set the lints enforce
# unless a repository extends it, so `sbxloop: …` is not one.
CONVENTIONAL_TYPES = (
    "build",
    "chore",
    "ci",
    "docs",
    "feat",
    "fix",
    "perf",
    "refactor",
    "revert",
    "style",
    "test",
)
CONVENTIONAL_TITLE = re.compile(r"^(" + "|".join(CONVENTIONAL_TYPES) + r")(\([^()]*\))?!?: \S")
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


def _is_ref_refusal(exc: GithubOpsError) -> bool:
    """Whether a refs POST was refused by the repository's rules rather
    than by an existing ref: a 422 that does not say "already exists"."""
    text = str(exc)
    is_422 = exc.http_status == 422 or "HTTP 422" in text
    return is_422 and "already exists" not in text.lower()


# GitHub's answer to a draft PR on a plan or instance without drafts —
# private repositories on GitHub Free, GHE instances predating the
# feature (#677). Matched case-insensitively; the wording is GitHub's.
_DRAFT_UNSUPPORTED = "draft pull requests are not supported"


def _is_draft_unsupported(exc: GithubOpsError) -> bool:
    """Whether a PR create was refused because the repository has no draft
    pull requests at all — a 422 saying so, which no lookup and no retry
    of the same request would change."""
    text = str(exc)
    is_422 = exc.http_status == 422 or "HTTP 422" in text
    return is_422 and _DRAFT_UNSUPPORTED in text.lower()


# A refs POST 422 that is not "already exists", by what GitHub's message
# says (#677): (needle in the lowercased message, the advice). The first
# match wins; a message none of these fit is quoted as it came — every
# 422 used to be "the branch name", which sent an operator whose base
# wants signed commits off to change `branch_prefix`.
_REF_REFUSALS: tuple[tuple[str, str], ...] = (
    (
        "signature",
        "the base requires signed commits; GitHub signs commits the loop creates through "
        "its API only when it authenticates as a GitHub App (GITHUB_APP_ID + private key)",
    ),
    (
        "signed",
        "the base requires signed commits; GitHub signs commits the loop creates through "
        "its API only when it authenticates as a GitHub App (GITHUB_APP_ID + private key)",
    ),
    (
        "locked",
        "the repository is locked (a migration in progress, or archived) — nothing the "
        "loop does changes that",
    ),
    (
        "archived",
        "the repository is archived — nothing the loop does changes that",
    ),
    (
        "branch name",
        "the repository's rules do not admit that branch name — set `[github] "
        "branch_prefix` (or the [[github.repos]] entry's) to a prefix its rulesets allow",
    ),
    (
        "creations being restricted",
        "the repository's rules do not admit that branch name — set `[github] "
        "branch_prefix` (or the [[github.repos]] entry's) to a prefix its rulesets allow",
    ),
)


def _explain_ref_refusal(exc: GithubOpsError) -> str:
    """The advice for a refs POST GitHub refused, from its own words; a
    message none of :data:`_REF_REFUSALS` fits gets no advice beyond the
    quote — a wrong knob named is worse than none."""
    text = str(exc).lower()
    for needle, advice in _REF_REFUSALS:
        if needle in text:
            return advice
    return "GitHub's refusal is quoted above; the repository's rules or state say why"


def _is_pr_collision(exc: GithubOpsError) -> bool:
    """Whether a PR create *may* have failed because that head already has
    an open PR. GitHub's answer is HTTP 422 "A pull request already exists
    for owner:branch" — but the `gh` transport used to hand back only
    "Validation Failed (HTTP 422)" (field run r8tzse1qa, #387), so the
    status alone is the test and the caller confirms with a lookup."""
    return exc.http_status == 422 or "HTTP 422" in str(exc)


class RepositoryProbe(NamedTuple):
    """What :func:`ensure_repository` learned about the delivery repository."""

    #: Whether this call created it.
    created: bool
    #: ``has_issues`` off the repository payload (#631): False when Issues
    #: are disabled — follow-ups then land as a PR comment, since
    #: ``POST /issues`` answers 410 Gone. None when the payload did not say.
    has_issues: bool | None
    #: The repository's ``html_url`` as GitHub reports it — the one link
    #: that is right on any GitHub host (#623). None when the payload did
    #: not carry it.
    url: str | None = None


def _html_url(data: object) -> str | None:
    if not isinstance(data, dict):
        return None
    value = data.get("html_url")
    return value if isinstance(value, str) and value else None


def _has_issues(data: object) -> bool | None:
    if not isinstance(data, dict):
        return None
    value = data.get("has_issues")
    return value if isinstance(value, bool) else None


def ensure_repository(
    ops: GithubOps, repo: str, *, create: bool = False, public: bool = False
) -> RepositoryProbe:
    """Probe the delivery repository; create it when explicitly allowed.

    ``created`` is True when this call made the repository. Creation is opt-in
    (``create``) rather than automatic on 404 so a typo'd ``--repo`` fails
    loudly instead of silently delivering into a brand-new repository. New
    repositories are private unless ``public``, and ``auto_init`` so the
    default branch exists and the normal PR delivery path applies unchanged.

    The probe asks the worker for "missing" as data rather than catching a
    404: three field runs showed the expected miss painted as a red error
    panel in the transcript before this code got to say it was fine (#222).
    """
    data = ops.repo_lookup(repo)
    if data is not None:
        return RepositoryProbe(created=False, has_issues=_has_issues(data), url=_html_url(data))
    if not create:
        raise DeliveryError(
            f"repository {repo} does not exist — create it first, or pass "
            "--create-repo (config: [github] create_repo = true) to have "
            "sbxloop create it"
        )
    owner, name = repo.split("/", 1)
    try:
        user = ops.raw("GET", "/user")
    except GithubOpsError as exc:
        # ``GET /user`` needs a user token — a GitHub App installation
        # token gets 403 "Resource not accessible by integration" (#581).
        # Without a readable login, ``POST /user/repos`` (same constraint)
        # could not work either, so the org route is the only one left —
        # and for an org-owned target it is also the right one.
        log.warning(
            "deliver.identity_unavailable",
            repo=repo,
            http_status=exc.http_status,
            error=str(exc),
            hint="creating via the organization route",
        )
        user = {}
    login = str(user.get("login", "")) if isinstance(user, dict) else ""
    body = {"name": name, "private": not public, "auto_init": True}
    if login.lower() == owner.lower():
        made = ops.raw("POST", "/user/repos", body)
    else:
        made = ops.raw("POST", f"/orgs/{owner}/repos", body)
    return RepositoryProbe(created=True, has_issues=_has_issues(made), url=_html_url(made))


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
    branch: str | None = None,
    closes: int | None = None,
    pr_number: int | None = None,
    round_no: int | None = None,
    parent: str | None = None,
    title: str | None = None,
    commit_message: str | None = None,
    authored_body: str | None = None,
    verification: str | None = None,
) -> PrRef:
    """Publish source_dir as one commit on a branch and open (or update) a PR.

    ``authored_body`` is the description the agent wrote (``.sbxloop/pr-body``
    under the workspace, #678): it becomes the PR's body on the create, or
    replaces the open PR's body on a re-delivery. Without it the body is the
    repository's own pull request template, when ``source_dir`` has one,
    followed by the loop's summary. ``verification`` is what the sandbox's
    checks did not decide (#682) — advisory failures, or that nothing ran
    under `ci-only` — and closes the body as its own section, whichever
    way the body was written.

    ``branch`` overrides the per-run branch name so a fix round lands on the
    pull request it was fixing: the existing branch is looked up and
    force-moved to the new commit (#518). ``pr_number`` is that pull request
    when the caller already knows it — a re-delivery then never POSTs a new
    PR at all (the open PR follows its branch); without it, a 422 on the
    create is confirmed by looking the branch's open PR up (see the module
    notes). ``round_no`` is the delivery round when the caller counts them
    (the engine's fix rounds); it only decorates the force-move event.

    ``parent`` is the commit the new one descends from when the caller is
    *continuing* published history — a restarted run adopting the branch a
    previous attempt pushed (#600). It changes only the new commit's
    *parent*, never the tree it is layered onto: the entry list is always a
    diff against ``base``, so the tree must be built on ``base``'s tree or
    the result would be the union of the previous attempt's tree and this
    run's base-relative changes — a tree neither the agent built nor the
    reviewer diffed. With ``parent`` the earlier commits stay reachable
    from the branch instead of being force-moved away, while the tree is
    still exactly what this run's workspace holds.

    ``closes`` is the issue this delivery resolves; it becomes a
    ``Closes #N`` line in the PR body, so GitHub links issue and PR and
    closes the issue on merge on its own.

    ``title`` and ``commit_message`` are the rendered naming templates
    (#621, :func:`render_naming`); unset, the loop's historical wording.
    A re-delivery whose title differs from the open PR's retitles it
    (``deliver.title_changed``) — how a fix round cures a title-lint check.
    """
    plan: DeliveryPlan | None = None
    if not _is_checkout_root(source_dir):
        # Fail before any API call when there is nothing to send; the
        # git-diff plan can only decide that once the base commit is known.
        plan = _plan_snapshot(source_dir, exclude)

    if base is None:
        base = ops.default_branch(repo)
    base_sha = _base_commit_sha(ops, repo, base)
    if base_sha is None:
        log.warning(
            "deliver.bootstrap_empty_repo",
            run=run_id,
            repo=repo,
            base=base,
            hint="base branch has no commit; creating an initial one",
        )
        # An existing-but-empty repository has a default branch name and no
        # ref behind it (GitHub answers 409; an absent branch on a non-empty
        # repo — unusual explicit `base` — answers 404; the worker folds
        # both into "missing"). Bootstrap an initial commit so the normal PR
        # path (branch off base, open a PR against it) applies unchanged.
        _bootstrap_empty_repo(ops, repo, base, run_id=run_id, outcome=outcome)
        base_sha = _base_commit_sha(ops, repo, base)
        if base_sha is None:
            raise DeliveryError(f"base branch {base!r} of {repo} still missing after bootstrap")
    # Always the base branch's tree: the entry list is a diff against
    # `base` (see `parent` above), so layering it on any other tree would
    # deliver something no one built or reviewed (#600).
    base_tree = _commit_tree_sha(ops, repo, base_sha)

    if plan is None:
        plan = _plan_git_diff(source_dir, base_sha, exclude)
    if plan is None:
        # A checkout with nothing to diff against (the agent git-init-ed
        # the workspace itself): the snapshot is still the right delivery,
        # said out loud in the PR body rather than silently.
        log.warning(
            "deliver.snapshot_fallback",
            run=run_id,
            repo=repo,
            reason="no base commit to diff against",
        )
        plan = _plan_snapshot(source_dir, exclude)
        plan.note = "delivered as a workspace snapshot: no base commit to diff against"

    log.info(
        "deliver.plan",
        run=run_id,
        repo=repo,
        base=base,
        base_sha=base_sha[:12],
        entries=len(plan.entries),
        uploads=len(plan.uploads),
        upload_bytes=sum(len(raw) for raw in plan.uploads.values()),
    )
    shas = _create_blobs(ops, repo, plan.uploads, run_id=run_id)
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
                "message": commit_message or _commit_message(run_id, outcome),
                "tree": tree,
                "parents": [parent or base_sha],
            },
        ),
        f"commit for {repo}",
    )
    branch = branch or branch_name(run_id)
    _point_branch(ops, repo, branch, commit, run_id=run_id, round_no=round_no)
    log.info("deliver.branch_pushed", run=run_id, repo=repo, branch=branch, commit=commit[:12])

    title = _clip_title(title) if title else _title(outcome)
    if pr_number is not None:
        # The PR already exists and its branch just moved under it: there
        # is nothing to create. Blind-POSTing and parsing the refusal is
        # how a fix round died with its PR number in hand (#387 field run).
        data = ops.pr_get(repo, pr_number)
        url = str(data.get("html_url") or "")
        log.info("deliver.pr_refreshed", run=run_id, repo=repo, pr=pr_number, url=url)
        _retitle(
            ops, repo, pr_number, current=str(data.get("title") or ""), wanted=title, run_id=run_id
        )
        if authored_body and authored_body.strip():
            _rebody(
                ops,
                repo,
                pr_number,
                body=_body(
                    run_id,
                    outcome,
                    plan,
                    closes=closes,
                    authored=authored_body,
                    verification=verification,
                ),
                run_id=run_id,
            )
        return PrRef(number=pr_number, url=url)
    template = pr_template(source_dir)
    if template is not None:
        log.info("deliver.pr_template", run=run_id, repo=repo, path=template[0])
    body = _body(
        run_id,
        outcome,
        plan,
        closes=closes,
        template=template[1] if template else None,
        authored=authored_body,
        verification=verification,
    )
    try:
        pr = ops.pr_create(
            repo,
            base=base,
            head=branch,
            title=title,
            body=body,
            draft=draft,
        )
    except GithubOpsError as exc:
        if draft and _is_draft_unsupported(exc):
            # No drafts on this plan or instance (#677): one retry as a
            # ready PR. The landing's un-draft step then has nothing to
            # do; `deliver_draft` was a preference, not a requirement.
            log.info(
                "deliver.draft_unsupported",
                run=run_id,
                repo=repo,
                hint="the repository has no draft pull requests; opening the PR ready for review",
            )
            draft = False
            pr = ops.pr_create(repo, base=base, head=branch, title=title, body=body, draft=False)
            log.info(
                "deliver.pr_opened", run=run_id, repo=repo, pr=pr.number, url=pr.url, draft=False
            )
            return pr
        if not _is_pr_collision(exc):
            raise
        existing = _find_open_pr(ops, repo, branch)
        if existing is None:
            raise
        log.info(
            "deliver.pr_reused",
            run=run_id,
            repo=repo,
            pr=existing.number,
            url=existing.url,
            hint="a re-delivery refreshed the branch behind an existing open PR",
        )
        return existing
    log.info("deliver.pr_opened", run=run_id, repo=repo, pr=pr.number, url=pr.url, draft=draft)
    return pr


def _point_branch(
    ops: GithubOps,
    repo: str,
    branch: str,
    commit: str,
    *,
    run_id: str,
    round_no: int | None = None,
) -> None:
    """Point the delivery branch at ``commit``: create it when it does not
    exist yet, force-move it when it does.

    The branch name is a pure function of the run id, so it already exists
    for every fix round's re-delivery (round >= 2) and for a manual
    ``sbxloop deliver <run>`` after a failed first attempt (#223).
    Force-moving rather than suffixing keeps one branch (and one PR) per
    run: the newer commit is built from the same artifacts and supersedes
    the old one.

    The ref is looked *up* before anything is created (#518). A blind POST
    is guaranteed to 422 on every round after the first, and the worker
    paints that expected refusal as a ``worker.error`` in the run's
    chronology — one doomed API call and one red panel per healthy
    re-delivery. The lookup makes the collision an answer, not an error;
    the 422 catch stays only for the race where the ref appears between
    the lookup and the create.
    """
    previous = ops.ref_lookup(repo, f"heads/{branch}")
    if previous is not None:
        _force_move(ops, repo, branch, commit, run_id=run_id, round_no=round_no, previous=previous)
        return
    try:
        ops.raw("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
    except GithubOpsError as exc:
        if _is_ref_refusal(exc):
            # A 422 that is not "already exists" is the repository refusing
            # the ref: a ruleset admitting only certain branch patterns
            # (#621), a base that wants signed commits, a locked repository
            # (#677). No retry changes any of these; the advice comes from
            # GitHub's own words, never a knob its message does not name.
            raise DeliveryError(
                f"{repo} refused to create branch {branch!r}: {exc}. {_explain_ref_refusal(exc)}"
            ) from exc
        if not _is_ref_collision(exc):
            raise
        _force_move(
            ops,
            repo,
            branch,
            commit,
            run_id=run_id,
            round_no=round_no,
            previous=None,
            hint="the branch appeared between the lookup and the create",
        )


def _force_move(
    ops: GithubOps,
    repo: str,
    branch: str,
    commit: str,
    *,
    run_id: str,
    round_no: int | None,
    previous: str | None,
    hint: str | None = None,
) -> None:
    """Force-move ``branch`` to ``commit`` and say what it superseded."""
    fields: dict[str, Any] = {"run": run_id, "repo": repo, "branch": branch, "to": commit[:12]}
    if previous is not None:
        fields["from"] = previous[:12]
    if round_no is not None:
        fields["round"] = round_no
    if hint is not None:
        fields["hint"] = hint
    log.info("deliver.branch_force_moved", **fields)
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
    submodule_notes: list[str] = []
    for change in hostgit.changes_since(source_dir, diff_base, notes=submodule_notes):
        hit = exclusion_hit(change.path.split("/"), exclude)
        if hit is None:
            kept.append(change)
        else:
            excluded[hit] = excluded.get(hit, 0) + 1
    for note in submodule_notes:
        # Work inside a submodule, or a gitlink at a commit nobody else can
        # fetch (#692): not the superproject's to deliver, said in the PR
        # body and the log rather than dropped in silence.
        log.warning("deliver.submodule_change_skipped", detail=note)
    if not kept:
        why = f"{source_dir} has no changes relative to {diff_base[:12]}"
        if submodule_notes:
            why += " that can be delivered: " + "; ".join(submodule_notes)
        raise DeliveryError(f"nothing to deliver: {why}")
    entries: list[dict[str, Any]] = []
    uploads: dict[str, bytes] = {}
    lines: list[str] = []
    for change in kept:
        if change.is_gitlink:
            # A submodule pointer: the tree entry IS the commit sha, there is
            # no blob to upload. A removed submodule drops its path the same
            # way a removed file does.
            sha = change.sha if change.status != "deleted" else None
            entries.append({"path": change.path, "mode": change.mode, "type": "commit", "sha": sha})
            lines.append(
                f"- {STATUS_MARKER[change.status]} `{change.path}`"
                + (f" (submodule → {change.sha[:12]})" if sha else " (submodule)")
            )
            continue
        lines.append(f"- {STATUS_MARKER[change.status]} `{change.path}`")
        if change.status == "deleted":
            entries.append({"path": change.path, "mode": FILE_MODE, "type": "blob", "sha": None})
            continue
        entries.append({"path": change.path, "mode": change.mode, "type": "blob"})
        full = source_dir / change.path
        uploads[change.path] = (
            str(full.readlink()).encode() if change.mode == "120000" else full.read_bytes()
        )
    not_delivered: list[str] = []
    if excluded:
        not_delivered.append(
            f"{sum(excluded.values())} file(s) excluded ({', '.join(sorted(excluded))})"
        )
    not_delivered.extend(submodule_notes)
    return DeliveryPlan(
        mode="git-diff",
        entries=entries,
        uploads=uploads,
        lines=lines,
        excluded_note="; ".join(not_delivered) or None,
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


def _create_blobs(
    ops: GithubOps, repo: str, uploads: dict[str, bytes], *, run_id: str | None = None
) -> dict[str, str]:
    """Create all blobs via batched worker jobs; returns relative path -> sha."""
    shas: dict[str, str] = {}
    chunks = _manifest_chunks(uploads)
    for index, chunk in enumerate(chunks, start=1):
        log.debug(
            "deliver.blob_batch",
            run=run_id,
            repo=repo,
            batch=index,
            batches=len(chunks),
            files=len(chunk),
            b64_bytes=sum(len(entry["content_b64"]) for entry in chunk),
        )
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
    return _clip_title(f"sbxloop: {outcome}")


def _clip_title(title: str) -> str:
    title = " ".join(title.split())
    return title if len(title) <= TITLE_CLIP else title[: TITLE_CLIP - 1] + "…"


def _commit_message(run_id: str, outcome: str) -> str:
    return f"sbxloop run {run_id}: deliver artifacts\n\nOutcome: {outcome}"


def render_naming(template: str, *, title: str | None, outcome: str, run_id: str, repo: str) -> str:
    """Render a `[github]` naming template (#621). ``{title}`` is the
    plan's own title when the model gave one, else the outcome — so the
    default templates render byte-identically to what the loop always
    wrote when no title was authored."""
    return template.format(
        title=" ".join((title or outcome).split()), outcome=outcome, run_id=run_id, repo=repo
    )


def _retitle(
    ops: GithubOps, repo: str, number: int, *, current: str, wanted: str, run_id: str
) -> None:
    """Rename the open PR when a re-delivery wants a different title —
    the model retitled it in a fix round, or the template changed. Best
    effort: a refused rename must not fail a delivery that landed."""
    if not wanted or not current or current == wanted:
        return  # no current title = GitHub did not say; nothing to compare
    try:
        ops.raw("PATCH", f"/repos/{repo}/pulls/{number}", {"title": wanted})
    except GithubOpsError:
        log.warning("deliver.title_unchanged", run=run_id, repo=repo, pr=number, exc_info=True)
        return
    log.info("deliver.title_changed", run=run_id, repo=repo, pr=number, old=current, new=wanted)


def pr_template(root: Path) -> tuple[str, str] | None:
    """The repository's pull request template (#678): ``(path, text)`` for
    the first of :data:`PR_TEMPLATE_PATHS` under ``root`` with any text in
    it, else ``None``. Read as bytes and decoded leniently — a template is
    prose, and one odd byte must not fail a delivery."""
    for rel in PR_TEMPLATE_PATHS:
        path = root / rel
        try:
            if not path.is_file():
                continue
            text = path.read_bytes()[:PR_TEMPLATE_CAP].decode("utf-8", "replace").strip()
        except OSError:
            continue
        if text:
            return rel, text
    return None


def conventional_titles(root: Path) -> str | None:
    """What, if anything, says this repository lints pull-request titles as
    conventional commits (#678): the path of the evidence, else ``None``.
    A workflow counts only when it runs one of the known title actions;
    reading it is bounded to the workflows directory, never the tree."""
    for rel in _COMMITLINT_FILES:
        if (root / rel).is_file():
            return rel
    package = root / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_bytes()[: PR_TEMPLATE_CAP * 4])
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and "commitlint" in data:
            return "package.json (commitlint)"
    workflows = root / ".github" / "workflows"
    if workflows.is_dir():
        for path in sorted(workflows.iterdir()):
            if path.suffix not in (".yml", ".yaml") or not path.is_file():
                continue
            try:
                text = path.read_bytes()[: PR_TEMPLATE_CAP * 4].decode("utf-8", "replace")
            except OSError:
                continue
            for action in _TITLE_ACTIONS:
                if action in text:
                    return f".github/workflows/{path.name} ({action})"
    return None


def conventional_title(title: str) -> str:
    """``title`` as a conventional-commit subject (#678): kept when it
    already is one (the planner read the repository's history), else
    ``chore:`` + the subject lowercased at its first letter — the guess
    config-conventional's default rules accept (a sentence-case subject
    is itself a violation), and a red title check is still a fix round
    away via ``.sbxloop/pr-title``."""
    title = " ".join(title.split())
    if CONVENTIONAL_TITLE.match(title):
        return title
    return f"chore: {title[:1].lower()}{title[1:]}" if title else "chore: deliver artifacts"


def pr_conventions(root: Path | None) -> str:
    """The decompose prompt's paragraph on this repository's pull-request
    conventions (#678): its title lint, its template — or nothing, when
    the workspace declares neither, so the model is not taught a
    convention the repository does not have."""
    if root is None:
        return ""
    lines: list[str] = []
    evidence = conventional_titles(root)
    if evidence:
        lines.append(
            f"- This repository lints pull request titles as conventional commits "
            f"(`{evidence}`): `pr_title` MUST read `type(scope): summary` — a lowercase "
            f"type from feat, fix, docs, refactor, test, chore, build, ci, perf; the "
            f"scope optional; the summary lowercase, imperative, without a trailing period."
        )
    template = pr_template(root)
    if template:
        lines.append(
            f"- This repository has a pull request template (`{template[0]}`). The last "
            f"task MUST write the template filled in for this change — each section "
            f"answered, each checklist item ticked only when it is true — to "
            f"`{PR_BODY_FILE}` under the workspace root, alone in that file; the loop "
            f"uses it as the pull request's description. It is never delivered as a "
            f"file, and a task must not commit it."
        )
    return "\n".join(lines)


def _summary(run_id: str, outcome: str, plan: DeliveryPlan) -> str:
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


def _body(
    run_id: str,
    outcome: str,
    plan: DeliveryPlan,
    *,
    closes: int | None = None,
    template: str | None = None,
    authored: str | None = None,
    verification: str | None = None,
) -> str:
    """The pull request's description (#678). The agent's own
    ``.sbxloop/pr-body`` wins outright — it filled the repository's
    template in — with the run's provenance and ``Closes`` after a rule;
    otherwise a repository template opens the body verbatim, so a check
    that parses it (danger, a PR lint) sees the sections it expects, and
    the loop's summary follows. A ``verification`` note (#682: what the
    sandbox's checks did not decide) is its own section before the
    footer, so a reviewer reads it whichever way the body was written.
    ``Closes #N`` is always the last line: it is what settles the issue."""
    footer = f"\nCloses #{closes}\n" if closes is not None else ""
    if verification and verification.strip():
        footer = f"\n**Verification:** {verification.strip()}\n" + footer
    if authored and authored.strip():
        return (
            f"{authored.strip()}\n\n---\n\nArtifacts produced by sbxloop run `{run_id}`.\n" + footer
        )
    summary = _summary(run_id, outcome, plan)
    if template:
        return f"{template.strip()}\n\n---\n\n{summary}{footer}"
    return summary + footer


def _rebody(ops: GithubOps, repo: str, number: int, *, body: str, run_id: str) -> None:
    """Replace the open PR's description when a fix round authored one
    (#678) — a check that judges the body is otherwise incurable. Best
    effort, like the retitle: a refused edit must not fail a delivery
    that landed."""
    try:
        ops.raw("PATCH", f"/repos/{repo}/pulls/{number}", {"body": body})
    except GithubOpsError:
        log.warning("deliver.body_unchanged", run=run_id, repo=repo, pr=number, exc_info=True)
        return
    log.info("deliver.body_changed", run=run_id, repo=repo, pr=number, chars=len(body))
