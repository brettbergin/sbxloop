"""Host GithubOps facade + GithubReporterHook tests (stubbed WorkerClient)."""

from __future__ import annotations

from typing import Any

import pytest

from sbxloop.errors import GithubOpsError
from sbxloop.events import EventBus, HostEventTypes
from sbxloop.gh.ops import GithubOps, IssueRef
from sbxloop.gh.reporter import GithubReporterHook
from sbxloop_worker.protocol import ErrorInfo, JobRequest, JobResult


class StubWorkerClient:
    """Records github.op jobs and replies from a canned op->json map."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.jobs: list[JobRequest] = []

    def submit(self, job: JobRequest) -> JobResult:
        self.jobs.append(job)
        assert job.op is not None
        response = self.responses.get(job.op)
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

    def test_error_result_raises(self) -> None:
        ops, _ = make_ops({"issue.create": "FAIL"})
        with pytest.raises(GithubOpsError, match="HTTP 403"):
            ops.issue_create("o/r", "T")


class RecordingOps:
    """GithubOps stand-in recording reporter interactions."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []
        self.comments: list[tuple[int, str]] = []

    def issue_create(self, repo: str, title: str, body: str = "", labels: Any = None) -> IssueRef:
        self.created.append((repo, title))
        return IssueRef(number=42, url="https://x/42")

    def issue_comment(self, repo: str, number: int, body: str) -> str:
        self.comments.append((number, body))
        return "https://c"


class TestGithubReporterHook:
    def make(self) -> tuple[GithubReporterHook, RecordingOps, EventBus]:
        ops = RecordingOps()
        hook = GithubReporterHook(ops, "o/r")  # type: ignore[arg-type]
        bus = EventBus()
        bus.attach_hook(hook)
        return hook, ops, bus

    def test_full_run_reporting(self) -> None:
        _hook, ops, bus = self.make()
        bus.emit(HostEventTypes.RUN_START, "r1", outcome="do the thing")
        bus.emit(HostEventTypes.TASK_END, "r1", task_id="t1", title="first", state="done")
        bus.emit(HostEventTypes.TASK_END, "r1", task_id="t2", title="second", state="failed")
        bus.emit(HostEventTypes.RUN_END, "r1", state="completed")

        assert ops.created == [("o/r", "sbxloop run r1")]
        assert len(ops.comments) == 3
        assert "✅ `t1`" in ops.comments[0][1]
        assert "❌ `t2`" in ops.comments[1][1]
        final = ops.comments[2][1]
        assert "finished: **completed**" in final
        assert "`t1` first" in final

    def test_task_events_before_run_start_are_ignored(self) -> None:
        _hook, ops, bus = self.make()
        bus.emit(HostEventTypes.TASK_END, "r1", task_id="t1", state="done")
        bus.emit(HostEventTypes.RUN_END, "r1", state="failed")
        assert ops.created == []
        assert ops.comments == []

    def test_reporting_failure_is_swallowed(self) -> None:
        class ExplodingOps(RecordingOps):
            def issue_create(self, *a: Any, **k: Any) -> IssueRef:
                raise RuntimeError("github down")

        hook = GithubReporterHook(ExplodingOps(), "o/r")  # type: ignore[arg-type]
        bus = EventBus()
        bus.attach_hook(hook)
        bus.emit(HostEventTypes.RUN_START, "r1", outcome="x")  # must not raise
        assert hook.issue is None

    def test_unrelated_events_ignored(self) -> None:
        _hook, ops, bus = self.make()
        bus.emit("agent.message", "r1", content="hi")
        assert ops.created == []
