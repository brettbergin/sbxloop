"""Host GithubOps facade tests (stubbed WorkerClient)."""

from __future__ import annotations

from typing import Any, NamedTuple

import pytest

from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import FailedCheck, GithubOps, IssueRef
from sbxloop_worker.protocol import ErrorInfo, JobRequest, JobResult
from tests.fakes.github_errors import worker_error


class Fails(NamedTuple):
    """Reply to this op with a recorded worker error shape (#226)."""

    fixture: str


class StubWorkerClient:
    """Records github.op jobs and replies from a canned op->json map.

    A value is the response json, ``"FAIL"``, or a :class:`Fails` marker
    naming a recorded worker error shape.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.jobs: list[JobRequest] = []

    def submit(self, job: JobRequest) -> JobResult:
        self.jobs.append(job)
        assert job.op is not None
        response = self.responses.get(job.op)
        if isinstance(response, Fails):
            kind, message, status = worker_error(response.fixture)
            return JobResult(
                job_id=job.job_id,
                status="error",
                error=ErrorInfo(type=kind, message=message, http_status=status),
            )
        if response == "FAIL":
            return JobResult(
                job_id=job.job_id,
                status="error",
                error=ErrorInfo(type="GithubOpError", message="HTTP 403: forbidden"),
            )
        return JobResult(job_id=job.job_id, status="ok", output_json=response)


def make_ops(responses: dict[str, Any]) -> tuple[GithubOps, StubWorkerClient]:
    client = StubWorkerClient(responses)
    return GithubOps(client, "r1"), client  # type: ignore[arg-type]


class TestGithubOpsFacade:
    def test_issue_create_typed(self) -> None:
        ops, client = make_ops({"issue.create": {"number": 5, "url": "https://x/5"}})
        ref = ops.issue_create("o/r", "Title", body="Body", labels=["sbxloop"])
        assert ref == IssueRef(number=5, url="https://x/5")
        job = client.jobs[0]
        assert job.kind == "github.op"
        assert job.run_id == "r1"
        assert job.params["labels"] == ["sbxloop"]

    def test_pr_create_and_comment(self) -> None:
        ops, client = make_ops(
            {
                "pr.create": {"number": 9, "url": "https://p/9"},
                "pr.comment": {"url": "https://c/1"},
            }
        )
        pr = ops.pr_create("o/r", base="main", head="dev", title="T", draft=True)
        assert pr.number == 9
        url = ops.pr_comment("o/r", pr.number, "looks good")
        assert url == "https://c/1"
        assert client.jobs[0].params["draft"] is True

    def test_contents_read(self) -> None:
        ops, _ = make_ops({"contents.read": {"content": "file body", "binary": False}})
        assert ops.contents_read("o/r", "README.md", ref="main") == "file body"

    def test_status_and_repo_and_search_and_raw(self) -> None:
        ops, client = make_ops(
            {
                "status.create": {"id": 1, "state": "success"},
                "repo.get": {"full_name": "o/r"},
                "search.issues": [{"number": 3}],
                "raw.api": {"anything": True},
            }
        )
        ops.status_create("o/r", "sha1", "success", description="ok")
        assert ops.repo_get("o/r")["full_name"] == "o/r"
        assert ops.search_issues("q") == [{"number": 3}]
        assert ops.raw("GET", "/rate_limit") == {"anything": True}
        assert [j.op for j in client.jobs] == [
            "status.create",
            "repo.get",
            "search.issues",
            "raw.api",
        ]

    def test_repo_lookup_returns_none_for_a_miss(self) -> None:
        """The probe asks for the miss as data (allow_missing) so an
        expected 404 never becomes a failed job / error event (#222)."""
        ops, client = make_ops({"repo.get": {"missing": True, "http_status": 404}})
        assert ops.repo_lookup("o/r") is None
        assert client.jobs[0].params == {"repo": "o/r", "allow_missing": True}

        ops, _ = make_ops({"repo.get": {"full_name": "o/r", "default_branch": "main"}})
        assert ops.repo_lookup("o/r") == {"full_name": "o/r", "default_branch": "main"}

    def test_ref_lookup(self) -> None:
        ops, client = make_ops({"ref.get": {"ref": "refs/heads/main", "sha": "abc"}})
        assert ops.ref_lookup("o/r", "heads/main") == "abc"
        assert client.jobs[0].params == {"repo": "o/r", "ref": "heads/main", "allow_missing": True}

        ops, _ = make_ops({"ref.get": {"missing": True, "http_status": 409}})
        assert ops.ref_lookup("o/r", "heads/main") is None

        ops, _ = make_ops({"ref.get": {"ref": "refs/heads/main"}})
        with pytest.raises(GithubOpsError, match="no sha"):
            ops.ref_lookup("o/r", "heads/main")

        # a real failure (403, network) is still a failed job and still raises
        ops, _ = make_ops({"ref.get": "FAIL"})
        with pytest.raises(GithubOpsError, match="HTTP 403"):
            ops.ref_lookup("o/r", "heads/main")

    def test_error_result_raises(self) -> None:
        ops, _ = make_ops({"issue.create": "FAIL"})
        with pytest.raises(GithubOpsError, match="HTTP 403") as info:
            ops.issue_create("o/r", "T")
        # No structured status from the stub -> host field stays unset
        # (callers then fall back to message matching, see deliver tests).
        assert info.value.http_status is None

    def test_error_http_status_is_carried_onto_host_error(self) -> None:
        class StatusClient(StubWorkerClient):
            def submit(self, job: JobRequest) -> JobResult:
                return JobResult(
                    job_id=job.job_id,
                    status="error",
                    error=ErrorInfo(type="GithubOpError", message="empty", http_status=409),
                )

        ops = GithubOps(StatusClient({}), "r1")  # type: ignore[arg-type]
        with pytest.raises(GithubOpsError) as info:
            ops.repo_get("o/r")
        assert info.value.http_status == 409

    def test_blobs_create_many_maps_paths_and_scales_timeout(self) -> None:
        ops, client = make_ops(
            {
                "blobs.create_many": {
                    "blobs": [
                        {"path": "a.txt", "sha": "s1"},
                        {"path": "sub/b.bin", "sha": "s2"},
                    ]
                }
            }
        )
        files = [
            {"path": "a.txt", "content_b64": "YQ=="},
            {"path": "sub/b.bin", "content_b64": "Yg=="},
        ]
        assert ops.blobs_create_many("o/r", files) == {"a.txt": "s1", "sub/b.bin": "s2"}
        job = client.jobs[0]
        assert job.params == {"repo": "o/r", "files": files}
        # 2 files: flat op timeout plus the per-file allowance.
        assert job.timeout_s == ops.timeout_s + 2 * GithubOps.BLOB_BATCH_TIMEOUT_PER_FILE_S

    def test_blobs_create_many_malformed_response(self) -> None:
        ops, _ = make_ops({"blobs.create_many": {"blobs": [{"path": "a.txt"}]}})
        with pytest.raises(GithubOpsError, match="malformed"):
            ops.blobs_create_many("o/r", [{"path": "a.txt", "content_b64": "YQ=="}])
        ops, _ = make_ops({"blobs.create_many": {"nope": 1}})
        with pytest.raises(GithubOpsError, match="no blob list"):
            ops.blobs_create_many("o/r", [{"path": "a.txt", "content_b64": "YQ=="}])


class TestChecksFailedLogs:
    def test_maps_entries_and_doubles_the_timeout(self) -> None:
        ops, client = make_ops(
            {
                "checks.failed_logs": {
                    "checks": [
                        {
                            "name": "unit",
                            "conclusion": "failure",
                            "details_url": "https://ci/unit",
                            "excerpt": "FAILED test_x\n",
                        },
                        {
                            "name": "lint",
                            "conclusion": "cancelled",
                            "details_url": "",
                            "excerpt": "",
                        },
                    ]
                }
            }
        )
        assert ops.checks_failed_logs("o/r", "abc", max_chars=1234) == [
            FailedCheck("unit", "failure", "FAILED test_x\n", "https://ci/unit"),
            FailedCheck("lint", "cancelled", "", ""),
        ]
        job = client.jobs[0]
        assert job.op == "checks.failed_logs"
        assert job.params == {"repo": "o/r", "sha": "abc", "max_chars": 1234}
        # One log download per red check, from blob storage: slow.
        assert job.timeout_s == ops.timeout_s * 2

    def test_default_budget_and_empty_list(self) -> None:
        ops, client = make_ops({"checks.failed_logs": {"checks": []}})
        assert ops.checks_failed_logs("o/r", "abc") == []
        assert client.jobs[0].params["max_chars"] == 6000

    def test_malformed_results_raise(self) -> None:
        ops, _ = make_ops({"checks.failed_logs": {"nope": 1}})
        with pytest.raises(GithubOpsError, match="no check list"):
            ops.checks_failed_logs("o/r", "abc")
        ops, _ = make_ops({"checks.failed_logs": {"checks": [{"name": "unit"}]}})
        with pytest.raises(GithubOpsError, match="malformed entry"):
            ops.checks_failed_logs("o/r", "abc")
        ops, _ = make_ops({"checks.failed_logs": {"checks": ["unit"]}})
        with pytest.raises(GithubOpsError, match="malformed entry"):
            ops.checks_failed_logs("o/r", "abc")

    def test_worker_failure_raises(self) -> None:
        ops, _ = make_ops({"checks.failed_logs": "FAIL"})
        with pytest.raises(GithubOpsError, match="HTTP 403"):
            ops.checks_failed_logs("o/r", "abc")


class PathStubClient(StubWorkerClient):
    """``raw.api`` replies keyed by request path, for methods that make
    more than one raw call."""

    def submit(self, job: JobRequest) -> JobResult:
        self.jobs.append(job)
        assert job.op == "raw.api"
        return JobResult(
            job_id=job.job_id, status="ok", output_json=self.responses.get(job.params["path"])
        )


def review(login: str, state: str, body: str = "") -> dict[str, Any]:
    return {"user": {"login": login}, "state": state, "body": body}


def inline(login: str, body: str, path: str = "", line: int | None = None) -> dict[str, Any]:
    return {"user": {"login": login}, "body": body, "path": path, "line": line}


def make_feedback_ops(
    reviews: list[dict[str, Any]], comments: list[dict[str, Any]]
) -> tuple[GithubOps, PathStubClient]:
    client = PathStubClient(
        {"/repos/o/r/pulls/7/reviews": reviews, "/repos/o/r/pulls/7/comments": comments}
    )
    return GithubOps(client, "r1"), client  # type: ignore[arg-type]


class TestPrReviewFeedback:
    def test_renders_standing_objections_and_inline_comments(self) -> None:
        ops, client = make_feedback_ops(
            [
                review("alice", "CHANGES_REQUESTED", "Please handle the empty case."),
                review("bob", "COMMENTED", "drive-by"),
            ],
            [
                inline("alice", "off by one", path="src/x.py", line=12),
                inline("alice", "rename this", path="src/y.py"),
                inline("carol", "no anchor"),
                inline("carol", "   "),
            ],
        )
        out = ops.pr_review_feedback("o/r", 7)
        assert out == (
            "Please handle the empty case.\n\n"
            "- `src/x.py:12`: off by one\n\n"
            "- `src/y.py`: rename this\n\n"
            "- no anchor"
        )
        assert [j.params["path"] for j in client.jobs] == [
            "/repos/o/r/pulls/7/reviews",
            "/repos/o/r/pulls/7/comments",
        ]

    def test_latest_verdict_per_reviewer_wins(self) -> None:
        """A CHANGES_REQUESTED a later APPROVE cleared no longer stands."""
        ops, _ = make_feedback_ops(
            [
                review("alice", "CHANGES_REQUESTED", "fix it"),
                review("alice", "APPROVED", "thanks"),
                review("bob", "APPROVED"),
                review("bob", "CHANGES_REQUESTED", "wait, no"),
            ],
            [],
        )
        assert ops.pr_review_feedback("o/r", 7) == "wait, no"

    def test_excludes_the_loops_own_identity(self) -> None:
        ops, _ = make_feedback_ops(
            [
                review("sbxloop-bot", "CHANGES_REQUESTED", "my own review"),
                review("alice", "CHANGES_REQUESTED", "a human objection"),
            ],
            [
                inline("sbxloop-bot", "my own inline", path="a.py", line=1),
                inline("alice", "human inline", path="b.py", line=2),
            ],
        )
        out = ops.pr_review_feedback("o/r", 7, exclude_login="sbxloop-bot")
        assert out == "a human objection\n\n- `b.py:2`: human inline"

    def test_clips(self) -> None:
        ops, _ = make_feedback_ops([review("alice", "CHANGES_REQUESTED", "x" * 100)], [])
        assert ops.pr_review_feedback("o/r", 7, clip=10) == "x" * 10

    def test_nothing_standing_is_empty(self) -> None:
        ops, _ = make_feedback_ops([review("alice", "APPROVED", "lgtm")], [])
        assert ops.pr_review_feedback("o/r", 7) == ""
        ops, _ = make_feedback_ops([], [])
        assert ops.pr_review_feedback("o/r", 7) == ""

    def test_tolerates_malformed_payloads(self) -> None:
        client = PathStubClient(
            {"/repos/o/r/pulls/7/reviews": {"message": "nope"}, "/repos/o/r/pulls/7/comments": [1]}
        )
        ops = GithubOps(client, "r1")  # type: ignore[arg-type]
        assert ops.pr_review_feedback("o/r", 7) == ""


class TestLanding:
    """The primitives the daemon's landing stage merges a PR with.

    All four go through ``raw.api``, so what these pin is the request shape
    and — far more importantly — which GitHub answers come back as *data*
    rather than as exceptions. A refusal misread as an error costs a retry
    loop; an error misread as success merges nothing and says it did.
    """

    def test_ready_for_review_sends_the_mutation_and_reports_ready(self) -> None:
        ops, client = make_ops(
            {
                "raw.api": {
                    "data": {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}}
                }
            }
        )
        assert ops.pr_ready_for_review("PR_node1") is True
        params = client.jobs[0].params
        assert params["method"] == "POST"
        assert params["path"] == "/graphql"
        assert params["body"]["variables"] == {"id": "PR_node1"}
        assert "markPullRequestReadyForReview" in params["body"]["query"]

    def test_ready_for_review_reads_the_body_not_the_status(self) -> None:
        """GraphQL answers a failed mutation with 200 and an ``errors`` array.

        Trusting the status here would report a still-draft PR as ready, and
        a draft cannot be merged — the loop would then spend its whole merge
        budget on a refusal it had caused itself.
        """
        ops, _ = make_ops({"raw.api": {"errors": [{"message": "Could not resolve to a node"}]}})
        with pytest.raises(GithubOpsError, match="Could not resolve"):
            ops.pr_ready_for_review("PR_bogus")

    def test_ready_for_review_fails_closed_on_an_empty_answer(self) -> None:
        """No error and no pull request is not an answer to act on."""
        ops, _ = make_ops({"raw.api": {"data": {"markPullRequestReadyForReview": None}}})
        with pytest.raises(GithubOpsError, match="no pull request"):
            ops.pr_ready_for_review("PR_node1")

    def test_ready_for_review_still_draft_is_not_ready(self) -> None:
        ops, _ = make_ops(
            {
                "raw.api": {
                    "data": {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": True}}}
                }
            }
        )
        assert ops.pr_ready_for_review("PR_node1") is False

    def test_merge_sends_the_decided_sha(self) -> None:
        """The sha the caller judged rides on the request, so a push that
        landed since loses the race with a 409 instead of being merged over."""
        ops, client = make_ops({"raw.api": {"merged": True, "sha": "cafe", "message": "ok"}})
        outcome = ops.pr_merge("o/r", 42, method="squash", sha="beef")
        assert (outcome.merged, outcome.sha) == (True, "cafe")
        params = client.jobs[0].params
        assert params["method"] == "PUT"
        assert params["path"] == "/repos/o/r/pulls/42/merge"
        assert params["body"] == {"merge_method": "squash", "sha": "beef"}

    def test_merge_refusal_is_data_not_an_exception(self) -> None:
        """405 is GitHub's blanket "not mergeable right now", and nothing the
        loop can retry fixes it — so it must reach the caller as a verdict."""
        ops, _ = make_ops({"raw.api": Fails("pr_not_mergeable_405")})
        outcome = ops.pr_merge("o/r", 42, sha="beef")
        assert (outcome.merged, outcome.blocked, outcome.stale) == (False, True, False)
        assert "not mergeable" in outcome.reason

    def test_merge_head_moved_is_stale_not_blocked(self) -> None:
        """409 is a race, not a refusal: the next poll judges the new head."""
        ops, _ = make_ops({"raw.api": Fails("pr_head_moved_409")})
        outcome = ops.pr_merge("o/r", 42, sha="beef")
        assert (outcome.merged, outcome.stale, outcome.blocked) == (False, True, False)

    def test_merge_without_a_merged_flag_is_blocked(self) -> None:
        """A 200 that does not claim a merge has not made one."""
        ops, _ = make_ops({"raw.api": {"message": "nope"}})
        outcome = ops.pr_merge("o/r", 42, sha="beef")
        assert (outcome.merged, outcome.blocked) == (False, True)

    def test_merge_other_failures_still_raise(self) -> None:
        ops, _ = make_ops({"raw.api": "FAIL"})
        with pytest.raises(GithubOpsError):
            ops.pr_merge("o/r", 42, sha="beef")

    def test_update_branch_sends_the_expected_head(self) -> None:
        ops, client = make_ops({"raw.api": {"message": "Updating pull request branch."}})
        assert ops.pr_update_branch("o/r", 42, expected_head_sha="beef") is True
        params = client.jobs[0].params
        assert params["path"] == "/repos/o/r/pulls/42/update-branch"
        assert params["body"] == {"expected_head_sha": "beef"}

    def test_update_branch_refusal_is_false_not_an_exception(self) -> None:
        """422 covers "already current" and "your expected head moved". The
        next poll re-reads the PR either way, so neither is worth raising."""
        ops, _ = make_ops({"raw.api": Fails("update_branch_refused_422")})
        assert ops.pr_update_branch("o/r", 42, expected_head_sha="beef") is False

    def test_branch_delete_tolerates_an_absent_ref(self) -> None:
        """The merge already happened; a branch a repo setting removed first
        must not be reported as a failure of it."""
        ops, _ = make_ops({"raw.api": Fails("ref_missing_404")})
        ops.branch_delete("o/r", "sbxloop/r42")  # must not raise

    def test_branch_delete_reraises_anything_else(self) -> None:
        ops, _ = make_ops({"raw.api": "FAIL"})
        with pytest.raises(GithubOpsError):
            ops.branch_delete("o/r", "sbxloop/r42")
