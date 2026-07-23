"""In-process runner/backend/entrypoint tests (subprocess-free, coverage-visible)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdxloop_worker.__main__ import load_env_file, main
from sdxloop_worker.backends import BackendUnavailableError, get_backend
from sdxloop_worker.backends.echo import EchoBackend
from sdxloop_worker.protocol import Event, EventTypes, JobRequest
from sdxloop_worker.runner import JobRunner


def agent_job(**overrides: object) -> JobRequest:
    base: dict[str, object] = {
        "job_id": "j1",
        "run_id": "r1",
        "kind": "agent.session",
        "prompt": "hello",
    }
    base.update(overrides)
    return JobRequest.model_validate(base)


def run_job(
    tmp_path: Path, job: JobRequest, *, heartbeat: float = 0.0
) -> tuple[object, list[Event]]:
    events_path = tmp_path / "events.jsonl"
    result = JobRunner(
        job,
        events_path=events_path,
        result_path=tmp_path / "result.json",
        heartbeat_s=heartbeat,
        backend_name="echo",
    ).run()
    events = [Event.from_json_line(line) for line in events_path.read_text().splitlines()]
    return result, events


class TestRunnerInProcess:
    def test_agent_session(self, tmp_path: Path) -> None:
        result, events = run_job(tmp_path, agent_job())
        assert result.status == "ok"  # type: ignore[attr-defined]
        types = [e.type for e in events]
        assert types[0] == EventTypes.WORKER_START
        assert types[-1] == EventTypes.WORKER_END

    def test_agent_session_json_expected(self, tmp_path: Path) -> None:
        result, _ = run_job(tmp_path, agent_job(expect="json"))
        assert result.output_json == {"echo": "hello"}  # type: ignore[attr-defined]

    def test_shell_check(self, tmp_path: Path) -> None:
        job = JobRequest(job_id="j2", run_id="r1", kind="shell.check", argv=["sh", "-c", "exit 5"])
        result, _ = run_job(tmp_path, job)
        assert result.status == "ok"  # type: ignore[attr-defined]
        assert result.exit_code == 5  # type: ignore[attr-defined]

    def test_shell_timeout(self, tmp_path: Path) -> None:
        job = JobRequest(
            job_id="j2",
            run_id="r1",
            kind="shell.check",
            argv=["sleep", "5"],
            timeout_s=0.2,
        )
        result, events = run_job(tmp_path, job)
        assert result.status == "timeout"  # type: ignore[attr-defined]
        assert EventTypes.WORKER_ERROR in [e.type for e in events]

    def test_github_op_module_missing(self, tmp_path: Path) -> None:
        job = JobRequest(job_id="j3", run_id="r1", kind="github.op", op="issue.create")
        result, _ = run_job(tmp_path, job)
        assert result.status == "error"  # type: ignore[attr-defined]

    def test_heartbeat_thread(self, tmp_path: Path) -> None:
        script = tmp_path / "script.json"
        script.write_text(json.dumps([{"text": "slow", "sleep_s": 0.4}]))
        import os

        os.environ["SDXLOOP_ECHO_SCRIPT"] = str(script)
        try:
            result, events = run_job(tmp_path, agent_job(), heartbeat=0.1)
        finally:
            del os.environ["SDXLOOP_ECHO_SCRIPT"]
        assert result.status == "ok"  # type: ignore[attr-defined]
        assert [e.type for e in events].count(EventTypes.WORKER_HEARTBEAT) >= 2


class TestBackendRegistry:
    def test_get_backend_echo(self) -> None:
        assert get_backend("echo").name == "echo"

    def test_get_backend_env_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SDXLOOP_WORKER_BACKEND", "echo")
        assert get_backend().name == "echo"

    def test_get_backend_unknown(self) -> None:
        with pytest.raises(BackendUnavailableError, match="unknown agent backend"):
            get_backend("nope")

    def test_copilot_backend_unavailable_without_sdk(self, tmp_path: Path) -> None:
        # The copilot extra is not installed in the test environment.
        backend = get_backend("copilot")
        with pytest.raises(BackendUnavailableError, match="github-copilot-sdk"):
            backend.run_session(agent_job(), lambda *a, **k: None)  # type: ignore[arg-type,return-value]


class TestEchoScriptEdgeCases:
    def test_script_entry_must_be_object(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = tmp_path / "script.json"
        script.write_text(json.dumps(["not an object"]))
        monkeypatch.setenv("SDXLOOP_ECHO_SCRIPT", str(script))
        with pytest.raises(TypeError, match="must be an object"):
            EchoBackend().run_session(agent_job(), lambda *a, **k: None)  # type: ignore[arg-type,return-value]


class TestEntrypointInProcess:
    def test_main_happy_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SDXLOOP_WORKER_BACKEND", "echo")
        job_path = tmp_path / "job.json"
        job_path.write_text(agent_job().model_dump_json())
        code = main(
            [
                "run",
                "--job",
                str(job_path),
                "--events",
                str(tmp_path / "e.jsonl"),
                "--result",
                str(tmp_path / "r.json"),
                "--heartbeat",
                "0",
                "--env-file",
                str(tmp_path / "none.sh"),
            ]
        )
        assert code == 0
        assert (tmp_path / "r.json").is_file()

    def test_main_invalid_job(self, tmp_path: Path) -> None:
        job_path = tmp_path / "job.json"
        job_path.write_text("nope")
        code = main(
            [
                "run",
                "--job",
                str(job_path),
                "--events",
                str(tmp_path / "e.jsonl"),
                "--result",
                str(tmp_path / "r.json"),
            ]
        )
        assert code == 64


class TestEnvFile:
    def test_load_env_file(self, tmp_path: Path) -> None:
        path = tmp_path / "env.sh"
        path.write_text(
            "# header\n"
            "export SIMPLE=plain\n"
            "export QUOTED='has spaces'\n"
            'DOUBLE="also spaces"\n'
            "export EMPTY=\n"
            "garbage line without equals\n"
            "export BAD='unterminated\n"
        )
        loaded = load_env_file(path)
        assert loaded == {
            "SIMPLE": "plain",
            "QUOTED": "has spaces",
            "DOUBLE": "also spaces",
            "EMPTY": "",
        }

    def test_missing_file(self, tmp_path: Path) -> None:
        assert load_env_file(tmp_path / "nope.sh") == {}
