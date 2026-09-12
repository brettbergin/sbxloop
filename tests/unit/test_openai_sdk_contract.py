"""The published ``openai`` client against a loopback server speaking the
chat-completions wire shape: the request the SDK really sends (tools,
streaming, the credential as a bearer header), the SSE the backend really
parses, and the errors it reduces. No model, no network beyond loopback.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from sbxloop_worker.backends.openai import SESSION_DIR_ENV, OpenAIBackend
from sbxloop_worker.protocol import (
    OPENAI_BASE_URL_ENV,
    OPENAI_KEY_NAME_ENV,
    OPENAI_RETRIES_ENV,
    OPENAI_TIMEOUT_ENV,
    EventTypes,
    JobRequest,
)

pytestmark = pytest.mark.slow
KEY = "sk-contract-test-credential"


class Stub:
    """One scripted endpoint: the requests it saw, and what it answers."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.models_status = 200
        self.reject_stream_options = False
        self.replies: list[list[dict[str, Any]]] = []

    def next_reply(self) -> list[dict[str, Any]]:
        return self.replies.pop(0)


def sse(chunks: list[dict[str, Any]]) -> bytes:
    body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
    return body + b"data: [DONE]\n\n"


def make_handler(stub: Stub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            stub.headers.append({k.lower(): v for k, v in self.headers.items()})
            if self.path != "/v1/models":
                self._send(404, b'{"error": {"message": "no such route"}}', "application/json")
                return
            if stub.models_status != 200:
                self._send(
                    stub.models_status, b'{"error": {"message": "nope"}}', "application/json"
                )
                return
            body = json.dumps({"object": "list", "data": [{"id": "stub-model", "object": "model"}]})
            self._send(200, body.encode(), "application/json")

        def do_POST(self) -> None:
            stub.headers.append({k.lower(): v for k, v in self.headers.items()})
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length))
            stub.requests.append(request)
            if self.path != "/v1/chat/completions":
                self._send(404, b'{"error": {"message": "no such route"}}', "application/json")
                return
            if stub.reject_stream_options and "stream_options" in request:
                body = b'{"error": {"message": "stream_options is not supported", "type": "x"}}'
                self._send(400, body, "application/json")
                return
            if not stub.replies:
                self._send(503, b'{"error": {"message": "scripted outage"}}', "application/json")
                return
            self._send(200, sse(stub.next_reply()), "text/event-stream")

    return Handler


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Stub, str]]:
    pytest.importorskip("openai")
    state = Stub()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/v1"
    monkeypatch.setenv(OPENAI_BASE_URL_ENV, base_url)
    monkeypatch.setenv(OPENAI_KEY_NAME_ENV, "STUB_KEY")
    monkeypatch.setenv("STUB_KEY", KEY)
    monkeypatch.setenv(OPENAI_TIMEOUT_ENV, "10")
    monkeypatch.setenv(OPENAI_RETRIES_ENV, "0")
    monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path / "sessions"))
    try:
        yield state, base_url
    finally:
        server.shutdown()
        server.server_close()


def make_job(tmp_path: Path, **overrides: Any) -> JobRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "input.txt").write_text("fixture evidence\n")
    return JobRequest(
        **{
            "job_id": "sdk-contract",
            "run_id": "run-sdk-contract",
            "kind": "agent.session",
            "prompt": "Read the fixture and report JSON.",
            "cwd": str(workspace),
            "permission_mode": "read_only",
            "expect": "json",
            "timeout_s": 10.0,
            "model": "stub-model",
            **overrides,
        }
    )


def tool_reply(call_id: str, name: str, arguments: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "model": "stub-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {"name": name, "arguments": arguments},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "model": "stub-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 15, "completion_tokens": 6},
        },
    ]


def text_reply(text: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "c2",
            "object": "chat.completion.chunk",
            "model": "stub-model",
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        },
        {
            "id": "c2",
            "object": "chat.completion.chunk",
            "model": "stub-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 30,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 4},
            },
        },
    ]


def test_real_sdk_streams_tools_and_usage_over_the_wire(
    tmp_path: Path, stub: tuple[Stub, str]
) -> None:
    state, _ = stub
    state.replies = [
        tool_reply("call_read", "read_file", '{"path": "input.txt"}'),
        text_reply('{"verified": true}'),
    ]
    events: list[tuple[str, dict[str, Any]]] = []
    result = OpenAIBackend().run_session(
        make_job(tmp_path), lambda event, **data: events.append((event, data))
    )
    assert result.output_json == {"verified": True}
    assert result.failure is None
    assert result.usage is not None
    assert (result.usage.input_tokens, result.usage.output_tokens) == (45, 11)
    assert result.usage.cache_read_tokens == 4
    assert result.usage.model == "stub-model"
    assert [e for e, _ in events].count(EventTypes.AGENT_TOOL_END) == 1
    assert any(e == EventTypes.AGENT_MESSAGE_DELTA for e, _ in events)

    first, second = state.requests
    assert first["model"] == "stub-model" and first["stream"] is True
    assert first["stream_options"] == {"include_usage": True}
    assert {tool["function"]["name"] for tool in first["tools"]} == {
        "read_file",
        "list_files",
        "search_files",
    }
    assert second["messages"][-2]["tool_calls"][0]["function"]["arguments"] == (
        '{"path": "input.txt"}'
    )
    assert "fixture evidence" in second["messages"][-1]["content"]
    assert all(h.get("authorization") == f"Bearer {KEY}" for h in state.headers)
    assert KEY not in json.dumps([data for _, data in events])


def test_real_sdk_falls_back_when_stream_options_is_refused(
    tmp_path: Path, stub: tuple[Stub, str]
) -> None:
    state, _ = stub
    state.reject_stream_options = True
    state.replies = [text_reply('{"ok": 1}')]
    result = OpenAIBackend().run_session(make_job(tmp_path), lambda event, **data: None)
    assert result.output_json == {"ok": 1}
    assert "stream_options" in state.requests[0]
    assert "stream_options" not in state.requests[1]


def test_real_sdk_auto_model_uses_the_listing(tmp_path: Path, stub: tuple[Stub, str]) -> None:
    state, _ = stub
    state.replies = [text_reply('{"ok": 1}')]
    result = OpenAIBackend().run_session(
        make_job(tmp_path, model="auto"), lambda event, **data: None
    )
    assert result.failure is None
    assert state.requests[0]["model"] == "stub-model"


def test_real_sdk_reduces_a_refused_connection_to_a_named_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub: tuple[Stub, str]
) -> None:
    pytest.importorskip("openai")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    monkeypatch.setenv(OPENAI_BASE_URL_ENV, f"http://127.0.0.1:{closed_port}/v1")
    result = OpenAIBackend().run_session(make_job(tmp_path), lambda event, **data: None)
    assert result.failure is not None
    assert result.failure.category == "unavailable"
    assert f"127.0.0.1:{closed_port}" in result.failure.reason
    assert KEY not in result.failure.reason


def test_real_sdk_reduces_a_server_error_to_a_named_failure(
    tmp_path: Path, stub: tuple[Stub, str]
) -> None:
    state, _ = stub
    state.replies = []  # every completion answers 503
    result = OpenAIBackend().run_session(make_job(tmp_path), lambda event, **data: None)
    assert result.failure is not None
    assert result.failure.category == "unavailable" and result.failure.http_status == 503
    assert "'stub-model'" in result.failure.reason
