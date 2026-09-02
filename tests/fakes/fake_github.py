"""A scripted stand-in for :class:`sbxloop.gh.ops.GithubOps`.

The engine's ``github_ops`` seam accepts a factory returning any GithubOps;
this one answers every call the pipeline makes from in-memory state and
records what it was asked, so a whole run — delivery, review, CI, landing —
is scripted without a network or a github sandbox worker.

Delivery (``deliver_workspace``) is answered the way the git data API would:
each ``git/commits`` POST mints a new commit sha and each refs POST/PATCH
moves the PR head to it, so ``pr_get`` reports the latest delivered head
and a re-delivery collides on the existing branch (422) and PR (422)
exactly as GitHub does. Everything else is a knob:

- ``pr``: what ``pr_get`` returns (draft/state/merged/mergeable/head...).
- ``checks``: one ``ChecksVerdict`` per ``pr_checks`` call; the last one
  repeats, and an empty list means green.
- ``failed_logs``, ``feedback``, ``reviews_payload``, ``comments_payload``:
  answers for ``checks_failed_logs``, ``pr_review_feedback`` and the raw
  review/comment reads ``human_objection`` makes.
- ``undraft_ok``, ``update_ok``: whether un-drafting / update-branch take.
- ``merge_outcomes``: one ``MergeOutcome`` per ``pr_merge``; last repeats;
  empty means merged.
- ``fail_once``: method name -> exception raised on that method's next
  call (then cleared), for interrupting a run mid-stage. The call is still
  recorded: GitHub was asked, it just did not answer.
- ``fail_always``: method name -> exception raised on **every** call, for
  persistent outages (checked after ``fail_once``).

Failures are also ledgered. ``failed_jobs`` records one
``(op, method, path, http_status)`` tuple per call this fake answers with a
``GithubOpsError``, which in production is a *failed worker job*: a
``worker.error`` event and a red chronology panel, not just a host-side
exception (#559). An ``allow_missing`` probe (``label_lookup``, and the real
``repo_lookup``/``ref_lookup``) resolves a miss as data and records nothing,
so ``assert_no_failed_jobs()`` — or the ``failed_job_paths`` property — lets a
test assert that a run's chronology carries no doomed calls.

Any GithubOps method this fake does not model reaches ``_op`` and fails
loudly rather than pretending.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote, unquote

from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import (
    ChecksVerdict,
    FailedCheck,
    GithubOps,
    IssueRef,
    MergeOutcome,
    PostedFinding,
    PrRef,
    ReviewComment,
    ReviewEvent,
    ReviewThread,
    SubmittedReview,
    ThreadComment,
    anchor_of,
)
from tests.fakes.github_errors import github_error

GREEN = ChecksVerdict("green", 1, (), ())
PENDING = ChecksVerdict("pending", 1, ("ci",), ())
RED = ChecksVerdict("red", 1, (), ("ci",))
NO_CHECKS = ChecksVerdict("green", 0, (), ())

MERGED = MergeOutcome(True, "merge0001", "Pull Request successfully merged")
BLOCKED_405 = MergeOutcome(False, "", "Pull Request is not mergeable (HTTP 405)", blocked=True)
STALE_409 = MergeOutcome(False, "", "Head branch was modified (HTTP 409)", stale=True)


def human_review(
    login: str, state: str, body: str = "", *, id: int | None = None
) -> dict[str, Any]:
    """One entry of the reviews payload, as GitHub shapes it."""
    return {"user": {"login": login}, "state": state, "body": body, "id": id}


def human_comment(
    login: str, body: str, *, path: str = "", line: int | None = None, id: int | None = None
) -> dict[str, Any]:
    """One entry of the pull request review comments payload."""
    return {"user": {"login": login}, "body": body, "path": path, "line": line, "id": id}


class FakeGithub(GithubOps):
    def __init__(
        self,
        *,
        repo: str = "o/r",
        number: int = 7,
        draft: bool = False,
        self_review: bool = False,
    ) -> None:
        # Deliberately no super().__init__: there is no worker client.
        self.run_id = "fake"
        self.timeout_s = 0.0
        self.repo = repo
        # ``self_review`` models the field: one token opens the PR and
        # reviews it, so the PR's author is the loop's own login and GitHub
        # refuses REQUEST_CHANGES/APPROVE (#513). The default keeps a
        # distinct author so the review-feature path stays exercised.
        self.user_login = "sbxloop-bot"
        self.pr_author = self.user_login if self_review else "someone-else"
        self.pr: dict[str, Any] = {
            "number": number,
            "user": {"login": self.pr_author},
            "html_url": f"https://github.com/{repo}/pull/{number}",
            "node_id": f"PR_node{number}",
            "draft": draft,
            "state": "open",
            "merged": False,
            "merge_commit_sha": None,
            "mergeable": True,
            "mergeable_state": "clean",
            "head": {"sha": "commit0"},
        }
        self.checks: list[ChecksVerdict] = []
        # Verdicts by commit, consulted before the scripted queue: the
        # baseline read of #611 asks about the merge base ("base123", the
        # compare stub's answer), and must not eat the head's script. No
        # checks on the base by default, so every red is the PR's own.
        self.checks_by_sha: dict[str, ChecksVerdict] = {"base123": NO_CHECKS}
        # What the base branch requires (#611): classic protection (None =
        # 404, unprotected) and the rulesets list.
        self.protection: dict[str, Any] | None = None
        self.rules: list[dict[str, Any]] = []
        self.failed_logs: list[FailedCheck] = []
        self.reviews_payload: list[dict[str, Any]] = []
        self.comments_payload: list[dict[str, Any]] = []
        self.feedback = ""
        self.undraft_ok = True
        self.update_ok = True
        # GitHub 422s a review whose inline comment is anchored outside the
        # diff — every event, COMMENT included. True models that.
        self.refuse_inline_comments = False
        self.merge_outcomes: list[MergeOutcome] = []
        # Anchors whose individual review comment GitHub refuses (422 "line
        # could not be resolved") in comment mode — per anchor, unlike
        # ``refuse_inline_comments`` which fails a whole review.
        self.refuse_anchors: set[str] = set()
        self.fail_once: dict[str, Exception] = {}
        self.fail_always: dict[str, Exception] = {}
        # Raised on every ``GET /user`` when set: models a GitHub App
        # installation token, which cannot call the user-scoped endpoint
        # (403 "Resource not accessible by integration", #581).
        self.fail_user_lookup: Exception | None = None
        # What the engine asked for.
        self.reviews: list[tuple[ReviewEvent, str, list[ReviewComment]]] = []
        self.merges: list[tuple[int, str, str]] = []
        self.updates: list[tuple[int, str]] = []
        self.deleted_branches: list[str] = []
        self.ready_calls: list[str] = []
        self.raw_calls: list[tuple[str, str, dict[str, Any] | None]] = []
        # Calls that would have failed a worker job: (op, method, path,
        # http_status). See the module docstring.
        self.failed_jobs: list[tuple[str, str, str, int | None]] = []
        self._missing_ok = False
        self.checks_calls: list[str] = []
        self.pr_kwargs: dict[str, Any] = {}
        self.blob_batches: list[list[dict[str, str]]] = []
        self.branches: set[str] = set()
        # Branches with no merge base against the base branch (#600): the
        # compare endpoint 404s for them, as GitHub does.
        self.unrelated_branches: set[str] = set()
        self.pr_created = False
        self.pr_create_calls = 0
        self.threads: list[ReviewThread] = []
        self.replies: list[tuple[int, str]] = []
        self.issue_comments: list[str] = []
        # Follow-up issues filed after the merge (#517), and the labels the
        # engine made sure exist; `existing_issues` seeds the label listing.
        self.issues_created: list[tuple[str, str, list[str]]] = []
        self.labels_created: list[str] = []
        # Labels the repository already carries before the run (#556): a
        # single-label GET finds these, and creating one is a 422.
        self.labels_existing: set[str] = set()
        self.existing_issues: list[dict[str, Any]] = []
        self.resolved: list[str] = []
        self._comment_id = 0
        self._commits = 0
        self._blobs = 0
        self._updates = 0

    # -- plumbing ------------------------------------------------------------

    @contextmanager
    def _allow_missing(self) -> Iterator[None]:
        """Answer a 404 inside as data, the way an ``allow_missing`` op does:
        the probe is a resolved miss, not a failed worker job."""
        previous = self._missing_ok
        self._missing_ok = True
        try:
            yield
        finally:
            self._missing_ok = previous

    def _record_failed_job(
        self, op: str, method: str, path: str, exc: BaseException | None = None
    ) -> None:
        """Ledger one call whose non-2xx answer fails the worker job."""
        status = getattr(exc, "http_status", None)
        self.failed_jobs.append((op, method, path, status if isinstance(status, int) else None))

    @property
    def failed_job_paths(self) -> list[str]:
        """The paths of the calls that would have failed a worker job."""
        return [path for _, _, path, _ in self.failed_jobs]

    def assert_no_failed_jobs(self) -> None:
        """Raise unless the run's chronology is free of failed worker jobs."""
        if self.failed_jobs:
            calls = ", ".join(
                f"{op} {method} {path}" + (f" (HTTP {status})" if status else "")
                for op, method, path, status in self.failed_jobs
            )
            raise AssertionError(f"failed worker jobs recorded: {calls}")

    def _maybe_fail(self, method: str) -> None:
        exc = self.fail_once.pop(method, None)
        if exc is None:
            exc = self.fail_always.get(method)
        if exc is not None:
            self._record_failed_job(method, "", method, exc)
            raise exc

    def _op(self, op: str, params: dict[str, Any], *, timeout_s: float | None = None) -> Any:
        raise AssertionError(f"FakeGithub does not model github op {op!r} ({params})")

    @property
    def number(self) -> int:
        return int(self.pr["number"])

    @property
    def head_sha(self) -> str:
        return str(self.pr["head"]["sha"])

    def _move_head(self, sha: str) -> None:
        self.pr["head"] = {"sha": sha}

    # -- repository / delivery -----------------------------------------------

    def repo_get(self, repo: str) -> dict[str, Any]:
        return {"default_branch": "main"}

    def repo_lookup(self, repo: str) -> dict[str, Any] | None:
        self._maybe_fail("repo_lookup")
        return {"default_branch": "main"}

    def ref_lookup(self, repo: str, ref: str) -> str | None:
        # A delivery branch (``sbxloop/<run>``) exists only once delivery
        # created it, and then sits at the PR head; anything else is a base
        # branch that is simply there.
        branch = ref.removeprefix("heads/")
        if branch.startswith("sbxloop/"):
            return self.head_sha if branch in self.branches else None
        return "base123"

    def label_lookup(self, repo: str, name: str) -> dict[str, Any] | None:
        """A repository label probe (#556): its data, or None when absent.
        A 404 is an answer — no failed worker job — while any other failure
        still raises, as the real ``label.get`` op does under
        ``allow_missing``."""
        with self._allow_missing():
            try:
                data = self.raw("GET", f"/repos/{repo}/labels/{quote(name, safe='')}")
            except GithubOpsError as exc:
                if exc.http_status == 404:
                    return None
                raise
        return data if isinstance(data, dict) else None

    def blobs_create_many(self, repo: str, files: list[dict[str, str]]) -> dict[str, str]:
        self.blob_batches.append(files)
        self._maybe_fail("blobs_create_many")
        shas: dict[str, str] = {}
        for entry in files:
            self._blobs += 1
            shas[entry["path"]] = f"blob{self._blobs}"
        return shas

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
        self.pr_kwargs = {
            "repo": repo,
            "base": base,
            "head": head,
            "title": title,
            "body": body,
            "draft": draft,
        }
        self.pr_create_calls += 1
        self._maybe_fail("pr_create")
        if self.pr_created:
            raise self._failed(
                "pr.create",
                "POST",
                f"/repos/{repo}/pulls",
                422,
                "github op pr.create failed: GithubOpError: gh api POST "
                # The gh transport's field wording: no "already exists"
                # prose, just the status (run r8tzse1qa).
                f"/repos/{repo}/pulls failed (rc=1): gh: Validation Failed (HTTP 422)",
            )
        self.pr_created = True
        self.pr["draft"] = draft
        return PrRef(number=self.number, url=str(self.pr["html_url"]))

    def _failed(
        self, op: str, method: str, path: str, status: int | None, message: str
    ) -> GithubOpsError:
        """Ledger a failed worker job and build the error to raise for it."""
        exc = GithubOpsError(message, http_status=status)
        self._record_failed_job(op, method, path, exc)
        return exc

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.raw_calls.append((method, path, body))
        self._maybe_fail("raw")
        # List reads arrive paged (`raw_pages`, #614). The fake holds every
        # list whole, so page one is the list and any later page is empty;
        # routing below matches on the path without its query.
        path, _, query = path.partition("?")
        if method == "GET" and "page=" in query and not query.endswith("page=1"):
            return []
        if method == "GET" and path == "/user":
            if self.fail_user_lookup is not None:
                self._record_failed_job("raw.api", method, path, self.fail_user_lookup)
                raise self.fail_user_lookup
            return {"login": self.user_login}
        if method == "GET" and "/git/commits/" in path:
            return {"tree": {"sha": "basetree"}}
        if method == "POST" and path.endswith("/git/trees"):
            return {"sha": "tree456"}
        if method == "POST" and path.endswith("/git/commits"):
            self._commits += 1
            return {"sha": f"commit{self._commits}"}
        if method == "POST" and path.endswith("/git/refs"):
            assert body is not None
            branch = str(body["ref"]).removeprefix("refs/heads/")
            if branch in self.branches:
                raise self._failed(
                    "raw.api",
                    method,
                    path,
                    422,
                    "github op raw.api failed: GithubOpError: gh api POST "
                    f"{path} failed (rc=1): Reference already exists (HTTP 422)",
                )
            self.branches.add(branch)
            self._move_head(str(body["sha"]))
            return {"ref": body["ref"]}
        if method == "PATCH" and "/git/refs/heads/" in path:
            assert body is not None
            self._move_head(str(body["sha"]))
            return {"ref": path}
        if method == "GET" and path.endswith("/reviews"):
            return list(self.reviews_payload)
        if method == "GET" and "/issues/" in path and path.endswith("/comments"):
            # The PR-as-issue comment listing: what pr_issue_comment posted.
            return [{"body": body} for body in self.issue_comments]
        if method == "GET" and path.endswith("/comments"):
            return list(self.comments_payload)
        if method == "GET" and path.endswith("/pulls") and "state=open&head=" in query:
            return [dict(self.pr)] if self.pr_created else []
        if method == "GET" and "/branches/" in path and path.endswith("/protection"):
            if self.protection is None:
                raise GithubOpsError(
                    "github op raw.api failed: GithubOpError: gh api GET "
                    f"{path} failed (rc=1): Branch not protected (HTTP 404)",
                    http_status=404,
                )
            return dict(self.protection)
        if method == "GET" and "/rules/branches/" in path:
            return list(self.rules)
        if method == "GET" and "/compare/" in path:
            # `base...head`: unrelated histories have no merge base, which
            # GitHub reports as a 404 rather than a comparison.
            head = path.rsplit("...", 1)[1]
            if head in self.unrelated_branches:
                raise GithubOpsError(
                    "github op raw.api failed: GithubOpError: gh api GET "
                    f"{path} failed (rc=1): Not Found (HTTP 404)",
                    http_status=404,
                )
            return {"merge_base_commit": {"sha": "base123"}}
        if method == "POST" and path.endswith("/labels") and body and "labels" in body:
            return [{"name": name} for name in body["labels"]]
        if method == "GET" and "/labels/" in path:
            # A single label lookup (#556): present, or a 404 like gh's.
            name = unquote(path.rsplit("/labels/", 1)[1])
            if name in self.labels_existing or name in self.labels_created:
                return {"name": name}
            if self._missing_ok:
                return None
            raise self._failed(
                "raw.api",
                method,
                path,
                404,
                "github op raw.api failed: GithubOpError: gh api GET "
                f"{path} failed (rc=1): Not Found (HTTP 404)",
            )
        if method == "POST" and path.endswith("/labels"):
            # Repository label creation (#517): an existing one is a 422.
            assert body is not None
            if body["name"] in self.labels_created or body["name"] in self.labels_existing:
                raise self._failed(
                    "raw.api",
                    method,
                    path,
                    422,
                    "github op raw.api failed: GithubOpError: gh api POST "
                    f"{path} failed (rc=1): Validation Failed: already_exists (HTTP 422)",
                )
            self.labels_created.append(str(body["name"]))
            return {"name": body["name"]}
        if method == "GET" and path.endswith("/issues") and "labels=" in query:
            return list(self.existing_issues)
        if method == "POST" and path.endswith("/pulls/" + str(self.number) + "/comments"):
            # One review comment, standalone (#513): a thread of its own.
            assert body is not None
            anchor = f"{body['path']}:{body['line']}"
            if anchor in self.refuse_anchors:
                exc = github_error("review_line_unresolved_422")
                self._record_failed_job("raw.api", method, path, exc)
                raise exc
            assert body["commit_id"] == self.pr["head"]["sha"], "anchored to the delivered head"
            self._comment_id += 1
            self.threads.append(
                ReviewThread(
                    node_id=f"PRRT_{self._comment_id}",
                    is_resolved=False,
                    path=str(body["path"]),
                    line=int(body["line"]),
                    comments=(ThreadComment(self._comment_id, self.user_login, str(body["body"])),),
                )
            )
            return {
                "id": self._comment_id,
                "html_url": f"{self.pr['html_url']}#discussion_r{self._comment_id}",
            }
        raise AssertionError(f"FakeGithub: unexpected raw call {method} {path}")

    # -- the pull request ----------------------------------------------------

    def issue_create(
        self, repo: str, title: str, body: str = "", labels: list[str] | None = None
    ) -> IssueRef:
        self._maybe_fail("issue_create")
        self.issues_created.append((title, body, list(labels or [])))
        number = 900 + len(self.issues_created)
        return IssueRef(number=number, url=f"https://github.com/{repo}/issues/{number}")

    def pr_get(self, repo: str, number: int) -> dict[str, Any]:
        self._maybe_fail("pr_get")
        return {**self.pr, "head": dict(self.pr["head"])}

    def pr_checks(self, repo: str, sha: str) -> ChecksVerdict:
        self.checks_calls.append(sha)
        self._maybe_fail("pr_checks")
        if sha in self.checks_by_sha:
            return self.checks_by_sha[sha]
        if not self.checks:
            return GREEN
        return self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]

    def checks_failed_logs(
        self, repo: str, sha: str, *, max_chars: int = 6000
    ) -> list[FailedCheck]:
        return list(self.failed_logs)

    def pr_review_feedback(
        self, repo: str, number: int, *, exclude_login: str | None = None, clip: int = 6000
    ) -> str:
        return self.feedback

    def pr_review_create(
        self,
        repo: str,
        number: int,
        event: ReviewEvent,
        body: str,
        comments: Sequence[ReviewComment] = (),
    ) -> SubmittedReview:
        self._maybe_fail("pr_review_create")
        if self.refuse_inline_comments and comments:
            raise self._failed(
                "raw.api",
                "POST",
                f"/repos/{repo}/pulls/{number}/reviews",
                422,
                "github op raw.api failed: GithubOpError: gh api POST "
                f"/repos/{repo}/pulls/{number}/reviews failed (rc=1): gh: "
                "Unprocessable Entity (HTTP 422)",
            )
        self.reviews.append((event, body, list(comments)))
        url = f"{self.pr['html_url']}#pullrequestreview-{len(self.reviews)}"
        review_id = len(self.reviews)
        posted: list[PostedFinding] = []
        for comment in comments:
            self._comment_id += 1
            node_id = f"PRRT_{self._comment_id}"
            self.threads.append(
                ReviewThread(
                    node_id=node_id,
                    is_resolved=False,
                    path=comment.path,
                    line=comment.line,
                    comments=(ThreadComment(self._comment_id, self.user_login, comment.body),),
                )
            )
            posted.append(PostedFinding(anchor_of(comment), self._comment_id, node_id))
        return SubmittedReview(url, event, review_id, tuple(posted))

    def pr_review_threads(self, repo: str, number: int) -> list[ReviewThread]:
        self._maybe_fail("pr_review_threads")
        return list(self.threads)

    def pr_comment_reply(self, repo: str, number: int, comment_id: int, body: str) -> str:
        self.replies.append((comment_id, body))
        self._maybe_fail("pr_comment_reply")
        for index, thread in enumerate(self.threads):
            if thread.root_comment_id == comment_id:
                self._comment_id += 1
                self.threads[index] = thread._replace(
                    comments=(
                        *thread.comments,
                        ThreadComment(self._comment_id, self.user_login, body),
                    )
                )
                break
        return f"{self.pr['html_url']}#discussion_r{comment_id}"

    def pr_issue_comment(self, repo: str, number: int, body: str) -> str:
        self.issue_comments.append(body)
        self._maybe_fail("pr_issue_comment")
        return f"{self.pr['html_url']}#issuecomment-{len(self.issue_comments)}"

    def resolve_review_thread(self, thread_node_id: str) -> bool:
        self.resolved.append(thread_node_id)
        self._maybe_fail("resolve_review_thread")
        for index, thread in enumerate(self.threads):
            if thread.node_id == thread_node_id:
                self.threads[index] = thread._replace(is_resolved=True)
                return True
        return False

    def pr_ready_for_review(self, node_id: str) -> bool:
        self.ready_calls.append(node_id)
        self._maybe_fail("pr_ready_for_review")
        if self.undraft_ok:
            self.pr["draft"] = False
        return self.undraft_ok

    def pr_merge(
        self,
        repo: str,
        number: int,
        *,
        method: str = "squash",
        sha: str = "",
        title: str = "",
        message: str = "",
    ) -> MergeOutcome:
        self.merges.append((number, method, sha))
        self._maybe_fail("pr_merge")
        if not self.merge_outcomes:
            outcome = MERGED
        elif len(self.merge_outcomes) > 1:
            outcome = self.merge_outcomes.pop(0)
        else:
            outcome = self.merge_outcomes[0]
        if outcome.merged:
            self.pr["merged"] = True
            self.pr["merge_commit_sha"] = outcome.sha
        return outcome

    def pr_update_branch(self, repo: str, number: int, *, expected_head_sha: str = "") -> bool:
        self.updates.append((number, expected_head_sha))
        self._maybe_fail("pr_update_branch")
        if self.update_ok:
            self._updates += 1
            self._move_head(f"updated{self._updates}")
            self.pr["mergeable_state"] = "clean"
        return self.update_ok

    def branch_delete(self, repo: str, branch: str) -> None:
        self.deleted_branches.append(branch)
        self._maybe_fail("branch_delete")
