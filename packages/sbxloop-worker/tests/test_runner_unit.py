"""In-process runner/backend/entrypoint tests (subprocess-free, coverage-visible)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sbxloop_worker.__main__ import apply_env_file, load_env_file, main
from sbxloop_worker.backends import BackendUnavailableError, get_backend
from sbxloop_worker.backends.echo import EchoBackend
from sbxloop_worker.protocol import Event, EventTypes, JobRequest
from sbxloop_worker.runner import JobRunner


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

    def test_shell_batch_per_command_results(self, tmp_path: Path) -> None:
        job = JobRequest(
            job_id="j2",
            run_id="r1",
            kind="shell.batch",
            commands=["echo one", "exit 3", "echo three >&2"],
        )
        result, _ = run_job(tmp_path, job)
        assert result.status == "ok"  # type: ignore[attr-defined]
        # job exit_code is the first nonzero, so the result is glanceable
        assert result.exit_code == 3  # type: ignore[attr-defined]
        per_command = result.output_json  # type: ignore[attr-defined]
        assert [(r["command"], r["exit_code"]) for r in per_command] == [
            ("echo one", 0),
            ("exit 3", 3),
            ("echo three >&2", 0),
        ]
        assert per_command[0]["output"] == "one\n"
        assert "three" in per_command[2]["output"]  # stderr captured too

    def test_shell_batch_all_pass(self, tmp_path: Path) -> None:
        job = JobRequest(job_id="j2", run_id="r1", kind="shell.batch", commands=["true", "true"])
        result, _ = run_job(tmp_path, job)
        assert result.exit_code == 0  # type: ignore[attr-defined]

    def test_shell_batch_timeout(self, tmp_path: Path) -> None:
        job = JobRequest(
            job_id="j2",
            run_id="r1",
            kind="shell.batch",
            commands=["sleep 5"],
            timeout_s=0.2,
        )
        result, events = run_job(tmp_path, job)
        assert result.status == "timeout"  # type: ignore[attr-defined]
        assert EventTypes.WORKER_ERROR in [e.type for e in events]

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

    def test_github_op_bad_params(self, tmp_path: Path) -> None:
        job = JobRequest(job_id="j3", run_id="r1", kind="github.op", op="issue.create")
        result, _ = run_job(tmp_path, job)
        assert result.status == "error"  # type: ignore[attr-defined]
        assert result.error.type == "GithubOpError"  # type: ignore[attr-defined]

    def test_heartbeat_thread(self, tmp_path: Path) -> None:
        # A 1 s job at a 50 ms cadence expects ~20 beats and asserts >= 2:
        # the old 0.4 s / 100 ms pairing left a 4x margin that a loaded CI
        # runner ate (one beat observed on py3.14 twice in one day).
        script = tmp_path / "script.json"
        script.write_text(json.dumps([{"text": "slow", "sleep_s": 1.0}]))
        import os

        os.environ["SBXLOOP_ECHO_SCRIPT"] = str(script)
        try:
            result, events = run_job(tmp_path, agent_job(), heartbeat=0.05)
        finally:
            del os.environ["SBXLOOP_ECHO_SCRIPT"]
        assert result.status == "ok"  # type: ignore[attr-defined]
        assert [e.type for e in events].count(EventTypes.WORKER_HEARTBEAT) >= 2


class TestBackendRegistry:
    def test_get_backend_echo(self) -> None:
        assert get_backend("echo").name == "echo"

    def test_get_backend_env_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SBXLOOP_WORKER_BACKEND", "echo")
        assert get_backend().name == "echo"

    def test_get_backend_unknown(self) -> None:
        with pytest.raises(BackendUnavailableError, match="unknown agent backend"):
            get_backend("nope")

    def test_copilot_backend_unavailable_without_sdk(self, tmp_path: Path) -> None:
        # The copilot extra is not installed in the test environment.
        backend = get_backend("copilot")
        with pytest.raises(BackendUnavailableError, match="github-copilot-sdk"):
            backend.run_session(agent_job(), lambda *a, **k: None)  # type: ignore[arg-type,return-value]


class TestCopilotEventHelpers:
    """Pure helpers from the copilot backend (importable without the SDK)."""

    def test_tool_args_prefers_glob_pattern(self) -> None:
        from sbxloop_worker.backends.copilot import _tool_args

        assert _tool_args({"pattern": "**/*.py"}) == "**/*.py"

    def test_tool_error_reads_failure_message(self) -> None:
        from types import SimpleNamespace

        from sbxloop_worker.backends.copilot import _tool_error

        # A failed ToolExecutionComplete: result is None, error carries the
        # reason (the SDK documents `result` as success-only).
        data = SimpleNamespace(
            success=False,
            result=None,
            error=SimpleNamespace(message="permission denied", code=None),
        )
        assert _tool_error(data) == "permission denied"

    def test_tool_error_none_when_absent(self) -> None:
        from types import SimpleNamespace

        from sbxloop_worker.backends.copilot import _tool_error

        assert _tool_error(SimpleNamespace(success=True, result=None, error=None)) is None


class TestRipgrepPageSizePlan:
    """The bundled-ripgrep page-size guard (issue #122), pure decision logic."""

    def test_4k_guest_changes_nothing(self) -> None:
        from sbxloop_worker.backends.copilot import ripgrep_page_size_plan

        assert ripgrep_page_size_plan(4096, "/usr/bin/rg", None) == ({}, None)
        assert ripgrep_page_size_plan(4096, None, None) == ({}, None)

    def test_non_4k_with_system_rg_reroutes(self) -> None:
        from sbxloop_worker.backends.copilot import ripgrep_page_size_plan

        updates, warning = ripgrep_page_size_plan(16384, "/usr/bin/rg", None)
        assert updates == {"USE_BUILTIN_RIPGREP": "false"}
        assert warning is not None
        assert "16384" in warning
        assert "/usr/bin/rg" in warning

    def test_non_4k_without_system_rg_warns_only(self) -> None:
        from sbxloop_worker.backends.copilot import ripgrep_page_size_plan

        updates, warning = ripgrep_page_size_plan(16384, None, None)
        assert updates == {}
        assert warning is not None
        assert "glob/grep" in warning
        assert "ripgrep" in warning

    def test_operator_setting_wins(self) -> None:
        # An explicit USE_BUILTIN_RIPGREP (either polarity) is never touched.
        from sbxloop_worker.backends.copilot import ripgrep_page_size_plan

        assert ripgrep_page_size_plan(16384, "/usr/bin/rg", "true") == ({}, None)
        assert ripgrep_page_size_plan(16384, None, "false") == ({}, None)


class TestEchoScriptEdgeCases:
    def test_script_entry_must_be_object(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = tmp_path / "script.json"
        script.write_text(json.dumps(["not an object"]))
        monkeypatch.setenv("SBXLOOP_ECHO_SCRIPT", str(script))
        with pytest.raises(TypeError, match="must be an object"):
            EchoBackend().run_session(agent_job(), lambda *a, **k: None)  # type: ignore[arg-type,return-value]

    def test_scripted_health_rides_through_to_the_job_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Session health must survive backend -> runner -> result file, so
        the engine's degraded-critic guard sees it (#123)."""
        script = tmp_path / "script.json"
        script.write_text(
            json.dumps(
                [
                    {
                        "text": "looks fine",
                        "health": {
                            "permission_denials": {"shell": 1},
                            "tool_failures": {"grep": 2},
                        },
                    }
                ]
            )
        )
        monkeypatch.setenv("SBXLOOP_ECHO_SCRIPT", str(script))
        result, _ = run_job(tmp_path, agent_job())
        assert result.health is not None  # type: ignore[attr-defined]
        assert result.health.tool_failures == {"grep": 2}  # type: ignore[attr-defined]
        assert result.health.permission_denials == {"shell": 1}  # type: ignore[attr-defined]
        assert result.health.degraded  # type: ignore[attr-defined]


class TestEntrypointInProcess:
    def test_main_happy_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SBXLOOP_WORKER_BACKEND", "echo")
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

    def test_the_real_environment_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # apply_env_file writes straight to os.environ, so every name it will
        # create has to be registered with monkeypatch first or it leaks into
        # the rest of the worker process. Deliberately NOT the `SBXLOOP_`
        # prefix: that is the config prefix, and a stray one makes every later
        # Config() raise "Extra inputs are not permitted".
        path = tmp_path / "env.sh"
        path.write_text("export WORKER_ENVFILE_TOK=from_file\nexport WORKER_ENVFILE_NEW=fresh\n")
        monkeypatch.setenv("WORKER_ENVFILE_TOK", "gho_alreadyhere")
        monkeypatch.setenv("WORKER_ENVFILE_NEW", "")
        monkeypatch.delenv("WORKER_ENVFILE_NEW")
        apply_env_file(path)
        assert os.environ["WORKER_ENVFILE_TOK"] == "gho_alreadyhere"
        assert os.environ["WORKER_ENVFILE_NEW"] == "fresh"

    def test_the_env_file_beats_an_sbx_proxy_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Field failure 2026-08-21: `setdefault` let sbx's injected sentinel
        win over the real token the provisioner had just written, so the
        plain-env fallback did nothing and every session 401'd. Nothing can
        authenticate with a sentinel, so the file has to win over one."""
        path = tmp_path / "env.sh"
        path.write_text("export COPILOT_GITHUB_TOKEN=gho_therealtoken\n")
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "sbx-cs-Xrz8X47IcldsQVJ0")
        apply_env_file(path)
        assert os.environ["COPILOT_GITHUB_TOKEN"] == "gho_therealtoken"


class TestPersistentEnvFile:
    def test_sandbox_persistent_env_loaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The worker loads /etc/sandbox-persistent.sh (where sbx documents
        persistent sandbox env) in addition to ~/.sbxloop/env.sh."""
        import os

        import sbxloop_worker.__main__ as main_mod

        persistent = tmp_path / "sandbox-persistent.sh"
        persistent.write_text("export SBXLOOP_TEST_PERSISTENT_SENTINEL=from-persistent\n")
        monkeypatch.setattr(main_mod, "PERSISTENT_ENV_FILE", persistent)
        monkeypatch.delenv("SBXLOOP_TEST_PERSISTENT_SENTINEL", raising=False)
        monkeypatch.setenv("SBXLOOP_WORKER_BACKEND", "echo")

        job_path = tmp_path / "job.json"
        job_path.write_text(agent_job().model_dump_json())
        code = main_mod.main(
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
        try:
            assert code == 0
            assert os.environ["SBXLOOP_TEST_PERSISTENT_SENTINEL"] == "from-persistent"
        finally:
            os.environ.pop("SBXLOOP_TEST_PERSISTENT_SENTINEL", None)

    def test_operator_env_from_the_env_file_reaches_the_jobs_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`[sandbox] env` / `secret_env` arrive as exports in the env file
        (#679); the worker loads them into its own environment, which every
        job's subprocess — the agent CLI, a verify command — inherits."""
        monkeypatch.delenv("SBXLOOP_TEST_OPERATOR_ENV", raising=False)
        env_file = tmp_path / "env.sh"
        env_file.write_text("export SBXLOOP_TEST_OPERATOR_ENV='from operator'\n")
        job_path = tmp_path / "job.json"
        job_path.write_text(
            JobRequest(
                job_id="j3",
                run_id="r1",
                kind="shell.check",
                argv=["sh", "-c", 'printf %s "$SBXLOOP_TEST_OPERATOR_ENV"'],
            ).model_dump_json()
        )
        result_path = tmp_path / "r.json"
        code = main(
            [
                "run",
                "--job",
                str(job_path),
                "--events",
                str(tmp_path / "e.jsonl"),
                "--result",
                str(result_path),
                "--heartbeat",
                "0",
                "--env-file",
                str(env_file),
            ]
        )
        try:
            assert code == 0
            result = json.loads(result_path.read_text())
            assert result["exit_code"] == 0
            assert result["output_text"] == "from operator"
        finally:
            os.environ.pop("SBXLOOP_TEST_OPERATOR_ENV", None)
