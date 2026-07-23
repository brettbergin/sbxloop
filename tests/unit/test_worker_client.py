"""WorkerClient transport tests: the REAL worker process through the fake sbx.

sys.executable (the test venv python, which has sdxloop_worker importable) is
used as the in-sandbox interpreter; the fake sbx rewrites the /home/agent
job/event/result paths into the fake sandbox filesystem, so these tests
exercise the genuine end-to-end submit path: write job -> exec worker ->
stream/poll events -> fetch result.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from sdxloop.errors import WorkerError, WorkerTimeoutError
from sdxloop.events import Event, EventBus
from sdxloop.sbx.cli import SbxCLI
from sdxloop.sbx.models import SandboxSpec
from sdxloop.sbx.sandbox import Sandbox
from sdxloop.worker.client import WorkerClient
from sdxloop_worker.protocol import EventTypes, JobRequest
from tests.conftest import FakeSbx


@pytest.fixture(autouse=True)
def echo_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SDXLOOP_WORKER_BACKEND", "echo")


@pytest.fixture
def sandbox(fake_sbx: FakeSbx, tmp_path: Path) -> Sandbox:
    cli = SbxCLI(binary=str(fake_sbx.binary))
    cli.create(SandboxSpec(name="boxa", role="agent", workspace=tmp_path))
    return Sandbox(cli, "boxa")


def make_client(sandbox: Sandbox, bus: EventBus, **kwargs: object) -> WorkerClient:
    kwargs.setdefault("python", sys.executable)
    return WorkerClient(sandbox, bus, **kwargs)  # type: ignore[arg-type]


def agent_job(**overrides: object) -> JobRequest:
    base: dict[str, object] = {
        "job_id": "j1",
        "run_id": "r1",
        "kind": "agent.session",
        "prompt": "ping",
    }
    base.update(overrides)
    return JobRequest.model_validate(base)


class TestStreamTransport:
    def test_submit_end_to_end(self, sandbox: Sandbox, fake_sbx: FakeSbx) -> None:
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(seen.append)

        result = make_client(sandbox, bus).submit(agent_job())

        assert result.status == "ok"
        assert result.output_text == "echo: ping"
        types = [e.type for e in seen]
        assert EventTypes.WORKER_START in types
        assert EventTypes.AGENT_MESSAGE in types
        assert EventTypes.WORKER_END in types

        # durable artifacts exist inside the sandbox fs
        fs = fake_sbx.sandbox_fs("boxa")
        assert (fs / "home/agent/.sdxloop/jobs/j1.json").is_file()
        assert (fs / "home/agent/.sdxloop/results/j1.json").is_file()
        assert (fs / "home/agent/.sdxloop/events/j1.jsonl").is_file()

    def test_shell_check_job(self, sandbox: Sandbox) -> None:
        job = JobRequest(
            job_id="j2",
            run_id="r1",
            kind="shell.check",
            argv=["sh", "-c", "echo verified; exit 3"],
        )
        result = make_client(sandbox, EventBus()).submit(job)
        assert result.status == "ok"
        assert result.exit_code == 3
        assert result.output_text is not None
        assert "verified" in result.output_text

    def test_garbage_stdout_becomes_worker_stdout_events(
        self, sandbox: Sandbox, tmp_path: Path
    ) -> None:
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(seen.append)
        client = make_client(sandbox, bus)
        # exercise the line handler directly with a mix of noise and events
        client._handle_line(agent_job(), "not json at all")
        client._handle_line(agent_job(), "")
        client._handle_line(
            agent_job(), Event.now(EventTypes.WORKER_HEARTBEAT, "r1").to_json_line()
        )
        types = [e.type for e in seen]
        assert types == [EventTypes.WORKER_STDOUT, EventTypes.WORKER_HEARTBEAT]
        assert seen[0].data["line"] == "not json at all"

    def test_timeout_kills_and_raises(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        script = tmp_path / "script.json"
        script.write_text(json.dumps([{"text": "slow", "sleep_s": 30}]))
        import os

        os.environ["SDXLOOP_ECHO_SCRIPT"] = str(script)
        try:
            client = make_client(sandbox, EventBus(), grace_s=1.0)
            with pytest.raises(WorkerTimeoutError, match="exceeded"):
                client.submit(agent_job(timeout_s=0.2))
        finally:
            del os.environ["SDXLOOP_ECHO_SCRIPT"]
        kills = [c for c in fake_sbx.invocations("exec") if "pkill" in c]
        assert kills, "expected a pkill inside the sandbox"
        assert any("j1" in arg for c in kills for arg in c)

    def test_missing_result_raises_worker_error(self, sandbox: Sandbox) -> None:
        client = make_client(sandbox, EventBus(), python="true")  # worker never runs
        with pytest.raises(WorkerError, match="produced no result"):
            client.submit(agent_job())

    def test_result_job_id_mismatch(self, sandbox: Sandbox) -> None:
        client = make_client(sandbox, EventBus())
        client.submit(agent_job())  # writes results/j1.json
        # craft a second job that reads j1's result via a doctored path
        sandbox.exec(
            [
                "sh",
                "-c",
                "cp /home/agent/.sdxloop/results/j1.json /home/agent/.sdxloop/results/jX.json",
            ]
        )
        with pytest.raises(WorkerError, match="mismatch"):
            client._fetch_result(agent_job(job_id="jX"), "/home/agent/.sdxloop/results/jX.json")


class TestPollTransport:
    def test_submit_end_to_end_poll(self, sandbox: Sandbox) -> None:
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(seen.append)
        client = make_client(sandbox, bus, transport="poll", poll_interval=0.1)

        result = client.submit(agent_job(job_id="jp"))

        assert result.status == "ok"
        assert result.output_text == "echo: ping"
        types = [e.type for e in seen]
        assert EventTypes.WORKER_START in types
        assert EventTypes.WORKER_END in types

    def test_poll_timeout(self, sandbox: Sandbox, tmp_path: Path) -> None:
        script = tmp_path / "script.json"
        script.write_text(json.dumps([{"text": "slow", "sleep_s": 30}]))
        import os

        os.environ["SDXLOOP_ECHO_SCRIPT"] = str(script)
        try:
            client = make_client(
                sandbox, EventBus(), transport="poll", poll_interval=0.1, grace_s=0.5
            )
            with pytest.raises(WorkerTimeoutError):
                client.submit(agent_job(timeout_s=0.2))
        finally:
            del os.environ["SDXLOOP_ECHO_SCRIPT"]


class TestInstall:
    def test_install_flow_with_wheel(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        import sdxloop

        wheel = tmp_path / f"sdxloop_worker-{sdxloop.__version__}-py3-none-any.whl"
        wheel.write_bytes(b"fake wheel bytes")
        client = make_client(sandbox, EventBus())
        # Scripted execs: venv creation, pip install, and import check all
        # succeed; the import check must print the lockstep version.
        fake_sbx.script("exec boxa python3 -m venv", returncode=0)
        fake_sbx.script("exec boxa /home/agent/.sdxloop/venv/bin/pip", returncode=0)
        fake_sbx.script(
            "exec boxa /home/agent/.sdxloop/venv/bin/python",
            stdout=f"{sdxloop.__version__}\n",
        )
        client.install(wheel=wheel, extras="copilot")

        # wheel staged into the sandbox
        assert (fake_sbx.sandbox_fs("boxa") / "tmp/sdxloop_worker.whl").read_bytes() == (
            b"fake wheel bytes"
        )
        pip_calls = [c for c in fake_sbx.invocations("exec") if any("pip" in a for a in c)]
        assert any("/tmp/sdxloop_worker.whl[copilot]" in a for c in pip_calls for a in c)

    def test_install_pypi_fallback(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sdxloop
        from sdxloop.worker import client as client_mod

        # no local wheel available -> install pinned from PyPI
        monkeypatch.setattr(client_mod, "resolve_worker_wheel", lambda: None)
        client = make_client(sandbox, EventBus())
        fake_sbx.script("exec boxa python3 -m venv", returncode=0)
        fake_sbx.script("exec boxa /home/agent/.sdxloop/venv/bin/pip", returncode=0)
        fake_sbx.script(
            "exec boxa /home/agent/.sdxloop/venv/bin/python",
            stdout=f"{sdxloop.__version__}\n",
        )
        client.install(wheel=None, extras="")

        pip_calls = [c for c in fake_sbx.invocations("exec") if any("pip" in a for a in c)]
        expected = f"sdxloop-worker=={sdxloop.__version__}"
        assert any(expected in a for c in pip_calls for a in c)

    def test_install_version_mismatch(self, sandbox: Sandbox, fake_sbx: FakeSbx) -> None:
        client = make_client(sandbox, EventBus())
        fake_sbx.script("exec boxa python3 -m venv", returncode=0)
        fake_sbx.script("exec boxa /home/agent/.sdxloop/venv/bin/pip", returncode=0)
        fake_sbx.script("exec boxa /home/agent/.sdxloop/venv/bin/python", stdout="9.9.9\n")
        with pytest.raises(WorkerError, match="does not match host"):
            client.install(wheel=None, extras="")

    def test_install_step_failure(self, sandbox: Sandbox, fake_sbx: FakeSbx) -> None:
        # venv fails, apt can't heal it, and the user-site fallback fails
        # too -> the error names the fallback step with the pip output
        client = make_client(sandbox, EventBus())
        fake_sbx.script("exec boxa python3 -m venv", returncode=1, stderr="no venv here")
        fake_sbx.script("exec boxa sh -c sudo -n apt-get", returncode=1, stderr="no sudo")
        fake_sbx.script("exec boxa python3 -m pip install", returncode=1, stderr="pip is broken")
        with pytest.raises(WorkerError, match=r"user-site fallback.*pip is broken"):
            client.install(wheel=None)


class TestInstallFallbacks:
    def test_venv_failure_self_heals_via_apt(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        import sdxloop

        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        # first venv attempt fails the Debian way; after apt, retry succeeds
        fake_sbx.script(
            "exec boxa python3 -m venv",
            returncode=1,
            stderr="The virtual environment was not created: ensurepip is not available.",
            once=True,
        )
        fake_sbx.script("exec boxa sh -c sudo -n apt-get", returncode=0, once=True)
        fake_sbx.script("exec boxa python3 -m venv", returncode=0, once=True)
        fake_sbx.script("exec boxa /home/agent/.sdxloop/venv/bin/pip", returncode=0)
        fake_sbx.script(
            "exec boxa /home/agent/.sdxloop/venv/bin/python",
            stdout=f"{sdxloop.__version__}\n",
        )
        client.install(wheel=wheel)
        assert client.python == "/home/agent/.sdxloop/venv/bin/python"
        apt_calls = [c for c in fake_sbx.invocations("exec") if any("apt-get" in a for a in c)]
        assert apt_calls, "expected an apt-get self-heal attempt"

    def test_user_site_fallback_when_venv_impossible(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        import sdxloop

        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        fake_sbx.script("exec boxa python3 -m venv", returncode=1, stderr="venv: not supported")
        fake_sbx.script("exec boxa sh -c sudo -n apt-get", returncode=1, stderr="no sudo")
        # PEP 668: plain --user refused, --break-system-packages accepted
        fake_sbx.script(
            "exec boxa python3 -m pip install --quiet --user --break-system-packages",
            returncode=0,
            once=True,
        )
        fake_sbx.script(
            "exec boxa python3 -m pip install --quiet --user",
            returncode=1,
            stderr="error: externally-managed-environment",
            once=True,
        )
        fake_sbx.script("exec boxa python3 -c", stdout=f"{sdxloop.__version__}\n")
        client.install(wheel=wheel)
        assert client.python == "python3"
        pip_calls = [c for c in fake_sbx.invocations("exec") if any("pip" in a for a in c)]
        assert any("--break-system-packages" in a for c in pip_calls for a in c)

    def test_install_error_includes_stdout_when_stderr_empty(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        # sbx exec surfaced the real error on stdout; stderr empty
        fake_sbx.script(
            "exec boxa python3 -m venv",
            returncode=1,
            stdout="Error: no python3-venv on this template\n",
        )
        fake_sbx.script("exec boxa sh -c sudo -n apt-get", returncode=1)
        fake_sbx.script(
            "exec boxa python3 -m pip install",
            returncode=1,
            stdout="pip exploded on stdout only\n",
        )
        with pytest.raises(WorkerError, match="pip exploded on stdout only"):
            client.install(wheel=wheel)
