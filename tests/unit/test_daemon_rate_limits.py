"""Status probes use the active agent's credential context without lifecycle recovery."""

from types import SimpleNamespace as NS

import pytest

from sbxloop.config import Config
from sbxloop.daemon.agentbox import DaemonAgent
from sbxloop.errors import WorkerError, WorkerTimeoutError
from sbxloop.events import EventBus
from sbxloop_worker.protocol import JobResult
from sbxloop_worker.rate_limits import RateLimitReport


@pytest.mark.parametrize("backend", ["copilot", "claude", "codex"])
@pytest.mark.parametrize(
    "outcome", ["success", "worker_error", "timeout", "invalid", "wrong_backend"]
)
def test_query_uses_separate_client_in_same_agent_sandbox(tmp_path, monkeypatch, backend, outcome):
    from sbxloop.daemon import agentbox

    config = Config.model_validate({"home": str(tmp_path), "agent": {"backend": backend}})
    agent = DaemonAgent(config, object(), EventBus(), worker_python="python")

    def credential_context():
        return {"SECRET": "never-on-argv"}

    active = NS(sandbox=object(), python="agent-python", job_env=credential_context)
    agent._client = active
    agent._sandbox = active.sandbox
    probes = []
    jobs = []

    class Probe:
        def __init__(self, sandbox, bus, **kwargs):
            probes.append(kwargs)
            assert sandbox is active.sandbox and bus is agent.bus
            assert kwargs["job_env"] is credential_context
            assert kwargs["python"] == "agent-python" and kwargs["role"] == "agent"
            assert kwargs["backend"] == backend and kwargs["grace_s"] == 2

        def submit(self, job):
            jobs.append(job)
            if outcome == "worker_error":
                raise WorkerError("never-on-argv")
            if outcome == "timeout":
                raise WorkerTimeoutError("never-on-argv")
            report = RateLimitReport(
                backend=backend if outcome != "wrong_backend" else "other",
                status="unsupported",
                reason="not supported",
            )
            return JobResult(
                job_id=job.job_id,
                status="ok",
                output_json={"secret": "never-on-argv"}
                if outcome == "invalid"
                else report.model_dump(mode="json"),
            )

    monkeypatch.setattr(agentbox, "WorkerClient", Probe)
    report = agent.agent_rate_limits()
    assert report.backend == backend
    expected = "unsupported" if outcome == "success" else "unavailable"
    assert report.status == ("timeout" if outcome == "timeout" else expected)
    assert "never-on-argv" not in report.model_dump_json()
    assert len(probes) == len(jobs) == 1
    assert jobs[0].kind == "agent.rate_limits" and jobs[0].params == {"backend": backend}
    assert jobs[0].prompt is None and jobs[0].resume_session_id is None
    assert jobs[0].host_tools == [] and jobs[0].timeout_s <= 10
    assert agent._client is active and agent._sandbox is active.sandbox
    assert agent._last_reprovision_at is None


def test_status_does_not_provision_an_absent_sandbox(tmp_path):
    agent = DaemonAgent(
        Config.model_validate({"home": str(tmp_path)}), object(), EventBus(), worker_python="python"
    )
    report = agent.agent_rate_limits()
    assert report.status == "unavailable" and agent._client is None
