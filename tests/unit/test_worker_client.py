"""WorkerClient transport tests: the REAL worker process through the fake sbx.

sys.executable (the test venv python, which has sbxloop_worker importable) is
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

from sbxloop.errors import WorkerError, WorkerTimeoutError
from sbxloop.events import Event, EventBus
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxSpec
from sbxloop.sbx.sandbox import Sandbox
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import EventTypes, JobRequest
from tests.conftest import FakeSbx


@pytest.fixture(autouse=True)
def echo_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBXLOOP_WORKER_BACKEND", "echo")


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
        assert (fs / "home/agent/.sbxloop/jobs/j1.json").is_file()
        assert (fs / "home/agent/.sbxloop/results/j1.json").is_file()
        assert (fs / "home/agent/.sbxloop/events/j1.jsonl").is_file()

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

        os.environ["SBXLOOP_ECHO_SCRIPT"] = str(script)
        try:
            client = make_client(sandbox, EventBus(), grace_s=1.0)
            with pytest.raises(WorkerTimeoutError, match="exceeded"):
                client.submit(agent_job(timeout_s=0.2))
        finally:
            del os.environ["SBXLOOP_ECHO_SCRIPT"]
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
                "cp /home/agent/.sbxloop/results/j1.json /home/agent/.sbxloop/results/jX.json",
            ]
        )
        with pytest.raises(WorkerError, match="mismatch"):
            client._fetch_result(agent_job(job_id="jX"), "/home/agent/.sbxloop/results/jX.json")


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

        os.environ["SBXLOOP_ECHO_SCRIPT"] = str(script)
        try:
            client = make_client(
                sandbox, EventBus(), transport="poll", poll_interval=0.1, grace_s=0.5
            )
            with pytest.raises(WorkerTimeoutError):
                client.submit(agent_job(timeout_s=0.2))
        finally:
            del os.environ["SBXLOOP_ECHO_SCRIPT"]


class TestInstall:
    def test_install_flow_with_wheel(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        import sbxloop

        wheel = tmp_path / f"sbxloop_worker-{sbxloop.__version__}-py3-none-any.whl"
        wheel.write_bytes(b"fake wheel bytes")
        client = make_client(sandbox, EventBus())
        # Scripted execs: venv creation, pip install, and import check all
        # succeed; the import check must print the lockstep version.
        fake_sbx.script("exec boxa python3 -m venv", returncode=0)
        fake_sbx.script("exec boxa /home/agent/.sbxloop/venv/bin/pip", returncode=0)
        fake_sbx.script(
            "exec boxa /home/agent/.sbxloop/venv/bin/python -c",
            stdout=f"{sbxloop.__version__}\n",
        )
        fake_sbx.script(
            "exec boxa /home/agent/.sbxloop/venv/bin/python -m sbxloop_worker", returncode=64
        )
        client.install(wheel=wheel, extras="copilot")

        # wheel staged into the sandbox UNDER ITS CANONICAL FILENAME: pip
        # validates the filename structure and rejects renamed wheels
        assert (fake_sbx.sandbox_fs("boxa") / "tmp" / wheel.name).read_bytes() == (
            b"fake wheel bytes"
        )
        pip_calls = [c for c in fake_sbx.invocations("exec") if any("pip" in a for a in c)]
        assert any(f"/tmp/{wheel.name}[copilot]" in a for c in pip_calls for a in c)

    def test_install_pypi_fallback(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sbxloop
        from sbxloop.worker import client as client_mod

        # no local wheel available -> install pinned from PyPI
        monkeypatch.setattr(client_mod, "resolve_worker_wheel", lambda: None)
        client = make_client(sandbox, EventBus())
        fake_sbx.script("exec boxa python3 -m venv", returncode=0)
        fake_sbx.script("exec boxa /home/agent/.sbxloop/venv/bin/pip", returncode=0)
        fake_sbx.script(
            "exec boxa /home/agent/.sbxloop/venv/bin/python -c",
            stdout=f"{sbxloop.__version__}\n",
        )
        fake_sbx.script(
            "exec boxa /home/agent/.sbxloop/venv/bin/python -m sbxloop_worker", returncode=64
        )
        client.install(wheel=None, extras="")

        pip_calls = [c for c in fake_sbx.invocations("exec") if any("pip" in a for a in c)]
        expected = f"sbxloop-worker=={sbxloop.__version__}"
        assert any(expected in a for c in pip_calls for a in c)

    def test_install_version_mismatch(self, sandbox: Sandbox, fake_sbx: FakeSbx) -> None:
        client = make_client(sandbox, EventBus())
        fake_sbx.script("exec boxa python3 -m venv", returncode=0)
        fake_sbx.script("exec boxa /home/agent/.sbxloop/venv/bin/pip", returncode=0)
        fake_sbx.script("exec boxa /home/agent/.sbxloop/venv/bin/python -c", stdout="9.9.9\n")
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
        import sbxloop

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
        fake_sbx.script("exec boxa /home/agent/.sbxloop/venv/bin/pip", returncode=0)
        fake_sbx.script(
            "exec boxa /home/agent/.sbxloop/venv/bin/python -c",
            stdout=f"{sbxloop.__version__}\n",
        )
        fake_sbx.script(
            "exec boxa /home/agent/.sbxloop/venv/bin/python -m sbxloop_worker", returncode=64
        )
        client.install(wheel=wheel)
        assert client.python == "/home/agent/.sbxloop/venv/bin/python"
        apt_calls = [c for c in fake_sbx.invocations("exec") if any("apt-get" in a for a in c)]
        assert apt_calls, "expected an apt-get self-heal attempt"

    def test_ensure_dev_tools_installs_apt_packages_up_front(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        import sbxloop

        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        fake_sbx.script("exec boxa sh -c sudo -n apt-get", returncode=0)
        fake_sbx.script("exec boxa python3 -m venv", returncode=0)
        fake_sbx.script("exec boxa /home/agent/.sbxloop/venv/bin/pip", returncode=0)
        fake_sbx.script(
            "exec boxa /home/agent/.sbxloop/venv/bin/python -c",
            stdout=f"{sbxloop.__version__}\n",
        )
        fake_sbx.script(
            "exec boxa /home/agent/.sbxloop/venv/bin/python -m sbxloop_worker", returncode=64
        )
        client.install(wheel=wheel, ensure_dev_tools=True)
        execs = fake_sbx.invocations("exec")
        apt_idx = [i for i, c in enumerate(execs) if any("apt-get" in a for a in c)]
        venv_idx = [i for i, c in enumerate(execs) if "-m venv" in " ".join(c)]
        assert apt_idx, "expected the dev-tools apt install"
        assert venv_idx and apt_idx[0] < venv_idx[0], "apt must run before venv creation"
        # both packages named in one invocation
        apt_cmd = " ".join(execs[apt_idx[0]])
        assert "python3-venv" in apt_cmd and "python3-pip" in apt_cmd

    def test_ensure_dev_tools_failure_is_nonfatal_but_loud(
        self,
        sandbox: Sandbox,
        fake_sbx: FakeSbx,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import sbxloop

        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        fake_sbx.script("exec boxa sh -c sudo -n apt-get", returncode=100, stderr="apt exploded")
        fake_sbx.script("exec boxa python3 -m venv", returncode=0)
        fake_sbx.script("exec boxa /home/agent/.sbxloop/venv/bin/pip", returncode=0)
        fake_sbx.script(
            "exec boxa /home/agent/.sbxloop/venv/bin/python -c",
            stdout=f"{sbxloop.__version__}\n",
        )
        fake_sbx.script(
            "exec boxa /home/agent/.sbxloop/venv/bin/python -m sbxloop_worker", returncode=64
        )
        with caplog.at_level("WARNING"):
            client.install(wheel=wheel, ensure_dev_tools=True)
        assert any("dev-tools ensure failed" in r.getMessage() for r in caplog.records)
        assert any("apt exploded" in r.getMessage() for r in caplog.records)

    def test_install_without_flag_skips_dev_tools(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        import sbxloop

        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        fake_sbx.script("exec boxa python3 -m venv", returncode=0)
        fake_sbx.script("exec boxa /home/agent/.sbxloop/venv/bin/pip", returncode=0)
        fake_sbx.script(
            "exec boxa /home/agent/.sbxloop/venv/bin/python -c",
            stdout=f"{sbxloop.__version__}\n",
        )
        fake_sbx.script(
            "exec boxa /home/agent/.sbxloop/venv/bin/python -m sbxloop_worker", returncode=64
        )
        client.install(wheel=wheel)
        execs = fake_sbx.invocations("exec")
        assert not [c for c in execs if any("apt-get" in a for a in c)]

    def test_user_site_fallback_when_venv_impossible(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        import sbxloop

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
        fake_sbx.script("exec boxa python3 -c", stdout=f"{sbxloop.__version__}\n")
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


class TestRealPipInstall:
    def test_staged_wheel_installs_with_real_pip(self, sandbox: Sandbox, fake_sbx: FakeSbx) -> None:
        """End-to-end install regression: REAL venv + REAL pip against the
        REAL workspace wheel through the fake sbx. This is the test that
        would have caught the renamed-wheel bug ('Invalid wheel filename'):
        pip validates the staged FILENAME itself, so nothing short of
        actually running pip exercises that contract."""
        import sbxloop
        from sbxloop.worker import wheel as wheel_mod

        # another test file may have poisoned the build cache with None
        wheel_mod._workspace_build.cache_clear()
        wheel = wheel_mod.resolve_worker_wheel()
        if wheel is None:
            pytest.skip("no worker wheel available (uv not on PATH)")
        client = make_client(sandbox, EventBus())
        # Fully real: pip resolves pydantic from PyPI. --no-deps would pass
        # the import check but fail the entrypoint smoke check (pydantic is
        # only needed by __main__) - which is exactly the class of breakage
        # the smoke check exists to catch at install time.
        client.install(wheel=wheel, extras="")
        assert client.python == "/home/agent/.sbxloop/venv/bin/python"
        # the worker is genuinely importable from the sandbox venv
        result = sandbox.exec(
            [
                "/home/agent/.sbxloop/venv/bin/python",
                "-c",
                "import sbxloop_worker; print(sbxloop_worker.__version__)",
            ]
        )
        assert result.ok
        assert result.stdout.strip() == sbxloop.__version__


def script_ladder_success(fake_sbx: FakeSbx) -> None:
    """Script the full install ladder to succeed (venv, pip, checks)."""
    import sbxloop

    fake_sbx.script("exec boxa python3 -m venv", returncode=0)
    fake_sbx.script("exec boxa /home/agent/.sbxloop/venv/bin/pip", returncode=0)
    fake_sbx.script(
        "exec boxa /home/agent/.sbxloop/venv/bin/python -c",
        stdout=f"{sbxloop.__version__}\n",
    )
    fake_sbx.script(
        "exec boxa /home/agent/.sbxloop/venv/bin/python -m sbxloop_worker", returncode=64
    )


class TestPrebakedTemplate:
    """expect_prebaked: verify the baked worker with fast probes and skip the
    install ladder; ANY probe failure degrades to the ladder, never the run."""

    def write_manifest(self, sandbox: Sandbox, *, python: str, version: str | None = None) -> None:
        import sbxloop

        manifest = {
            "worker_version": version or sbxloop.__version__,
            "python": python,
            "runtime_cached": True,
            "baked_at": 0.0,
        }
        sandbox.write_text("/home/agent/.sbxloop/bake.json", json.dumps(manifest))

    def test_verified_prebaked_skips_ladder(self, sandbox: Sandbox, fake_sbx: FakeSbx) -> None:
        """The REAL probe chain: manifest read, import/version check, and
        entrypoint smoke all run genuinely (sys.executable has the worker
        importable) — no scripting, no ladder invocations."""
        self.write_manifest(sandbox, python=sys.executable)
        client = make_client(sandbox, EventBus(), python="python3")
        client.install(extras="copilot", ensure_dev_tools=True, expect_prebaked=True)

        assert client.prebaked
        assert client.python == sys.executable  # adopted from the manifest
        joined = [" ".join(c) for c in fake_sbx.invocations("exec")]
        assert not [j for j in joined if "-m venv" in j or "pip install" in j or "apt-get" in j]
        # no wheel was staged either — the fast path never resolves one
        assert not [c for c in fake_sbx.invocations("cp") if any(".whl" in a for a in c)]

    def test_missing_manifest_falls_back_to_ladder(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        script_ladder_success(fake_sbx)
        client = make_client(sandbox, EventBus())
        client.install(wheel=wheel, expect_prebaked=True)
        assert not client.prebaked
        assert [c for c in fake_sbx.invocations("exec") if "-m venv" in " ".join(c)]

    def test_stale_version_falls_back_and_warns(
        self,
        sandbox: Sandbox,
        fake_sbx: FakeSbx,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        self.write_manifest(sandbox, python=sys.executable, version="0.0.0")
        script_ladder_success(fake_sbx)
        client = make_client(sandbox, EventBus())
        with caplog.at_level("WARNING"):
            client.install(wheel=wheel, expect_prebaked=True)
        assert not client.prebaked
        assert any("stale template" in r.getMessage() for r in caplog.records)
        assert any("sbxloop bake" in r.getMessage() for r in caplog.records)

    def test_corrupt_manifest_falls_back(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        sandbox.write_text("/home/agent/.sbxloop/bake.json", "not json{")
        script_ladder_success(fake_sbx)
        client = make_client(sandbox, EventBus())
        client.install(wheel=wheel, expect_prebaked=True)
        assert not client.prebaked

    def test_broken_baked_python_falls_back(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        """Manifest points at an interpreter that fails the version probe
        (e.g. the venv did not survive into the template)."""
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        self.write_manifest(sandbox, python="false")
        script_ladder_success(fake_sbx)
        client = make_client(sandbox, EventBus())
        client.install(wheel=wheel, expect_prebaked=True)
        assert not client.prebaked

    def test_smoke_probe_failure_falls_back(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        """Version probe passes but the entrypoint probe does not exit 64."""
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        self.write_manifest(sandbox, python=sys.executable)
        fake_sbx.script(
            f"exec boxa {sys.executable} -m sbxloop_worker",
            returncode=1,
            stderr="entrypoint broken in template",
            once=True,
        )
        script_ladder_success(fake_sbx)
        client = make_client(sandbox, EventBus())
        client.install(wheel=wheel, expect_prebaked=True)
        assert not client.prebaked

    def test_without_flag_manifest_is_ignored(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        """No configured template → no probe execs, straight to the ladder."""
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        self.write_manifest(sandbox, python=sys.executable)
        script_ladder_success(fake_sbx)
        client = make_client(sandbox, EventBus())
        client.install(wheel=wheel)
        assert not client.prebaked
        assert not [c for c in fake_sbx.invocations("exec") if "cat" in c]


class TestNoResultDiagnostics:
    def test_missing_result_error_carries_rc_and_stderr(
        self, sandbox: Sandbox, tmp_path: Path
    ) -> None:
        """When the worker process dies without writing a result, the error
        must carry the exec exit code and the stderr tail — the field
        alternative is an unactionable 'produced no result file'."""
        crasher = tmp_path / "crasher.sh"
        crasher.write_text("#!/bin/sh\necho 'boom from the sandbox' >&2\nexit 7\n")
        crasher.chmod(0o755)
        client = make_client(sandbox, EventBus(), python=str(crasher))
        with pytest.raises(WorkerError) as excinfo:
            client.submit(agent_job())
        message = str(excinfo.value)
        assert "produced no result file" in message
        assert "exec rc=7" in message
        assert "boom from the sandbox" in message

    def test_missing_result_error_includes_events_tail(self, sandbox: Sandbox) -> None:
        """If the worker got far enough to emit events before dying, the
        last events ride along in the error."""
        # a fake partial events file left behind by a dying worker
        partial = Event.now("worker.start", "r1", job_id="j1").to_json_line()
        sandbox.write_text("/home/agent/.sbxloop/events/j1.jsonl", partial + "\n")
        client = make_client(sandbox, EventBus(), python="false")  # rc=1, no output
        with pytest.raises(WorkerError) as excinfo:
            client.submit(agent_job())
        message = str(excinfo.value)
        assert "exec rc=1" in message
        assert "worker.start" in message


class TestEntrypointSmoke:
    def test_smoke_failure_fails_install_with_output(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        import sbxloop

        wheel = tmp_path / f"sbxloop_worker-{sbxloop.__version__}-py3-none-any.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        fake_sbx.script("exec boxa python3 -m venv", returncode=0)
        fake_sbx.script("exec boxa /home/agent/.sbxloop/venv/bin/pip", returncode=0)
        fake_sbx.script(
            "exec boxa /home/agent/.sbxloop/venv/bin/python -c",
            stdout=f"{sbxloop.__version__}\n",
        )
        fake_sbx.script(
            "exec boxa /home/agent/.sbxloop/venv/bin/python -m sbxloop_worker",
            returncode=1,
            stderr="Traceback: entrypoint exploded under sbx exec",
        )
        with pytest.raises(WorkerError, match=r"entrypoint check failed.*entrypoint exploded"):
            client.install(wheel=wheel)


class TestLoginShellWrapping:
    def test_worker_runs_under_login_shell(self, sandbox: Sandbox, fake_sbx: FakeSbx) -> None:
        """sbx injects secrets via the session/profile machinery, so the
        worker must run under `sh -lc` to see them (field: SDK 'Session was
        not created with authentication info')."""
        make_client(sandbox, EventBus()).submit(agent_job())
        worker_execs = [
            c
            for c in fake_sbx.invocations("exec")
            if any("sbxloop_worker" in a for a in c) and "pkill" not in c
        ]
        assert worker_execs, "no worker exec recorded"
        head = worker_execs[0][:4]
        assert head[0] == "exec"
        assert head[2] == "sh"
        assert head[3] == "-lc"


class TestWorkerCwd:
    def test_cwd_travels_on_argv_and_worker_chdirs(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        """--cwd /workspace → the fake rewrites it onto the mount symlink, the
        worker chdirs there, and a relative write lands in the HOST workspace
        (tmp_path is boxa's workspace, mounted by the fake at /workspace)."""
        job = JobRequest(
            job_id="j9",
            run_id="r1",
            kind="shell.check",
            argv=["sh", "-c", "printf hi > produced.txt"],
            cwd="/workspace",
        )
        result = make_client(sandbox, EventBus()).submit(job)
        assert result.status == "ok"
        assert result.exit_code == 0
        assert (tmp_path / "produced.txt").read_text() == "hi"
        # the worker argv carried --cwd (agent sessions inherit process cwd)
        execs = [c for c in fake_sbx.invocations("exec") if any("--cwd" in a for a in c)]
        assert execs

    def test_agent_session_cwd_reaches_backend(
        self, sandbox: Sandbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An agent job with cwd runs the echo backend inside the workspace:
        scripted 'files' writes propagate to the host through the mount."""
        script = tmp_path / "script.json"
        script.write_text(
            json.dumps([{"text": "done", "files": {"out/hello.txt": "hello sbxloop"}}])
        )
        monkeypatch.setenv("SBXLOOP_ECHO_SCRIPT", str(script))
        result = make_client(sandbox, EventBus()).submit(agent_job(job_id="j10", cwd="/workspace"))
        assert result.status == "ok"
        assert (tmp_path / "out/hello.txt").read_text() == "hello sbxloop"

    def test_missing_cwd_fails_job_cleanly(self, sandbox: Sandbox) -> None:
        job = JobRequest(
            job_id="j11",
            run_id="r1",
            kind="shell.check",
            argv=["true"],
            cwd="/workspace/nope/nope",
        )
        with pytest.raises(WorkerError, match="produced no result file"):
            make_client(sandbox, EventBus()).submit(job)


class TestResourceTelemetry:
    def test_role_enriches_resource_events(self, sandbox: Sandbox) -> None:
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(seen.append)
        client = make_client(sandbox, bus, role="agent")
        client._handle_line(
            agent_job(),
            Event.now(
                EventTypes.SANDBOX_RESOURCES, "r1", level="ok", disk_used_pct=42.0
            ).to_json_line(),
        )
        client._handle_line(
            agent_job(),
            Event.now(EventTypes.AGENT_MESSAGE, "r1", content="hi").to_json_line(),
        )
        assert seen[0].data["role"] == "agent"
        assert "role" not in seen[1].data  # only resource events are enriched

    def test_submit_passes_thresholds_and_role(self, sandbox: Sandbox) -> None:
        """End-to-end: limits ride the worker argv, the worker emits a
        baseline sample classified against them, and the host stamps the
        sandbox role onto the republished event."""
        from sbxloop.config import Limits

        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(seen.append)
        client = make_client(sandbox, bus, role="agent", limits=Limits())
        result = client.submit(agent_job())
        assert result.status == "ok"
        samples = [e for e in seen if e.type == EventTypes.SANDBOX_RESOURCES]
        assert samples, "worker emitted no baseline resource sample"
        assert samples[0].data["role"] == "agent"
        assert samples[0].data["level"] in ("ok", "warn", "abort")
        assert 0.0 <= samples[0].data["disk_used_pct"] <= 100.0

    def test_submit_without_limits_still_samples(self, sandbox: Sandbox) -> None:
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(seen.append)
        result = make_client(sandbox, bus).submit(agent_job())
        assert result.status == "ok"
        samples = [e for e in seen if e.type == EventTypes.SANDBOX_RESOURCES]
        assert samples and samples[0].data["level"] == "ok"  # thresholds disabled
