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

Any GithubOps method this fake does not model reaches ``_op`` and fails
loudly rather than pretending.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import (
    ChecksVerdict,
    FailedCheck,
    GithubOps,
    MergeOutcome,
    PrRef,
    ReviewComment,
    ReviewEvent,
    SubmittedReview,
)

GREEN = ChecksVerdict("green", 1, (), ())
PENDING = ChecksVerdict("pending", 1, ("ci",), ())
RED = ChecksVerdict("red", 1, (), ("ci",))
NO_CHECKS = ChecksVerdict("green", 0, (), ())

MERGED = MergeOutcome(True, "merge0001", "Pull Request successfully merged")
BLOCKED_405 = MergeOutcome(False, "", "Pull Request is not mergeable (HTTP 405)", blocked=True)
STALE_409 = MergeOutcome(False, "", "Head branch was modified (HTTP 409)", stale=True)


def human_review(login: str, state: str, body: str = "") -> dict[str, Any]:
    """One entry of the reviews payload, as GitHub shapes it."""
    return {"user": {"login": login}, "state": state, "body": body}


class FakeGithub(GithubOps):
    def __init__(self, *, repo: str = "o/r", number: int = 7, draft: bool = False) -> None:
        # Deliberately no super().__init__: there is no worker client.
        self.run_id = "fake"
        self.timeout_s = 0.0
        self.repo = repo
        self.pr: dict[str, Any] = {
            "number": number,
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
        self.user_login = "sbxloop-bot"
        self.fail_once: dict[str, Exception] = {}
        # What the engine asked for.
        self.reviews: list[tuple[ReviewEvent, str, list[ReviewComment]]] = []
        self.merges: list[tuple[int, str, str]] = []
        self.updates: list[tuple[int, str]] = []
        self.deleted_branches: list[str] = []
        self.ready_calls: list[str] = []
        self.raw_calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.checks_calls: list[str] = []
        self.pr_kwargs: dict[str, Any] = {}
        self.blob_batches: list[list[dict[str, str]]] = []
        self.branches: set[str] = set()
        self.pr_created = False
        self._commits = 0
        self._blobs = 0
        self._updates = 0

    # -- plumbing ------------------------------------------------------------

    def _maybe_fail(self, method: str) -> None:
        exc = self.fail_once.pop(method, None)
        if exc is not None:
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
        return "base123"

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
        self._maybe_fail("pr_create")
        if self.pr_created:
            raise GithubOpsError(
                "github op pr.create failed: GithubOpError: gh api POST "
                f"/repos/{repo}/pulls failed (rc=1): A pull request already exists for "
                f"{repo.split('/', 1)[0]}:{head}. (HTTP 422)",
                http_status=422,
            )
        self.pr_created = True
        self.pr["draft"] = draft
        return PrRef(number=self.number, url=str(self.pr["html_url"]))

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.raw_calls.append((method, path, body))
        self._maybe_fail("raw")
        if method == "GET" and path == "/user":
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
                raise GithubOpsError(
                    "github op raw.api failed: GithubOpError: gh api POST "
                    f"{path} failed (rc=1): Reference already exists (HTTP 422)",
                    http_status=422,
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
        if method == "GET" and path.endswith("/comments"):
            return list(self.comments_payload)
        if method == "GET" and "/pulls?state=open&head=" in path:
            return [dict(self.pr)] if self.pr_created else []
        raise AssertionError(f"FakeGithub: unexpected raw call {method} {path}")

    # -- the pull request ----------------------------------------------------

    def pr_get(self, repo: str, number: int) -> dict[str, Any]:
        self._maybe_fail("pr_get")
        return {**self.pr, "head": dict(self.pr["head"])}

    def pr_checks(self, repo: str, sha: str) -> ChecksVerdict:
        self.checks_calls.append(sha)
        self._maybe_fail("pr_checks")
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
            raise GithubOpsError(
                "github op raw.api failed: GithubOpError: gh api POST "
                f"/repos/{repo}/pulls/{number}/reviews failed (rc=1): gh: "
                "Unprocessable Entity (HTTP 422)",
                http_status=422,
            )
        self.reviews.append((event, body, list(comments)))
        url = f"{self.pr['html_url']}#pullrequestreview-{len(self.reviews)}"
        return SubmittedReview(url, event)

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
