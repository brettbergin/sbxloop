"""Contract tests: the real worker process, echo backend, full protocol."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from worker_harness import WorkerHarness

from sbxloop_worker.hosttools import response_path
from sbxloop_worker.protocol import (
    Event,
    EventTypes,
    HostToolCall,
    HostToolResponse,
    HostToolSpec,
    JobRequest,
)


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
        # agent.message events name the answering model so the transcript
        # header can attribute the reply to a model slug.
        messages = [e for e in events if e.type == EventTypes.AGENT_MESSAGE]
        assert all(e.data.get("model") == "echo" for e in messages)

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
        proc = harness.run(agent_job(expect="json"), env={"SBXLOOP_ECHO_SCRIPT": str(script)})
        assert proc.returncode == 0
        result = harness.result()
        assert result.status == "ok"
        assert result.output_json == {"tasks": [1, 2]}

    def test_expect_json_missing_is_error(self, harness: WorkerHarness) -> None:
        script = harness.write_script([{"text": "no json here at all"}])
        proc = harness.run(agent_job(expect="json"), env={"SBXLOOP_ECHO_SCRIPT": str(script)})
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
        proc = harness.run(agent_job(), env={"SBXLOOP_ECHO_SCRIPT": str(script)})
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
        harness.run(agent_job(), env={"SBXLOOP_ECHO_SCRIPT": str(script)})
        types = [e.type for e in harness.events()]
        assert EventTypes.AGENT_TOOL_START in types
        assert EventTypes.AGENT_TOOL_END in types

    def test_heartbeats_emitted(self, harness: WorkerHarness) -> None:
        script = harness.write_script([{"text": "slow", "sleep_s": 0.6}])
        harness.run(agent_job(), heartbeat=0.1, env={"SBXLOOP_ECHO_SCRIPT": str(script)})
        types = [e.type for e in harness.events()]
        assert types.count(EventTypes.WORKER_HEARTBEAT) >= 2


class TestHostTools:
    """The echo backend drives the real round trip: request event out on the
    event file, response file in — exactly what the host does through sbx."""

    def _host(self, harness: WorkerHarness, tools_dir: Path, answers: dict[str, str]) -> None:
        """Play the host: watch the events file, answer each request."""
        seen: set[str] = set()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            for event in harness.events():
                if event.type != EventTypes.AGENT_TOOL_REQUEST:
                    continue
                call = HostToolCall.model_validate(event.data)
                if call.call_id in seen:
                    continue
                seen.add(call.call_id)
                text = answers.get(call.name)
                response = (
                    HostToolResponse(call_id=call.call_id, ok=True, text=text)
                    if text is not None
                    else HostToolResponse(call_id=call.call_id, ok=False, error="unknown tool")
                )
                response_path(tools_dir, call.call_id).write_text(response.model_dump_json())
                if event.type == EventTypes.WORKER_END:
                    return
            time.sleep(0.05)

    def test_echo_round_trip(self, harness: WorkerHarness, tmp_path: Path) -> None:
        tools_dir = tmp_path / "tools" / "j1"
        script = harness.write_script(
            [
                {
                    "text": "asked the host",
                    "host_tool_calls": [
                        {"name": "sbx_control", "arguments": {"command": "status"}},
                        {"name": "nope", "arguments": {}},
                    ],
                }
            ]
        )
        job = agent_job(
            host_tools=[
                HostToolSpec(name="sbx_control", description="daemon verb"),
                HostToolSpec(name="nope", description="unknown on the host"),
            ],
            host_tools_dir=str(tools_dir),
        )
        host = threading.Thread(
            target=self._host, args=(harness, tools_dir, {"sbx_control": "paused=false"})
        )
        host.start()
        proc = harness.run(job, env={"SBXLOOP_ECHO_SCRIPT": str(script)})
        host.join(timeout=15)
        assert proc.returncode == 0, proc.stderr
        result = harness.result()
        assert result.status == "ok"
        assert result.output_text == "asked the host\npaused=false\n[nope failed: unknown tool]"
        events = harness.events()
        requests = [e for e in events if e.type == EventTypes.AGENT_TOOL_REQUEST]
        responses = [e for e in events if e.type == EventTypes.AGENT_TOOL_RESPONSE]
        assert [HostToolCall.model_validate(e.data).name for e in requests] == [
            "sbx_control",
            "nope",
        ]
        assert [e.data["ok"] for e in responses] == [True, False]
        assert all(e.job_id == "j1" for e in requests + responses)

    def test_host_tool_timeout_is_error_result(
        self, harness: WorkerHarness, tmp_path: Path
    ) -> None:
        script = harness.write_script(
            [{"text": "x", "host_tool_calls": [{"name": "slow", "arguments": {}}]}]
        )
        job = agent_job(
            host_tools=[HostToolSpec(name="slow", description="never answered")],
            host_tools_dir=str(tmp_path / "tools"),
            host_tool_timeout_s=0.3,
        )
        proc = harness.run(job, env={"SBXLOOP_ECHO_SCRIPT": str(script)})
        assert proc.returncode == 0
        result = harness.result()
        assert result.status == "error"
        assert result.error is not None and "slow" in result.error.message
        types = [e.type for e in harness.events()]
        assert EventTypes.AGENT_TOOL_REQUEST in types and EventTypes.AGENT_TOOL_RESPONSE in types


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


class TestGithubOpErrors:
    def test_github_op_bad_params_is_error_result(self, harness: WorkerHarness) -> None:
        # Missing params fail validation before any transport/network use.
        job = JobRequest(job_id="j3", run_id="r1", kind="github.op", op="issue.create", params={})
        proc = harness.run(job)
        assert proc.returncode == 0
        result = harness.result()
        assert result.status == "error"
        assert result.error is not None
        assert result.error.type == "GithubOpError"
        assert "missing required params" in result.error.message


class TestEntrypoint:
    def test_invalid_job_file_exits_64(self, harness: WorkerHarness) -> None:
        harness.job_path.write_text("{not json")
        import subprocess
        import sys

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "sbxloop_worker",
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
                "SBXLOOP_ECHO_SCRIPT": str(script),
                "PRESET": "process",
            },
        )
        assert proc.returncode == 0


class TestEchoScriptCursor:
    def test_script_consumed_in_order_across_processes(self, harness: WorkerHarness) -> None:
        script = harness.write_script([{"text": "first"}, {"text": "second"}])
        env = {"SBXLOOP_ECHO_SCRIPT": str(script)}

        harness.run(agent_job(job_id="ja"), env=env)
        assert harness.result().output_text == "first"

        harness.run(agent_job(job_id="jb"), env=env)
        assert harness.result().output_text == "second"

    def test_exhausted_script_fails_job(self, harness: WorkerHarness) -> None:
        script = harness.write_script([{"text": "only"}])
        env = {"SBXLOOP_ECHO_SCRIPT": str(script)}
        harness.run(agent_job(job_id="ja"), env=env)
        harness.run(agent_job(job_id="jb"), env=env)
        result = harness.result()
        assert result.status == "error"
        assert result.error is not None
        assert "exhausted" in result.error.message
