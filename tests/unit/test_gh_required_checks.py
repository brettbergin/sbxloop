"""``GithubOps.pr_required_checks`` (#674): which of the checks on a pull
request's head GitHub itself holds the merge for, from the rollup's
``isRequired`` — readable with pull access where classic protection is
admin-only."""

from __future__ import annotations

from typing import Any

import pytest

from sbxloop.gh.ops import (
    GithubOps,
    PaginationError,
    fold_required_contexts,
    rollup_next_cursor,
)
from tests.unit.test_gh_review_threads import PR, REPO, CursorOps, RawOps


def check_run(name: str, *, required: bool) -> dict[str, Any]:
    return {"__typename": "CheckRun", "name": name, "isRequired": required}


def status(context: str, *, required: bool) -> dict[str, Any]:
    return {"__typename": "StatusContext", "context": context, "isRequired": required}


def rollup_payload(
    *nodes: Any, end_cursor: str | None = None, rollup: bool = True
) -> dict[str, Any]:
    commit: dict[str, Any] = {"oid": "commit0", "statusCheckRollup": None}
    if rollup:
        commit["statusCheckRollup"] = {
            "contexts": {
                "pageInfo": {"hasNextPage": end_cursor is not None, "endCursor": end_cursor},
                "nodes": list(nodes),
            }
        }
    return {"data": {"repository": {"pullRequest": {"commits": {"nodes": [{"commit": commit}]}}}}}


class TestFold:
    def test_required_check_runs_and_statuses_by_their_shared_name(self) -> None:
        payload = rollup_payload(
            check_run("ci", required=True),
            check_run("docs", required=False),
            status("ci/circle", required=True),
            {"__typename": "CheckRun", "name": "odd"},  # no isRequired: not gated on
            "garbage",
        )
        assert fold_required_contexts(payload) == ["ci", "ci/circle"]

    def test_no_rollup_yet_is_nothing_required(self) -> None:
        assert fold_required_contexts(rollup_payload(rollup=False)) == []
        assert fold_required_contexts({"data": {"repository": None}}) == []
        assert fold_required_contexts(None) == []
        assert rollup_next_cursor(rollup_payload(rollup=False)) is None

    def test_next_cursor(self) -> None:
        assert rollup_next_cursor(rollup_payload(end_cursor="c1")) == "c1"
        assert rollup_next_cursor(rollup_payload(end_cursor=None)) is None


class TestPrRequiredChecks:
    def test_reads_every_page_and_dedupes(self) -> None:
        ops = CursorOps(
            {
                None: rollup_payload(
                    *[check_run(f"ci-{i}", required=i % 2 == 0) for i in range(100)],
                    end_cursor="c1",
                ),
                "c1": rollup_payload(
                    check_run("ci-0", required=True), status("lint", required=True)
                ),
            }
        )
        required = ops.pr_required_checks(REPO, PR)
        assert required[:3] == ("ci-0", "ci-2", "ci-4")
        assert required[-2:] == ("ci-98", "lint")
        assert len(required) == 51
        assert [c[2]["variables"] for c in ops.calls if c[2]] == [
            {"owner": "o", "name": "r", "number": PR, "cursor": None},
            {"owner": "o", "name": "r", "number": PR, "cursor": "c1"},
        ]

    def test_nothing_reported_is_an_empty_answer(self) -> None:
        ops = RawOps({("POST", "/graphql"): rollup_payload(rollup=False)})
        assert ops.pr_required_checks(REPO, PR) == ()

    def test_graphql_errors_are_raised_not_read_as_empty(self) -> None:
        from sbxloop.errors import GithubOpsError

        ops = RawOps({("POST", "/graphql"): {"errors": [{"type": "FORBIDDEN"}]}})
        with pytest.raises(GithubOpsError, match="statusCheckRollup failed"):
            ops.pr_required_checks(REPO, PR)
        ops = RawOps({("POST", "/graphql"): "nope"})
        with pytest.raises(GithubOpsError, match="malformed"):
            ops.pr_required_checks(REPO, PR)

    def test_an_endless_rollup_is_refused(self) -> None:
        ops = CursorOps({None: rollup_payload(end_cursor="c"), "c": rollup_payload(end_cursor="c")})
        with pytest.raises(PaginationError, match="more than 1000 checks"):
            ops.pr_required_checks(REPO, PR)

    def test_the_query_asks_is_required_for_this_pull_request(self) -> None:
        query = GithubOps._ROLLUP_QUERY
        assert "commits(last: 1)" in query
        assert "contexts(first: 100, after: $cursor)" in query
        assert "... on CheckRun { name isRequired(pullRequestNumber: $number) }" in query
        assert "... on StatusContext { context isRequired(pullRequestNumber: $number) }" in query
