"""Review thread capture, reply and resolve (#520 step 1).

A review round's findings are only reconcilable later if the ids of the
threads they created survive the call that created them, so these tests
drive :class:`GithubOps` against a recording raw-transport stub and assert
both the captured mapping and the exact request shapes.
"""

from __future__ import annotations

from typing import Any

import pytest

from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import (
    GithubOps,
    PostedFinding,
    ReviewComment,
    ReviewThread,
    SubmittedReview,
    ThreadComment,
    anchor_of,
    fold_review_threads,
)

REPO = "o/r"
PR = 7


def thread_node(node_id: str, path: str, line: int | None, comment_id: int) -> dict[str, Any]:
    return {
        "id": node_id,
        "isResolved": False,
        "path": path,
        "line": line,
        "comments": {
            "nodes": [
                {
                    "databaseId": comment_id,
                    "body": f"[major] on {path}",
                    "author": {"login": "sbxloop-bot"},
                }
            ]
        },
    }


def threads_payload(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {
        "data": {
            "repository": {"pullRequest": {"reviewThreads": {"nodes": list(nodes)}}},
        }
    }


class RawOps(GithubOps):
    """GithubOps with ``raw`` replaced by a scripted, recording stub."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.run_id = "r1"
        self.timeout_s = 0.0
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def _op(self, op: str, params: dict[str, Any], *, timeout_s: float | None = None) -> Any:
        raise AssertionError(f"unexpected worker op {op!r}")

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, path, body))
        try:
            answer = self.routes[(method, path)]
        except KeyError:  # pragma: no cover - a mis-scripted test
            raise AssertionError(f"unexpected raw call {method} {path}") from None
        if isinstance(answer, Exception):
            raise answer
        return answer


def review_ops(
    *,
    review_id: int | None = 55,
    comments_payload: Any = None,
    threads: Any = None,
) -> RawOps:
    review: dict[str, Any] = {"html_url": "https://github.com/o/r/pull/7#review"}
    if review_id is not None:
        review["id"] = review_id
    return RawOps(
        {
            ("POST", f"/repos/{REPO}/pulls/{PR}/reviews"): review,
            (
                "GET",
                f"/repos/{REPO}/pulls/{PR}/reviews/{review_id}/comments",
            ): comments_payload if comments_payload is not None else [],
            ("POST", "/graphql"): threads if threads is not None else threads_payload(),
        }
    )


class TestThreadCapture:
    def test_inline_findings_capture_comment_and_thread_ids(self) -> None:
        comments = [
            ReviewComment(path="a.py", line=10, body="[major] one"),
            ReviewComment(path="b.py", line=3, body="[major] two"),
        ]
        ops = review_ops(
            comments_payload=[
                {"id": 101, "path": "a.py", "line": 10},
                {"id": 102, "path": "b.py", "line": 3},
            ],
            threads=threads_payload(
                thread_node("PRRT_1", "a.py", 10, 101),
                thread_node("PRRT_2", "b.py", 3, 102),
            ),
        )
        submitted = ops.pr_review_create(REPO, PR, "REQUEST_CHANGES", "body", comments)

        assert submitted.review_id == 55
        assert submitted.event == "REQUEST_CHANGES"
        assert submitted.posted == (
            PostedFinding("a.py:10", 101, "PRRT_1"),
            PostedFinding("b.py:3", 102, "PRRT_2"),
        )
        assert submitted.inline == submitted.posted
        assert submitted.body_only == ()

    def test_finding_github_dropped_is_recorded_body_only(self) -> None:
        """An anchor GitHub refused keeps its anchor, with no comment id."""
        comments = [
            ReviewComment(path="a.py", line=10, body="[major] one"),
            ReviewComment(path="gone.py", line=99, body="[major] outside the diff"),
        ]
        ops = review_ops(
            comments_payload=[{"id": 101, "path": "a.py", "line": 10}],
            threads=threads_payload(thread_node("PRRT_1", "a.py", 10, 101)),
        )
        submitted = ops.pr_review_create(REPO, PR, "REQUEST_CHANGES", "body", comments)

        assert submitted.posted == (
            PostedFinding("a.py:10", 101, "PRRT_1"),
            PostedFinding("gone.py:99", None, None),
        )
        assert [p.anchor for p in submitted.body_only] == ["gone.py:99"]

    def test_review_without_inline_comments_posts_nothing_to_capture(self) -> None:
        ops = review_ops()
        submitted = ops.pr_review_create(REPO, PR, "APPROVE", "body")
        assert submitted.posted == ()
        # No follow-up reads at all when there was nothing anchored.
        assert [c[0] for c in ops.calls] == ["POST"]

    def test_capture_survives_a_failed_comments_read(self) -> None:
        """The review is posted; a 404 on the read must not lose it."""
        ops = review_ops(comments_payload=GithubOpsError("boom", http_status=404))
        submitted = ops.pr_review_create(
            REPO, PR, "REQUEST_CHANGES", "body", [ReviewComment(path="a.py", line=1, body="x")]
        )
        assert submitted.posted == (PostedFinding("a.py:1", None, None),)

    def test_capture_survives_a_failed_thread_lookup(self) -> None:
        ops = review_ops(
            comments_payload=[{"id": 101, "path": "a.py", "line": 1}],
            threads=GithubOpsError("graphql down"),
        )
        submitted = ops.pr_review_create(
            REPO, PR, "REQUEST_CHANGES", "body", [ReviewComment(path="a.py", line=1, body="x")]
        )
        assert submitted.posted == (PostedFinding("a.py:1", 101, None),)

    def test_missing_review_id_still_records_anchors(self) -> None:
        ops = review_ops(review_id=None)
        submitted = ops.pr_review_create(
            REPO, PR, "REQUEST_CHANGES", "body", [ReviewComment(path="a.py", line=1, body="x")]
        )
        assert submitted.review_id is None
        assert submitted.posted == (PostedFinding("a.py:1", None, None),)

    def test_comment_falls_back_to_original_line(self) -> None:
        ops = review_ops(
            comments_payload=[{"id": 101, "path": "a.py", "original_line": 4, "line": None}],
            threads=threads_payload(thread_node("PRRT_1", "a.py", 4, 101)),
        )
        submitted = ops.pr_review_create(
            REPO, PR, "REQUEST_CHANGES", "body", [ReviewComment(path="a.py", line=4, body="x")]
        )
        assert submitted.posted == (PostedFinding("a.py:4", 101, "PRRT_1"),)

    def test_event_fallback_still_captures(self) -> None:
        """A refused REQUEST_CHANGES retries as COMMENT and captures then."""
        calls = {"n": 0}
        ops = review_ops(
            comments_payload=[{"id": 101, "path": "a.py", "line": 1}],
            threads=threads_payload(thread_node("PRRT_1", "a.py", 1, 101)),
        )
        original = ops.raw

        def flaky(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
            if method == "POST" and path.endswith("/reviews"):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise GithubOpsError("not an accepted reviewer", http_status=422)
            return original(method, path, body)

        ops.raw = flaky  # type: ignore[method-assign]
        submitted = ops.pr_review_create(
            REPO, PR, "REQUEST_CHANGES", "body", [ReviewComment(path="a.py", line=1, body="x")]
        )
        assert submitted.event == "COMMENT"
        assert submitted.posted == (PostedFinding("a.py:1", 101, "PRRT_1"),)

    def test_anchor_of_matches_finding_anchor_shape(self) -> None:
        assert anchor_of(ReviewComment(path="a/b.py", line=12, body="x")) == "a/b.py:12"


class TestSubmittedReviewShape:
    def test_defaults_keep_older_call_sites_working(self) -> None:
        submitted = SubmittedReview("u", "COMMENT")
        assert submitted.review_id is None
        assert submitted.posted == ()
        assert submitted.gates_merge is False


class TestReplyAndResolve:
    def test_pr_comment_reply_call_shape(self) -> None:
        ops = RawOps(
            {
                ("POST", f"/repos/{REPO}/pulls/{PR}/comments/101/replies"): {
                    "html_url": "https://github.com/o/r/pull/7#discussion_r9"
                }
            }
        )
        url = ops.pr_comment_reply(REPO, PR, 101, "addressed in abc123: renamed it")
        assert url.endswith("#discussion_r9")
        assert ops.calls == [
            (
                "POST",
                f"/repos/{REPO}/pulls/{PR}/comments/101/replies",
                {"body": "addressed in abc123: renamed it"},
            )
        ]

    def test_pr_comment_reply_propagates_errors(self) -> None:
        ops = RawOps(
            {
                ("POST", f"/repos/{REPO}/pulls/{PR}/comments/101/replies"): GithubOpsError(
                    "gone", http_status=404
                )
            }
        )
        with pytest.raises(GithubOpsError):
            ops.pr_comment_reply(REPO, PR, 101, "hi")

    def test_pr_issue_comment_call_shape(self) -> None:
        ops = RawOps(
            {("POST", f"/repos/{REPO}/issues/{PR}/comments"): {"html_url": "https://x/#c1"}}
        )
        assert ops.pr_issue_comment(REPO, PR, "Reconciliation — round 2") == "https://x/#c1"
        assert ops.calls[0][1] == f"/repos/{REPO}/issues/{PR}/comments"
        assert ops.calls[0][2] == {"body": "Reconciliation — round 2"}

    def test_resolve_review_thread_returns_state(self) -> None:
        ops = RawOps(
            {
                ("POST", "/graphql"): {
                    "data": {"resolveReviewThread": {"thread": {"isResolved": True}}}
                }
            }
        )
        assert ops.resolve_review_thread("PRRT_1") is True
        body = ops.calls[0][2]
        assert body is not None
        assert "resolveReviewThread" in body["query"]
        assert body["variables"] == {"id": "PRRT_1"}

    def test_resolve_review_thread_raises_on_graphql_errors(self) -> None:
        ops = RawOps({("POST", "/graphql"): {"errors": [{"message": "no such thread"}]}})
        with pytest.raises(GithubOpsError, match="no such thread"):
            ops.resolve_review_thread("PRRT_1")

    def test_resolve_review_thread_raises_on_empty_answer(self) -> None:
        ops = RawOps({("POST", "/graphql"): {"data": {"resolveReviewThread": None}}})
        with pytest.raises(GithubOpsError, match="no thread"):
            ops.resolve_review_thread("PRRT_1")


class TestReviewThreadListing:
    def test_pr_review_threads_folds_nodes(self) -> None:
        payload = threads_payload(thread_node("PRRT_1", "a.py", 10, 101))
        payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"][0]["comments"][
            "nodes"
        ].append(
            {
                "databaseId": 202,
                "body": "addressed in abc123 [run=r1 round=1]",
                "author": {"login": "sbxloop-bot"},
            }
        )
        ops = RawOps({("POST", "/graphql"): payload})
        threads = ops.pr_review_threads(REPO, PR)

        assert len(threads) == 1
        thread = threads[0]
        assert thread.node_id == "PRRT_1"
        assert thread.anchor == "a.py:10"
        assert thread.root_comment_id == 101
        assert thread.is_resolved is False
        assert thread.has_reply_from("sbxloop-bot") is True
        assert thread.has_reply_marked("run=r1 round=1") is True
        assert thread.has_reply_marked("run=r1 round=2") is False
        body = ops.calls[0][2]
        assert body is not None
        assert body["variables"] == {"owner": "o", "name": "r", "number": PR}

    def test_thread_without_reply_is_not_reconciled(self) -> None:
        ops = RawOps({("POST", "/graphql"): threads_payload(thread_node("PRRT_1", "a.py", 1, 5))})
        thread = ops.pr_review_threads(REPO, PR)[0]
        assert thread.has_reply_from("sbxloop-bot") is False

    def test_pr_review_threads_raises_on_graphql_errors(self) -> None:
        ops = RawOps({("POST", "/graphql"): {"errors": [{"message": "boom"}]}})
        with pytest.raises(GithubOpsError, match="boom"):
            ops.pr_review_threads(REPO, PR)

    def test_fold_ignores_malformed_nodes(self) -> None:
        payload = threads_payload(
            thread_node("PRRT_1", "a.py", 1, 5),
            {"isResolved": True},  # no id
            "nonsense",  # type: ignore[arg-type]
        )
        assert [t.node_id for t in fold_review_threads(payload)] == ["PRRT_1"]

    def test_fold_of_nonsense_is_empty(self) -> None:
        assert fold_review_threads(None) == []
        assert fold_review_threads({"data": {}}) == []

    def test_thread_without_line_anchors_on_path(self) -> None:
        threads = fold_review_threads(threads_payload(thread_node("PRRT_1", "a.py", None, 5)))
        assert threads[0].anchor == "a.py"
        assert threads[0].line is None


class TestFakeGithubModelsThreads:
    def test_fake_captures_resolves_and_replies(self) -> None:
        from tests.fakes.fake_github import FakeGithub

        gh = FakeGithub()
        submitted = gh.pr_review_create(
            "o/r",
            7,
            "REQUEST_CHANGES",
            "body",
            [ReviewComment(path="a.py", line=2, body="[major] x")],
        )
        assert submitted.posted[0].anchor == "a.py:2"
        comment_id = submitted.posted[0].comment_id
        assert comment_id is not None

        gh.pr_comment_reply("o/r", 7, comment_id, "addressed in sha1: fixed")
        thread = gh.pr_review_threads("o/r", 7)[0]
        assert thread.has_reply_from(gh.user_login) is True

        assert gh.resolve_review_thread(thread.node_id) is True
        assert gh.pr_review_threads("o/r", 7)[0].is_resolved is True
        assert gh.resolved == [thread.node_id]

        gh.pr_issue_comment("o/r", 7, "Reconciliation — round 1")
        assert gh.issue_comments == ["Reconciliation — round 1"]

    def test_thread_comment_is_typed(self) -> None:
        comment = ThreadComment(1, "someone", "body")
        thread = ReviewThread("n", False, "a.py", 1, (comment,))
        assert thread.comments[0].login == "someone"
