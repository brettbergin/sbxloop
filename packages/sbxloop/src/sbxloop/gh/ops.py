"""Typed facade over github.op jobs running in the github-ops sandbox.

The host never talks to GitHub with the user PAT directly — every operation
becomes a ``github.op`` JobRequest submitted to the github sandbox, which is
the only environment holding ``GH_TOKEN``.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, field_validator

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


# What a PR PATCH answered with. Deliberately laxer than PrRef: a PATCH
# succeeds on a closed PR and on a number that has drifted to an unrelated
# PR, so the caller has to inspect `state`/`head_ref` before trusting it —
# and a 200 that omits `html_url` must not turn a completed run into a
# crashed one, so `url` tolerates a missing value instead of raising (#488).
class PrUpdate(BaseModel):
    number: int
    url: str = ""
    state: str = ""
    head_ref: str = ""

    @field_validator("url", "state", "head_ref", mode="before")
    @classmethod
    def _blank_when_missing(cls, value: object) -> object:
        return "" if value is None else value


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


class SubmittedReview(NamedTuple):
    """A posted review: its url, and the event GitHub actually accepted.

    ``event`` is not necessarily the one requested — see
    :meth:`GithubOps.pr_review_create`.
    """

    url: str
    event: ReviewEvent

    @property
    def gates_merge(self) -> bool:
        """Whether this review can hold the merge. A COMMENT cannot."""
        return self.event in ("APPROVE", "REQUEST_CHANGES")


class ChecksVerdict(NamedTuple):
    """Every check run on a head commit, folded to one answer.

    ``pending`` is deliberately distinct from ``green``: a PR whose checks
    have not reported yet has not passed, and reading "no failures so far"
    as success is exactly how a red PR gets settled as done.
    """

    state: CheckState
    total: int
    pending: tuple[str, ...]
    failed: tuple[str, ...]

    def summary(self) -> str:
        if self.state == "green":
            return f"all {self.total} check(s) passed"
        if self.state == "pending":
            return f"{len(self.pending)} of {self.total} check(s) still running"
        return f"{len(self.failed)} of {self.total} check(s) failed: {', '.join(self.failed)}"


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
    for run in runs:
        if not isinstance(run, dict):
            continue
        name = str(run.get("name") or "check")
        conclusion = run.get("conclusion")
        if conclusion is None:
            pending.append(name)
        elif str(conclusion).lower() not in PASSING_CONCLUSIONS:
            failed.append(name)
    total = len(runs)
    if failed:
        # Red beats pending: the build is already known broken, and waiting
        # on the stragglers only delays the fix.
        return ChecksVerdict("red", total, tuple(pending), tuple(failed))
    if pending:
        return ChecksVerdict("pending", total, tuple(pending), ())
    return ChecksVerdict("green", total, (), ())


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
        if login is not None and who != login:
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

    def pr_update(
        self,
        repo: str,
        number: int,
        *,
        title: str = "",
        body: str = "",
        base: str = "",
        state: str = "",
    ) -> PrUpdate:
        """Update a PR we already know the number of (#488).

        Empty fields are dropped rather than sent, so a caller refreshing
        only the body cannot accidentally clear the title (and, as a
        consequence, this method cannot blank a field out).

        Returns a :class:`PrUpdate`, not a :class:`PrRef`: GitHub answers
        200 for a title/body edit on a *closed* PR, so the caller must check
        ``state``/``head_ref`` before believing the number it passed still
        names the PR it meant.
        """
        params: dict[str, Any] = {"repo": repo, "number": number}
        if title:
            params["title"] = title
        if body:
            params["body"] = body
        if base:
            params["base"] = base
        if state:
            params["state"] = state
        return PrUpdate.model_validate(self._op("pr.update", params))

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
        """Every check run on ``sha``, folded to one verdict."""
        return fold_check_runs(self.raw("GET", f"/repos/{repo}/commits/{sha}/check-runs"))

    def pr_review_state(self, repo: str, number: int, *, login: str | None = None) -> str:
        """``APPROVED`` / ``CHANGES_REQUESTED`` / ``NONE`` — each reviewer's
        latest verdict only. ``login`` narrows it to one reviewer."""
        return fold_reviews(self.raw("GET", f"/repos/{repo}/pulls/{number}/reviews"), login=login)

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
            return SubmittedReview(url, kind)

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
