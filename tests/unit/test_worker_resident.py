"""The resident transport: one worker process per sandbox, jobs on its
stdin, events and results on its stdout.

Field (db, 2026-09-19): every `sbx exec` and `sbx cp` costs ~1.1s of
round trip through the sandbox backend, whatever it runs, so the one
process per job protocol paid three of them per job (cp the job in, exec
the worker, cp the result out) and one more per host-tool response. A
resident worker pays the exec once per sandbox.

These run the real worker (sys.executable has sbxloop_worker importable)
under the fake sbx with a live stdin pipe (SBX_FAKE_EXEC_STDIN=stream).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from sbxloop.errors import WorkerError, WorkerTimeoutError
from sbxloop.events import Event, EventBus
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxSpec
from sbxloop.sbx.sandbox import Sandbox
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import EventTypes, HostToolCall, HostToolResponse, JobRequest
from tests.conftest import FakeSbx


@pytest.fixture(autouse=True)
def resident_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBXLOOP_WORKER_BACKEND", "echo")
    # The fake forwards a live stdin pipe to the exec'd process, the way
    # sbx does where the exec-stdin-env probe passes.
    monkeypatch.setenv("SBX_FAKE_EXEC_STDIN", "stream")


@pytest.fixture
def sandbox(fake_sbx: FakeSbx, tmp_path: Path) -> Sandbox:
    cli = SbxCLI(binary=str(fake_sbx.binary))
    cli.create(SandboxSpec(name="boxa", role="agent", workspace=tmp_path))
    return Sandbox(cli, "boxa")


def make_client(sandbox: Sandbox, bus: EventBus | None = None, **kwargs: object) -> WorkerClient:
    kwargs.setdefault("python", sys.executable)
    kwargs.setdefault("transport", "resident")
    # Stdin delivery is what makes a resident worker possible; a provider
    # (even an empty one) is the provisioner's word that it works here.
    kwargs.setdefault("job_env", lambda: {"SBXLOOP_TEST_TOKEN": "t0k3n"})
    return WorkerClient(sandbox, bus or EventBus(), **kwargs)  # type: ignore[arg-type]


def job(job_id: str = "j1", **overrides: object) -> JobRequest:
    base: dict[str, object] = {
        "job_id": job_id,
        "run_id": "r1",
        "kind": "agent.session",
        "prompt": f"ping {job_id}",
    }
    base.update(overrides)
    return JobRequest.model_validate(base)


def script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, responses: list[dict]) -> None:
    path = tmp_path / "script.json"
    path.write_text(json.dumps(responses))
    monkeypatch.setenv("SBXLOOP_ECHO_SCRIPT", str(path))


def sbx_calls(fake_sbx: FakeSbx) -> list[str]:
    return [c[0] for c in fake_sbx.invocations() if c and c[0] in ("exec", "cp")]


class TestOneProcessPerSandbox:
    def test_two_jobs_share_one_exec_and_copy_nothing(
        self, sandbox: Sandbox, fake_sbx: FakeSbx
    ) -> None:
        client = make_client(sandbox)
        before = len(fake_sbx.invocations())
        first = client.submit(job("j1"))
        second = client.submit(job("j2"))
        assert first.status == "ok" and first.output_text == "echo: ping j1"
        assert second.status == "ok" and second.output_text == "echo: ping j2"
        # One exec started the resident worker; no cp staged a job or
        # fetched a result, and no second exec ran the second job.
        later = [c[0] for c in fake_sbx.invocations()[before:] if c[0] in ("exec", "cp")]
        assert later == ["exec"]
        client.close()

    def test_events_reach_the_bus_tagged_with_their_job(self, sandbox: Sandbox) -> None:
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(seen.append)
        client = make_client(sandbox, bus)
        client.submit(job("j1"))
        client.submit(job("j2"))
        starts = [e for e in seen if e.type == EventTypes.WORKER_START]
        assert [e.job_id for e in starts] == ["j1", "j2"]
        assert all(e.run_id == "r1" for e in seen)
        messages = [e for e in seen if e.type == EventTypes.AGENT_MESSAGE]
        assert [e.data["content"] for e in messages] == ["echo: ping j1", "echo: ping j2"]
        client.close()

    def test_the_delivered_env_reaches_the_job(
        self, sandbox: Sandbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The credentials travel the pipe once, before the first job, and
        again only when they change (a rotating installation token)."""
        script(
            tmp_path,
            monkeypatch,
            [
                {"text": "one", "events": [{"type": "worker.probe", "data": {}}]},
                {"text": "two"},
            ],
        )
        exports = {"SBXLOOP_TEST_TOKEN": "first"}
        client = make_client(sandbox, job_env=lambda: dict(exports))
        seen_env: list[str] = []
        # The echo backend runs inside the worker; read the env it saw
        # through a file the job writes.
        marker = tmp_path / "env-seen"
        check = ["sh", "-c", f"printenv SBXLOOP_TEST_TOKEN > {marker}"]
        client.submit(job("j1", kind="shell.check", argv=check, prompt=None))
        seen_env.append(marker.read_text().strip())
        exports["SBXLOOP_TEST_TOKEN"] = "rotated"
        client.submit(job("j2", kind="shell.check", argv=check, prompt=None))
        seen_env.append(marker.read_text().strip())
        assert seen_env == ["first", "rotated"]
        client.close()


class TestHostTools:
    def test_a_tool_response_is_answered_without_a_copy(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script(
            tmp_path,
            monkeypatch,
            [{"text": "asked", "host_tool_calls": [{"name": "answer", "arguments": {"n": 41}}]}],
        )
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(seen.append)

        def handler(call: HostToolCall) -> HostToolResponse:
            return HostToolResponse(
                call_id=call.call_id, ok=True, text=str(call.arguments["n"] + 1)
            )

        client = make_client(sandbox, bus)
        before = len(fake_sbx.invocations())
        result = client.submit(
            job("j1", host_tools=[{"name": "answer", "description": "adds one", "parameters": {}}]),
            tool_handler=handler,
        )
        assert result.status == "ok" and result.output_text == "asked\n42"
        responses = [e for e in seen if e.type == EventTypes.AGENT_TOOL_RESPONSE]
        assert responses and responses[0].data["ok"] is True
        later = [c[0] for c in fake_sbx.invocations()[before:] if c[0] in ("exec", "cp")]
        # The one exec is the resident worker; the response rode its stdin
        # and the tools directory was cleaned inside the VM, not by an exec.
        assert later == ["exec"]
        assert client._brokers == {}
        assert not (fake_sbx.sandbox_fs("boxa") / "home/agent/.sbxloop/tools/j1").exists()
        client.close()


class TestFailure:
    def test_a_timeout_cancels_the_job_and_keeps_the_worker(
        self, sandbox: Sandbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script(tmp_path, monkeypatch, [{"text": "slow", "sleep_s": 30}, {"text": "quick"}])
        client = make_client(sandbox, grace_s=0.5)
        with pytest.raises(WorkerTimeoutError):
            client.submit(job("j1", timeout_s=1.0))
        # The resident worker survives one job's timeout: the next job runs.
        result = client.submit(job("j2"))
        assert result.status == "ok" and result.output_text == "quick"
        client.close()

    def test_a_dead_worker_fails_the_job_and_the_next_submit_restarts_it(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script(tmp_path, monkeypatch, [{"text": "first"}, {"text": "after restart"}])
        client = make_client(sandbox)
        assert client.submit(job("j1")).output_text == "first"
        resident = client._resident
        assert resident is not None and resident.alive
        # The sandbox reboots underneath (or the process is killed): the
        # exec ends.
        resident.proc.kill()
        deadline = time.monotonic() + 10
        while resident.alive and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not resident.alive
        before = len(fake_sbx.invocations("exec"))
        assert client.submit(job("j2")).output_text == "after restart"
        assert len(fake_sbx.invocations("exec")) == before + 1
        client.close()

    def test_a_job_lost_mid_flight_raises_a_worker_error(
        self, sandbox: Sandbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script(tmp_path, monkeypatch, [{"text": "never", "sleep_s": 30}])
        client = make_client(sandbox, grace_s=0.5)
        errors: list[BaseException] = []

        def run() -> None:
            try:
                client.submit(job("j1", timeout_s=20))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        resident = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            resident = client._resident
            if resident is not None and "j1" in resident.pending:
                break
            time.sleep(0.05)
        assert resident is not None
        resident.proc.kill()
        thread.join(10)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], WorkerError)
        assert "resident worker" in str(errors[0])
        client.close()


class TestFallback:
    def test_without_stdin_delivery_the_client_streams_as_before(
        self, sandbox: Sandbox, fake_sbx: FakeSbx
    ) -> None:
        """No job_env provider means this sbx does not pass exec stdin
        through, so a resident worker could never be spoken to."""
        client = make_client(sandbox, job_env=None)
        before = len(fake_sbx.invocations())
        assert client.submit(job("j1")).output_text == "echo: ping j1"
        later = [c[0] for c in fake_sbx.invocations()[before:] if c[0] in ("exec", "cp")]
        assert later == ["cp", "exec", "cp"]
        assert client._resident is None
