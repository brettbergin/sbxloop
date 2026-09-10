from pathlib import Path

import pytest

from sbxloop.events import EventBus
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.sandbox import Sandbox
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import Event, JobRequest


def test_worker_event_causes_host_secret_attachment(tmp_path: Path, monkeypatch):
    import asyncio
    import sys
    import types

    from sbxloop.events import EventBus
    from sbxloop.worker.client import WorkerClient
    from sbxloop_worker.protocol import Event, JobRequest
    from tests.unit.test_daemon_discord import make_bridge

    secret = tmp_path / "outside-workspace" / "secrets.env"
    secret.parent.mkdir()
    marker = b"AUDIT_SYNTHETIC_HOST_SECRET=attachment-reproduction"
    secret.write_bytes(marker)
    fake = types.ModuleType("discord")

    class File:
        def __init__(self, path, filename=None):
            self.data = Path(path).read_bytes()

    fake.File = File
    monkeypatch.setitem(sys.modules, "discord", fake)
    bridge, chat_client, _ = make_bridge(tmp_path / "bridge", max_attachment_bytes=1000)
    bus = EventBus()
    accepted = []
    bus.subscribe(accepted.append)
    client = WorkerClient(types.SimpleNamespace(name="audit-agent"), bus, role="agent")
    job = JobRequest(job_id="audit-job", run_id="audit", kind="agent.session", prompt="do the task")
    # Bytes an agent can append to its own event log; no host publisher is invoked.
    forged = Event.now(
        "run.published",
        "audit",
        job_id="audit-job",
        sink="chat",
        message="result",
        paths=[str(secret)],
    )
    client._handle_line(job, forged.to_json_line())
    assert accepted == []
    channel = chat_client.channels[42]
    for event in accepted:
        for chunk in bridge._render("audit", event):
            asyncio.run(bridge._send(channel, chunk.text, embed=chunk.embed, files=chunk.files))
    assert channel.sent_kwargs == []


@pytest.mark.parametrize(
    "event_type", ["run.published", "run.merged", "chat.reply", "future.event"]
)
def test_worker_cannot_publish_host_or_unknown_events(event_type: str) -> None:
    bus = EventBus()
    received = []
    bus.subscribe(received.append)
    client = WorkerClient(Sandbox(SbxCLI(), "unused"), bus)
    job = JobRequest(job_id="j1", run_id="r1", kind="agent.session", prompt="task")
    assert client._handle_line(job, Event.now(event_type, "r1", job_id="j1").to_json_line()) is None
    assert received == []


@pytest.mark.parametrize("run_id,job_id", [("other", "j1"), ("r1", "other")])
def test_worker_cannot_impersonate_another_job(run_id: str, job_id: str) -> None:
    client = WorkerClient(Sandbox(SbxCLI(), "unused"))
    job = JobRequest(job_id="j1", run_id="r1", kind="agent.session", prompt="task")
    assert (
        client._handle_line(
            job, Event.now("agent.message", run_id, job_id=job_id, text="hello").to_json_line()
        )
        is None
    )


def test_ordinary_worker_events_are_bound_to_the_transport() -> None:
    client = WorkerClient(Sandbox(SbxCLI(), "unused"), role="agent")
    job = JobRequest(job_id="j1", run_id="r1", kind="agent.session", prompt="task")
    event = client._handle_line(
        job, Event.now("sandbox.resources", "r1", role="github").to_json_line()
    )
    assert event is not None
    assert (event.run_id, event.job_id, event.data["role"]) == ("r1", "j1", "agent")


def test_genuine_host_publications_still_reach_the_bus() -> None:
    bus = EventBus()
    received = []
    bus.subscribe(received.append)
    bus.emit("run.published", "r1", sink="chat", paths=["host-approved-artifact"])
    assert received[0].data["paths"] == ["host-approved-artifact"]


def test_host_model_attribution_does_not_overwrite_sdk_reported_model(monkeypatch) -> None:
    from sbxloop_worker.protocol import JobResult

    client = WorkerClient(Sandbox(SbxCLI(), "unused"), backend="claude")
    job = JobRequest(job_id="j1", run_id="r1", kind="agent.session", prompt="task", model="auto")
    received = []
    client.bus.subscribe(received.append)

    def submit(request):
        client._handle_line(
            request,
            Event.now(
                "agent.usage",
                "r1",
                job_id="j1",
                model="actual-model",
                requested_model="forged",
                agent_phase="forged",
                model_source="forged",
                input_tokens=10,
            ).to_json_line(),
        )
        return JobResult(job_id="j1", status="ok")

    monkeypatch.setattr(client, "_submit", submit)
    client.submit(job, agent="operator", agent_phase="operator_plan", model_source="model")
    data = received[0].data
    assert data["model"] == "actual-model" and data["requested_model"] == "auto"
    assert data["agent_phase"] == "operator_plan" and data["model_source"] == "model"
    assert data["backend"] == "claude" and data["agent"] == "operator"
    assert client._model_context == {}
