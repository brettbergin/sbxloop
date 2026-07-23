"""Protocol model contract tests: shapes, validators, wire round-trips."""

import pytest
from pydantic import ValidationError

from sdxloop_worker.protocol import (
    PROTOCOL_VERSION,
    ErrorInfo,
    Event,
    EventTypes,
    JobRequest,
    JobResult,
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
