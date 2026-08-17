"""Worker-side host-tool round trip: emit the request, wait for the file."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from sbxloop_worker.hosttools import (
    HostToolTimeout,
    request_tool,
    response_path,
    safe_call_id,
)
from sbxloop_worker.protocol import Event, EventTypes, HostToolCall, HostToolResponse


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, type: str, **data: Any) -> Event:
        event = Event.now(type, "r1", "j1", **data)
        self.events.append(event)
        return event

    def types(self) -> list[str]:
        return [e.type for e in self.events]


def call(**overrides: Any) -> HostToolCall:
    base: dict[str, Any] = {"call_id": "c1", "name": "list_runs", "arguments": {"limit": 2}}
    base.update(overrides)
    return HostToolCall.model_validate(base)


def test_waits_until_response_file_appears(tmp_path: Path) -> None:
    emit = Recorder()
    tools_dir = tmp_path / "tools" / "j1"  # does not exist yet: request_tool creates it

    def answer() -> None:
        time.sleep(0.15)
        response_path(tools_dir, "c1").write_text(
            HostToolResponse(call_id="c1", ok=True, text="two runs").model_dump_json()
        )

    threading.Thread(target=answer).start()
    response = request_tool(emit, tools_dir, call(), timeout_s=5.0, poll_s=0.02)
    assert response.ok and response.text == "two runs"
    assert emit.types() == [EventTypes.AGENT_TOOL_REQUEST, EventTypes.AGENT_TOOL_RESPONSE]
    request = emit.events[0]
    assert HostToolCall.model_validate(request.data) == call()
    done = emit.events[1].data
    assert done["ok"] is True and done["name"] == "list_runs" and done["elapsed_s"] >= 0


def test_partial_json_is_retried_until_complete(tmp_path: Path) -> None:
    emit = Recorder()
    path = response_path(tmp_path, "c1")
    full = HostToolResponse(call_id="c1", ok=True, text="late but whole").model_dump_json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(full[: len(full) // 2])  # sbx cp mid-flight

    def finish() -> None:
        time.sleep(0.1)
        path.write_text(full)

    threading.Thread(target=finish).start()
    response = request_tool(emit, tmp_path, call(), timeout_s=5.0, poll_s=0.02)
    assert response.text == "late but whole"


def test_timeout_raises_and_emits_failed_response(tmp_path: Path) -> None:
    emit = Recorder()
    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleep(s: float) -> None:
        now[0] += s

    with pytest.raises(HostToolTimeout, match="list_runs"):
        request_tool(emit, tmp_path, call(), timeout_s=1.0, poll_s=0.25, sleep=sleep, clock=clock)
    assert emit.types() == [EventTypes.AGENT_TOOL_REQUEST, EventTypes.AGENT_TOOL_RESPONSE]
    assert emit.events[1].data["ok"] is False and emit.events[1].data["error"] == "timeout"


def test_error_response_is_returned_not_raised(tmp_path: Path) -> None:
    emit = Recorder()
    response_path(tmp_path, "c1").parent.mkdir(parents=True, exist_ok=True)
    response_path(tmp_path, "c1").write_text(
        json.dumps({"v": 1, "call_id": "c1", "ok": False, "text": "", "error": "no such run"})
    )
    response = request_tool(emit, tmp_path, call(), timeout_s=1.0, poll_s=0.01)
    assert not response.ok and response.error == "no such run"
    assert emit.events[1].data["error"] == "no such run"


def test_unsafe_call_id_is_replaced() -> None:
    assert safe_call_id("call_1:abc.def-9") == "call_1:abc.def-9"
    assert safe_call_id("../escape") != "../escape"
    assert safe_call_id(None) and safe_call_id("") and safe_call_id("a b") != "a b"
