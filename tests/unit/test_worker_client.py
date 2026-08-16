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

from sbxloop import toolchains
from sbxloop.errors import WorkerError, WorkerTimeoutError
from sbxloop.events import Event, EventBus
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxSpec
from sbxloop.sbx.sandbox import Sandbox
from sbxloop.worker.client import WorkerClient, _PollDrain
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


def script_search_fallback_probe(fake_sbx: FakeSbx, *, returncode: int = 0) -> None:
    """Script the page-size/ripgrep probe (unscripted it would run on the
    host, where the answer varies by machine — 16 KiB on Apple silicon)."""
    fake_sbx.script('exec boxa sh -c test "$(getconf PAGESIZE)"', returncode=returncode)


def script_toolchain_probe(
    fake_sbx: FakeSbx, name: str, *, returncode: int = 0, stderr: str = ""
) -> None:
    """Script one language toolchain's presence probe.

    Unscripted it would run on the *host*, where the answer depends on the
    machine: the test venv has ensurepip, a mac has clang, and the CI
    runners ship a global `tsc`. So every toolchain probe a test can reach
    must be pinned explicitly — including the probes of any toolchain a
    selection pulls in via ``requires``.

    Looked up by canonical name rather than ``resolve(...)[0]``: resolve
    also returns requirements, so for "typescript" the first element is
    *javascript*, and scripting that instead left the real tsc probe to run
    against the host.
    """
    key = toolchains.normalize_language(name)
    assert key is not None, f"unknown toolchain {name!r}"
    toolchain = next(t for t in toolchains.TOOLCHAINS if t.name == key)
    fake_sbx.script(f"exec boxa sh -c {toolchain.probe}", returncode=returncode, stderr=stderr)


def script_git_probe(fake_sbx: FakeSbx, *, returncode: int = 0) -> None:
    """Script the baseline git probe (#252). Unscripted it runs on the host,
    where git is present on every dev machine and CI runner — so tests that
    assert the exact apt command pin it rather than rely on that."""
    fake_sbx.script(f"exec boxa sh -c {toolchains.GIT.probe}", returncode=returncode)


def script_probes_for(fake_sbx: FakeSbx, languages: list[str], *, returncode: int = 1) -> None:
    """Script every probe a ``languages`` selection will run, requirements
    included — the safe way to set up a test that passes ``languages=``."""
    for toolchain in toolchains.resolve(languages):
        script_toolchain_probe(fake_sbx, toolchain.name, returncode=returncode)


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

    def test_submit_agent_name_stamps_agent_events(self, sandbox: Sandbox) -> None:
        """submit(agent=...) attributes that job's agent.* events to the
        phase persona (the transcript header shows who is speaking); worker
        lifecycle events stay unattributed, and the mapping is dropped once
        the job completes."""
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(seen.append)

        client = make_client(sandbox, bus)
        result = client.submit(agent_job(), agent="planner")

        assert result.status == "ok"
        messages = [e for e in seen if e.type == EventTypes.AGENT_MESSAGE]
        assert messages and all(e.data["agent"] == "planner" for e in messages)
        starts = [e for e in seen if e.type == EventTypes.WORKER_START]
        assert starts and "agent" not in starts[0].data
        assert client._job_agents == {}

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

    def test_timeout_kill_never_escapes_the_sandbox(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        """The fake's pkill must emulate the microVM boundary. Job ids repeat
        across tests ("j1" everywhere), so a host-wide
        ``pkill -f sbxloop_worker.*j1`` from one test's timeout kill would
        TERM other xdist workers' live worker processes mid-test."""
        import subprocess
        import time

        # A process that matches the kill pattern but belongs to no sandbox.
        decoy = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
                "sbxloop_worker-decoy",
                "--job",
                "j1.json",
            ]
        )
        script = tmp_path / "script.json"
        script.write_text(json.dumps([{"text": "slow", "sleep_s": 30}]))
        import os

        os.environ["SBXLOOP_ECHO_SCRIPT"] = str(script)
        try:
            client = make_client(sandbox, EventBus(), grace_s=1.0)
            with pytest.raises(WorkerTimeoutError, match="exceeded"):
                client.submit(agent_job(timeout_s=0.2))
            time.sleep(0.5)  # let a stray SIGTERM (the bug) be delivered
            assert decoy.poll() is None, "pkill escaped the sandbox and killed a foreign process"
        finally:
            del os.environ["SBXLOOP_ECHO_SCRIPT"]
            decoy.kill()
            decoy.wait()

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

    def test_poll_end_to_end_non_ascii_prompt(self, sandbox: Sandbox) -> None:
        """Non-ASCII survives the full poll path (base64-encoded chunks)."""
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(seen.append)
        client = make_client(sandbox, bus, transport="poll", poll_interval=0.1)

        prompt = "café — “smart quotes” ☕"
        result = client.submit(agent_job(job_id="jn", prompt=prompt))

        assert result.status == "ok"
        assert result.output_text == f"echo: {prompt}"
        messages = [e for e in seen if e.type == EventTypes.AGENT_MESSAGE]
        assert messages
        assert messages[0].data["content"] == f"echo: {prompt}"

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


class TestPollDrain:
    """Byte-level poll-chunk semantics, driven by writing the events file
    directly in the fake sandbox fs between drain() calls (issue #65)."""

    EVENTS_PATH = "/home/agent/.sbxloop/events/jd.jsonl"

    def make_drain(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, bus: EventBus
    ) -> tuple[_PollDrain, Path]:
        client = make_client(sandbox, bus, transport="poll")
        host_events = fake_sbx.sandbox_fs("boxa") / self.EVENTS_PATH.lstrip("/")
        host_events.parent.mkdir(parents=True, exist_ok=True)
        return _PollDrain(client, agent_job(job_id="jd"), self.EVENTS_PATH), host_events

    def test_multibyte_char_split_across_chunks(self, sandbox: Sandbox, fake_sbx: FakeSbx) -> None:
        """A poll boundary mid-UTF-8-character must neither crash the host
        decode nor corrupt the reassembled line."""
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(seen.append)
        drain, host_events = self.make_drain(sandbox, fake_sbx, bus)

        # to_json_line escapes non-ASCII; serialize with ensure_ascii=False to
        # model a writer emitting raw UTF-8 (equally valid JSONL on disk).
        text = "café — ☕ done"
        payload = Event.now(EventTypes.AGENT_MESSAGE, "r1", text=text).model_dump(mode="json")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        split = data.index("☕".encode()) + 1  # one byte into a 3-byte character

        host_events.write_bytes(data[:split])
        assert drain.drain() is False
        assert drain.offset == split  # offset counts raw bytes, not re-encoded text
        assert seen == []  # no newline yet: nothing published

        host_events.write_bytes(data)
        assert drain.drain() is False
        assert drain.offset == len(data)
        assert [e.type for e in seen] == [EventTypes.AGENT_MESSAGE]
        assert seen[0].data["text"] == text  # character reassembled, no U+FFFD

    def test_crlf_lines_do_not_drift_offset(self, sandbox: Sandbox, fake_sbx: FakeSbx) -> None:
        r"""\r\n in worker output: offset stays byte-exact across polls, so no
        line is replayed or dropped (the old text-layer round-trip lost one
        byte per \r\n to universal-newline translation)."""
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(seen.append)
        drain, host_events = self.make_drain(sandbox, fake_sbx, bus)

        first = (
            Event.now(EventTypes.WORKER_START, "r1").to_json_line()
            + "\r\n"
            + Event.now(EventTypes.WORKER_HEARTBEAT, "r1").to_json_line()
            + "\r\n"
        ).encode()
        host_events.write_bytes(first)
        assert drain.drain() is False
        assert drain.offset == len(first)

        second = (
            Event.now(EventTypes.AGENT_MESSAGE, "r1", text="hi").to_json_line() + "\r\n"
        ).encode()
        host_events.write_bytes(first + second)
        assert drain.drain() is False
        assert drain.offset == len(first) + len(second)
        assert [e.type for e in seen] == [
            EventTypes.WORKER_START,
            EventTypes.WORKER_HEARTBEAT,
            EventTypes.AGENT_MESSAGE,
        ]

    def test_worker_end_literal_in_event_data_is_not_completion(
        self, sandbox: Sandbox, fake_sbx: FakeSbx
    ) -> None:
        """Only a parsed worker.end event finishes the poll — an agent event
        whose DATA contains the literal string must not."""
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(seen.append)
        drain, host_events = self.make_drain(sandbox, fake_sbx, bus)

        decoy_line = Event.now(
            EventTypes.AGENT_MESSAGE, "r1", quoted_type="worker.end"
        ).to_json_line()
        assert '"worker.end"' in decoy_line  # the old substring check would end here
        host_events.write_bytes((decoy_line + "\n").encode())
        assert drain.drain() is False

        end_line = Event.now(EventTypes.WORKER_END, "r1").to_json_line()
        host_events.write_bytes((decoy_line + "\n" + end_line + "\n").encode())
        assert drain.drain() is True
        assert [e.type for e in seen] == [EventTypes.AGENT_MESSAGE, EventTypes.WORKER_END]


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
        script_toolchain_probe(
            fake_sbx,
            "python",
            returncode=1,
            stderr="ModuleNotFoundError: No module named 'ensurepip'",
        )
        script_search_fallback_probe(fake_sbx)
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

    def test_ensure_dev_tools_probe_success_skips_apt(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # A template that already has ensurepip+pip must not touch apt (or
        # the network) at all — the ensure is a genuine no-op.
        import sbxloop

        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        script_toolchain_probe(fake_sbx, "python", returncode=0)
        script_search_fallback_probe(fake_sbx)
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
        assert not [c for c in execs if any("apt-get" in a for a in c)]

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
        script_toolchain_probe(
            fake_sbx,
            "python",
            returncode=1,
            stderr="ModuleNotFoundError: No module named 'ensurepip'",
        )
        script_search_fallback_probe(fake_sbx)
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

    def test_ensure_dev_tools_default_is_python(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # No `languages` argument -> exactly the pre-#140 behavior: the
        # Python probe runs and nothing else is provisioned.
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        script_git_probe(fake_sbx, returncode=0)
        script_toolchain_probe(fake_sbx, "python", returncode=1)
        script_search_fallback_probe(fake_sbx)
        fake_sbx.script("exec boxa sh -c sudo -n apt-get", returncode=0)
        # The uv + managed-Python install script (#250) runs after apt; the
        # fake would otherwise execute it on the host, where dpkg is absent.
        fake_sbx.script("exec boxa sh -c set -e; case", returncode=0)
        self._script_happy_install(fake_sbx)
        client.install(wheel=wheel, ensure_dev_tools=True)
        apt_cmds = [
            " ".join(c) for c in fake_sbx.invocations("exec") if any("apt-get" in a for a in c)
        ]
        assert apt_cmds == [
            "exec boxa sh -c sudo -n apt-get update -q && "
            "sudo -n apt-get install -y -q python3-venv python3-pip curl ca-certificates"
        ]
        scripts = [
            " ".join(c)
            for c in fake_sbx.invocations("exec")
            if any("uv python install" in a for a in c)
        ]
        assert len(scripts) == 1 and "python3.13" in scripts[0], scripts

    def test_ensure_dev_tools_installs_git_as_baseline(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # #252: git is provisioned regardless of `languages` — here with a
        # selection that does not name it and whose own toolchain is present
        # — and rides the SAME apt call as any missing language packages.
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        script_git_probe(fake_sbx, returncode=1)
        script_toolchain_probe(fake_sbx, "python", returncode=1)
        script_search_fallback_probe(fake_sbx)
        fake_sbx.script("exec boxa sh -c sudo -n apt-get", returncode=0)
        self._script_happy_install(fake_sbx)
        client.install(wheel=wheel, ensure_dev_tools=True, languages=["python"])
        apt_cmds = [
            " ".join(c) for c in fake_sbx.invocations("exec") if any("apt-get" in a for a in c)
        ]
        assert apt_cmds == [
            "exec boxa sh -c sudo -n apt-get update -q && "
            "sudo -n apt-get install -y -q git python3-venv python3-pip"
        ]

    def test_ensure_dev_tools_git_probe_success_installs_nothing(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # A template that ships git costs no apt for it, even when nothing
        # else is selected — probe first, like every other entry.
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        script_git_probe(fake_sbx, returncode=0)
        script_search_fallback_probe(fake_sbx)
        self._script_happy_install(fake_sbx)
        client.install(wheel=wheel, ensure_dev_tools=True, languages=["nonesuch"])
        assert not [c for c in fake_sbx.invocations("exec") if any("apt-get" in a for a in c)]

    def test_ensure_dev_tools_unselected_language_installs_nothing(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # Opt-in only: an empty selection must not fall back to "install
        # everything" — and must not even probe for what was not selected.
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        script_search_fallback_probe(fake_sbx)
        self._script_happy_install(fake_sbx)
        client.install(wheel=wheel, ensure_dev_tools=True, languages=["nonesuch"])
        assert not [c for c in fake_sbx.invocations("exec") if any("apt-get" in a for a in c)]

    def test_ensure_dev_tools_provisions_the_selected_language(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # C/C++ selected, Python not: the compiler toolchain is installed and
        # python3-venv is not, which is the whole point of #140.
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        script_toolchain_probe(fake_sbx, "cpp", returncode=1)
        script_search_fallback_probe(fake_sbx)
        fake_sbx.script("exec boxa sh -c sudo -n apt-get", returncode=0)
        self._script_happy_install(fake_sbx)
        client.install(wheel=wheel, ensure_dev_tools=True, languages=["c++"])
        apt_cmds = [
            " ".join(c) for c in fake_sbx.invocations("exec") if any("apt-get" in a for a in c)
        ]
        assert len(apt_cmds) == 1, apt_cmds
        for package in ("build-essential", "cmake", "ninja-build", "pkg-config"):
            assert package in apt_cmds[0]
        assert "python3-venv" not in apt_cmds[0]

    def test_ensure_dev_tools_batches_apt_across_languages(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # Two apt languages must cost ONE `update && install`, not two.
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        script_toolchain_probe(fake_sbx, "python", returncode=1)
        script_toolchain_probe(fake_sbx, "cpp", returncode=1)
        script_search_fallback_probe(fake_sbx)
        fake_sbx.script("exec boxa sh -c sudo -n apt-get", returncode=0)
        self._script_happy_install(fake_sbx)
        client.install(wheel=wheel, ensure_dev_tools=True, languages=["python", "cpp"])
        apt_cmds = [
            " ".join(c) for c in fake_sbx.invocations("exec") if any("apt-get" in a for a in c)
        ]
        assert len(apt_cmds) == 1, apt_cmds
        assert "python3-venv" in apt_cmds[0] and "build-essential" in apt_cmds[0]

    def test_ensure_dev_tools_runs_install_script_after_apt(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # Java is the first entry with both paths: the JDK comes from apt,
        # then a script records JAVA_HOME. Order matters — the script reads
        # the javac that apt just installed.
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        script_toolchain_probe(fake_sbx, "java", returncode=1)
        script_search_fallback_probe(fake_sbx)
        fake_sbx.script("exec boxa sh -c sudo -n apt-get", returncode=0)
        fake_sbx.script("exec boxa sh -c grep -qs", returncode=0)
        self._script_happy_install(fake_sbx)
        client.install(wheel=wheel, ensure_dev_tools=True, languages=["java"])
        execs = [" ".join(c) for c in fake_sbx.invocations("exec")]
        apt_idx = [i for i, c in enumerate(execs) if "apt-get" in c]
        script_idx = [i for i, c in enumerate(execs) if "JAVA_HOME" in c and "grep -qs" in c]
        assert apt_idx and script_idx, execs
        assert apt_idx[0] < script_idx[-1], "the install script must run after the apt batch"
        assert "openjdk" in execs[apt_idx[0]] and "maven" in execs[apt_idx[0]]

    def test_ensure_dev_tools_install_script_failure_is_nonfatal_but_loud(
        self,
        sandbox: Sandbox,
        fake_sbx: FakeSbx,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        script_toolchain_probe(fake_sbx, "java", returncode=1)
        script_search_fallback_probe(fake_sbx)
        fake_sbx.script("exec boxa sh -c sudo -n apt-get", returncode=0)
        fake_sbx.script("exec boxa sh -c grep -qs", returncode=1, stderr="tee: permission denied")
        self._script_happy_install(fake_sbx)
        with caplog.at_level("WARNING"):
            client.install(wheel=wheel, ensure_dev_tools=True, languages=["java"])
        messages = [r.getMessage() for r in caplog.records]
        assert any("dev-tools ensure failed for java" in m for m in messages), messages
        # the warning names what is now missing, not just that something broke
        assert any("JAVA_HOME" in m for m in messages), messages
        assert any("tee: permission denied" in m for m in messages), messages

    def test_ensure_dev_tools_installs_required_toolchains_too(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # Selecting TypeScript must provision the Node runtime it is built
        # on, and provision it FIRST — `npm i -g typescript` is meaningless
        # before node exists.
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        script_probes_for(fake_sbx, ["ts"])
        script_search_fallback_probe(fake_sbx)
        fake_sbx.script("exec boxa sh -c sudo -n apt-get", returncode=0)
        fake_sbx.script("exec boxa sh -c set -e; case", returncode=0)
        fake_sbx.script("exec boxa sh -c set -e; sudo -n npm", returncode=0)
        self._script_happy_install(fake_sbx)
        client.install(wheel=wheel, ensure_dev_tools=True, languages=["ts"])
        execs = [" ".join(c) for c in fake_sbx.invocations("exec")]
        node_idx = [i for i, c in enumerate(execs) if "nodejs.org" in c]
        tsc_idx = [i for i, c in enumerate(execs) if "npm install -g" in c]
        assert node_idx and tsc_idx, execs
        assert node_idx[0] < tsc_idx[0], "the node runtime must install before tsc"

    def _script_happy_install(self, fake_sbx: FakeSbx) -> None:
        import sbxloop

        # First matching script wins, so a test that pins these probes
        # earlier keeps its answer; these are the defaults for the rest.
        script_toolchain_probe(fake_sbx, "python", returncode=0)
        script_git_probe(fake_sbx, returncode=0)
        fake_sbx.script("exec boxa python3 -m venv", returncode=0)
        fake_sbx.script("exec boxa /home/agent/.sbxloop/venv/bin/pip", returncode=0)
        fake_sbx.script(
            "exec boxa /home/agent/.sbxloop/venv/bin/python -c",
            stdout=f"{sbxloop.__version__}\n",
        )
        fake_sbx.script(
            "exec boxa /home/agent/.sbxloop/venv/bin/python -m sbxloop_worker", returncode=64
        )

    def test_search_fallback_installs_ripgrep_on_non_4k_guest(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # Probe fails (non-4-KiB pages, no rg on PATH) -> apt installs ripgrep.
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        script_search_fallback_probe(fake_sbx, returncode=1)
        fake_sbx.script("exec boxa sh -c sudo -n apt-get", returncode=0)
        self._script_happy_install(fake_sbx)
        client.install(wheel=wheel, ensure_dev_tools=True)
        apt_cmds = [
            " ".join(c) for c in fake_sbx.invocations("exec") if any("apt-get" in a for a in c)
        ]
        assert any("ripgrep" in cmd for cmd in apt_cmds), apt_cmds

    def test_search_fallback_probe_ok_skips_apt(
        self, sandbox: Sandbox, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # A 4-KiB guest (or one that already ships rg) must not touch apt.
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        script_search_fallback_probe(fake_sbx, returncode=0)
        self._script_happy_install(fake_sbx)
        client.install(wheel=wheel, ensure_dev_tools=True)
        execs = fake_sbx.invocations("exec")
        assert not [c for c in execs if any("ripgrep" in a for a in c)]

    def test_search_fallback_failure_is_nonfatal_but_loud(
        self,
        sandbox: Sandbox,
        fake_sbx: FakeSbx,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        client = make_client(sandbox, EventBus())
        script_search_fallback_probe(fake_sbx, returncode=1)
        fake_sbx.script("exec boxa sh -c sudo -n apt-get", returncode=100, stderr="apt exploded")
        self._script_happy_install(fake_sbx)
        with caplog.at_level("WARNING"):
            client.install(wheel=wheel, ensure_dev_tools=True)
        assert any("search-fallback ensure failed" in r.getMessage() for r in caplog.records)
        assert any("Unsupported system page size" in r.getMessage() for r in caplog.records)

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
        # the whole verification is ONE exec round trip (#127): manifest
        # read, import check, and entrypoint smoke run inside one script
        assert len(fake_sbx.invocations("exec")) == 1
        # (the host has git, so the probe reported it and no top-up ran)
        assert client._prebake_missing == []

    def test_verified_prebaked_without_git_tops_up_baseline(
        self,
        sandbox: Sandbox,
        fake_sbx: FakeSbx,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A template that passes every worker check but lacks git (#252):
        the ONE prebake probe reports it, and install() apt-installs git
        without falling back to the ladder or spending another probe.

        The fake exec runs on the host, so "no git on PATH" is staged with a
        PATH holding only a python3 shim (a script, not a symlink — a
        symlinked venv python would resolve its prefix from the link)."""
        shim_dir = tmp_path / "shim-bin"
        shim_dir.mkdir()
        shim = shim_dir / "python3"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", str(shim_dir))
        self.write_manifest(sandbox, python=sys.executable)
        fake_sbx.script("exec boxa sh -c sudo -n apt-get", returncode=0)
        client = make_client(sandbox, EventBus(), python="python3")
        client.install(extras="copilot", ensure_dev_tools=True, expect_prebaked=True)

        assert client.prebaked
        assert client._prebake_missing == [toolchains.GIT]
        joined = [" ".join(c) for c in fake_sbx.invocations("exec")]
        assert not [j for j in joined if "-m venv" in j or "pip install" in j]
        assert joined[1:] == [
            "exec boxa sh -c sudo -n apt-get update -q && sudo -n apt-get install -y -q git"
        ]

    @pytest.mark.parametrize(
        "verdict",
        [
            pytest.param({"stage": "ok"}, id="git-absent"),
            pytest.param({"stage": "ok", "git": "yes"}, id="git-non-boolean"),
        ],
    )
    def test_verified_prebaked_with_malformed_git_verdict_tops_up(
        self,
        sandbox: Sandbox,
        fake_sbx: FakeSbx,
        monkeypatch: pytest.MonkeyPatch,
        verdict: dict[str, object],
    ) -> None:
        """Fail closed (#252): an otherwise-successful "ok" verdict whose
        ``git`` field is absent or not a bool must NOT be read as "git
        present" — the apt top-up runs, and the fast path is still taken (no
        ladder). The real probe always emits a bool, so the malformed verdict
        is staged by swapping the probe script for one that prints it as-is.
        """
        from sbxloop.worker import client as client_mod

        verdict = {**verdict, "python": sys.executable}
        monkeypatch.setattr(client_mod, "_PREBAKE_PROBE", f"print({json.dumps(verdict)!r})")
        self.write_manifest(sandbox, python=sys.executable)
        fake_sbx.script("exec boxa sh -c sudo -n apt-get", returncode=0)
        client = make_client(sandbox, EventBus(), python="python3")
        client.install(extras="copilot", ensure_dev_tools=True, expect_prebaked=True)

        assert client.prebaked
        assert client._prebake_missing == [toolchains.GIT]
        joined = [" ".join(c) for c in fake_sbx.invocations("exec")]
        assert not [j for j in joined if "-m venv" in j or "pip install" in j]
        assert joined[1:] == [
            "exec boxa sh -c sudo -n apt-get update -q && sudo -n apt-get install -y -q git"
        ]

    def test_verified_prebaked_without_git_skips_top_up_for_non_agent(
        self,
        sandbox: Sandbox,
        fake_sbx: FakeSbx,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The github sandbox only runs API ops: same missing-git verdict,
        # but without ensure_dev_tools nothing is installed.
        shim_dir = tmp_path / "shim-bin"
        shim_dir.mkdir()
        shim = shim_dir / "python3"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", str(shim_dir))
        self.write_manifest(sandbox, python=sys.executable)
        client = make_client(sandbox, EventBus(), python="python3")
        client.install(extras="", expect_prebaked=True)

        assert client.prebaked
        assert len(fake_sbx.invocations("exec")) == 1

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
        """Version probe passes but the entrypoint probe does not exit 64.

        The probes run inside one in-sandbox script, so the smoke failure is
        staged with a half-broken interpreter: ``-c`` (the import check)
        delegates to the real python, ``-m sbxloop_worker`` (the entrypoint
        smoke) exits 1.
        """
        wheel = tmp_path / "w.whl"
        wheel.write_bytes(b"x")
        broken = tmp_path / "half-broken-python"
        broken.write_text(
            '#!/bin/sh\ncase "$*" in\n'
            "  -m\\ sbxloop_worker*) exit 1;;\n"
            f'  *) exec "{sys.executable}" "$@";;\n'
            "esac\n"
        )
        broken.chmod(0o755)
        self.write_manifest(sandbox, python=str(broken))
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
        assert not [c for c in fake_sbx.invocations("exec") if any("manifest_path" in a for a in c)]


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
