"""Host GithubOps facade + GithubReporterHook tests (stubbed WorkerClient)."""

from __future__ import annotations

from typing import Any, NamedTuple

import pytest

from sbxloop.errors import GithubOpsError
from sbxloop.events import EventBus, HostEventTypes
from sbxloop.gh.ops import GithubOps, IssueRef
from sbxloop.gh.reporter import GithubReporterHook
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


class RecordingOps:
    """GithubOps stand-in recording reporter interactions."""

    def __init__(self, existing_issues: list[dict[str, Any]] | None = None) -> None:
        self.created: list[tuple[str, str]] = []
        self.comments: list[tuple[int, str]] = []
        self.searches: list[str] = []
        self.raw_calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.existing_issues = existing_issues or []

    def issue_create(self, repo: str, title: str, body: str = "", labels: Any = None) -> IssueRef:
        self.created.append((repo, title))
        return IssueRef(number=42, url="https://x/42")

    def issue_comment(self, repo: str, number: int, body: str) -> str:
        self.comments.append((number, body))
        return "https://c"

    def search_issues(self, query: str, per_page: int = 30) -> list[dict[str, Any]]:
        self.searches.append(query)
        return self.existing_issues

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.raw_calls.append((method, path, body))
        return {}


class TestGithubReporterHook:
    """Run start/end are explicit open_run/close_run calls (the engine emits
    the bus lifecycle events outside the hook's attach window — #58); only
    task progress arrives via the bus."""

    def make(
        self, existing_issues: list[dict[str, Any]] | None = None
    ) -> tuple[GithubReporterHook, RecordingOps, EventBus]:
        ops = RecordingOps(existing_issues)
        hook = GithubReporterHook(ops, "o/r")  # type: ignore[arg-type]
        bus = EventBus()
        bus.attach_hook(hook)
        return hook, ops, bus

    def test_full_run_reporting(self) -> None:
        hook, ops, bus = self.make()
        hook.open_run("r1", "do the thing")
        bus.emit(HostEventTypes.TASK_END, "r1", task_id="t1", title="first", state="done")
        bus.emit(HostEventTypes.TASK_END, "r1", task_id="t2", title="second", state="failed")
        hook.close_run("r1", "completed")

        assert ops.created == [("o/r", "sbxloop run r1")]
        assert len(ops.comments) == 3
        assert "✅ `t1`" in ops.comments[0][1]
        assert "❌ `t2`" in ops.comments[1][1]
        final = ops.comments[2][1]
        assert "finished: **completed**" in final
        assert "`t1` first" in final
        # a completed run closes its tracking issue
        assert ops.raw_calls == [
            ("PATCH", "/repos/o/r/issues/42", {"state": "closed", "state_reason": "completed"})
        ]

    def test_failed_run_leaves_the_issue_open(self) -> None:
        hook, ops, _bus = self.make()
        hook.open_run("r1", "do the thing")
        hook.close_run("r1", "failed")
        assert any("finished: **failed**" in body for _n, body in ops.comments)
        assert ops.raw_calls == []

    def test_resume_reuses_existing_tracking_issue(self) -> None:
        hook, ops, _bus = self.make(
            existing_issues=[{"number": 7, "title": "sbxloop run r1", "html_url": "https://x/7"}]
        )
        hook.open_run("r1", "again")
        assert ops.created == []  # found, not duplicated
        assert hook.issue is not None and hook.issue.number == 7

    def test_task_events_without_open_run_are_ignored(self) -> None:
        hook, ops, bus = self.make()
        bus.emit(HostEventTypes.TASK_END, "r1", task_id="t1", state="done")
        hook.close_run("r1", "failed")
        assert ops.created == []
        assert ops.comments == []

    def test_reporting_failure_is_swallowed(self) -> None:
        class ExplodingOps(RecordingOps):
            def issue_create(self, *a: Any, **k: Any) -> IssueRef:
                raise RuntimeError("github down")

        hook = GithubReporterHook(ExplodingOps(), "o/r")  # type: ignore[arg-type]
        hook.open_run("r1", "x")  # must not raise
        assert hook.issue is None
        hook.close_run("r1", "completed")  # must not raise either

    def test_close_run_failure_is_swallowed(self) -> None:
        class ExplodingComment(RecordingOps):
            def issue_comment(self, *a: Any, **k: Any) -> str:
                raise RuntimeError("github down")

        hook = GithubReporterHook(ExplodingComment(), "o/r")  # type: ignore[arg-type]
        hook.open_run("r1", "x")
        hook.close_run("r1", "completed")  # must not raise

    def test_unrelated_events_ignored(self) -> None:
        _hook, ops, bus = self.make()
        bus.emit("agent.message", "r1", content="hi")
        assert ops.created == []


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
