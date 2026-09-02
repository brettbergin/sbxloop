"""Typed facade over github.op jobs running in the github-ops sandbox.

The host never talks to GitHub with the user PAT directly — every operation
becomes a ``github.op`` JobRequest submitted to the github sandbox, which is
the only environment holding ``GH_TOKEN``.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel

from sbxloop.config import MergeMethod
from sbxloop.errors import GithubOpsError
from sbxloop.ids import new_job_id
from sbxloop.log import get_logger
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import JobRequest

log = get_logger(__name__)


class IssueRef(BaseModel):
    number: int
    url: str


class PrRef(BaseModel):
    number: int
    url: str


# What a review says about a PR. REQUEST_CHANGES/APPROVE are the ones that
# carry weight: under branch protection they gate the merge, so "the review
# was accepted" becomes a state GitHub enforces rather than one sbxloop only
# tracks. COMMENT is the degraded mode for an identity the repo will not
# accept as a reviewer.
ReviewEvent = Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]

# Folded verdict of a head commit's check runs.
CheckState = Literal["pending", "red", "green"]


class MergeOutcome(NamedTuple):
    """The result of asking GitHub to merge a PR.

    ``blocked`` is the one refusal that is an *answer* rather than an error:
    GitHub says 405 for every "this PR is not mergeable right now" — a draft,
    a failing required check, a protection rule wanting an approval this
    identity cannot give. None of those is fixable by retrying, so the caller
    hands the PR to a human instead of spinning.

    ``stale`` is 409: the head moved between the poll that decided to merge
    and the merge itself. That is a race, not a refusal, and the next poll
    re-decides against the new head.
    """

    merged: bool
    sha: str
    reason: str
    blocked: bool = False
    stale: bool = False


class ReviewComment(BaseModel):
    """One inline comment, anchored to a line of the PR's diff."""

    path: str
    line: int
    body: str
    # RIGHT is the post-change side; a comment on a deleted line needs LEFT.
    side: Literal["LEFT", "RIGHT"] = "RIGHT"


class PostedFinding(NamedTuple):
    """Where one review finding actually landed on the PR.

    ``anchor`` is the ``path:line`` key the engine carries across rounds.
    ``comment_id`` is the REST id of the inline comment that anchors the
    finding's thread — ``None`` when the finding was posted in the review
    body instead (anchor refused by GitHub, cap overflow, or no line at
    all), which is exactly the case a later reconciliation pass must fall
    back to a plain PR comment for. ``thread_node_id`` is the GraphQL node
    id of that comment's review thread, needed to resolve it; ``None`` when
    there is no inline comment or the lookup could not answer.
    """

    anchor: str
    comment_id: int | None = None
    thread_node_id: str | None = None


def normalize_login(login: str) -> str:
    """One canonical form for a GitHub identity.

    GraphQL reports an App actor as its bare slug (``sbxloop``) while REST
    attributes the same actor as ``sbxloop[bot]`` — the two spellings must
    compare equal, or the loop misreads its own review threads as a
    human's (field failure r9t8hnv33: fully reconciled PRs ended blocked
    on "human review threads have no reply", with the loop ack-replying to
    its own findings). Logins are case-insensitive on GitHub, so casefold
    too.
    """
    return login.removesuffix("[bot]").casefold()


def logins_match(a: str, b: str) -> bool:
    """Whether two login spellings name the same identity. Two empty
    logins never match: an unknown identity equals nobody."""
    return bool(a) and bool(b) and normalize_login(a) == normalize_login(b)


def is_bot_user(user: Any) -> bool:
    """Whether a REST ``user`` object is a GitHub App (``type == "Bot"``).

    Carried alongside the login (#613, #622) so a reviewer's kind is a
    fact read from GitHub, not a guess from a ``[bot]`` suffix — a human
    ``foo`` and an App ``foo[bot]`` are different accounts.
    """
    return isinstance(user, dict) and str(user.get("type") or "") == "Bot"


class ThreadComment(NamedTuple):
    """One comment inside a review thread."""

    comment_id: int | None
    login: str
    body: str
    # The author is a GitHub App (GraphQL ``author.__typename == "Bot"``).
    is_bot: bool = False


class ReviewThread(NamedTuple):
    """An inline review thread as it stands on the PR right now.

    Read for idempotency: a reconciliation pass skips a thread that already
    carries its own reply.
    """

    node_id: str
    is_resolved: bool
    path: str
    line: int | None
    comments: tuple[ThreadComment, ...] = ()

    @property
    def anchor(self) -> str:
        return f"{self.path}:{self.line}" if self.line is not None else self.path

    @property
    def root_comment_id(self) -> int | None:
        return self.comments[0].comment_id if self.comments else None

    @property
    def opened_by_bot(self) -> bool:
        return bool(self.comments) and self.comments[0].is_bot

    def has_reply_from(self, login: str) -> bool:
        return any(logins_match(c.login, login) for c in self.comments[1:])

    def has_reply_marked(self, marker: str) -> bool:
        return any(marker in c.body for c in self.comments[1:])


class SubmittedReview(NamedTuple):
    """A posted review: its url, and the event GitHub actually accepted.

    ``event`` is not necessarily the one requested — see
    :meth:`GithubOps.pr_review_create`.

    ``review_id`` and ``posted`` are the thread identity a later round needs
    to reply on a finding rather than restate it in a fresh review body.
    """

    url: str
    event: ReviewEvent
    review_id: int | None = None
    posted: tuple[PostedFinding, ...] = ()

    @property
    def gates_merge(self) -> bool:
        """Whether this review can hold the merge. A COMMENT cannot."""
        return self.event in ("APPROVE", "REQUEST_CHANGES")

    @property
    def inline(self) -> tuple[PostedFinding, ...]:
        """Findings that got their own thread."""
        return tuple(p for p in self.posted if p.comment_id is not None)

    @property
    def body_only(self) -> tuple[PostedFinding, ...]:
        """Findings that ended up in the review body, with no thread."""
        return tuple(p for p in self.posted if p.comment_id is None)


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


class ChecksVerdict(NamedTuple):
    """Every check run and commit status on a head commit, folded to one
    answer.

    ``pending`` is deliberately distinct from ``green``: a PR whose checks
    have not reported yet has not passed, and reading "no failures so far"
    as success is exactly how a red PR gets settled as done.

    Check runs (the Checks API — GitHub Actions and most modern apps) and
    commit statuses (the older Status API — Jenkins, Buildkite, Travis,
    CircleCI's default, Codecov, many org bots) are two namespaces GitHub
    keeps separate and the merge box shows together; the verdict merges
    them the same way (#610). Names are the check-run ``name`` or the
    status ``context``, untagged, so a required-context list from branch
    protection (which names both kinds the same way) can be matched
    against them.
    """

    state: CheckState
    total: int
    pending: tuple[str, ...]
    failed: tuple[str, ...]
    # The names that passed, so a required context that has not reported
    # at all can be told from one that reported green (#611).
    passed: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return (*self.failed, *self.pending, *self.passed)

    def merge(self, other: ChecksVerdict) -> ChecksVerdict:
        """Both verdicts as one: red beats pending beats green, names and
        counts pooled."""
        pending = (*self.pending, *other.pending)
        failed = (*self.failed, *other.failed)
        passed = (*self.passed, *other.passed)
        state: CheckState = "red" if failed else ("pending" if pending else "green")
        return ChecksVerdict(state, self.total + other.total, pending, failed, passed)

    def summary(self) -> str:
        if self.state == "green":
            return f"all {self.total} check(s) passed"
        if self.state == "pending":
            return f"{len(self.pending)} of {self.total} check(s) still running"
        return f"{len(self.failed)} of {self.total} check(s) failed: {', '.join(self.failed)}"


class FailedCheck(NamedTuple):
    """One red check run or commit status, with the text that explains it.

    ``excerpt`` is the job log (head+tail clipped) for a GitHub Actions
    check, the check's own title/summary/text for another check run, or
    the one-line ``description`` a commit status carries — what a fix round
    reads to learn *why* the build is red, not just that it is. ``url`` is
    the check's ``details_url`` / the status's ``target_url``: when the
    excerpt is empty (a status, or logs the token cannot read) it is the
    only lead the brief has (#629).
    """

    name: str
    conclusion: str
    excerpt: str
    url: str


# Check-run conclusions that are not failures. ``neutral`` and ``skipped``
# are deliberately included: a skipped job is not a red build, and treating
# it as one would wedge the fix loop against something no commit can change.
# Everything else — failure, timed_out, cancelled, action_required, stale, or
# a conclusion GitHub adds later — counts as failed. Unknown conclusions fail
# closed: a check nobody understands must not read as permission to merge.
PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


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
    for run in runs:
        if not isinstance(run, dict):
            continue
        name = str(run.get("name") or "check")
        conclusion = run.get("conclusion")
        if conclusion is None:
            pending.append(name)
        elif str(conclusion).lower() not in PASSING_CONCLUSIONS:
            failed.append(name)
        else:
            passed.append(name)
    return _verdict(len(runs), pending, failed, passed)


def _verdict(total: int, pending: list[str], failed: list[str], passed: list[str]) -> ChecksVerdict:
    if failed:
        # Red beats pending: the build is already known broken, and waiting
        # on the stragglers only delays the fix.
        return ChecksVerdict("red", total, tuple(pending), tuple(failed), tuple(passed))
    if pending:
        return ChecksVerdict("pending", total, tuple(pending), (), tuple(passed))
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


def fold_reviews(payload: Any, *, login: str | None = None) -> str:
    """``GET /repos/{repo}/pulls/{n}/reviews`` folded to one state.

    GitHub keeps every review ever submitted, so only each reviewer's
    *latest* verdict counts — an APPROVE after a REQUEST_CHANGES clears it.
    ``COMMENT`` reviews never change a reviewer's standing verdict (GitHub's
    own rule) and are skipped. ``login`` narrows the fold to one reviewer,
    which is how the loop asks "did *my* review get satisfied?" without a
    human's approval answering on its behalf.

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
        if login is not None and not logins_match(who, login):
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
                    is_bot=str(author.get("__typename") or "") == "Bot",
                )
            )
        threads.append(
            ReviewThread(
                node_id=node_id,
                is_resolved=bool(node.get("isResolved")),
                path=str(node.get("path") or ""),
                line=int(raw_line) if isinstance(raw_line, int) else None,
                comments=tuple(comments),
            )
        )
    return threads


class GithubOps:
    def __init__(
        self,
        client: WorkerClient,
        run_id: str,
        *,
        timeout_s: float = 120.0,
    ) -> None:
        self.client = client
        self.run_id = run_id
        self.timeout_s = timeout_s

    def _op(self, op: str, params: dict[str, Any], *, timeout_s: float | None = None) -> Any:
        job = JobRequest(
            job_id=new_job_id(),
            run_id=self.run_id,
            kind="github.op",
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
        try:
            data = self.raw("GET", f"/repos/{repo}/compare/{base}...{head}")
        except GithubOpsError as exc:
            if exc.http_status == 404:
                return None
            raise
        merge_base = data.get("merge_base_commit") if isinstance(data, dict) else None
        sha = merge_base.get("sha") if isinstance(merge_base, dict) else None
        return str(sha) if sha else None

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
        self, repo: str, number: int, *, exclude_login: str | None = None, clip: int = 6000
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

        reviews = raw_pages(self, f"/repos/{repo}/pulls/{number}/reviews")
        latest: dict[str, dict[str, Any]] = {}
        for review in reviews:
            if not isinstance(review, dict):
                continue
            login = login_of(review)
            if exclude_login is not None and logins_match(login, exclude_login):
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
            if exclude_login is not None and logins_match(login_of(comment), exclude_login):
                continue
            body = str(comment.get("body") or "").strip()
            if not body:
                continue
            path = str(comment.get("path") or "")
            line = comment.get("line") or comment.get("original_line")
            anchor = f"`{path}:{line}`: " if path and line else f"`{path}`: " if path else ""
            parts.append(f"- {anchor}{body}")
        return "\n\n".join(parts)[:clip]

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
                            threads_by_comment[comment.comment_id] = thread.node_id
            except GithubOpsError as exc:
                log.warning("gh.review_threads_read_failed", repo=repo, pr=number, error=str(exc))
        posted: list[PostedFinding] = []
        for anchor in wanted:
            comment_id = by_anchor.get(anchor)
            posted.append(
                PostedFinding(
                    anchor=anchor,
                    comment_id=comment_id,
                    thread_node_id=(
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
                            threads_by_comment[entry.comment_id] = thread.node_id
            except GithubOpsError as exc:
                log.warning("gh.review_threads_read_failed", repo=repo, pr=number, error=str(exc))
        return tuple(
            PostedFinding(
                anchor=anchor_of(c),
                comment_id=by_anchor.get(anchor_of(c)),
                thread_node_id=threads_by_comment.get(by_anchor[anchor_of(c)])
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

    def resolve_review_thread(self, thread_node_id: str) -> bool:
        """Mark a review thread resolved; True when it now is.

        GraphQL answers a failed mutation with a 200 and an ``errors`` array,
        so the body is the verdict, not the status.
        """
        data = self.raw(
            "POST",
            "/graphql",
            {"query": self._RESOLVE_MUTATION, "variables": {"id": thread_node_id}},
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
        try:
            self.raw("DELETE", f"/repos/{repo}/git/refs/heads/{branch}")
        except GithubOpsError as exc:
            if exc.http_status in (404, 422):
                log.debug("gh.branch_already_gone", repo=repo, branch=branch, detail=str(exc))
                return
            raise

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

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        params: dict[str, Any] = {"method": method, "path": path}
        if body is not None:
            params["body"] = body
        return self._op("raw.api", params)

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
