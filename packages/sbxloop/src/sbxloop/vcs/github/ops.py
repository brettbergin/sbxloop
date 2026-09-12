"""Typed facade over github.op jobs running in the github-ops sandbox.

The host never talks to GitHub with the user PAT directly — every operation
becomes a ``github.op`` JobRequest submitted to the github sandbox, which is
the only environment holding ``GH_TOKEN``.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

from sbxloop.config import MergeMethod
from sbxloop.errors import GithubOpsError
from sbxloop.ids import new_job_id
from sbxloop.log import get_logger
from sbxloop.vcs.github.permissions import READ_PROBES
from sbxloop.vcs.github.protection import read_base_requirements
from sbxloop.vcs.github.review_locations import right_side_ranges
from sbxloop.vcs.model import (
    BaseRequirements,
    CheckState as CheckState,
    ChecksVerdict as ChecksVerdict,
    CloseReason as CloseReason,
    FailedCheck as FailedCheck,
    Identity as Identity,
    IssueRef as IssueRef,
    MergeOutcome as MergeOutcome,
    PostedFinding as PostedFinding,
    PrRef as PrRef,
    QueueEntry as QueueEntry,
    QueueEntryState as QueueEntryState,
    QueueState as QueueState,
    ReviewComment as ReviewComment,
    ReviewEvent as ReviewEvent,
    ReviewThread as ReviewThread,
    ReviewVerdict as ReviewVerdict,
    SubmittedReview as SubmittedReview,
    ThreadComment as ThreadComment,
    approval_summary as approval_summary,
    identities_match as identities_match,
    logins_match as logins_match,
    normalize_login as normalize_login,
)
from sbxloop.vcs.protocol import Capability, VcsOps
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import JobRequest, TransportSpec

log = get_logger(__name__)


def _typename_kind(typename: Any) -> bool | None:
    """GraphQL ``author.__typename`` as a kind: ``Bot`` → True, any other
    named type → False, absent (a deleted account, an unrequested field)
    → None."""
    if not typename:
        return None
    return str(typename) == "Bot"


def is_bot_user(user: Any) -> bool:
    """Whether a REST ``user`` object is a GitHub App (``type == "Bot"``).

    Carried alongside the login (#613, #622) so a reviewer's kind is a
    fact read from GitHub, not a guess from a ``[bot]`` suffix — a human
    ``foo`` and an App ``foo[bot]`` are different accounts.
    """
    return isinstance(user, dict) and str(user.get("type") or "") == "Bot"


def user_kind(user: Any) -> bool | None:
    """A REST ``user`` object's kind for :func:`identities_match`: True for
    an App, False for a user, None when the payload carries no ``type``."""
    if not isinstance(user, dict) or not user.get("type"):
        return None
    return is_bot_user(user)


def user_identity(user: Any) -> Identity:
    """A REST ``user`` object as an :data:`Identity`."""
    login = str(user.get("login") or "") if isinstance(user, dict) else ""
    return login, user_kind(user)


# -- reading lists -------------------------------------------------------------
#
# Every GitHub list endpoint pages at 30 by default and 100 at most. A read
# that takes the first page for the whole list is a silent truncation, and
# on the reads that gate a merge (reviews, review comments, threads) a
# silent truncation is a silent merge over unseen feedback (#614). Every
# list read goes through `raw_pages`; a list longer than it will follow is
# refused, not cut — "we could not tell" is not "there is nothing there".

PAGE_SIZE = 100
# Ten full pages is a thousand entries; a pull request or issue with more
# history than that is not one the loop should be judging by list-walk.
MAX_PAGES = 10


class PaginationError(GithubOpsError):
    """A list longer than the reader will follow (or a GraphQL connection
    with a next page the query does not fetch). The read is incomplete
    and must be treated as unread, never as "what we saw is all there
    is"."""


class MalformedResponse(GithubOpsError):
    """GitHub answered, but not in the shape the operation is defined to
    return — a list where an object was due, an object without the field
    the caller exists to read. Never a miss and never a refusal: those
    carry a status. A caller that can do nothing with the answer treats
    it as unread."""

    def __init__(self, what: str, data: Any) -> None:
        super().__init__(f"{what} returned a malformed result: {data!r}")
        self.data = data


def raw_lookup(
    ops: Any,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    missing: Sequence[int] = (404,),
) -> Any:
    """:meth:`GithubOps.raw_lookup` for any ops object (#558): the real one
    asks the worker to answer the miss as data; a duck-typed stand-in
    without the method gets the same ``None`` from its raised error."""
    lookup = getattr(ops, "raw_lookup", None)
    if lookup is not None:
        return lookup(method, path, body, missing=missing)
    try:
        # Positional `body` only when there is one: a stand-in's `raw`
        # may take (method, path) alone.
        return ops.raw(method, path) if body is None else ops.raw(method, path, body)
    except GithubOpsError as exc:
        if exc.http_status in missing:
            return None
        raise


def raw_pages(ops: GithubOps, path: str, *, key: str | None = None) -> list[Any]:
    """Every entry of a REST list endpoint, following ``page=`` until a
    short page.

    ``key`` names the list inside an envelope (``check_runs`` on the
    check-runs endpoint, ``statuses`` on the combined status). A response
    that is not the expected shape ends the walk with what was read so
    far, matching the single-page callers' "not a list → nothing" reading.
    Raises :class:`PaginationError` when :data:`MAX_PAGES` full pages did
    not reach the end.
    """
    sep = "&" if "?" in path else "?"
    rows: list[Any] = []
    for page in range(1, MAX_PAGES + 1):
        data = ops.raw("GET", f"{path}{sep}per_page={PAGE_SIZE}&page={page}")
        if key is not None:
            data = data.get(key) if isinstance(data, dict) else None
        if not isinstance(data, list):
            return rows
        rows.extend(data)
        if len(data) < PAGE_SIZE:
            return rows
    raise PaginationError(
        f"GET {path} has more than {MAX_PAGES * PAGE_SIZE} entries; "
        "the list was not read to its end"
    )


def fold_review_verdicts(
    payload: Any, *, exclude: Identity | None = None
) -> tuple[ReviewVerdict, ...]:
    """Every reviewer's *standing* verdict from the reviews payload (#675):
    the latest APPROVED / CHANGES_REQUESTED per reviewer, a DISMISSED
    clearing it, COMMENT reviews skipped — the same fold as
    :func:`fold_reviews`, kept per reviewer so a caller can count the
    approvals and name who objects. ``exclude`` drops the loop's own
    identity: its review lives in the run, and GitHub would not count it
    toward the base's approvals anyway."""
    if not isinstance(payload, list):
        return ()
    latest: dict[str, tuple[str, bool]] = {}
    for review in payload:
        if not isinstance(review, dict):
            continue
        state = str(review.get("state") or "").upper()
        if state not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            continue
        identity = user_identity(review.get("user"))
        if not identity[0] or (exclude is not None and identities_match(identity, exclude)):
            continue
        latest[identity[0]] = (state, bool(identity[1]))
    return tuple(
        ReviewVerdict(login, state, is_bot)
        for login, (state, is_bot) in latest.items()
        if state != "DISMISSED"
    )


# Check-run conclusions that are not failures. ``neutral`` and ``skipped``
# are deliberately included: a skipped job is not a red build, and treating
# it as one would wedge the fix loop against something no commit can change.
# `action_required` is its own bucket (#612): a workflow a maintainer has
# not approved yet is neither red nor running. Everything else — failure,
# timed_out, cancelled, stale, or a conclusion GitHub adds later — counts
# as failed. Unknown conclusions fail closed: a check nobody understands
# must not read as permission to merge.
PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
APPROVAL_CONCLUSIONS = frozenset({"action_required"})


def fold_check_runs(payload: Any) -> ChecksVerdict:
    """``GET /repos/{repo}/commits/{sha}/check-runs`` folded to a verdict.

    A run with no ``conclusion`` yet is pending, whatever its status says. A
    head commit with no checks at all reads as ``green``: a repository
    without CI must not deadlock the loop waiting for a report that will
    never come.
    """
    runs = payload.get("check_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        return ChecksVerdict("green", 0, (), ())
    pending: list[str] = []
    failed: list[str] = []
    passed: list[str] = []
    approval: list[str] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        name = str(run.get("name") or "check")
        conclusion = run.get("conclusion")
        if conclusion is None:
            pending.append(name)
        elif str(conclusion).lower() in APPROVAL_CONCLUSIONS:
            approval.append(name)
        elif str(conclusion).lower() not in PASSING_CONCLUSIONS:
            failed.append(name)
        else:
            passed.append(name)
    return _verdict(len(runs), pending, failed, passed, approval)


def _verdict(
    total: int,
    pending: list[str],
    failed: list[str],
    passed: list[str],
    approval: Sequence[str] = (),
) -> ChecksVerdict:
    approval = tuple(approval)
    if failed:
        # Red beats pending: the build is already known broken, and waiting
        # on the stragglers only delays the fix.
        return ChecksVerdict("red", total, tuple(pending), tuple(failed), tuple(passed), approval)
    if pending or approval:
        return ChecksVerdict("pending", total, tuple(pending), (), tuple(passed), approval)
    return ChecksVerdict("green", total, (), (), tuple(passed))


# Commit-status states that are not failures. The Status API has exactly
# four: success, pending, failure, error — error is a red build too (the
# CI system itself broke), and anything unrecognized fails closed like an
# unknown check-run conclusion.
PASSING_STATUS_STATES = frozenset({"success"})
PENDING_STATUS_STATES = frozenset({"pending"})


def fold_statuses(payload: Any) -> ChecksVerdict:
    """``GET /repos/{repo}/commits/{sha}/status`` folded to a verdict.

    The combined endpoint already keeps only the newest status per
    ``context``, so every entry counts once. Folded from the ``statuses``
    list, NEVER from the payload's top-level ``state``: a commit with no
    statuses at all answers ``state: "pending"`` with an empty list, and
    reading that as pending would deadlock the loop on every repository
    that only uses the Checks API — the exact "no CI must not block" case
    ``fold_check_runs`` handles for its side.
    """
    statuses = payload.get("statuses") if isinstance(payload, dict) else None
    if not isinstance(statuses, list):
        return ChecksVerdict("green", 0, (), ())
    pending: list[str] = []
    failed: list[str] = []
    passed: list[str] = []
    total = 0
    for status in statuses:
        if not isinstance(status, dict):
            continue
        total += 1
        name = str(status.get("context") or "status")
        state = str(status.get("state") or "").lower()
        if state in PENDING_STATUS_STATES:
            pending.append(name)
        elif state not in PASSING_STATUS_STATES:
            failed.append(name)
        else:
            passed.append(name)
    return _verdict(total, pending, failed, passed)


def fold_reviews(payload: Any, *, login: str | None = None, is_bot: bool | None = None) -> str:
    """``GET /repos/{repo}/pulls/{n}/reviews`` folded to one state.

    GitHub keeps every review ever submitted, so only each reviewer's
    *latest* verdict counts — an APPROVE after a REQUEST_CHANGES clears it.
    ``COMMENT`` reviews never change a reviewer's standing verdict (GitHub's
    own rule) and are skipped. ``login`` (with ``is_bot``, the loop's own
    kind when known, #622) narrows the fold to one reviewer, which is how
    the loop asks "did *my* review get satisfied?" without a human's
    approval answering on its behalf.

    Returns ``APPROVED``, ``CHANGES_REQUESTED`` or ``NONE``.
    """
    if not isinstance(payload, list):
        return "NONE"
    latest: dict[str, str] = {}
    for review in payload:
        if not isinstance(review, dict):
            continue
        state = str(review.get("state") or "").upper()
        if state not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            continue
        who = str((review.get("user") or {}).get("login") or "")
        if login is not None and not identities_match(
            user_identity(review.get("user")), (login, is_bot)
        ):
            continue
        # A dismissed review no longer stands; recording it stops a later
        # entry-free fold from resurrecting the verdict it replaced.
        latest[who] = "NONE" if state == "DISMISSED" else state
    if any(state == "CHANGES_REQUESTED" for state in latest.values()):
        return "CHANGES_REQUESTED"
    if any(state == "APPROVED" for state in latest.values()):
        return "APPROVED"
    return "NONE"


def review_payload(
    event: ReviewEvent, body: str, comments: Sequence[ReviewComment] = ()
) -> dict[str, Any]:
    """The POST body for the reviews API.

    ``comments`` is omitted rather than sent empty: a review with no inline
    anchors is an ordinary summary review, and GitHub rejects an empty array
    on some paths.
    """
    payload: dict[str, Any] = {"event": event, "body": body}
    if comments:
        payload["comments"] = [
            {"path": c.path, "line": c.line, "side": c.side, "body": c.body} for c in comments
        ]
    return payload


def anchor_of(comment: ReviewComment) -> str:
    """The ``path:line`` key a finding is tracked by across rounds."""
    return f"{comment.path}:{comment.line}"


def _review_threads_connection(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    nodes = (((payload.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get(
        "reviewThreads"
    )
    return nodes if isinstance(nodes, dict) else {}


def _rollup_connection(payload: Any) -> dict[str, Any]:
    """The ``statusCheckRollup.contexts`` connection of the PR's last
    commit, or ``{}`` when the commit has no rollup yet (nothing has
    reported on it — GitHub serves ``null``, not an empty connection)."""
    data = payload.get("data") if isinstance(payload, dict) else None
    repository = data.get("repository") if isinstance(data, dict) else None
    pr = repository.get("pullRequest") if isinstance(repository, dict) else None
    commits = pr.get("commits") if isinstance(pr, dict) else None
    nodes = commits.get("nodes") if isinstance(commits, dict) else None
    first = nodes[0] if isinstance(nodes, list) and nodes else None
    commit = first.get("commit") if isinstance(first, dict) else None
    rollup = commit.get("statusCheckRollup") if isinstance(commit, dict) else None
    contexts = rollup.get("contexts") if isinstance(rollup, dict) else None
    return contexts if isinstance(contexts, dict) else {}


def rollup_next_cursor(payload: Any) -> str | None:
    """The cursor of the rollup page after this one, or ``None`` on the last."""
    info = _rollup_connection(payload).get("pageInfo")
    if not isinstance(info, dict) or not info.get("hasNextPage"):
        return None
    cursor = info.get("endCursor")
    return str(cursor) if cursor else None


def fold_required_contexts(payload: Any) -> list[str]:
    """The names of one rollup page's contexts GitHub marks ``isRequired``
    for the pull request (#674): a check run's ``name`` or a status'
    ``context`` — the shared namespace of #610. A node in a shape not
    understood is skipped; it cannot be named, so it cannot be gated on."""
    out: list[str] = []
    entries = _rollup_connection(payload).get("nodes")
    for node in entries if isinstance(entries, list) else []:
        if not isinstance(node, dict) or node.get("isRequired") is not True:
            continue
        name = node.get("name") or node.get("context")
        if name:
            out.append(str(name))
    return out


# GitHub's ``MergeQueueEntryState`` in the loop's words. LOCKED is the
# queue holding the entry while it merges it — field-unverified beyond
# GitHub's schema description, and read as mergeable since the queue has
# already decided to merge. Anything else is ``unknown``, never mergeable.
_QUEUE_STATES: dict[str, QueueEntryState] = {
    "QUEUED": "queued",
    "AWAITING_CHECKS": "testing",
    "MERGEABLE": "mergeable",
    "LOCKED": "mergeable",
    "UNMERGEABLE": "blocked",
}


def fold_queue_entry(node: Any) -> QueueEntry | None:
    """A GraphQL ``MergeQueueEntry`` node as a typed row; ``None`` when
    there is no entry (the PR is not queued) or the node has no id."""
    if not isinstance(node, dict) or not node.get("id"):
        return None
    head = node.get("headCommit")
    position = node.get("position")
    return QueueEntry(
        id=str(node["id"]),
        state=_QUEUE_STATES.get(str(node.get("state") or ""), "unknown"),
        position=int(position) if isinstance(position, int) else None,
        head=str(head.get("oid") or "") if isinstance(head, dict) else "",
    )


def fold_queue_state(payload: Any) -> QueueState:
    """The queue read's ``pullRequest`` folded to :class:`QueueState`
    (#676). A payload with no pull request raises: the caller is waiting
    on the queue and cannot tell "not queued" from "not read"."""
    data = payload.get("data") if isinstance(payload, dict) else None
    repo = data.get("repository") if isinstance(data, dict) else None
    pr = repo.get("pullRequest") if isinstance(repo, dict) else None
    if not isinstance(pr, dict):
        raise GithubOpsError(f"mergeQueueEntry returned no pull request: {payload!r}")
    removals = pr.get("timelineItems")
    count = removals.get("totalCount") if isinstance(removals, dict) else None
    nodes = removals.get("nodes") if isinstance(removals, dict) else None
    last = nodes[-1] if isinstance(nodes, list) and nodes else None
    reason = last.get("reason") if isinstance(last, dict) else None
    merge = pr.get("mergeCommit")
    return QueueState(
        merged=bool(pr.get("merged")),
        closed=str(pr.get("state") or "").upper() == "CLOSED",
        entry=fold_queue_entry(pr.get("mergeQueueEntry")),
        removals=int(count) if isinstance(count, int) else 0,
        removed_reason=str(reason or ""),
        merge_sha=str(merge.get("oid") or "") if isinstance(merge, dict) else "",
    )


def review_threads_next_cursor(payload: Any) -> str | None:
    """The cursor of the page after this one, or ``None`` on the last."""
    info = _review_threads_connection(payload).get("pageInfo")
    if not isinstance(info, dict) or not info.get("hasNextPage"):
        return None
    cursor = info.get("endCursor")
    return str(cursor) if cursor else None


def fold_review_threads(payload: Any) -> list[ReviewThread]:
    """One GraphQL ``pullRequest.reviewThreads`` page folded to typed rows.

    Malformed nodes are skipped rather than raising: a thread the API
    describes in a shape we do not understand must not take down the
    reconciliation pass that was going to leave it alone anyway. A thread
    whose comments connection has a further page is different — it is
    understood and *incomplete*, and a reply the loop did not see may be
    the one that answers or reopens it — so that raises
    :class:`PaginationError` naming the thread (#614).
    """
    threads: list[ReviewThread] = []
    entries = _review_threads_connection(payload).get("nodes")
    for node in entries if isinstance(entries, list) else []:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        raw_line = node.get("line")
        comments: list[ThreadComment] = []
        connection = node.get("comments") or {}
        info = connection.get("pageInfo") if isinstance(connection, dict) else None
        if isinstance(info, dict) and info.get("hasNextPage"):
            anchor = f"{node.get('path') or '?'}:{raw_line if raw_line is not None else '?'}"
            raise PaginationError(
                f"review thread {anchor} ({node_id}) has more comments than were read; "
                "it cannot be judged reconciled"
            )
        comment_nodes = connection.get("nodes") if isinstance(connection, dict) else None
        for comment in comment_nodes if isinstance(comment_nodes, list) else []:
            if not isinstance(comment, dict):
                continue
            database_id = comment.get("databaseId")
            author = comment.get("author") or {}
            comments.append(
                ThreadComment(
                    comment_id=int(database_id) if isinstance(database_id, int) else None,
                    login=str(author.get("login") or ""),
                    body=str(comment.get("body") or ""),
                    is_bot=_typename_kind(author.get("__typename")),
                )
            )
        threads.append(
            ReviewThread(
                thread_id=node_id,
                is_resolved=bool(node.get("isResolved")),
                path=str(node.get("path") or ""),
                line=int(raw_line) if isinstance(raw_line, int) else None,
                comments=tuple(comments),
            )
        )
    return threads


def github_transport(api_url: str) -> TransportSpec:
    """The descriptor every job to the GitHub backend carries (#1015):
    GitHub's REST root from ``[github] api_url``, the token as a bearer,
    lists paged by number, GitHub's ``Accept`` and API-version headers,
    and the ``gh`` CLI allowed."""
    return TransportSpec(api_url=api_url)


class GithubOps:
    #: The forge kind this backend answers for; the loop's ``[vcs] kind``.
    KIND: ClassVar[str] = "github"

    def __init__(
        self,
        client: WorkerClient,
        run_id: str,
        *,
        timeout_s: float = 120.0,
        transport: TransportSpec | None = None,
    ) -> None:
        self.client = client
        self.run_id = run_id
        self.timeout_s = timeout_s
        # How the worker reaches the forge (#1015); None sends no
        # descriptor and the worker serves the job as GitHub, from the
        # API root the sandbox's environment names.
        self.transport = transport

    def _op(self, op: str, params: dict[str, Any], *, timeout_s: float | None = None) -> Any:
        if self.transport is not None:
            params = {**params, "transport": self.transport.model_dump(mode="json")}
        job = JobRequest(
            job_id=new_job_id(),
            run_id=self.run_id,
            kind="vcs.op",
            op=op,
            params=params,
            timeout_s=timeout_s if timeout_s is not None else self.timeout_s,
        )
        started = time.monotonic()
        result = self.client.submit(job)
        error = result.error
        log.debug(
            "gh.op",
            run=self.run_id,
            job=job.job_id,
            op=op,
            repo=params.get("repo"),
            status=result.status,
            http_status=error.http_status if error is not None else None,
            duration_s=round(time.monotonic() - started, 2),
        )
        if result.status != "ok":
            assert result.error is not None
            raise GithubOpsError(
                f"github op {op} failed: {result.error.type}: {result.error.message}",
                http_status=result.error.http_status,
            )
        return result.output_json

    def issue_create(
        self,
        repo: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
    ) -> IssueRef:
        params: dict[str, Any] = {"repo": repo, "title": title, "body": body}
        if labels:
            params["labels"] = labels
        return IssueRef.model_validate(self._op("issue.create", params))

    def issue_comment(self, repo: str, number: int, body: str) -> str:
        data = self._op("issue.comment", {"repo": repo, "number": number, "body": body})
        return str(data.get("url", ""))

    def pr_create(
        self,
        repo: str,
        base: str,
        head: str,
        title: str,
        body: str = "",
        *,
        draft: bool = False,
    ) -> PrRef:
        return PrRef.model_validate(
            self._op(
                "pr.create",
                {
                    "repo": repo,
                    "base": base,
                    "head": head,
                    "title": title,
                    "body": body,
                    "draft": draft,
                },
            )
        )

    def pr_comment(self, repo: str, number: int, body: str) -> str:
        data = self._op("pr.comment", {"repo": repo, "number": number, "body": body})
        return str(data.get("url", ""))

    # -- pull request review ------------------------------------------------
    #
    # These go through `raw.api` rather than dedicated worker ops: the
    # reviews and check-runs endpoints need no parameter shaping the generic
    # transport does not already do, and a typed method here keeps the
    # untyped escape hatch confined to one layer instead of spreading
    # `raw()` calls through the daemon.

    def pr_get(self, repo: str, number: int) -> dict[str, Any]:
        """The PR itself — head sha (what the checks hang off), head ref
        (the branch a fix run must land on) and merge state."""
        data = self.raw("GET", f"/repos/{repo}/pulls/{number}")
        if not isinstance(data, dict):
            raise GithubOpsError(f"pr_get returned a malformed result: {data!r}")
        return data

    def pr_checks(self, repo: str, sha: str) -> ChecksVerdict:
        """Every check run AND commit status on ``sha``, folded to one
        verdict (#610). "No CI" means both lists empty — a repository
        reporting only through the Status API is not a repository without
        CI, and its red must not read as green."""
        runs = fold_check_runs(
            {
                "check_runs": raw_pages(
                    self, f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs"
                )
            }
        )
        statuses = fold_statuses(
            {"statuses": raw_pages(self, f"/repos/{repo}/commits/{sha}/status", key="statuses")}
        )
        return runs.merge(statuses)

    def merge_base(self, repo: str, base: str, head: str) -> str | None:
        """The commit ``head`` is built on: the merge base of ``base`` and
        ``head``, or None when GitHub cannot compare them (unrelated
        histories, 404). What #611 folds checks on to tell a red the PR
        caused from one it inherited."""
        data = self.raw_lookup("GET", f"/repos/{repo}/compare/{base}...{head}")
        merge_base = data.get("merge_base_commit") if isinstance(data, dict) else None
        sha = merge_base.get("sha") if isinstance(merge_base, dict) else None
        return str(sha) if sha else None

    def pr_review_verdicts(
        self, repo: str, number: int, *, exclude: Identity | None = None
    ) -> tuple[ReviewVerdict, ...]:
        """Each reviewer's standing verdict (#675), the loop's own excluded.
        One request per page of reviews."""
        return fold_review_verdicts(
            raw_pages(self, f"/repos/{repo}/pulls/{number}/reviews"), exclude=exclude
        )

    def pr_request_reviewers(self, repo: str, number: int, reviewers: Sequence[str]) -> None:
        """Ask GitHub for reviews from ``reviewers`` (#675): user logins,
        or ``org/team`` slugs for team reviewers. Write access suffices.
        A login GitHub refuses (not a collaborator, the PR's own author)
        fails the whole request; the caller decides how loud to be."""
        users = [name for name in reviewers if "/" not in name]
        teams = [name.split("/", 1)[1] for name in reviewers if "/" in name]
        body: dict[str, Any] = {}
        if users:
            body["reviewers"] = users
        if teams:
            body["team_reviewers"] = teams
        if not body:
            return
        self.raw("POST", f"/repos/{repo}/pulls/{number}/requested_reviewers", body)

    def pr_review_state(self, repo: str, number: int, *, login: str | None = None) -> str:
        """``APPROVED`` / ``CHANGES_REQUESTED`` / ``NONE`` — each reviewer's
        latest verdict only. ``login`` narrows it to one reviewer."""
        return fold_reviews(raw_pages(self, f"/repos/{repo}/pulls/{number}/reviews"), login=login)

    def checks_failed_logs(
        self, repo: str, sha: str, *, max_chars: int = 6000
    ) -> list[FailedCheck]:
        """The red check runs and commit statuses on ``sha``, each with its
        log, output, or description excerpt.

        A dedicated worker op rather than ``raw.api``: the Actions logs
        endpoint answers a text body behind a redirect, which the JSON
        transport cannot carry. The job gets twice the usual timeout — one
        log download per failing check, from blob storage, is slow.
        """
        data = self._op(
            "checks.failed_logs",
            {"repo": repo, "sha": sha, "max_chars": max_chars},
            timeout_s=self.timeout_s * 2,
        )
        checks = data.get("checks") if isinstance(data, dict) else None
        if not isinstance(checks, list):
            raise GithubOpsError(f"checks.failed_logs returned no check list: {data!r}")
        failed: list[FailedCheck] = []
        for entry in checks:
            if not isinstance(entry, dict) or not all(
                isinstance(entry.get(key), str)
                for key in ("name", "conclusion", "details_url", "excerpt")
            ):
                raise GithubOpsError(f"checks.failed_logs returned a malformed entry: {entry!r}")
            failed.append(
                FailedCheck(
                    name=entry["name"],
                    conclusion=entry["conclusion"],
                    excerpt=entry["excerpt"],
                    url=entry["details_url"],
                )
            )
        return failed

    def pr_review_feedback(
        self,
        repo: str,
        number: int,
        *,
        exclude_login: str | None = None,
        exclude_is_bot: bool | None = None,
        clip: int = 6000,
    ) -> str:
        """The objections standing on a PR, as one markdown block a fix
        round can act on; ``""`` when nothing stands.

        Latest verdict per reviewer only, matching how :func:`fold_reviews`
        judges the PR — a CHANGES_REQUESTED that a later APPROVE cleared is
        not an objection any more. Inline review comments are quoted with
        their ``path:line`` anchors so the fix agent can find the lines.
        ``exclude_login`` drops one identity's reviews and comments: the
        loop's own review is already known to the caller that posted it.
        """

        def login_of(entry: dict[str, Any]) -> str:
            return str((entry.get("user") or {}).get("login") or "")

        def excluded(entry: dict[str, Any]) -> bool:
            return exclude_login is not None and identities_match(
                user_identity(entry.get("user")), (exclude_login, exclude_is_bot)
            )

        reviews = raw_pages(self, f"/repos/{repo}/pulls/{number}/reviews")
        latest: dict[str, dict[str, Any]] = {}
        for review in reviews:
            if not isinstance(review, dict):
                continue
            login = login_of(review)
            if excluded(review):
                continue
            state = str(review.get("state") or "").upper()
            if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest[login] = review
        parts: list[str] = []
        for review in latest.values():
            if str(review.get("state") or "").upper() != "CHANGES_REQUESTED":
                continue
            body = str(review.get("body") or "").strip()
            if body:
                parts.append(body)
        comments = raw_pages(self, f"/repos/{repo}/pulls/{number}/comments")
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            if excluded(comment):
                continue
            body = str(comment.get("body") or "").strip()
            if not body:
                continue
            path = str(comment.get("path") or "")
            line = comment.get("line") or comment.get("original_line")
            anchor = f"`{path}:{line}`: " if path and line else f"`{path}`: " if path else ""
            parts.append(f"- {anchor}{body}")
        return "\n\n".join(parts)[:clip]

    def pr_review_locations(
        self, repo: str, number: int, *, commit_id: str | None
    ) -> dict[str, tuple[range, ...]]:
        """Read the PR's commentable RIGHT-side ranges for the reviewed head.

        Check both refs around the paginated read: PR file patches describe
        the current diff, not necessarily the head the agent reviewed. An
        unreadable/incomplete listing or moving refs cannot authorize an
        inline post. The caller preserves those findings in the body.
        """

        def refs() -> tuple[str, str]:
            pr = self.pr_get(repo, number)
            head, base = pr.get("head"), pr.get("base")
            head_sha = head.get("sha") if isinstance(head, dict) else None
            base_sha = base.get("sha") if isinstance(base, dict) else None
            if not isinstance(head_sha, str) or not isinstance(base_sha, str) or not base_sha:
                raise GithubOpsError("PR diff locations need both head and base SHAs")
            if not commit_id or head_sha != commit_id:
                raise GithubOpsError("PR head no longer matches the reviewed commit")
            return head_sha, base_sha

        before = refs()
        locations: dict[str, tuple[range, ...]] = {}
        path = f"/repos/{repo}/pulls/{number}/files"
        for page in range(1, MAX_PAGES + 1):
            data = self.raw("GET", f"{path}?per_page={PAGE_SIZE}&page={page}")
            if not isinstance(data, list):
                raise GithubOpsError("PR diff locations need a list of changed files")
            for entry in data:
                name = entry.get("filename") if isinstance(entry, dict) else None
                if not isinstance(name, str) or not name or name in locations:
                    raise GithubOpsError("PR diff locations contain missing or duplicate filenames")
                locations[name] = right_side_ranges(entry.get("patch"))
            if len(data) < PAGE_SIZE:
                break
        else:
            raise PaginationError("PR diff locations exceed the changed-file page limit")
        if refs() != before:
            raise GithubOpsError("PR base changed while reading diff locations")
        return locations

    def pr_review_create(
        self,
        repo: str,
        number: int,
        event: ReviewEvent,
        body: str,
        comments: Sequence[ReviewComment] = (),
    ) -> SubmittedReview:
        """Submit a review, with optional inline comments on the diff.

        A ``REQUEST_CHANGES`` or ``APPROVE`` from an identity the repository
        will not accept as a reviewer is refused by the API — a PR author
        cannot approve their own, among other rules. Losing the feedback
        over that would be the worst outcome, so it is resubmitted as a
        plain ``COMMENT``, which any identity may leave.

        The returned ``event`` is what was *actually* accepted, not what was
        asked for. Callers must read it: a COMMENT gates nothing, so an
        acceptance loop that assumed REQUEST_CHANGES had landed would wait
        forever for an approval no one was ever asked to give.
        """
        path = f"/repos/{repo}/pulls/{number}/reviews"

        def submit(kind: ReviewEvent) -> SubmittedReview:
            data = self.raw("POST", path, review_payload(kind, body, comments))
            url = str(data.get("html_url", "")) if isinstance(data, dict) else ""
            raw_id = data.get("id") if isinstance(data, dict) else None
            review_id = int(raw_id) if isinstance(raw_id, int) else None
            posted = self._capture_posted(repo, number, review_id, comments)
            return SubmittedReview(url, kind, review_id, posted)

        try:
            return submit(event)
        except GithubOpsError:
            if event == "COMMENT":
                raise
            log.warning(
                "gh.review_event_refused",
                repo=repo,
                pr=number,
                requested=event,
                hint=(
                    "this identity is not an accepted reviewer on the repo; "
                    "posting the feedback as a COMMENT review, which does not "
                    "gate the merge"
                ),
            )
            return submit("COMMENT")

    def _capture_posted(
        self,
        repo: str,
        number: int,
        review_id: int | None,
        comments: Sequence[ReviewComment],
    ) -> tuple[PostedFinding, ...]:
        """Map each requested finding to the comment GitHub actually created.

        A finding whose anchor GitHub dropped (or that was never inline to
        begin with) is still recorded, with ``comment_id=None`` — losing it
        here would make it invisible to reconciliation, which is the whole
        failure this capture exists to end.

        Capture is best-effort: a review *was* posted, and failing the whole
        call because the follow-up read 404'd would throw away feedback that
        is already on the PR.
        """
        if not comments:
            return ()
        wanted = [anchor_of(c) for c in comments]
        if review_id is None:
            return tuple(PostedFinding(anchor) for anchor in wanted)
        try:
            data = raw_pages(self, f"/repos/{repo}/pulls/{number}/reviews/{review_id}/comments")
        except GithubOpsError as exc:
            log.warning("gh.review_comments_read_failed", repo=repo, pr=number, error=str(exc))
            return tuple(PostedFinding(anchor) for anchor in wanted)
        by_anchor: dict[str, int] = {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            comment_id = entry.get("id")
            if not isinstance(comment_id, int):
                continue
            line = entry.get("line")
            if line is None:
                line = entry.get("original_line")
            anchor = f"{entry.get('path') or ''}:{line}"
            by_anchor.setdefault(anchor, comment_id)
        threads_by_comment: dict[int, str] = {}
        if by_anchor:
            try:
                for thread in self.pr_review_threads(repo, number):
                    for comment in thread.comments:
                        if comment.comment_id is not None:
                            threads_by_comment[comment.comment_id] = thread.thread_id
            except GithubOpsError as exc:
                log.warning("gh.review_threads_read_failed", repo=repo, pr=number, error=str(exc))
        posted: list[PostedFinding] = []
        for anchor in wanted:
            comment_id = by_anchor.get(anchor)
            posted.append(
                PostedFinding(
                    anchor=anchor,
                    comment_id=comment_id,
                    thread_id=(
                        threads_by_comment.get(comment_id) if comment_id is not None else None
                    ),
                )
            )
        return tuple(posted)

    def pr_review_comments_create(
        self,
        repo: str,
        number: int,
        comments: Sequence[ReviewComment],
        *,
        commit_id: str,
    ) -> tuple[PostedFinding, ...]:
        """Post each finding as its own review comment — the single-identity
        review (#513).

        GitHub refuses ``REQUEST_CHANGES`` and ``APPROVE`` from a PR's own
        author, so when the loop reviews the PR it opened, the review
        feature buys nothing but 422s. Individual review comments
        (``POST /pulls/{n}/comments``) are accepted from anyone, and each
        opens a thread that can be replied to and resolved exactly like one
        a review created — which is all reconciliation needs.

        Per anchor, not per review: a finding anchored outside the diff
        fails *its* comment (422 "line could not be resolved") and is
        returned with ``comment_id=None`` for the caller to put in the
        body, instead of taking every other finding down with it (#514).
        Thread ids are looked up once afterwards, best effort.
        """
        if not comments:
            return ()
        by_anchor: dict[str, int] = {}
        for comment in comments:
            anchor = anchor_of(comment)
            try:
                data = self.raw(
                    "POST",
                    f"/repos/{repo}/pulls/{number}/comments",
                    {
                        "body": comment.body,
                        "commit_id": commit_id,
                        "path": comment.path,
                        "line": comment.line,
                        "side": comment.side,
                    },
                )
            except GithubOpsError as exc:
                log.warning(
                    "gh.review_comment_refused",
                    repo=repo,
                    pr=number,
                    anchor=anchor,
                    error=str(exc)[:300],
                    hint="the finding goes in the review comment's body instead",
                )
                continue
            comment_id = data.get("id") if isinstance(data, dict) else None
            if isinstance(comment_id, int):
                by_anchor[anchor] = comment_id
        threads_by_comment: dict[int, str] = {}
        if by_anchor:
            try:
                for thread in self.pr_review_threads(repo, number):
                    for entry in thread.comments:
                        if entry.comment_id is not None:
                            threads_by_comment[entry.comment_id] = thread.thread_id
            except GithubOpsError as exc:
                log.warning("gh.review_threads_read_failed", repo=repo, pr=number, error=str(exc))
        return tuple(
            PostedFinding(
                anchor=anchor_of(c),
                comment_id=by_anchor.get(anchor_of(c)),
                thread_id=threads_by_comment.get(by_anchor[anchor_of(c)])
                if anchor_of(c) in by_anchor
                else None,
            )
            for c in comments
        )

    # -- reconciling review findings ----------------------------------------
    #
    # Replying on a finding's own thread, and resolving it, is what turns
    # "the fix round addressed it" into something a human reading the PR can
    # see. These are the only GitHub writes that touch an existing thread.

    # One page of threads per call, walked by cursor (#614). A thread's
    # own comments are read at the connection's maximum and refused
    # beyond it (`fold_review_threads`): a thread whose replies were not
    # all read cannot be judged answered or not.
    _THREADS_QUERY = (
        "query($owner: String!, $name: String!, $number: Int!, $cursor: String) { "
        "repository(owner: $owner, name: $name) { pullRequest(number: $number) { "
        "reviewThreads(first: 100, after: $cursor) { "
        "pageInfo { hasNextPage endCursor } "
        "nodes { id isResolved path line "
        "comments(first: 100) { pageInfo { hasNextPage } "
        "nodes { databaseId body author { login __typename } } } } } } } }"
    )

    _RESOLVE_MUTATION = (
        "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) "
        "{ thread { isResolved } } }"
    )

    def pr_comment_reply(self, repo: str, number: int, comment_id: int, body: str) -> str:
        """Reply in the thread rooted at ``comment_id``; returns its url."""
        data = self.raw(
            "POST",
            f"/repos/{repo}/pulls/{number}/comments/{comment_id}/replies",
            {"body": body},
        )
        return str(data.get("html_url", "")) if isinstance(data, dict) else ""

    def pr_issue_comment(self, repo: str, number: int, body: str) -> str:
        """A plain PR-level comment — the fallback for body-only findings."""
        data = self.raw("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})
        return str(data.get("html_url", "")) if isinstance(data, dict) else ""

    def resolve_review_thread(self, thread_id: str) -> bool:
        """Mark a review thread resolved; True when it now is. ``thread_id``
        is the opaque id a :class:`ReviewThread` or :class:`PostedFinding`
        carries — here, the thread's GraphQL node id.

        GraphQL answers a failed mutation with a 200 and an ``errors`` array,
        so the body is the verdict, not the status.
        """
        data = self.raw(
            "POST",
            "/graphql",
            {"query": self._RESOLVE_MUTATION, "variables": {"id": thread_id}},
        )
        if not isinstance(data, dict):
            raise GithubOpsError(f"resolveReviewThread returned a malformed result: {data!r}")
        errors = data.get("errors")
        if errors:
            raise GithubOpsError(f"resolveReviewThread failed: {errors!r}")
        thread = ((data.get("data") or {}).get("resolveReviewThread") or {}).get("thread")
        if not isinstance(thread, dict) or "isResolved" not in thread:
            raise GithubOpsError(f"resolveReviewThread returned no thread: {data!r}")
        return bool(thread["isResolved"])

    def pr_review_threads(self, repo: str, number: int) -> list[ReviewThread]:
        """Every inline review thread on the PR, with its replies, across
        every page of the connection (#614)."""
        owner, _, name = repo.partition("/")
        threads: list[ReviewThread] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            data = self.raw(
                "POST",
                "/graphql",
                {
                    "query": self._THREADS_QUERY,
                    "variables": {"owner": owner, "name": name, "number": number, "cursor": cursor},
                },
            )
            if not isinstance(data, dict):
                raise GithubOpsError(f"reviewThreads returned a malformed result: {data!r}")
            errors = data.get("errors")
            if errors:
                raise GithubOpsError(f"reviewThreads failed: {errors!r}")
            threads.extend(fold_review_threads(data))
            cursor = review_threads_next_cursor(data)
            if cursor is None:
                return threads
        raise PaginationError(
            f"{repo}#{number} has more than {MAX_PAGES * 100} review threads; "
            "the list was not read to its end"
        )

    # Which of the checks on the PR's head GitHub itself would hold the
    # merge for (#674). ``isRequired`` is evaluated against the pull
    # request's base rules — classic protection and rulesets both — and is
    # readable with pull access, unlike classic protection itself (admin
    # only). Field-unverified beyond GitHub's schema: the argument is
    # ``pullRequestNumber`` on both ``CheckRun`` and ``StatusContext``.
    _ROLLUP_QUERY = (
        "query($owner: String!, $name: String!, $number: Int!, $cursor: String) { "
        "repository(owner: $owner, name: $name) { pullRequest(number: $number) { "
        "commits(last: 1) { nodes { commit { oid statusCheckRollup { "
        "contexts(first: 100, after: $cursor) { "
        "pageInfo { hasNextPage endCursor } "
        "nodes { __typename "
        "... on CheckRun { name isRequired(pullRequestNumber: $number) } "
        "... on StatusContext { context isRequired(pullRequestNumber: $number) } "
        "} } } } } } } }"
    )

    def pr_required_checks(self, repo: str, number: int) -> tuple[str, ...]:
        """The checks on the PR's last commit that GitHub marks required
        for this pull request, across every page of the rollup (#674).

        Only what has *reported* on the head appears in a rollup: a
        required check that has not started yet is not in the answer, and
        an empty tuple means "none of what has reported is required" —
        which, before anything reports, is no answer at all. Callers ask
        again as checks arrive.
        """
        owner, _, name = repo.partition("/")
        required: list[str] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            data = self.raw(
                "POST",
                "/graphql",
                {
                    "query": self._ROLLUP_QUERY,
                    "variables": {"owner": owner, "name": name, "number": number, "cursor": cursor},
                },
            )
            if not isinstance(data, dict):
                raise GithubOpsError(f"statusCheckRollup returned a malformed result: {data!r}")
            errors = data.get("errors")
            if errors:
                raise GithubOpsError(f"statusCheckRollup failed: {errors!r}")
            required.extend(fold_required_contexts(data))
            cursor = rollup_next_cursor(data)
            if cursor is None:
                return tuple(dict.fromkeys(required))
        raise PaginationError(
            f"{repo}#{number} has more than {MAX_PAGES * 100} checks on its head; "
            "the required set was not read to its end"
        )

    # -- landing a pull request ---------------------------------------------
    #
    # The last stretch of an autonomous run: take the delivery out of draft,
    # keep it current with its base, and merge it. Same `raw.api` rationale as
    # the review block above — no request shaping the generic transport does
    # not already do, so no new worker op.

    # REST cannot un-draft a pull request; `markPullRequestReadyForReview` is
    # the only path GitHub offers, so this one call is GraphQL. Both worker
    # transports reach it unchanged: `gh api -X POST /graphql --input -` and
    # the stdlib client both POST this body to the same endpoint.
    _READY_MUTATION = (
        "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) "
        "{ pullRequest { isDraft } } }"
    )

    def pr_ready_for_review(self, node_id: str) -> bool:
        """Take a draft PR out of draft; True when it is now ready.

        GraphQL answers a failed mutation with **a 200 status and an ``errors``
        array**, so unlike every other call here the status is not the verdict
        and the body has to be read. Trusting the status would report a PR as
        ready that is still a draft, and a draft cannot be merged — the loop
        would then spend its whole merge budget on a refusal it caused itself.
        """
        data = self.raw(
            "POST", "/graphql", {"query": self._READY_MUTATION, "variables": {"id": node_id}}
        )
        if not isinstance(data, dict):
            raise GithubOpsError(
                f"markPullRequestReadyForReview returned a malformed result: {data!r}"
            )
        errors = data.get("errors")
        if errors:
            raise GithubOpsError(f"markPullRequestReadyForReview failed: {errors!r}")
        result = ((data.get("data") or {}).get("markPullRequestReadyForReview") or {}).get(
            "pullRequest"
        )
        # A mutation that reported no error but also no pull request is not an
        # answer we can act on; fail closed rather than assume it worked.
        if not isinstance(result, dict) or "isDraft" not in result:
            raise GithubOpsError(
                f"markPullRequestReadyForReview returned no pull request: {data!r}"
            )
        return not bool(result["isDraft"])

    def pr_merge(
        self,
        repo: str,
        number: int,
        *,
        method: MergeMethod = "squash",
        sha: str = "",
        title: str = "",
        message: str = "",
    ) -> MergeOutcome:
        """Merge a PR. ``sha`` is the head the caller decided against.

        Sending ``sha`` makes a concurrent push lose the race with a 409
        instead of being merged over: the poll that judged this PR green read
        one head, and anything else on the branch by now has not been judged
        at all. See :class:`MergeOutcome` for why 405 and 409 come back as
        data rather than exceptions.
        """
        body: dict[str, Any] = {"merge_method": method}
        if sha:
            body["sha"] = sha
        if title:
            body["commit_title"] = title
        if message:
            body["commit_message"] = message
        try:
            data = self.raw("PUT", f"/repos/{repo}/pulls/{number}/merge", body)
        except GithubOpsError as exc:
            if exc.http_status == 405:
                return MergeOutcome(False, "", str(exc), blocked=True)
            if exc.http_status == 409:
                return MergeOutcome(False, "", str(exc), stale=True)
            raise
        if not isinstance(data, dict) or not data.get("merged"):
            # A 200 that does not claim a merge is not one. Treat it as
            # blocked: something about the PR said no, and no retry fixes it.
            return MergeOutcome(False, "", f"merge was not confirmed: {data!r}", blocked=True)
        return MergeOutcome(True, str(data.get("sha") or ""), str(data.get("message") or "merged"))

    # A base that merges through a merge queue refuses PUT /merge outright
    # (#676); the queue is entered and read through GraphQL — the REST API
    # has no queue surface. Field-unverified beyond GitHub's schema:
    # ``expectedHeadOid`` on the enqueue input (the same race guard the
    # merge's ``sha`` gives), ``headCommit`` on the entry, and the
    # ``reason`` of a ``RemovedFromMergeQueueEvent``.
    _ENQUEUE_MUTATION = (
        "mutation($id: ID!, $head: GitObjectID) { "
        "enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $head}) { "
        "mergeQueueEntry { id state position headCommit { oid } } } }"
    )
    _QUEUE_QUERY = (
        "query($owner: String!, $name: String!, $number: Int!) { "
        "repository(owner: $owner, name: $name) { pullRequest(number: $number) { "
        "merged state mergeCommit { oid } "
        "mergeQueueEntry { id state position headCommit { oid } } "
        "timelineItems(last: 1, itemTypes: [REMOVED_FROM_MERGE_QUEUE_EVENT]) { "
        "totalCount nodes { ... on RemovedFromMergeQueueEvent { reason } } } "
        "} } }"
    )

    def pr_enqueue(self, node_id: str, *, head: str = "") -> QueueEntry:
        """Add the PR to its base's merge queue; the entry GitHub made.

        ``head`` is the commit the caller judged: GitHub refuses the
        enqueue when the branch has moved past it, the way the merge's
        ``sha`` makes a concurrent push lose the race. A refusal — the PR
        is not mergeable, the queue is not required for its base, the
        branch moved — is a GraphQL ``errors`` array under a 200 and is
        raised as :class:`GithubOpsError` with GitHub's words.
        """
        variables: dict[str, Any] = {"id": node_id}
        if head:
            variables["head"] = head
        data = self.raw(
            "POST", "/graphql", {"query": self._ENQUEUE_MUTATION, "variables": variables}
        )
        if not isinstance(data, dict):
            raise GithubOpsError(f"enqueuePullRequest returned a malformed result: {data!r}")
        errors = data.get("errors")
        if errors:
            raise GithubOpsError(f"enqueuePullRequest failed: {errors!r}")
        entry = fold_queue_entry(
            ((data.get("data") or {}).get("enqueuePullRequest") or {}).get("mergeQueueEntry")
        )
        if entry is None:
            raise GithubOpsError(f"enqueuePullRequest returned no queue entry: {data!r}")
        return entry

    def pr_queue_state(self, repo: str, number: int) -> QueueState:
        """Where the PR stands with its base's merge queue (#676): merged,
        closed, its live entry, and the queue's removals so far."""
        owner, _, name = repo.partition("/")
        data = self.raw(
            "POST",
            "/graphql",
            {
                "query": self._QUEUE_QUERY,
                "variables": {"owner": owner, "name": name, "number": number},
            },
        )
        if not isinstance(data, dict):
            raise GithubOpsError(f"mergeQueueEntry returned a malformed result: {data!r}")
        errors = data.get("errors")
        if errors:
            raise GithubOpsError(f"mergeQueueEntry failed: {errors!r}")
        return fold_queue_state(data)

    def pr_update_branch(self, repo: str, number: int, *, expected_head_sha: str = "") -> bool:
        """Merge the base branch into the PR's branch; True when accepted.

        Needed wherever protection requires branches to be up to date before
        merging. GitHub answers 202 with a message and **not** the new head
        sha, so the caller cannot record what this produced — it has to
        observe the branch on its next poll.
        """
        body: dict[str, Any] = {}
        if expected_head_sha:
            body["expected_head_sha"] = expected_head_sha
        try:
            self.raw("PUT", f"/repos/{repo}/pulls/{number}/update-branch", body)
        except GithubOpsError as exc:
            # 422 is GitHub's answer for "the branch cannot be updated" —
            # already current, or the expected head moved. Neither is worth
            # raising over: the next poll re-reads the PR either way.
            if exc.http_status == 422:
                log.info("gh.update_branch_refused", repo=repo, pr=number, detail=str(exc))
                return False
            raise
        return True

    def branch_delete(self, repo: str, branch: str) -> None:
        """Delete a branch ref, tolerating one that is already gone.

        Best-effort tidying after a merge: a repository with
        ``delete_branch_on_merge`` on has already removed it (404), and a
        protected branch refuses (422). Neither should be reported as a
        failure of the merge that just succeeded.
        """
        # A miss is the outcome wanted (#558): no failed job for it.
        self.raw_lookup("DELETE", f"/repos/{repo}/git/refs/heads/{branch}", missing=(404, 422))

    def contents_read(self, repo: str, path: str, ref: str | None = None) -> str:
        params: dict[str, Any] = {"repo": repo, "path": path}
        if ref:
            params["ref"] = ref
        data = self._op("contents.read", params)
        return str(data.get("content", ""))

    def status_create(
        self,
        repo: str,
        sha: str,
        state: str,
        *,
        context: str = "sbxloop",
        description: str = "",
        target_url: str = "",
    ) -> None:
        params: dict[str, Any] = {"repo": repo, "sha": sha, "state": state, "context": context}
        if description:
            params["description"] = description
        if target_url:
            params["target_url"] = target_url
        self._op("status.create", params)

    def repo_get(self, repo: str) -> dict[str, Any]:
        data = self._op("repo.get", {"repo": repo})
        assert isinstance(data, dict)
        return data

    def default_branch(self, repo: str) -> str:
        """The branch GitHub reports as the repository's default.

        The one place the loop learns which branch a repository lives on
        when no ``deliver_base`` is configured. There is no guess when the
        field is missing: a ``main`` assumed against a ``master`` or
        ``develop`` repository would deliver, merge from and gate against
        a branch that does not exist (#672).
        """
        name = self.repo_get(repo).get("default_branch")
        if not isinstance(name, str) or not name:
            raise GithubOpsError(
                f"GitHub did not report a default branch for {repo}; "
                "set [github] deliver_base to name the branch to deliver against"
            )
        return name

    def repo_lookup(self, repo: str) -> dict[str, Any] | None:
        """Probe a repository: its data, or None when it does not exist.

        The miss travels as data (``allow_missing``) rather than as a failed
        job, so an expected "no" never raises the worker's error event and
        never paints a red panel in the transcript (#222).
        """
        data = self._op("repo.get", {"repo": repo, "allow_missing": True})
        assert isinstance(data, dict)
        return None if data.get("missing") else data

    def ref_lookup(self, repo: str, ref: str) -> str | None:
        """Resolve ``ref`` (e.g. ``heads/main``) to a commit sha, or None
        when there is no such ref — including the empty-repository case
        GitHub reports as 409 rather than 404. Same rationale as
        :meth:`repo_lookup`: the miss is an answer, not an error."""
        data = self._op("ref.get", {"repo": repo, "ref": ref, "allow_missing": True})
        if not isinstance(data, dict):
            raise GithubOpsError(f"ref.get returned a malformed result: {data!r}")
        if data.get("missing"):
            return None
        sha = data.get("sha")
        if not sha:
            raise GithubOpsError(f"ref.get returned no sha for {ref!r}: {data!r}")
        return str(sha)

    def label_lookup(self, repo: str, name: str) -> dict[str, Any] | None:
        """Probe one repository label: its data, or None when the repository
        does not carry it.

        Same rationale as :meth:`ref_lookup`: "no such label" is the routine
        answer to an existence question (#556), so it travels as data rather
        than as a failed job that would paint a red panel in the run's
        chronology. Anything other than a 404 — a 403 from a token without
        repo scope, a 5xx — still raises."""
        data = self._op("label.get", {"repo": repo, "name": name, "allow_missing": True})
        if not isinstance(data, dict):
            raise GithubOpsError(f"label.get returned a malformed result: {data!r}")
        return None if data.get("missing") else data

    def search_issues(self, query: str, per_page: int = 30) -> list[dict[str, Any]]:
        data = self._op("search.issues", {"query": query, "per_page": per_page})
        return data if isinstance(data, list) else []

    # -- named operations over the generic transport ------------------------
    #
    # Everything below the review block is one REST call (or one paged
    # walk) with its path, verb and body fixed here, so no other module
    # spells a GitHub path. Each returns the shape its callers read and
    # raises :class:`MalformedResponse` when GitHub's answer is not that
    # shape; a status GitHub reports still arrives as a plain
    # :class:`GithubOpsError` with ``http_status`` set.

    @staticmethod
    def _dict(what: str, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise MalformedResponse(what, data)
        return data

    @staticmethod
    def _list(what: str, data: Any) -> list[Any]:
        if not isinstance(data, list):
            raise MalformedResponse(what, data)
        return data

    def rate_limit(self) -> dict[str, Any]:
        """The credential's rate-limit budget (``GET /rate_limit``) — the
        cheapest authenticated read there is, which is why the daemon's
        health check makes it."""
        return self._dict("GET /rate_limit", self.raw("GET", "/rate_limit"))

    def authenticated_user(self) -> dict[str, Any]:
        """The credential's own account (``GET /user``): its ``login`` and
        ``type``. A GitHub App installation token gets 403 here."""
        return self._dict("GET /user", self.raw("GET", "/user"))

    def repo_create(
        self, repo: str, *, private: bool = True, for_user: bool = False
    ) -> dict[str, Any]:
        """Create ``owner/name`` with an initial commit, under the
        credential's own account (``for_user``) or the ``owner``
        organization; the repository payload GitHub answers with."""
        owner, name = repo.split("/", 1)
        body = {"name": name, "private": private, "auto_init": True}
        path = "/user/repos" if for_user else f"/orgs/{owner}/repos"
        return self._dict(f"POST {path}", self.raw("POST", path, body))

    def compare_lookup(self, repo: str, base: str, head: str) -> dict[str, Any] | None:
        """GitHub's comparison of ``base`` with ``head`` (its
        ``merge_base_commit`` among other things), or None when GitHub
        answers 404 — unrelated histories, or a base the token cannot
        see; the caller tells those apart."""
        data = self.raw_lookup("GET", f"/repos/{repo}/compare/{base}...{head}")
        if data is None:
            return None
        return self._dict(f"GET /repos/{repo}/compare", data)

    # -- issues ---------------------------------------------------------------

    def issue_get(self, repo: str, number: int | str) -> dict[str, Any]:
        """The issue (or pull request, which the issues API also serves):
        state, labels, title, body."""
        path = f"/repos/{repo}/issues/{number}"
        return self._dict(f"GET {path}", self.raw("GET", path))

    def issue_comments(self, repo: str, number: int | str) -> list[Any]:
        """Every comment on the issue, oldest first, across every page."""
        return raw_pages(self, f"/repos/{repo}/issues/{number}/comments")

    def issue_events(self, repo: str, number: int | str) -> list[Any]:
        """The issue's timeline events (labeled, closed, ...), across every
        page."""
        return raw_pages(self, f"/repos/{repo}/issues/{number}/events")

    def issues_list(
        self,
        repo: str,
        *,
        state: str = "open",
        labels: Sequence[str] = (),
        per_page: int = PAGE_SIZE,
        page: int = 1,
        sort: str = "",
        direction: str = "",
    ) -> list[Any]:
        """One page of the repository's issues — pull requests included, as
        the endpoint lists them — filtered by ``state`` (``open``,
        ``closed``, ``all``) and, when given, ``labels`` (all of them)."""
        query = f"state={state}&per_page={per_page}"
        if sort:
            query += f"&sort={sort}"
        if direction:
            query += f"&direction={direction}"
        if labels:
            query += f"&labels={quote(','.join(labels), safe='')}"
        query += f"&page={page}"
        path = f"/repos/{repo}/issues?{query}"
        return self._list(f"GET /repos/{repo}/issues", self.raw("GET", path))

    def issue_search(self, query: str, *, per_page: int) -> dict[str, Any]:
        """The search API's answer to ``query``: ``items``, ``total_count``
        and ``incomplete_results`` — the caller judges whether the answer
        is whole."""
        path = "/search/issues?" + urlencode({"q": query, "per_page": per_page})
        return self._dict("GET /search/issues", self.raw("GET", path))

    def label_create(self, repo: str, *, name: str, color: str, description: str) -> dict[str, Any]:
        """Create a repository label; one that exists is GitHub's 422,
        raised for the caller to read."""
        path = f"/repos/{repo}/labels"
        return self._dict(
            f"POST {path}",
            self.raw("POST", path, {"name": name, "color": color, "description": description}),
        )

    def labels_list(self, repo: str) -> list[Any]:
        """Every label the repository carries, across every page."""
        return raw_pages(self, f"/repos/{repo}/labels")

    def issue_labels_add(self, repo: str, number: int | str, labels: Sequence[str]) -> None:
        """Put ``labels`` on the issue or pull request (existing ones stay)."""
        self.raw("POST", f"/repos/{repo}/issues/{number}/labels", {"labels": list(labels)})

    def issue_label_remove(self, repo: str, number: int | str, label: str) -> None:
        """Take ``label`` off the issue; one that is not there is a success,
        not a failed job (#558)."""
        self.raw_lookup("DELETE", f"/repos/{repo}/issues/{number}/labels/{quote(label, safe='')}")

    def issue_close(
        self, repo: str, number: int | str, *, reason: CloseReason = "completed"
    ) -> None:
        """Close the issue with ``reason``, which GitHub takes verbatim as
        its ``state_reason``; closing a closed issue is a no-op success."""
        self.raw(
            "PATCH",
            f"/repos/{repo}/issues/{number}",
            {"state": "closed", "state_reason": reason},
        )

    def issue_comment_delete(
        self, repo: str, comment_id: int, *, number: int | None = None
    ) -> None:
        """Delete one issue comment by its id. ``number`` is the issue it is
        on, which a forge that addresses comments under their issue needs
        (#1017); GitHub addresses them by id alone."""
        self.raw("DELETE", f"/repos/{repo}/issues/comments/{comment_id}")

    # -- pull requests -------------------------------------------------------

    def pr_list_open(self, repo: str, *, head: str) -> list[Any]:
        """The open pull requests whose head is the branch ``head`` of
        ``repo``'s owner — none, or the one a re-delivery refreshes."""
        owner = repo.split("/", 1)[0]
        data = self.raw("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{head}")
        return data if isinstance(data, list) else []

    def pr_update(
        self, repo: str, number: int, *, title: str | None = None, body: str | None = None
    ) -> dict[str, Any]:
        """Change the pull request's title and/or body; the PR as it now is."""
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = title
        if body is not None:
            fields["body"] = body
        path = f"/repos/{repo}/pulls/{number}"
        return self._dict(f"PATCH {path}", self.raw("PATCH", path, fields))

    def pr_files(self, repo: str, number: int) -> list[Any]:
        """The files the pull request changes, with their patches, across
        every page."""
        return raw_pages(self, f"/repos/{repo}/pulls/{number}/files")

    def pr_reviews(self, repo: str, number: int) -> list[Any]:
        """Every review submitted on the pull request, across every page."""
        return raw_pages(self, f"/repos/{repo}/pulls/{number}/reviews")

    def pr_review_comments(self, repo: str, number: int) -> list[Any]:
        """Every inline review comment on the pull request, across every
        page."""
        return raw_pages(self, f"/repos/{repo}/pulls/{number}/comments")

    def check_runs(self, repo: str, sha: str) -> list[Any]:
        """The check runs reported on ``sha``, across every page — the
        entries :func:`fold_check_runs` folds."""
        return raw_pages(self, f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs")

    # -- the git data API (a commit without a checkout) ---------------------

    def commit_get(self, repo: str, sha: str) -> dict[str, Any]:
        """The commit object behind ``sha``: its ``tree``, parents, message."""
        path = f"/repos/{repo}/git/commits/{sha}"
        return self._dict(f"GET {path}", self.raw("GET", path))

    def tree_create(
        self, repo: str, *, base_tree: str, entries: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Create a tree from ``entries`` on top of ``base_tree``; the tree
        object, whose ``sha`` the caller commits."""
        path = f"/repos/{repo}/git/trees"
        return self._dict(
            f"POST {path}", self.raw("POST", path, {"base_tree": base_tree, "tree": entries})
        )

    def commit_create(
        self, repo: str, *, message: str, tree: str, parents: list[str]
    ) -> dict[str, Any]:
        """Create a commit of ``tree`` with ``parents``; the commit object."""
        path = f"/repos/{repo}/git/commits"
        return self._dict(
            f"POST {path}",
            self.raw("POST", path, {"message": message, "tree": tree, "parents": parents}),
        )

    def ref_create(self, repo: str, ref: str, sha: str) -> None:
        """Create ``refs/heads/...`` (the full ref) at ``sha``. A ref that
        already exists is GitHub's 422, raised for the caller to read."""
        self.raw("POST", f"/repos/{repo}/git/refs", {"ref": ref, "sha": sha})

    def ref_force_update(self, repo: str, branch: str, sha: str) -> None:
        """Move ``branch`` to ``sha``, discarding whatever it pointed at."""
        self.raw("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": sha, "force": True})

    def contents_put(
        self, repo: str, path: str, *, message: str, content_b64: str, branch: str
    ) -> dict[str, Any]:
        """Create or replace one file on ``branch`` through the contents API
        — the one write that works on a repository with no commit yet."""
        target = f"/repos/{repo}/contents/{path}"
        return self._dict(
            f"PUT {target}",
            self.raw("PUT", target, {"message": message, "content": content_b64, "branch": branch}),
        )

    # -- what a credential may do, and what the repository runs --------------

    def permission_probe(self, permission: str, repo: str, base: str) -> bool | None:
        """Whether a fine-grained token can make :data:`READ_PROBES`'s read
        for ``permission`` (#696): False on 401/403, True on any other
        answer (an empty list, a 404 on an empty repository, a 422 all mean
        the permission is there), None when the probe needs a ``base`` the
        repository does not have yet."""
        template = READ_PROBES[permission]
        if "{base}" in template and not base:
            return None
        try:
            self.raw("GET", template.format(repo=repo, base=base))
        except GithubOpsError as exc:
            if exc.http_status in (401, 403):
                return False
        return True

    def workflows_list(self, repo: str) -> list[Any]:
        """The repository's Actions workflows (``state`` says whether each
        is active); the first hundred."""
        data = self.raw("GET", f"/repos/{repo}/actions/workflows?per_page=100")
        workflows = data.get("workflows") if isinstance(data, dict) else None
        return self._list(f"GET /repos/{repo}/actions/workflows", workflows)

    def workflow_runs(self, repo: str, *, branch: str, per_page: int = 1) -> list[Any]:
        """The latest Actions runs on ``branch``, newest first."""
        data = self.raw("GET", f"/repos/{repo}/actions/runs?branch={branch}&per_page={per_page}")
        runs = data.get("workflow_runs") if isinstance(data, dict) else None
        return self._list(f"GET /repos/{repo}/actions/runs", runs)

    def base_requirements(self, repo: str, base: str) -> BaseRequirements:
        """What ``base`` requires before a merge, read from classic
        protection and rulesets (:func:`read_base_requirements`); never
        raises — an unreadable source leaves its half ``unknown``."""
        return read_base_requirements(self, repo, base)

    # -- what this backend can do --------------------------------------------

    # GitHub does everything the roles rely on. The one it cannot answer
    # for itself: commits created through the API arrive signed only when
    # the credential is a GitHub App, and the credential's kind is the
    # provisioner's knowledge, not the transport's — so it is UNKNOWN here
    # and the doctor, which knows the credential, says.
    CAPABILITIES: ClassVar[dict[str, Capability]] = {
        "merge_queue": Capability.SUPPORTED,
        "review_threads": Capability.SUPPORTED,
        "draft_changes": Capability.SUPPORTED,
        "request_changes_review": Capability.SUPPORTED,
        "short_lived_token": Capability.SUPPORTED,
        "remote_commit": Capability.SUPPORTED,
        "required_checks_introspection": Capability.SUPPORTED,
        "bot_identity": Capability.SUPPORTED,
        "signed_api_commits": Capability.UNKNOWN,
    }

    def capabilities(self) -> dict[str, Capability]:
        """One :class:`Capability` per name in
        :data:`sbxloop.vcs.protocol.CAPABILITIES`. A ``merge_queue`` here
        means the forge has one; whether *this* base uses it is
        :attr:`~sbxloop.vcs.model.BaseRequirements.merge_queue`."""
        return dict(self.CAPABILITIES)

    # -- the generic transport ------------------------------------------------
    #
    # Private to this package: every path GitHub is asked for is spelt in a
    # named operation above (or in ``gh/protection.py`` / ``gh/labels.py``),
    # never by a caller. ``tests/unit/test_gh_raw_is_private.py`` holds the
    # line.

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        params: dict[str, Any] = {"method": method, "path": path}
        if body is not None:
            params["body"] = body
        return self._op("raw.api", params)

    def raw_lookup(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        missing: Sequence[int] = (404,),
    ) -> Any:
        """A REST call whose "no" is an answer (#558): a response with a
        status in ``missing`` comes back as ``None``, and — the point —
        never as a failed worker job, so an existence probe costs no red
        chronology panel and no daemon WARNING. The miss travels as data
        from the worker (``allow_missing_statuses``); a worker from before
        that raises as it always did and the status is read off the error
        here, so the answer is the same either way.
        """
        params: dict[str, Any] = {
            "method": method,
            "path": path,
            "allow_missing_statuses": [int(s) for s in missing],
        }
        if body is not None:
            params["body"] = body
        try:
            data = self._op("raw.api", params)
        except GithubOpsError as exc:
            if exc.http_status in missing:
                return None
            raise
        if isinstance(data, dict) and data.get("missing") is True:
            return None
        return data

    def token_scopes(self) -> tuple[str, ...] | None:
        """The credential's classic OAuth scopes (``repo``, ``workflow``,
        ...), or ``None`` for a token that has none to report — a
        fine-grained PAT or an App installation token, whose permissions
        are per-resource (#696)."""
        result = self._op("token.scopes", {})
        scopes = result.get("scopes") if isinstance(result, dict) else None
        if scopes is None:
            return None
        return tuple(str(s) for s in scopes)

    # Extra seconds of job timeout granted per file in a blob batch: the
    # batch job makes one REST call per file, so the flat per-op timeout
    # would starve large manifests.
    BLOB_BATCH_TIMEOUT_PER_FILE_S = 2.0

    def blobs_create_many(self, repo: str, files: list[dict[str, str]]) -> dict[str, str]:
        """Create git blobs for a manifest of {path, content_b64} entries in
        one worker job; returns path -> blob sha."""
        data = self._op(
            "blobs.create_many",
            {"repo": repo, "files": files},
            timeout_s=self.timeout_s + self.BLOB_BATCH_TIMEOUT_PER_FILE_S * len(files),
        )
        blobs = data.get("blobs") if isinstance(data, dict) else None
        if not isinstance(blobs, list):
            raise GithubOpsError(f"blobs.create_many returned no blob list: {data!r}")
        shas: dict[str, str] = {}
        for blob in blobs:
            if not isinstance(blob, dict) or not blob.get("path") or not blob.get("sha"):
                raise GithubOpsError(f"blobs.create_many returned a malformed entry: {blob!r}")
            shas[str(blob["path"])] = str(blob["sha"])
        return shas


def _implements(ops: GithubOps) -> VcsOps:
    """The type checker's proof that :class:`GithubOps` satisfies every role
    in :mod:`sbxloop.vcs.protocol`: a signature that drifts from its role
    fails here, in ``mypy``, before a consumer annotated with the role can
    be handed something that does not answer it."""
    return ops
