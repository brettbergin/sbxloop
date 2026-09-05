"""``GithubOps.pr_enqueue`` / ``pr_queue_state`` (#676): a base that merges
through a merge queue is entered and watched through GraphQL — the REST
API has no queue surface."""

from __future__ import annotations

from typing import Any

import pytest

from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import GithubOps, QueueEntry, QueueState, fold_queue_entry, fold_queue_state
from tests.unit.test_gh_review_threads import PR, REPO, RawOps


def entry(state: str = "QUEUED", position: int | None = 2, head: str = "queue0") -> dict[str, Any]:
    return {
        "id": "MQE_1",
        "state": state,
        "position": position,
        "headCommit": {"oid": head} if head else None,
    }


def queue_payload(
    *,
    merged: bool = False,
    state: str = "OPEN",
    entry_node: Any = None,
    removals: int = 0,
    reason: str | None = None,
    merge_sha: str | None = None,
) -> dict[str, Any]:
    nodes = [{"reason": reason}] if removals else []
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "merged": merged,
                    "state": state,
                    "mergeCommit": {"oid": merge_sha} if merge_sha else None,
                    "mergeQueueEntry": entry_node,
                    "timelineItems": {"totalCount": removals, "nodes": nodes},
                }
            }
        }
    }


class TestFold:
    def test_an_entry_is_typed_and_a_missing_one_is_none(self) -> None:
        assert fold_queue_entry(entry()) == QueueEntry("MQE_1", "QUEUED", 2, "queue0")
        assert fold_queue_entry(entry(position=None, head="")) == QueueEntry(
            "MQE_1", "QUEUED", None, ""
        )
        assert fold_queue_entry(None) is None
        assert fold_queue_entry({"state": "QUEUED"}) is None, "no id is no entry"
        assert fold_queue_entry("junk") is None

    def test_the_queue_state_folds_merged_closed_entry_and_removals(self) -> None:
        queued = fold_queue_state(queue_payload(entry_node=entry("AWAITING_CHECKS")))
        assert queued == QueueState(
            False, False, QueueEntry("MQE_1", "AWAITING_CHECKS", 2, "queue0"), 0, "", ""
        )
        merged = fold_queue_state(queue_payload(merged=True, state="MERGED", merge_sha="m1"))
        assert merged.merged and not merged.closed and merged.entry is None
        assert merged.merge_sha == "m1"
        closed = fold_queue_state(queue_payload(state="CLOSED"))
        assert closed.closed and not closed.merged
        removed = fold_queue_state(queue_payload(removals=3, reason="CI_FAILED"))
        assert removed.removals == 3 and removed.removed_reason == "CI_FAILED"
        # A removal event with no reason is counted, its reason empty.
        assert fold_queue_state(queue_payload(removals=1)).removed_reason == ""

    def test_no_pull_request_is_not_an_answer(self) -> None:
        with pytest.raises(GithubOpsError, match="no pull request"):
            fold_queue_state({"data": {"repository": None}})
        with pytest.raises(GithubOpsError, match="no pull request"):
            fold_queue_state("junk")


class TestEnqueue:
    def test_enqueues_the_judged_head_and_returns_the_entry(self) -> None:
        ops = RawOps(
            {
                ("POST", "/graphql"): {
                    "data": {"enqueuePullRequest": {"mergeQueueEntry": entry(position=1)}}
                }
            }
        )
        assert ops.pr_enqueue("PR_node7", head="commit0") == QueueEntry(
            "MQE_1", "QUEUED", 1, "queue0"
        )
        (call,) = ops.calls
        assert call[2] is not None
        assert call[2]["variables"] == {"id": "PR_node7", "head": "commit0"}
        assert "expectedHeadOid: $head" in call[2]["query"]

    def test_no_head_leaves_the_guard_out(self) -> None:
        ops = RawOps(
            {("POST", "/graphql"): {"data": {"enqueuePullRequest": {"mergeQueueEntry": entry()}}}}
        )
        ops.pr_enqueue("PR_node7")
        assert ops.calls[0][2] is not None
        assert ops.calls[0][2]["variables"] == {"id": "PR_node7"}

    def test_a_refusal_is_raised_with_githubs_words(self) -> None:
        ops = RawOps(
            {
                ("POST", "/graphql"): {
                    "data": {"enqueuePullRequest": None},
                    "errors": [
                        {"type": "UNPROCESSABLE", "message": "Pull request is not mergeable"}
                    ],
                }
            }
        )
        with pytest.raises(GithubOpsError, match=r"enqueuePullRequest failed.*not mergeable"):
            ops.pr_enqueue("PR_node7", head="commit0")

    def test_no_entry_and_a_malformed_result_fail_closed(self) -> None:
        ops = RawOps({("POST", "/graphql"): {"data": {"enqueuePullRequest": {}}}})
        with pytest.raises(GithubOpsError, match="no queue entry"):
            ops.pr_enqueue("PR_node7")
        ops = RawOps({("POST", "/graphql"): "nope"})
        with pytest.raises(GithubOpsError, match="malformed"):
            ops.pr_enqueue("PR_node7")


class TestQueueState:
    def test_reads_the_pull_request_by_number(self) -> None:
        ops = RawOps({("POST", "/graphql"): queue_payload(entry_node=entry())})
        state = ops.pr_queue_state(REPO, PR)
        assert state.entry == QueueEntry("MQE_1", "QUEUED", 2, "queue0")
        (call,) = ops.calls
        assert call[2] is not None
        assert call[2]["variables"] == {"owner": "o", "name": "r", "number": PR}
        query = call[2]["query"]
        assert "mergeQueueEntry { id state position headCommit { oid } }" in query
        assert "itemTypes: [REMOVED_FROM_MERGE_QUEUE_EVENT]" in query
        assert "... on RemovedFromMergeQueueEvent { reason }" in query

    def test_errors_are_raised(self) -> None:
        ops = RawOps({("POST", "/graphql"): {"errors": [{"type": "NOT_FOUND"}]}})
        with pytest.raises(GithubOpsError, match="mergeQueueEntry failed"):
            ops.pr_queue_state(REPO, PR)
        ops = RawOps({("POST", "/graphql"): None})
        with pytest.raises(GithubOpsError, match="malformed"):
            ops.pr_queue_state(REPO, PR)

    def test_the_mutation_is_the_documented_shape(self) -> None:
        assert "enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $head})" in (
            GithubOps._ENQUEUE_MUTATION
        )
