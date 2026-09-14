"""The openai worker backend against a stub speaking the chat-completions
and responses wire shapes — the tool round trip, streaming, sessions,
failures — the way GitHub behaviour is tested against the fake, never a
live endpoint."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sbxloop_worker.backends import BackendUnavailableError, get_backend, openai as openai_backend
from sbxloop_worker.backends.openai import (
    CODING_AGENT_PRESET,
    JSON_REASK,
    SESSION_DIR_ENV,
    EndpointError,
    EndpointRequestRejected,
    EndpointSettings,
    OpenAIBackend,
    endpoint_settings,
    parse_tool_arguments,
)
from sbxloop_worker.hosttools import response_path
from sbxloop_worker.protocol import (
    OPENAI_API_ENV,
    OPENAI_BASE_URL_ENV,
    OPENAI_KEY_NAME_ENV,
    OPENAI_REASONING_EFFORT_ENV,
    OPENAI_RETRIES_ENV,
    OPENAI_TIMEOUT_ENV,
    EventTypes,
    HostToolResponse,
    HostToolSpec,
    JobRequest,
)

KEY = "sk-stub-endpoint-credential-value"
Reply = list[dict[str, Any]] | Exception


def text_chunks(text: str, *, usage: dict[str, Any] | None = None, model: str = "served-model"):
    chunks = [
        {"model": model, "choices": [{"index": 0, "delta": {"content": piece}}]}
        for piece in (text[: len(text) // 2], text[len(text) // 2 :])
        if piece
    ]
    chunks.append(
        {
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 4},
        }
    )
    return chunks


def tool_chunks(calls: list[tuple[str, str, str]], *, model: str = "served-model"):
    """Tool calls the way a streaming server sends them: id and name first,
    the arguments string in pieces after."""
    chunks: list[dict[str, Any]] = []
    for index, (call_id, name, arguments) in enumerate(calls):
        head = {"index": index, "id": call_id, "function": {"name": name, "arguments": ""}}
        chunks.append({"model": model, "choices": [{"index": 0, "delta": {"tool_calls": [head]}}]})
        for piece in (arguments[:3], arguments[3:]):
            tail = {"index": index, "function": {"arguments": piece}}
            chunks.append(
                {"model": model, "choices": [{"index": 0, "delta": {"tool_calls": [tail]}}]}
            )
    chunks.append(
        {
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8},
        }
    )
    return chunks


class FakeTransport:
    def __init__(self, replies: list[Reply], models: list[str] | Exception | None = None):
        self.replies = deque(replies)
        self.requests: list[dict[str, Any]] = []
        self.models = ["served-model"] if models is None else models
        self.settings: EndpointSettings | None = None

    def stream(self, request: dict[str, Any]) -> Iterator[dict[str, Any]]:
        self.requests.append(json.loads(json.dumps(request)))
        if not self.replies:
            raise AssertionError("the stub has no reply left for this request")
        reply = self.replies.popleft()
        if isinstance(reply, Exception):
            raise reply
        yield from reply

    def stream_responses(self, request: dict[str, Any]) -> Iterator[dict[str, Any]]:
        yield from self.stream(request)

    def list_models(self) -> list[str]:
        if isinstance(self.models, Exception):
            raise self.models
        return list(self.models)


@pytest.fixture
def endpoint_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv(OPENAI_BASE_URL_ENV, "http://vllm:8000/v1")
    monkeypatch.setenv(OPENAI_KEY_NAME_ENV, "VLLM_KEY")
    monkeypatch.setenv("VLLM_KEY", KEY)
    monkeypatch.setenv(OPENAI_TIMEOUT_ENV, "30")
    monkeypatch.setenv(OPENAI_RETRIES_ENV, "0")
    sessions = tmp_path / "sessions"
    monkeypatch.setenv(SESSION_DIR_ENV, str(sessions))
    monkeypatch.setattr(OpenAIBackend, "ensure_available", lambda self: None)
    return sessions


def make_job(tmp_path: Path, **overrides: Any) -> JobRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "notes.txt").write_text("fixture evidence\n")
    return JobRequest(
        **{
            "job_id": "j1",
            "run_id": "r1",
            "kind": "agent.session",
            "prompt": "Read the notes and report.",
            "cwd": str(workspace),
            "permission_mode": "read_only",
            "timeout_s": 30.0,
            **overrides,
        }
    )


def run(transport: FakeTransport, job: JobRequest) -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    events: list[tuple[str, dict[str, Any]]] = []

    def capture(transport_settings: EndpointSettings) -> FakeTransport:
        transport.settings = transport_settings
        return transport

    backend = OpenAIBackend(transport_factory=capture)
    result = backend.run_session(job, lambda event, **data: events.append((event, data)))
    return result, events


# -- the loop ------------------------------------------------------------------


def test_tool_round_trip_streams_and_reports(endpoint_env: Path, tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            tool_chunks([("call_1", "read_file", '{"path": "notes.txt"}')]),
            text_chunks("The notes say: fixture evidence."),
        ]
    )
    result, events = run(transport, make_job(tmp_path, model="local-model"))

    assert result.output_text == "The notes say: fixture evidence."
    assert result.failure is None
    assert result.turns == 2
    assert result.session_id is not None and result.session_id.startswith("openai-v1:")
    assert result.usage is not None
    assert (result.usage.input_tokens, result.usage.output_tokens) == (30, 12)
    assert result.usage.model == "served-model" and result.usage.backend == "openai"
    assert result.health is None  # nothing denied, nothing failed

    # The wire shape: function tools with a JSON Schema, the assistant's tool
    # call echoed back with its arguments *string*, the tool result by id.
    first, second = transport.requests
    assert first["model"] == "local-model"
    assert {tool["type"] for tool in first["tools"]} == {"function"}
    assert {tool["function"]["name"] for tool in first["tools"]} == {
        "read_file",
        "list_files",
        "search_files",
    }
    assert first["tools"][0]["function"]["parameters"]["type"] == "object"
    assert first["messages"][0]["role"] == "system"
    assert first["messages"][-1] == {"role": "user", "content": "Read the notes and report."}
    assistant = second["messages"][-2]
    assert assistant["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "notes.txt"}'},
        }
    ]
    tool_message = second["messages"][-1]
    assert tool_message["role"] == "tool" and tool_message["tool_call_id"] == "call_1"
    assert "fixture evidence" in json.loads(tool_message["content"])["text"]

    kinds = [event for event, _ in events]
    assert kinds.count(EventTypes.AGENT_MESSAGE) == 1
    assert kinds.count(EventTypes.AGENT_MESSAGE_DELTA) == 2
    assert kinds.count(EventTypes.AGENT_USAGE) == 2
    start = next(data for event, data in events if event == EventTypes.AGENT_TOOL_START)
    assert (start["tool"], start["tool_call_id"], start["args"]) == (
        "read_file",
        "call_1",
        "notes.txt",
    )
    end = next(data for event, data in events if event == EventTypes.AGENT_TOOL_END)
    assert end["success"] is True and end["duration_ms"] is not None
    message = next(data for event, data in events if event == EventTypes.AGENT_MESSAGE)
    assert message == {
        "content": "The notes say: fixture evidence.",
        "model": "served-model",
        "backend": "openai",
    }
    assert transport.settings is not None
    assert transport.settings.api_key == KEY and transport.settings.timeout_s == 30.0
    assert transport.settings.max_retries == 0


def test_host_tool_calls_ride_the_response_file(endpoint_env: Path, tmp_path: Path) -> None:
    tools_dir = tmp_path / "host-tools"
    tools_dir.mkdir()
    response_path(tools_dir, "call_h1").write_text(
        HostToolResponse(call_id="call_h1", ok=True, text="42 open items").model_dump_json()
    )
    transport = FakeTransport(
        [
            tool_chunks([("call_h1", "count_items", '{"label": "bug"}')]),
            text_chunks("There are 42."),
        ]
    )
    job = make_job(
        tmp_path,
        host_tools=[HostToolSpec(name="count_items", description="Count items")],
        host_tools_dir=str(tools_dir),
        available_tools=[],
    )
    result, events = run(transport, job)
    assert result.output_text == "There are 42."
    assert [tool["function"]["name"] for tool in transport.requests[0]["tools"]] == ["count_items"]
    request = next(data for event, data in events if event == EventTypes.AGENT_TOOL_REQUEST)
    assert request == {"call_id": "call_h1", "name": "count_items", "arguments": {"label": "bug"}}
    assert transport.requests[1]["messages"][-1]["content"] == "42 open items"


def test_malformed_arguments_are_a_tool_failure_not_an_exception(
    endpoint_env: Path, tmp_path: Path
) -> None:
    transport = FakeTransport(
        [
            tool_chunks([("call_1", "read_file", '{"path": "notes.txt"')]),
            tool_chunks([("call_2", "read_file", '["notes.txt"]')]),
            text_chunks("I could not read it."),
        ]
    )
    result, events = run(transport, make_job(tmp_path))
    assert result.output_text == "I could not read it."
    ends = [data for event, data in events if event == EventTypes.AGENT_TOOL_END]
    assert [end["success"] for end in ends] == [False, False]
    assert "not valid JSON" in ends[0]["error"]
    assert "must be a JSON object" in ends[1]["error"]
    assert result.health is not None
    assert result.health.tool_failures == {"read_file": 2}
    assert "not valid JSON" in transport.requests[1]["messages"][-1]["content"]


@pytest.mark.parametrize("raw", [None, "", "{}", '{"a": 1}', {"a": 1}])
def test_parse_tool_arguments_accepts_an_object(raw: Any) -> None:
    assert isinstance(parse_tool_arguments(raw), dict)


def test_expect_json_reasks_once_then_fails(endpoint_env: Path, tmp_path: Path) -> None:
    transport = FakeTransport([text_chunks("Sure, here it is."), text_chunks('{"verified": true}')])
    result, _ = run(transport, make_job(tmp_path, expect="json"))
    assert result.output_json == {"verified": True}
    assert transport.requests[1]["messages"][-1] == {"role": "user", "content": JSON_REASK}

    transport = FakeTransport([text_chunks("Sure."), text_chunks("Still prose.")])
    result, _ = run(transport, make_job(tmp_path, expect="json"))
    assert result.output_json is None and result.output_text == "Still prose."
    assert len(transport.requests) == 2  # one reask, never a loop


# A workload plan the way a hosted model wrote one (field failure): a key that
# names a credential, whose empty value the text redactor turned into `***`.
PLAN = {
    "title": "Hacker News scraper",
    "tasks": [
        {
            "id": "t1",
            "verify_commands": ["python3 -m py_compile hn.py"],
            "needs": {"hosts": ["news.ycombinator.com"], "credentials": [], "sink": "artifact"},
        }
    ],
}


def plan_answer() -> str:
    plan = json.loads(json.dumps(PLAN))
    plan["tasks"][0]["note"] = f"the key {KEY} must not leak"
    return "```json\n" + json.dumps(plan, indent=2) + "\n```"


def assert_plan_parsed(result: Any) -> None:
    assert isinstance(result.output_json, dict), result.output_json
    needs = result.output_json["tasks"][0]["needs"]
    assert needs["credentials"] == [] and needs["hosts"] == ["news.ycombinator.com"]
    # Still redacted: as values in the parsed JSON, and in the display text.
    assert KEY not in json.dumps(result.output_json)
    assert KEY not in result.output_text


def test_expect_json_parses_before_redaction(endpoint_env: Path, tmp_path: Path) -> None:
    transport = FakeTransport([text_chunks(plan_answer())])
    result, events = run(transport, make_job(tmp_path, expect="json"))
    assert_plan_parsed(result)
    assert len(transport.requests) == 1  # parsed first time: no reask
    assert KEY not in json.dumps([data for _, data in events])


def test_tool_call_ceiling_nudges_in_session(endpoint_env: Path, tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            tool_chunks([("c1", "read_file", '{"path": "notes.txt"}')]),
            tool_chunks([("c2", "list_files", "{}")]),
            text_chunks("Stopping here."),
        ]
    )
    result, events = run(transport, make_job(tmp_path, max_tool_calls=1))
    assert result.output_text == "Stopping here."
    caps = [data for event, data in events if event == EventTypes.AGENT_TOOL_CAP]
    assert caps == [{"cap": 1, "calls": 2, "tool": "list_files"}]
    assert "Tool-call ceiling reached" in transport.requests[2]["messages"][-1]["content"]
    assert result.health is not None
    assert (result.health.tool_calls, result.health.tool_cap_denials) == (2, 1)
    assert result.health.tool_failures == {}


def test_unknown_and_read_only_refusals_are_denials(endpoint_env: Path, tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            tool_chunks([("c1", "write_file", '{"path": "x", "content": "y"}')]),
            text_chunks("Understood."),
        ]
    )
    result, events = run(transport, make_job(tmp_path))
    denied = next(data for event, data in events if event == EventTypes.AGENT_PERMISSION_DENIED)
    assert denied["kind"] == "write_file"
    assert result.health is not None
    assert result.health.permission_denials == {"write_file": 1}
    assert result.health.tool_failures == {}
    assert not (tmp_path / "workspace" / "x").exists()


def test_credential_never_reaches_an_event_or_the_model(endpoint_env: Path, tmp_path: Path) -> None:
    job = make_job(tmp_path)
    (tmp_path / "workspace" / "notes.txt").write_text(f"token here: {KEY}\n")
    transport = FakeTransport(
        [
            tool_chunks([("c1", "read_file", '{"path": "notes.txt"}')]),
            text_chunks(f"It said {KEY}."),
        ]
    )
    result, events = run(transport, job)
    assert KEY not in result.output_text
    assert KEY not in json.dumps([data for _, data in events])
    assert KEY not in json.dumps(transport.requests[1]["messages"][-1])
    assert "[REDACTED]" in result.output_text


def test_streaming_text_moves_the_status_line(endpoint_env: Path, tmp_path: Path) -> None:
    transport = FakeTransport([text_chunks("A long local generation.")])
    _, events = run(transport, make_job(tmp_path))
    deltas = [data["delta"] for event, data in events if event == EventTypes.AGENT_MESSAGE_DELTA]
    assert "".join(deltas) == "A long local generation."
    assert all(
        data["backend"] == "openai" for e, data in events if e == EventTypes.AGENT_MESSAGE_DELTA
    )


# -- the model ----------------------------------------------------------------


def test_auto_model_takes_the_endpoints_first_listed(endpoint_env: Path, tmp_path: Path) -> None:
    transport = FakeTransport(
        [text_chunks("hi", model="the-served-one")], models=["the-served-one"]
    )
    result, _ = run(transport, make_job(tmp_path, model="auto"))
    assert transport.requests[0]["model"] == "the-served-one"
    assert result.usage is not None and result.usage.model == "the-served-one"


def test_auto_model_without_a_listing_fails_naming_the_endpoint(
    endpoint_env: Path, tmp_path: Path
) -> None:
    transport = FakeTransport([], models=EndpointError("Not Found", status=404))
    result, _ = run(transport, make_job(tmp_path, model="auto"))
    assert result.failure is not None
    assert 'model = "auto"' in result.failure.reason
    assert "vllm:8000" in result.failure.reason
    assert transport.requests == []


# -- failures ----------------------------------------------------------------------


def test_connection_refusal_names_the_endpoint_not_the_key(
    endpoint_env: Path, tmp_path: Path
) -> None:
    transport = FakeTransport([EndpointError("Connection refused", connection=True)])
    result, _ = run(transport, make_job(tmp_path))
    assert result.failure is not None
    assert result.failure.category == "unavailable"
    assert "vllm:8000" in result.failure.reason
    assert KEY not in result.failure.reason
    assert result.failure.partial_progress is False


def test_404_names_the_model_and_the_base_url(endpoint_env: Path, tmp_path: Path) -> None:
    transport = FakeTransport([EndpointError("model not found", status=404)])
    result, _ = run(transport, make_job(tmp_path, model="missing-model"))
    assert result.failure is not None
    assert result.failure.http_status == 404
    assert "'missing-model'" in result.failure.reason
    assert "http://vllm:8000/v1" in result.failure.reason


def test_refused_credential_names_the_variable_never_the_value(
    endpoint_env: Path, tmp_path: Path
) -> None:
    transport = FakeTransport([EndpointError(f"bad key {KEY}", status=401)])
    result, _ = run(transport, make_job(tmp_path))
    assert result.failure is not None
    assert "VLLM_KEY" in result.failure.reason and KEY not in result.failure.reason


def test_throttle_carries_the_retry_hint(endpoint_env: Path, tmp_path: Path) -> None:
    transport = FakeTransport([EndpointError("slow down", status=429, retry_after_s=30)])
    before = time.time()
    result, _ = run(transport, make_job(tmp_path))
    assert result.failure is not None
    assert result.failure.category == "throttle"
    assert result.failure.retry_at is not None and result.failure.retry_at >= before + 29


def test_failure_after_a_tool_ran_is_partial_progress(endpoint_env: Path, tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            tool_chunks([("c1", "read_file", '{"path": "notes.txt"}')]),
            EndpointError("upstream broke", status=503),
        ]
    )
    result, _ = run(transport, make_job(tmp_path))
    assert result.failure is not None
    assert result.failure.category == "unavailable" and result.failure.partial_progress is True
    assert result.turns == 1


def test_deadline_ends_the_session_as_a_timeout(endpoint_env: Path, tmp_path: Path) -> None:
    class Slow(FakeTransport):
        def stream(self, request: dict[str, Any]) -> Iterator[dict[str, Any]]:
            time.sleep(0.05)
            yield from super().stream(request)

    transport = Slow([text_chunks("late")])
    with pytest.raises(subprocess.TimeoutExpired):
        run(transport, make_job(tmp_path, timeout_s=0.02))


# -- sessions ---------------------------------------------------------------------


def test_resume_continues_the_worker_held_transcript(endpoint_env: Path, tmp_path: Path) -> None:
    first = FakeTransport([text_chunks("First answer.")])
    result, _ = run(first, make_job(tmp_path))
    assert result.session_id is not None
    stored = json.loads(next(endpoint_env.glob("*.json")).read_text())
    assert [m["role"] for m in stored["messages"]] == ["system", "user", "assistant"]

    second = FakeTransport([text_chunks("Second answer.")])
    job = make_job(tmp_path, prompt="And then?", resume_session_id=result.session_id)
    resumed, _ = run(second, job)
    assert resumed.session_id == result.session_id
    roles = [m["role"] for m in second.requests[0]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert second.requests[0]["messages"][2]["content"] == "First answer."


def test_require_resume_fails_closed_without_a_transcript(
    endpoint_env: Path, tmp_path: Path
) -> None:
    transport = FakeTransport([text_chunks("fresh")])
    job = make_job(tmp_path, resume_session_id="openai-v1:deadbeef", require_resume=True)
    result, _ = run(transport, job)
    assert result.failure is not None
    assert result.failure.category == "recovery"
    assert "transcript is missing" in result.failure.reason
    assert transport.requests == []  # no fresh session replayed anything


def test_a_transcript_made_under_other_tools_starts_fresh_or_fails_closed(
    endpoint_env: Path, tmp_path: Path
) -> None:
    result, _ = run(FakeTransport([text_chunks("one")]), make_job(tmp_path))
    changed = make_job(tmp_path, permission_mode="auto", resume_session_id=result.session_id)
    fresh = FakeTransport([text_chunks("two")])
    resumed, _ = run(fresh, changed)
    assert resumed.session_id != result.session_id
    assert [m["role"] for m in fresh.requests[0]["messages"]] == ["system", "user"]
    strict = FakeTransport([text_chunks("three")])
    held, _ = run(strict, changed.model_copy(update={"require_resume": True}))
    assert held.failure is not None and held.failure.category == "recovery"
    assert "other tools" in held.failure.reason


def test_system_prompt_framing_follows_the_preset_flag(endpoint_env: Path, tmp_path: Path) -> None:
    transport = FakeTransport([text_chunks("ok")])
    run(transport, make_job(tmp_path, system_message="Project rules."))
    system = transport.requests[0]["messages"][0]["content"]
    assert system.startswith(CODING_AGENT_PRESET) and system.endswith("Project rules.")

    transport = FakeTransport([text_chunks("ok")])
    run(transport, make_job(tmp_path, system_message="You are an operator.", system_preset=False))
    assert transport.requests[0]["messages"][0]["content"] == "You are an operator."


# -- refusals and availability -----------------------------------------------------


def test_native_mcp_is_refused_by_name(endpoint_env: Path, tmp_path: Path) -> None:
    job = make_job(
        tmp_path, mcp_servers=[{"name": "docs", "transport": "http", "url": "https://x.example"}]
    )
    with pytest.raises(BackendUnavailableError, match="openai backend does not support native MCP"):
        run(FakeTransport([]), job)


def test_endpoint_settings_fail_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (OPENAI_BASE_URL_ENV, OPENAI_KEY_NAME_ENV, "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(BackendUnavailableError, match=f"{OPENAI_BASE_URL_ENV} is not set"):
        endpoint_settings()
    monkeypatch.setenv(OPENAI_BASE_URL_ENV, "http://vllm:8000/v1")
    with pytest.raises(BackendUnavailableError, match=r"OPENAI_API_KEY is not set.*vllm:8000"):
        endpoint_settings()
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder")
    settings = endpoint_settings()
    assert settings.key_env == "OPENAI_API_KEY" and settings.authority == "vllm:8000"
    assert (settings.timeout_s, settings.max_retries) == (600.0, 2)


def test_ensure_available_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "openai", None)
    with pytest.raises(BackendUnavailableError, match=r"sbxloop-worker\[openai\]"):
        OpenAIBackend().ensure_available()


def test_registry_resolves_the_backend_and_limits_are_unsupported() -> None:
    backend = get_backend("openai")
    assert isinstance(backend, OpenAIBackend) and backend.name == "openai"
    report = backend.rate_limits(timeout_s=1.0)
    assert report.status == "unsupported" and report.backend == "openai"
    assert openai_backend.BACKEND_NAME == "openai"


# -- the responses API -------------------------------------------------------------

REASONING = {
    "id": "rs_1",
    "type": "reasoning",
    "summary": [],
    "content": None,
    "status": None,
    "encrypted_content": "gAAAAB-opaque-reasoning-state==",
}


def responses_text(
    text: str,
    *,
    usage: dict[str, Any] | None = None,
    model: str = "served-model",
    reasoning: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """A text answer the way /v1/responses streams one: deltas, then the
    completed items, then the terminal response carrying the usage."""
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    output = [*([reasoning] if reasoning else []), message]
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": {"model": model, "status": "in_progress"}}
    ]
    for piece in (text[: len(text) // 2], text[len(text) // 2 :]):
        if piece:
            events.append(
                {
                    "type": "response.output_text.delta",
                    "output_index": len(output) - 1,
                    "delta": piece,
                }
            )
    for index, item in enumerate(output):
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    events.append(
        {
            "type": "response.completed",
            "response": {
                "model": model,
                "status": "completed",
                "output": output,
                "usage": usage or {"input_tokens": 10, "output_tokens": 4},
            },
        }
    )
    return events


def responses_calls(
    calls: list[tuple[str, str, str]],
    *,
    reasoning: dict[str, Any] | None = None,
    model: str = "served-model",
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = [*([reasoning] if reasoning else [])]
    output += [
        {
            "id": f"fc_{call_id}",
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
            "status": "completed",
        }
        for call_id, name, arguments in calls
    ]
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": {"model": model, "status": "in_progress"}}
    ]
    for index, item in enumerate(output):
        if item["type"] == "function_call":
            for piece in (item["arguments"][:3], item["arguments"][3:]):
                events.append(
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": index,
                        "delta": piece,
                    }
                )
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    events.append(
        {
            "type": "response.completed",
            "response": {
                "model": model,
                "status": "completed",
                "output": output,
                "usage": {
                    "input_tokens": 20,
                    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                    "output_tokens": 8,
                    "output_tokens_details": {"reasoning_tokens": 6},
                },
            },
        }
    )
    return events


def without_none(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if value is not None}


@pytest.fixture
def responses_env(endpoint_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(OPENAI_API_ENV, "responses")
    return endpoint_env


def test_responses_text_answer_and_usage(responses_env: Path, tmp_path: Path) -> None:
    usage = {
        "input_tokens": 50,
        "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 5},
        "output_tokens": 30,
        "output_tokens_details": {"reasoning_tokens": 12},
    }
    transport = FakeTransport([responses_text("The notes are fine.", usage=usage)])
    result, events = run(transport, make_job(tmp_path, model="gpt-test"))

    assert result.output_text == "The notes are fine." and result.failure is None
    assert result.turns == 1
    assert result.usage is not None
    assert (
        result.usage.input_tokens,
        result.usage.output_tokens,
        result.usage.cache_read_tokens,
        result.usage.cache_write_tokens,
    ) == (50, 30, 20, 5)
    assert result.usage.model == "served-model" and result.usage.backend == "openai"

    (request,) = transport.requests
    assert request["model"] == "gpt-test"
    assert request["input"] == [{"role": "user", "content": "Read the notes and report."}]
    assert request["instructions"] == CODING_AGENT_PRESET
    assert request["store"] is False
    assert request["include"] == ["reasoning.encrypted_content"]
    assert "messages" not in request and "reasoning" not in request
    assert {tool["name"] for tool in request["tools"]} == {
        "read_file",
        "list_files",
        "search_files",
    }
    tool = request["tools"][0]
    assert tool["type"] == "function" and tool["strict"] is False
    assert tool["parameters"]["type"] == "object" and "function" not in tool

    kinds = [event for event, _ in events]
    assert kinds.count(EventTypes.AGENT_MESSAGE) == 1
    assert kinds.count(EventTypes.AGENT_MESSAGE_DELTA) == 2
    assert kinds.count(EventTypes.AGENT_USAGE) == 1


def test_responses_function_call_round_trip(responses_env: Path, tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            responses_calls([("call_1", "read_file", '{"path": "notes.txt"}')]),
            responses_text("The notes say: fixture evidence."),
        ]
    )
    result, events = run(transport, make_job(tmp_path))

    assert result.output_text == "The notes say: fixture evidence."
    assert result.turns == 2
    assert result.usage is not None
    assert (result.usage.input_tokens, result.usage.output_tokens) == (30, 12)
    _, second = transport.requests
    user, call, output = second["input"]
    assert user == {"role": "user", "content": "Read the notes and report."}
    assert call == {
        "id": "fc_call_1",
        "type": "function_call",
        "call_id": "call_1",
        "name": "read_file",
        "arguments": '{"path": "notes.txt"}',
        "status": "completed",
    }
    assert output["type"] == "function_call_output" and output["call_id"] == "call_1"
    assert "fixture evidence" in json.loads(output["output"])["text"]
    start = next(data for event, data in events if event == EventTypes.AGENT_TOOL_START)
    assert (start["tool"], start["tool_call_id"], start["args"]) == (
        "read_file",
        "call_1",
        "notes.txt",
    )
    end = next(data for event, data in events if event == EventTypes.AGENT_TOOL_END)
    assert end["success"] is True


def test_responses_reasoning_items_are_replayed_verbatim(
    responses_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(OPENAI_REASONING_EFFORT_ENV, "high")
    transport = FakeTransport(
        [
            responses_calls([("call_1", "list_files", "{}")], reasoning=REASONING),
            responses_text("Done.", reasoning={**REASONING, "id": "rs_2"}),
        ]
    )
    result, _ = run(transport, make_job(tmp_path))
    assert result.output_text == "Done."
    first, second = transport.requests
    assert first["reasoning"] == {"effort": "high"}
    reasoning = second["input"][1]
    assert reasoning == without_none(REASONING)
    assert reasoning["encrypted_content"] == "gAAAAB-opaque-reasoning-state=="
    assert [item.get("type") for item in second["input"]] == [
        None,
        "reasoning",
        "function_call",
        "function_call_output",
    ]


def test_responses_tool_call_ceiling_nudges_in_session(responses_env: Path, tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            responses_calls([("c1", "read_file", '{"path": "notes.txt"}')]),
            responses_calls([("c2", "list_files", "{}")]),
            responses_text("Stopping here."),
        ]
    )
    result, events = run(transport, make_job(tmp_path, max_tool_calls=1))
    assert result.output_text == "Stopping here."
    caps = [data for event, data in events if event == EventTypes.AGENT_TOOL_CAP]
    assert caps == [{"cap": 1, "calls": 2, "tool": "list_files"}]
    last = transport.requests[2]["input"][-1]
    assert last["type"] == "function_call_output" and last["call_id"] == "c2"
    assert "Tool-call ceiling reached" in last["output"]
    assert result.health is not None
    assert (result.health.tool_calls, result.health.tool_cap_denials) == (2, 1)


def test_responses_resume_round_trips_the_transcript(responses_env: Path, tmp_path: Path) -> None:
    first = FakeTransport(
        [
            responses_calls([("c1", "list_files", "{}")], reasoning=REASONING),
            responses_text("First answer."),
        ]
    )
    result, _ = run(first, make_job(tmp_path))
    stored = json.loads(next(responses_env.glob("*.json")).read_text())
    assert stored["api"] == "responses"
    assert stored["messages"][1] == without_none(REASONING)

    second = FakeTransport([responses_text("Second answer.")])
    job = make_job(tmp_path, prompt="And then?", resume_session_id=result.session_id)
    resumed, _ = run(second, job)
    assert resumed.session_id == result.session_id and resumed.failure is None
    replayed = second.requests[0]["input"]
    assert replayed[:-1] == stored["messages"]
    assert replayed[-1] == {"role": "user", "content": "And then?"}
    assert second.requests[0]["instructions"] == CODING_AGENT_PRESET


def test_a_transcript_from_the_other_api_is_never_replayed(
    endpoint_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chat, _ = run(FakeTransport([text_chunks("chat answer")]), make_job(tmp_path))
    monkeypatch.setenv(OPENAI_API_ENV, "responses")
    job = make_job(tmp_path, resume_session_id=chat.session_id)

    strict = FakeTransport([responses_text("never")])
    held, _ = run(strict, job.model_copy(update={"require_resume": True}))
    assert held.failure is not None and held.failure.category == "recovery"
    assert "made under the chat API" in held.failure.reason
    assert "speaks responses" in held.failure.reason
    assert strict.requests == []

    fresh = FakeTransport([responses_text("fresh")])
    started, _ = run(fresh, job)
    assert started.session_id != chat.session_id
    assert fresh.requests[0]["input"] == [{"role": "user", "content": "Read the notes and report."}]

    monkeypatch.setenv(OPENAI_API_ENV, "chat")
    back = make_job(tmp_path, resume_session_id=started.session_id, require_resume=True)
    refused, _ = run(FakeTransport([text_chunks("never")]), back)
    assert refused.failure is not None
    assert "made under the responses API" in refused.failure.reason


def test_responses_credential_never_reaches_an_event_the_model_or_the_transcript(
    responses_env: Path, tmp_path: Path
) -> None:
    job = make_job(tmp_path)
    (tmp_path / "workspace" / "notes.txt").write_text(f"token here: {KEY}\n")
    transport = FakeTransport(
        [
            responses_calls([("c1", "read_file", '{"path": "notes.txt"}')]),
            responses_text(f"It said {KEY}."),
        ]
    )
    result, events = run(transport, job)
    assert KEY not in result.output_text and "[REDACTED]" in result.output_text
    assert KEY not in json.dumps([data for _, data in events])
    assert KEY not in json.dumps(transport.requests[1]["input"])
    assert KEY not in next(responses_env.glob("*.json")).read_text()


def test_responses_expect_json_parses_before_redaction(responses_env: Path, tmp_path: Path) -> None:
    transport = FakeTransport([responses_text(plan_answer())])
    result, events = run(transport, make_job(tmp_path, expect="json"))
    assert_plan_parsed(result)
    assert len(transport.requests) == 1
    assert KEY not in json.dumps([data for _, data in events])
    assert KEY not in next(responses_env.glob("*.json")).read_text()


@pytest.mark.parametrize(
    ("event", "category"),
    [
        (
            {
                "type": "response.failed",
                "response": {"error": {"code": "server_error", "message": "upstream broke"}},
            },
            "unavailable",
        ),
        ({"type": "error", "code": "rate_limit_exceeded", "message": "slow down"}, "throttle"),
        ({"type": "error", "code": "mystery", "message": "who knows"}, "unknown"),
    ],
)
def test_responses_stream_errors_are_classified_like_http(
    responses_env: Path, tmp_path: Path, event: dict[str, Any], category: str
) -> None:
    transport = FakeTransport([[{"type": "response.created", "response": {}}, event]])
    result, _ = run(transport, make_job(tmp_path))
    assert result.failure is not None and result.failure.category == category


def test_responses_404_names_the_chat_api_as_the_way_out(
    responses_env: Path, tmp_path: Path
) -> None:
    result, _ = run(FakeTransport([EndpointError("not found", status=404)]), make_job(tmp_path))
    assert result.failure is not None and result.failure.http_status == 404
    assert 'api = "chat"' in result.failure.reason


def test_chat_sends_reasoning_effort_only_when_configured(
    endpoint_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unset = FakeTransport([text_chunks("ok")])
    run(unset, make_job(tmp_path))
    assert "reasoning_effort" not in unset.requests[0]
    monkeypatch.setenv(OPENAI_REASONING_EFFORT_ENV, "minimal")
    configured = FakeTransport([text_chunks("ok")])
    run(configured, make_job(tmp_path))
    assert configured.requests[0]["reasoning_effort"] == "minimal"
    assert "reasoning" not in configured.requests[0]


def test_endpoint_settings_settle_the_api_and_check_the_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(OPENAI_KEY_NAME_ENV, raising=False)
    monkeypatch.delenv(OPENAI_REASONING_EFFORT_ENV, raising=False)
    monkeypatch.delenv(OPENAI_API_ENV, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder")
    monkeypatch.setenv(OPENAI_BASE_URL_ENV, "https://api.openai.com/v1")
    assert endpoint_settings().api == "responses"
    monkeypatch.setenv(OPENAI_API_ENV, "chat")
    assert endpoint_settings().api == "chat"
    monkeypatch.setenv(OPENAI_BASE_URL_ENV, "http://vllm:8000/v1")
    monkeypatch.setenv(OPENAI_API_ENV, "auto")
    assert endpoint_settings().api == "chat"
    assert endpoint_settings().reasoning_effort is None
    monkeypatch.setenv(OPENAI_API_ENV, "completions")
    with pytest.raises(BackendUnavailableError, match=OPENAI_API_ENV):
        endpoint_settings()
    monkeypatch.setenv(OPENAI_API_ENV, "responses")
    monkeypatch.setenv(OPENAI_REASONING_EFFORT_ENV, "extreme")
    with pytest.raises(BackendUnavailableError, match=OPENAI_REASONING_EFFORT_ENV):
        endpoint_settings()
    monkeypatch.setenv(OPENAI_REASONING_EFFORT_ENV, "xhigh")
    assert endpoint_settings().reasoning_effort == "xhigh"


# -- a request the endpoint rejects is not a provider outage ------------------------

FIELD_400 = (
    "Error code: 400 - {'error': {'message': \"Function tools with reasoning_effort are not "
    "supported for gpt-5.6-sol in /v1/chat/completions. To use function tools, use "
    "/v1/responses or set reasoning_effort to 'none'.\", 'type': 'invalid_request_error', "
    "'param': 'reasoning_effort'}}"
)


@pytest.mark.parametrize("status", [400, 422])
def test_a_rejected_request_fails_the_job_instead_of_holding_the_provider(
    endpoint_env: Path, tmp_path: Path, status: int
) -> None:
    transport = FakeTransport([EndpointError(f"{FIELD_400} {KEY}", status=status)])
    with pytest.raises(EndpointRequestRejected) as raised:
        run(transport, make_job(tmp_path, model="gpt-5.6-sol"))
    message = str(raised.value)
    assert raised.value.http_status == status
    assert "vllm:8000" in message and "'gpt-5.6-sol'" in message
    assert "/v1/chat/completions request" in message
    assert "not a provider outage" in message
    assert 'set [agent.openai] api = "responses"' in message
    assert KEY not in message


def test_the_field_400_reaches_the_host_as_a_job_error_without_a_provider_failure(
    endpoint_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The worker's result for the field failure: an error the phase fails
    on (``error.provider`` unset), never the provider hold that parked the
    run with "reset unknown"; genuine outages keep their provider failure."""
    from sbxloop_worker.runner import JobRunner

    def result_for(reply: Exception) -> Any:
        transport = FakeTransport([reply])
        monkeypatch.setattr(OpenAIBackend, "_transport", lambda self, settings: transport)
        return JobRunner(
            make_job(tmp_path, model="gpt-5.6-sol"),
            events_path=tmp_path / "events.jsonl",
            result_path=tmp_path / "result.json",
            heartbeat_s=0,
            backend_name="openai",
        ).run()

    rejected = result_for(EndpointError(FIELD_400, status=400))
    assert rejected.status == "error" and rejected.error is not None
    assert rejected.error.type == "EndpointRequestRejected"
    assert rejected.error.provider is None
    assert rejected.error.http_status == 400
    assert 'api = "responses"' in rejected.error.message

    for reply, category in (
        (EndpointError("slow down", status=429, retry_after_s=5), "throttle"),
        (EndpointError("upstream broke", status=503), "unavailable"),
    ):
        held = result_for(reply)
        assert held.error is not None and held.error.provider is not None
        assert held.error.provider.category == category
