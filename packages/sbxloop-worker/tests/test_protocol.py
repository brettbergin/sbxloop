"""Protocol model contract tests: shapes, validators, wire round-trips."""

import pytest
from pydantic import ValidationError

from sbxloop_worker.protocol import (
    PROTOCOL_VERSION,
    ErrorInfo,
    Event,
    EventTypes,
    HostToolCall,
    HostToolResponse,
    HostToolSpec,
    JobRequest,
    JobResult,
    SessionHealth,
    Usage,
)


def agent_job(**overrides: object) -> JobRequest:
    base: dict[str, object] = {
        "job_id": "j1",
        "run_id": "r1",
        "kind": "agent.session",
        "prompt": "do the thing",
    }
    base.update(overrides)
    return JobRequest.model_validate(base)


class TestJobRequest:
    def test_agent_session_roundtrip(self) -> None:
        job = agent_job(model="auto", permission_mode="read_only", expect="json")
        restored = JobRequest.model_validate_json(job.model_dump_json())
        assert restored == job
        assert restored.v == PROTOCOL_VERSION

    def test_agent_session_requires_prompt(self) -> None:
        with pytest.raises(ValidationError, match="non-empty prompt"):
            JobRequest(job_id="j1", run_id="r1", kind="agent.session")

    def test_agent_session_rejects_shell_fields(self) -> None:
        with pytest.raises(ValidationError, match="must not set argv"):
            agent_job(argv=["ls"])

    def test_shell_check_requires_argv(self) -> None:
        with pytest.raises(ValidationError, match="non-empty argv"):
            JobRequest(job_id="j1", run_id="r1", kind="shell.check")

    def test_shell_check_ok(self) -> None:
        job = JobRequest(
            job_id="j1", run_id="r1", kind="shell.check", argv=["pytest", "-q"], cwd="/work"
        )
        assert job.argv == ["pytest", "-q"]

    def test_shell_batch_requires_commands(self) -> None:
        with pytest.raises(ValidationError, match="non-empty commands"):
            JobRequest(job_id="j1", run_id="r1", kind="shell.batch")

    def test_host_tools_only_for_agent_session(self) -> None:
        with pytest.raises(ValidationError, match="must not set host_tools"):
            JobRequest(
                job_id="j1", run_id="r1", kind="shell.check", argv=["ls"], available_tools=[]
            )
        with pytest.raises(ValidationError, match="must not set host_tools"):
            JobRequest(
                job_id="j1", run_id="r1", kind="shell.batch", commands=["true"], host_tools_dir="/t"
            )

    def test_host_tools_require_dir(self) -> None:
        tool = HostToolSpec(name="sbx_control", description="run a daemon verb")
        with pytest.raises(ValidationError, match="requires host_tools_dir"):
            agent_job(host_tools=[tool])
        job = agent_job(host_tools=[tool], host_tools_dir="/home/agent/.sbxloop/tools/j1")
        assert job.host_tools[0].parameters == {"type": "object", "properties": {}}
        assert job.host_tool_timeout_s == 120.0
        assert job.available_tools is None

    def test_duplicate_host_tool_names_rejected(self) -> None:
        tool = HostToolSpec(name="dup", description="x")
        with pytest.raises(ValidationError, match="duplicate host tool name"):
            agent_job(host_tools=[tool, tool], host_tools_dir="/t")

    def test_host_tool_name_alphabet(self) -> None:
        with pytest.raises(ValidationError):
            HostToolSpec(name="not ok!", description="x")

    def test_host_tool_response_roundtrip(self) -> None:
        call = HostToolCall(call_id="c1", name="list_runs", arguments={"limit": 3})
        event = Event.now(EventTypes.AGENT_TOOL_REQUEST, "r1", "j1", **call.model_dump())
        assert HostToolCall.model_validate(event.data) == call
        response = HostToolResponse(call_id="c1", ok=False, text="", error="boom")
        restored = HostToolResponse.model_validate_json(response.model_dump_json())
        assert restored == response and restored.v == PROTOCOL_VERSION
        with pytest.raises(ValidationError):
            HostToolResponse.model_validate({"call_id": "c1", "ok": True, "extra": 1})

    def test_shell_batch_rejects_argv(self) -> None:
        with pytest.raises(ValidationError, match="must not set prompt, argv, or op"):
            JobRequest(job_id="j1", run_id="r1", kind="shell.batch", commands=["true"], argv=["ls"])

    def test_shell_batch_roundtrip(self) -> None:
        job = JobRequest(
            job_id="j1",
            run_id="r1",
            kind="shell.batch",
            commands=["git status", "pytest -q"],
            command_timeout_s=30.0,
            cwd="/work",
        )
        assert JobRequest.model_validate_json(job.model_dump_json()) == job

    def test_shell_check_rejects_commands(self) -> None:
        with pytest.raises(ValidationError, match="must not set prompt, commands, or op"):
            JobRequest(job_id="j1", run_id="r1", kind="shell.check", argv=["ls"], commands=["true"])

    def test_github_op_requires_op(self) -> None:
        with pytest.raises(ValidationError, match="requires an op name"):
            JobRequest(job_id="j1", run_id="r1", kind="github.op")

    def test_github_op_rejects_prompt(self) -> None:
        with pytest.raises(ValidationError, match="must not set prompt"):
            JobRequest(job_id="j1", run_id="r1", kind="github.op", op="issue.create", prompt="hi")

    def test_unknown_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JobRequest.model_validate(
                {
                    "job_id": "j1",
                    "run_id": "r1",
                    "kind": "shell.check",
                    "argv": ["true"],
                    "surprise": 1,
                }
            )


class TestJobResult:
    def test_ok_result(self) -> None:
        result = JobResult(job_id="j1", status="ok", output_text="done", session_id="s-9")
        assert result.error is None

    def test_error_requires_errorinfo(self) -> None:
        with pytest.raises(ValidationError, match="requires an error"):
            JobResult(job_id="j1", status="error")

    def test_timeout_requires_errorinfo(self) -> None:
        with pytest.raises(ValidationError, match="requires an error"):
            JobResult(job_id="j1", status="timeout")

    def test_error_result_roundtrip(self) -> None:
        result = JobResult(
            job_id="j1",
            status="error",
            error=ErrorInfo(type="BackendError", message="boom", detail="stack"),
            exit_code=3,
        )
        assert JobResult.model_validate_json(result.model_dump_json()) == result

    def test_output_json_accepts_list(self) -> None:
        result = JobResult(job_id="j1", status="ok", output_json=[1, 2])
        assert result.output_json == [1, 2]


class TestEvent:
    def test_now_and_jsonl_roundtrip(self) -> None:
        event = Event.now(EventTypes.AGENT_MESSAGE, "r1", job_id="j1", content="hi")
        line = event.to_json_line()
        assert "\n" not in line
        assert Event.from_json_line(line) == event

    def test_data_defaults_empty(self) -> None:
        event = Event.now(EventTypes.WORKER_START, "r1")
        assert event.data == {}
        assert event.job_id is None

    def test_from_json_line_rejects_garbage(self) -> None:
        with pytest.raises(ValueError):
            Event.from_json_line('{"nope": true}')


class TestUsage:
    def test_merged_sums_and_prefers_latest_model(self) -> None:
        a = Usage(model="m1", input_tokens=10, output_tokens=5, cost=0.5)
        b = Usage(model="m2", input_tokens=3, cache_read_tokens=7)
        merged = a.merged(b)
        assert merged.model == "m2"
        assert merged.input_tokens == 13
        assert merged.output_tokens == 5
        assert merged.cache_read_tokens == 7
        assert merged.cost == 0.5

    def test_merged_all_none(self) -> None:
        merged = Usage().merged(Usage())
        assert merged == Usage()


class TestSessionHealth:
    def test_jobresult_roundtrip_with_health(self) -> None:
        result = JobResult(
            job_id="j1",
            status="ok",
            health=SessionHealth(
                permission_denials={"shell": 2},
                tool_failures={"grep": 3, "glob": 1},
            ),
        )
        restored = JobResult.model_validate_json(result.model_dump_json())
        assert restored == result
        assert restored.health is not None
        assert restored.health.tool_failures["grep"] == 3

    def test_health_defaults_none(self) -> None:
        assert JobResult(job_id="j1", status="ok").health is None

    def test_degraded_only_on_tool_failures(self) -> None:
        # Denials alone are the read-only allowlist working as designed —
        # they never mark the session degraded (#123).
        assert not SessionHealth(permission_denials={"shell": 5}).degraded
        assert SessionHealth(tool_failures={"grep": 1}).degraded
        assert not SessionHealth().degraded

    def test_summary_names_kinds_and_counts(self) -> None:
        health = SessionHealth(
            permission_denials={"shell": 2}, tool_failures={"grep": 3, "glob": 1}
        )
        summary = health.summary()
        assert "tool failures: glob x1, grep x3" in summary
        assert "permission denials: shell x2" in summary
        assert SessionHealth().summary() == "healthy"
