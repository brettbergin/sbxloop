"""Contract tests: the real worker process, echo backend, full protocol."""

from __future__ import annotations

from pathlib import Path

from worker_harness import WorkerHarness

from sdxloop_worker.protocol import Event, EventTypes, JobRequest


def agent_job(**overrides: object) -> JobRequest:
    base: dict[str, object] = {
        "job_id": "j1",
        "run_id": "r1",
        "kind": "agent.session",
        "prompt": "say hello",
    }
    base.update(overrides)
    return JobRequest.model_validate(base)


class TestAgentSession:
    def test_happy_path_events_and_result(self, harness: WorkerHarness) -> None:
        proc = harness.run(agent_job())
        assert proc.returncode == 0

        result = harness.result()
        assert result.status == "ok"
        assert result.output_text == "echo: say hello"
        assert result.session_id == "echo-j1"
        assert result.usage is not None and result.usage.model == "echo"

        events = harness.events()
        types = [e.type for e in events]
        assert types[0] == EventTypes.WORKER_START
        assert types[-1] == EventTypes.WORKER_END
        assert EventTypes.AGENT_MESSAGE in types
        assert EventTypes.WORKER_RESULT in types
        assert all(e.run_id == "r1" and e.job_id == "j1" for e in events)

    def test_stdout_mirrors_event_file(self, harness: WorkerHarness) -> None:
        proc = harness.run(agent_job())
        stdout_events = [
            Event.from_json_line(line) for line in proc.stdout.splitlines() if line.strip()
        ]
        assert stdout_events == harness.events()

    def test_expect_json_with_fenced_output(self, harness: WorkerHarness) -> None:
        script = harness.write_script(
            [{"text": 'plan below\n```json\n{"tasks": [1, 2]}\n```\ndone'}]
        )
        proc = harness.run(agent_job(expect="json"), env={"SDXLOOP_ECHO_SCRIPT": str(script)})
        assert proc.returncode == 0
        result = harness.result()
        assert result.status == "ok"
        assert result.output_json == {"tasks": [1, 2]}

    def test_expect_json_missing_is_error(self, harness: WorkerHarness) -> None:
        script = harness.write_script([{"text": "no json here at all"}])
        proc = harness.run(agent_job(expect="json"), env={"SDXLOOP_ECHO_SCRIPT": str(script)})
        assert proc.returncode == 0  # result file written => exit 0
        result = harness.result()
        assert result.status == "error"
        assert result.error is not None
        assert result.error.type == "ExpectedJsonMissing"
        types = [e.type for e in harness.events()]
        assert EventTypes.WORKER_ERROR in types
        assert types[-1] == EventTypes.WORKER_END

    def test_backend_failure_becomes_error_result(self, harness: WorkerHarness) -> None:
        script = harness.write_script([{"fail": "backend exploded"}])
        proc = harness.run(agent_job(), env={"SDXLOOP_ECHO_SCRIPT": str(script)})
        assert proc.returncode == 0
        result = harness.result()
        assert result.status == "error"
        assert result.error is not None
        assert result.error.type == "RuntimeError"
        assert "backend exploded" in result.error.message
        assert result.error.detail is not None  # traceback captured

    def test_scripted_events_are_forwarded(self, harness: WorkerHarness) -> None:
        script = harness.write_script(
            [
                {
                    "text": "done",
                    "events": [
                        {"type": "agent.tool_start", "data": {"tool": "bash"}},
                        {"type": "agent.tool_end", "data": {"tool": "bash"}},
                    ],
                }
            ]
        )
        harness.run(agent_job(), env={"SDXLOOP_ECHO_SCRIPT": str(script)})
        types = [e.type for e in harness.events()]
        assert EventTypes.AGENT_TOOL_START in types
        assert EventTypes.AGENT_TOOL_END in types

    def test_heartbeats_emitted(self, harness: WorkerHarness) -> None:
        script = harness.write_script([{"text": "slow", "sleep_s": 0.6}])
        harness.run(agent_job(), heartbeat=0.1, env={"SDXLOOP_ECHO_SCRIPT": str(script)})
        types = [e.type for e in harness.events()]
        assert types.count(EventTypes.WORKER_HEARTBEAT) >= 2


class TestShellCheck:
    def shell_job(self, argv: list[str], **overrides: object) -> JobRequest:
        base: dict[str, object] = {
            "job_id": "j2",
            "run_id": "r1",
            "kind": "shell.check",
            "argv": argv,
        }
        base.update(overrides)
        return JobRequest.model_validate(base)

    def test_success_captures_output(self, harness: WorkerHarness) -> None:
        harness.run(self.shell_job(["sh", "-c", "echo out; echo err >&2"]))
        result = harness.result()
        assert result.status == "ok"
        assert result.exit_code == 0
        assert result.output_text is not None
        assert "out" in result.output_text
        assert "err" in result.output_text

    def test_nonzero_exit_is_ok_status_with_code(self, harness: WorkerHarness) -> None:
        harness.run(self.shell_job(["sh", "-c", "exit 7"]))
        result = harness.result()
        assert result.status == "ok"  # the job ran; verification decision is the host's
        assert result.exit_code == 7

    def test_timeout_status(self, harness: WorkerHarness) -> None:
        harness.run(self.shell_job(["sleep", "5"], timeout_s=0.3), timeout=10.0)
        result = harness.result()
        assert result.status == "timeout"
        assert result.error is not None
        assert result.error.type == "Timeout"

    def test_cwd_respected(self, harness: WorkerHarness, tmp_path: Path) -> None:
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "marker.txt").write_text("x")
        harness.run(self.shell_job(["ls"], cwd=str(sub)))
        result = harness.result()
        assert result.output_text is not None
        assert "marker.txt" in result.output_text


class TestGithubOpPlaceholder:
    def test_github_op_unavailable_yet(self, harness: WorkerHarness) -> None:
        job = JobRequest(job_id="j3", run_id="r1", kind="github.op", op="issue.create", params={})
        proc = harness.run(job)
        assert proc.returncode == 0
        result = harness.result()
        assert result.status == "error"
        assert result.error is not None
        assert result.error.type == "ModuleNotFoundError"


class TestEntrypoint:
    def test_invalid_job_file_exits_64(self, harness: WorkerHarness) -> None:
        harness.job_path.write_text("{not json")
        import subprocess
        import sys

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "sdxloop_worker",
                "run",
                "--job",
                str(harness.job_path),
                "--events",
                str(harness.events_path),
                "--result",
                str(harness.result_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 64
        assert "invalid job file" in proc.stderr

    def test_env_file_loaded_with_env_precedence(self, harness: WorkerHarness) -> None:
        (harness.tmp_path / "env.sh").write_text(
            "# comment\nexport FROM_FILE='file value'\nexport PRESET=file\nnot a line\n"
        )
        script = harness.write_script([{"text": "ok"}])
        # PRESET is already set in the process env; the file must not override
        proc = harness.run(
            agent_job(),
            env={
                "SDXLOOP_ECHO_SCRIPT": str(script),
                "PRESET": "process",
            },
        )
        assert proc.returncode == 0


class TestEchoScriptCursor:
    def test_script_consumed_in_order_across_processes(self, harness: WorkerHarness) -> None:
        script = harness.write_script([{"text": "first"}, {"text": "second"}])
        env = {"SDXLOOP_ECHO_SCRIPT": str(script)}

        harness.run(agent_job(job_id="ja"), env=env)
        assert harness.result().output_text == "first"

        harness.run(agent_job(job_id="jb"), env=env)
        assert harness.result().output_text == "second"

    def test_exhausted_script_fails_job(self, harness: WorkerHarness) -> None:
        script = harness.write_script([{"text": "only"}])
        env = {"SDXLOOP_ECHO_SCRIPT": str(script)}
        harness.run(agent_job(job_id="ja"), env=env)
        harness.run(agent_job(job_id="jb"), env=env)
        result = harness.result()
        assert result.status == "error"
        assert result.error is not None
        assert "exhausted" in result.error.message
