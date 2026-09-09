"""Run/worker/sandbox recovery integration. Executed by CI's slow shard."""

from __future__ import annotations

import pytest

from sbxloop.provider import ProviderRecovery
from tests.unit.test_engine import BUILD, Harness, task, taskgraph
from tests.unit.test_engine_workload import PASS


@pytest.mark.parametrize("kind", ["code", "workload"])
def test_provider_hold_preserves_work_and_resumes_same_session(
    fake_sbx, tmp_path, monkeypatch, kind
):
    harness = Harness(fake_sbx, tmp_path, monkeypatch)
    harness.script(
        [
            taskgraph(task("t1", verify=["test -f partial.txt"])),
            {
                "text": "Created partial.txt",
                "files": {"partial.txt": "retained\n"},
                "session_id": "interrupted-session",
                "provider_failure": {
                    "backend": "claude",
                    "category": "billing",
                    "reason": "Insufficient credit",
                    "partial_progress": True,
                    "http_status": 400,
                },
            },
        ]
    )
    engine = harness.engine()
    result = engine.start("write the file", kind=kind)
    assert result.state == "provider_held"
    run_id = result.run_id
    task_row = engine.store.get_tasks(run_id)[0]
    assert task_row.state == "executing"
    assert task_row.revisions == task_row.replans == 0
    assert not [
        row for row in engine.store.phase_attempts(run_id) if row.phase in ("verify", "judge")
    ]
    assert harness.consumed() == 2
    assert harness.sandboxes_left()
    assert (result.workspace / "partial.txt").read_text() == "retained\n"
    # A new engine is a daemon restart; no provisioning/setup or task
    # repair may replay the earlier operation, and the SDK session survives.
    harness.script([BUILD, *([PASS] if kind == "workload" else [])])
    resumed_engine = harness.engine(keep_sandboxes=True)
    resumed = resumed_engine.resume(run_id)
    assert resumed.state == "completed"
    requests = harness.agent_jobs(run_id)
    continued = [request for request in requests if request.get("require_resume")]
    assert len(continued) == 1
    assert continued[0]["resume_session_id"] == "interrupted-session"
    assert "do not repeat" in continued[0]["prompt"]
    assert not ProviderRecovery(resumed_engine.store, engine.config.agent.backend).pending(run_id)
    assert (resumed.workspace / "partial.txt").read_text() == "retained\n"
