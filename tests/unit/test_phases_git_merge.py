"""A conflict merge is sent to the agent worker, never run on the host."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop import hostgit
from sbxloop.config import Config
from sbxloop.engine.phases import PhaseRunner
from sbxloop.errors import WorkerError
from sbxloop_worker.protocol import ErrorInfo, JobRequest, JobResult
from sbxloop_worker.runner import JobRunner
from tests.unit.test_hostgit import git, make_run_clone, push_upstream_commit


class RecordingAgent:
    def __init__(self, *, fail: bool = False) -> None:
        self.jobs: list[JobRequest] = []
        self.fail = fail

    def submit(self, job: JobRequest) -> JobResult:
        self.jobs.append(job)
        if self.fail:
            return JobResult(
                job_id=job.job_id,
                status="error",
                error=ErrorInfo(type="GitMergeError", message="fetch failed"),
            )
        return JobResult(
            job_id=job.job_id,
            status="ok",
            output_json={"merged": False, "conflicts": ["file.txt"], "message": "conflict"},
        )


def test_merge_is_dispatched_to_agent_workdir() -> None:
    agent = RecordingAgent()
    phases = PhaseRunner(agent, Config(), "r1", "fix it", workdir="/workspace")  # type: ignore[arg-type]
    result = phases.merge_from_base("develop", base_sha="a" * 40)
    assert result.conflicts == ("file.txt",)
    (job,) = agent.jobs
    assert job.kind == "git.merge"
    assert job.cwd == "/workspace"
    assert job.params == {"base_branch": "develop", "base_sha": "a" * 40}


def test_worker_failure_reaches_the_fix_round() -> None:
    phases = PhaseRunner(RecordingAgent(fail=True), Config(), "r1", "fix it", workdir="/workspace")  # type: ignore[arg-type]
    with pytest.raises(WorkerError, match="fetch failed"):
        phases.merge_from_base("main", base_sha="a" * 40)


def test_worker_merge_preserves_conflict_workflow(tmp_path: Path) -> None:
    upstream, clone = make_run_clone(tmp_path)
    push_upstream_commit(tmp_path, upstream)
    (clone / "new.txt").write_text("work in progress\n")
    # A repository hook is permitted inside the worker's synthetic agent
    # fixture. Its execution must happen only when the worker job runs.
    marker = tmp_path / "worker-hook-ran"
    hook = clone / ".git/hooks/post-commit"
    hook.write_text(f'#!/bin/sh\nprintf marker > "{marker}"\n')
    hook.chmod(0o700)
    with hostgit.base_bundle(clone, str(upstream), "main", token=None) as (sha, bundle):
        assert bundle is not None
        job = JobRequest(
            job_id="j1",
            run_id="r1",
            kind="git.merge",
            cwd=str(clone),
            params={"base_branch": "main", "base_sha": sha, "bundle_path": str(bundle)},
        )
        assert not marker.exists()
        result = JobRunner(job, tmp_path / "events", tmp_path / "result", heartbeat_s=0).run()
    assert result.status == "ok"
    assert result.output_json["merged"] is True
    assert marker.exists()
    assert (clone / "new.txt").read_text() == "work in progress\n"
    assert (clone / "pusher.txt").exists()
    git("diff", "--exit-code", cwd=clone)
