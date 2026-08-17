"""Host tools in the Copilot backend, with ``copilot.tools`` stubbed in.

The SDK's custom-tool API (``Tool``, ``ToolResult``, ``ToolInvocation``) was
desk-verified against github-copilot-sdk 1.0.8; here the wiring is
exercised without the SDK: the backend must register one ``Tool`` per
``HostToolSpec`` with ``skip_permission`` (the read-only allowlist would
otherwise turn every custom-tool call away), append host tool names to the
``available_tools`` allowlist, and map the host's response file onto the
SDK's result vocabulary (success / failure / timeout).
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sbxloop_worker.backends import copilot as copilot_backend
from sbxloop_worker.backends.copilot import CopilotBackend, _arguments_dict
from sbxloop_worker.hosttools import HostToolTimeout
from sbxloop_worker.protocol import Event, HostToolResponse, HostToolSpec, JobRequest


@dataclass
class _StubTool:
    name: str
    description: str
    handler: Any = None
    parameters: dict[str, Any] | None = None
    skip_permission: bool = False


@dataclass
class _StubToolResult:
    text_result_for_llm: str = ""
    result_type: str = "success"
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
def stub_copilot_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = types.ModuleType("copilot.tools")
    tools.Tool = _StubTool  # type: ignore[attr-defined]
    tools.ToolResult = _StubToolResult  # type: ignore[attr-defined]
    package = types.ModuleType("copilot")
    package.tools = tools  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "copilot", package)
    monkeypatch.setitem(sys.modules, "copilot.tools", tools)


def _job(**overrides: Any) -> JobRequest:
    base: dict[str, Any] = {
        "job_id": "j1",
        "run_id": "r1",
        "kind": "agent.session",
        "prompt": "hi",
        "host_tools": [
            HostToolSpec(
                name="sbx_control",
                description="run a daemon verb",
                parameters={"type": "object", "properties": {"command": {"type": "string"}}},
            ),
            HostToolSpec(name="list_runs", description="recent runs"),
        ],
        "host_tools_dir": "/home/agent/.sbxloop/tools/j1",
    }
    base.update(overrides)
    return JobRequest.model_validate(base)


def _emit(type: str, **data: Any) -> Event:
    return Event.now(type, "r1", "j1", **data)


class TestToolKwargs:
    def test_host_tools_registered_with_skip_permission(self, stub_copilot_tools: None) -> None:
        kwargs = CopilotBackend()._tool_kwargs(_job(), _emit)
        tools = kwargs["tools"]
        assert [t.name for t in tools] == ["sbx_control", "list_runs"]
        assert all(isinstance(t, _StubTool) and t.skip_permission for t in tools)
        assert tools[0].parameters == {
            "type": "object",
            "properties": {"command": {"type": "string"}},
        }
        assert tools[0].description == "run a daemon verb"
        assert "available_tools" not in kwargs

    def test_available_tools_appends_host_tool_names(self, stub_copilot_tools: None) -> None:
        kwargs = CopilotBackend()._tool_kwargs(_job(available_tools=[]), _emit)
        assert kwargs["available_tools"] == ["sbx_control", "list_runs"]
        kwargs = CopilotBackend()._tool_kwargs(_job(available_tools=["bash"]), _emit)
        assert kwargs["available_tools"] == ["bash", "sbx_control", "list_runs"]

    def test_no_host_tools_means_no_tool_kwargs(self, stub_copilot_tools: None) -> None:
        plain = JobRequest(job_id="j1", run_id="r1", kind="agent.session", prompt="hi")
        assert CopilotBackend()._tool_kwargs(plain, _emit) == {}

    def test_host_tools_without_emitter_fail_loudly(self, stub_copilot_tools: None) -> None:
        with pytest.raises(RuntimeError, match="host_tools need"):
            CopilotBackend()._tool_kwargs(_job(), None)


class TestHandler:
    """The registered handler relays through ``request_tool`` and maps the
    response onto the SDK's ToolResult vocabulary."""

    def _handler(self, monkeypatch: pytest.MonkeyPatch, outcome: Any) -> tuple[Any, list[Any]]:
        calls: list[Any] = []

        def fake_request_tool(emit: Any, tools_dir: Any, call: Any, timeout_s: float, **_: Any):
            calls.append((Path(str(tools_dir)), call, timeout_s))
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        monkeypatch.setattr(copilot_backend, "request_tool", fake_request_tool)
        job = _job(host_tool_timeout_s=7.5)
        tool = CopilotBackend()._host_tool(job.host_tools[0], job, _emit)
        return tool.handler, calls

    def test_success_maps_to_text(
        self, stub_copilot_tools: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler, calls = self._handler(
            monkeypatch, HostToolResponse(call_id="c1", ok=True, text="paused=false")
        )
        invocation = SimpleNamespace(tool_call_id="c1", arguments={"command": "status"})
        result = asyncio.run(handler(invocation))
        assert isinstance(result, _StubToolResult)
        assert result.text_result_for_llm == "paused=false" and result.result_type == "success"
        (tools_dir, call, timeout_s), *_ = calls
        assert tools_dir == Path("/home/agent/.sbxloop/tools/j1")
        assert call.call_id == "c1" and call.name == "sbx_control"
        assert call.arguments == {"command": "status"} and timeout_s == 7.5

    def test_failure_maps_to_failure_result(
        self, stub_copilot_tools: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler, _ = self._handler(
            monkeypatch, HostToolResponse(call_id="c1", ok=False, error="unknown verb")
        )
        result = asyncio.run(handler(SimpleNamespace(tool_call_id="c1", arguments={})))
        assert result.result_type == "failure"
        assert result.error == "unknown verb" and "unknown verb" in result.text_result_for_llm

    def test_timeout_maps_to_timeout_result(
        self, stub_copilot_tools: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler, _ = self._handler(
            monkeypatch, HostToolTimeout("host tool 'sbx_control' timed out")
        )
        result = asyncio.run(handler(SimpleNamespace(tool_call_id="c1", arguments={})))
        assert result.result_type == "timeout" and "timed out" in result.text_result_for_llm

    def test_unsafe_or_missing_call_id_is_replaced(
        self, stub_copilot_tools: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler, calls = self._handler(monkeypatch, HostToolResponse(call_id="x", ok=True))
        asyncio.run(handler(SimpleNamespace(tool_call_id="../etc", arguments=None)))
        asyncio.run(handler(SimpleNamespace()))
        assert all(c[1].call_id and "/" not in c[1].call_id for c in calls)
        assert calls[0][1].arguments == {} and calls[1][1].arguments == {}


class TestArgumentsDict:
    def test_shapes(self) -> None:
        assert _arguments_dict({"a": 1}) == {"a": 1}
        assert _arguments_dict('{"a": 1}') == {"a": 1}
        assert _arguments_dict("[1, 2]") == {"input": [1, 2]}
        assert _arguments_dict("not json") == {"input": "not json"}
        assert _arguments_dict(None) == {} and _arguments_dict("  ") == {}
