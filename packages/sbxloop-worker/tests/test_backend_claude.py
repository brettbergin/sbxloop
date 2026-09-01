"""ClaudeBackend (#533) against a fake ``claude_agent_sdk``.

The backend defers every SDK import, so a stand-in module injected into
``sys.modules`` exercises the real request/response path — options
construction, the message-stream fold into events, permission decisions,
usage accounting, resume fallback — without the SDK, the Claude Code CLI,
or a network.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from sbxloop_worker.backends import BackendUnavailableError, get_backend
from sbxloop_worker.backends.claude import (
    ClaudeBackend,
    _auth_diagnostic,
    read_only_denial,
    unavailable_denial,
    usage_from_result,
)
from sbxloop_worker.protocol import Event, EventTypes, HostToolResponse, HostToolSpec, JobRequest

# -- the fake SDK -------------------------------------------------------------


class TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class ToolUseBlock:
    def __init__(self, id: str, name: str, input: dict[str, Any]) -> None:
        self.id, self.name, self.input = id, name, input


class ToolResultBlock:
    def __init__(self, tool_use_id: str, content: Any, is_error: bool | None = None) -> None:
        self.tool_use_id, self.content, self.is_error = tool_use_id, content, is_error


class AssistantMessage:
    def __init__(self, content: list[Any], model: str | None = None) -> None:
        self.content, self.model = content, model


class UserMessage:
    def __init__(self, content: list[Any]) -> None:
        self.content = content


class ResultMessage:
    def __init__(
        self,
        session_id: str | None = None,
        result: str | None = None,
        num_turns: int | None = None,
        usage: Any = None,
    ) -> None:
        self.session_id, self.result, self.num_turns, self.usage = (
            session_id,
            result,
            num_turns,
            usage,
        )


class PermissionResultAllow:
    behavior = "allow"


class PermissionResultDeny:
    behavior = "deny"

    def __init__(self, message: str = "", interrupt: bool = False) -> None:
        self.message, self.interrupt = message, interrupt


class ClaudeAgentOptions:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.__dict__.update(kwargs)
        self.resume = kwargs.get("resume")


def _tool(name: str, description: str, schema: Any):
    def decorate(fn: Any) -> Any:
        fn.tool_meta = (name, description, schema)
        return fn

    return decorate


def _create_server(name: str, version: str, tools: list[Any]) -> dict[str, Any]:
    return {"name": name, "version": version, "tools": tools}


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """A scripted claude_agent_sdk: yields ``mod.script`` messages and
    records the options every session was opened with."""
    mod = types.ModuleType("claude_agent_sdk")
    mod.script = []  # type: ignore[attr-defined]
    mod.opened_with = []  # type: ignore[attr-defined]
    mod.queries = []  # type: ignore[attr-defined]
    mod.fail_on_resume = False  # type: ignore[attr-defined]

    class ClaudeSDKClient:
        def __init__(self, options: Any = None) -> None:
            self.options = options

        async def __aenter__(self) -> ClaudeSDKClient:
            mod.opened_with.append(self.options)
            if mod.fail_on_resume and getattr(self.options, "resume", None):
                raise RuntimeError("No conversation found to resume")
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def query(self, prompt: str) -> None:
            mod.queries.append(prompt)

        async def receive_response(self):
            for message in mod.script:
                if isinstance(message, Exception):
                    raise message
                yield message

    for name, obj in {
        "ClaudeSDKClient": ClaudeSDKClient,
        "ClaudeAgentOptions": ClaudeAgentOptions,
        "PermissionResultAllow": PermissionResultAllow,
        "PermissionResultDeny": PermissionResultDeny,
        "tool": _tool,
        "create_sdk_mcp_server": _create_server,
        "AssistantMessage": AssistantMessage,
        "UserMessage": UserMessage,
        "ResultMessage": ResultMessage,
        "TextBlock": TextBlock,
        "ToolUseBlock": ToolUseBlock,
        "ToolResultBlock": ToolResultBlock,
    }.items():
        setattr(mod, name, obj)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    monkeypatch.setattr("sbxloop_worker.backends.claude.shutil.which", lambda _: "/usr/bin/claude")
    return mod


# -- helpers ------------------------------------------------------------------


def collect_emit() -> tuple[list[Event], Any]:
    events: list[Event] = []

    def emit(type_: str, **data: Any) -> Event:
        event = Event.now(type_, "r1", job_id="j1", **data)
        events.append(event)
        return event

    return events, emit


def job(**overrides: Any) -> JobRequest:
    base: dict[str, Any] = {
        "job_id": "j1",
        "run_id": "r1",
        "kind": "agent.session",
        "prompt": "do the work",
    }
    base.update(overrides)
    return JobRequest.model_validate(base)


def full_script(mod: types.ModuleType) -> None:
    mod.script = [
        AssistantMessage(
            [
                TextBlock("planning the change"),
                ToolUseBlock("t1", "Bash", {"command": "pytest -q"}),
            ],
            model="claude-opus-5",
        ),
        UserMessage([ToolResultBlock("t1", "3 passed", is_error=False)]),
        AssistantMessage([TextBlock("done")]),
        ResultMessage(
            session_id="sess-1",
            result="all tests pass",
            num_turns=2,
            usage={
                "input_tokens": 120,
                "output_tokens": 34,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 15,
            },
        ),
    ]


# -- tests --------------------------------------------------------------------


class TestSession:
    def test_full_session_events_and_result(self, sdk: types.ModuleType) -> None:
        full_script(sdk)
        events, emit = collect_emit()
        result = ClaudeBackend().run_session(job(), emit)

        assert result.output_text == "all tests pass"
        assert result.session_id == "sess-1"
        assert result.turns == 2
        assert result.usage is not None
        assert result.usage.model == "claude-opus-5"
        assert result.usage.input_tokens == 120
        assert result.usage.output_tokens == 34
        assert result.usage.cache_read_tokens == 900
        assert result.usage.cache_write_tokens == 15
        assert result.health is None

        types_seen = [e.type for e in events]
        assert types_seen == [
            EventTypes.AGENT_MESSAGE,
            EventTypes.AGENT_TOOL_START,
            EventTypes.AGENT_TOOL_END,
            EventTypes.AGENT_MESSAGE,
            EventTypes.AGENT_USAGE,
        ]
        start = next(e for e in events if e.type == EventTypes.AGENT_TOOL_START)
        assert start.data["tool"] == "Bash"
        assert start.data["args"] == "pytest -q"
        end = next(e for e in events if e.type == EventTypes.AGENT_TOOL_END)
        assert end.data["tool_call_id"] == "t1"
        assert end.data["tool"] == "Bash"
        assert end.data["success"] is True
        assert "3 passed" in end.data["output"]
        assert sdk.queries == ["do the work"]

    def test_json_expectation_extracts_fence(self, sdk: types.ModuleType) -> None:
        sdk.script = [
            ResultMessage(session_id="s", result='report\n```json\n{"verdict": "pass"}\n```')
        ]
        _, emit = collect_emit()
        result = ClaudeBackend().run_session(job(expect="json"), emit)
        assert result.output_json == {"verdict": "pass"}

    def test_failed_tool_counts_toward_health(self, sdk: types.ModuleType) -> None:
        sdk.script = [
            AssistantMessage([ToolUseBlock("t1", "Bash", {"command": "boom"})]),
            UserMessage([ToolResultBlock("t1", "exploded", is_error=True)]),
            ResultMessage(session_id="s", result="gave up"),
        ]
        _, emit = collect_emit()
        result = ClaudeBackend().run_session(job(), emit)
        assert result.health is not None
        assert result.health.tool_failures == {"Bash": 1}

    def test_sdk_error_carries_auth_diagnostic(
        self, sdk: types.ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        sdk.script = [RuntimeError("process exited 1")]
        _, emit = collect_emit()
        with pytest.raises(RuntimeError, match="auth diagnostic"):
            ClaudeBackend().run_session(job(), emit)


class TestOptions:
    def test_auto_mode_without_cap_bypasses_permissions(self, sdk: types.ModuleType) -> None:
        sdk.script = [ResultMessage(session_id="s", result="ok")]
        _, emit = collect_emit()
        ClaudeBackend().run_session(job(permission_mode="auto"), emit)
        opts = sdk.opened_with[0]
        assert opts.kwargs["permission_mode"] == "bypassPermissions"
        assert "can_use_tool" not in opts.kwargs

    def test_model_and_system_message_travel(self, sdk: types.ModuleType) -> None:
        sdk.script = [ResultMessage(session_id="s", result="ok")]
        _, emit = collect_emit()
        ClaudeBackend().run_session(job(model="claude-opus-5", system_message="be terse"), emit)
        opts = sdk.opened_with[0]
        assert opts.kwargs["model"] == "claude-opus-5"
        assert opts.kwargs["system_prompt"] == {
            "type": "preset",
            "preset": "claude_code",
            "append": "be terse",
        }

    def test_auto_model_is_left_to_the_sdk(self, sdk: types.ModuleType) -> None:
        sdk.script = [ResultMessage(session_id="s", result="ok")]
        _, emit = collect_emit()
        ClaudeBackend().run_session(job(model="auto"), emit)
        assert "model" not in sdk.opened_with[0].kwargs


class TestPermissions:
    @staticmethod
    async def _decide(handler: Any, tool_name: str) -> Any:
        return await handler(tool_name, {}, types.SimpleNamespace(tool_use_id="t9"))

    def _handler(self, sdk: types.ModuleType, the_job: JobRequest) -> tuple[Any, list[Event]]:
        sdk.script = [ResultMessage(session_id="s", result="ok")]
        events, emit = collect_emit()
        ClaudeBackend().run_session(the_job, emit)
        return sdk.opened_with[0].kwargs["can_use_tool"], events

    def test_read_only_denies_write_and_allows_reads(self, sdk: types.ModuleType) -> None:
        import asyncio

        handler, events = self._handler(sdk, job(permission_mode="read_only"))
        denied = asyncio.run(self._decide(handler, "Write"))
        assert denied.behavior == "deny"
        assert "read-only" in denied.message
        allowed = asyncio.run(self._decide(handler, "Read"))
        assert allowed.behavior == "allow"
        host = asyncio.run(self._decide(handler, "mcp__sbxloop__daemon_log"))
        assert host.behavior == "allow"
        assert any(e.type == EventTypes.AGENT_PERMISSION_DENIED for e in events)

    def test_governor_cap_turns_calls_away(self, sdk: types.ModuleType) -> None:
        import asyncio

        handler, events = self._handler(sdk, job(permission_mode="auto", max_tool_calls=1))
        first = asyncio.run(self._decide(handler, "Bash"))
        assert first.behavior == "allow"
        second = asyncio.run(self._decide(handler, "Bash"))
        assert second.behavior == "deny"
        assert "ceiling" in second.message
        assert any(e.type == EventTypes.AGENT_TOOL_CAP for e in events)

    def test_available_tools_restriction(self, sdk: types.ModuleType) -> None:
        import asyncio

        handler, _ = self._handler(sdk, job(permission_mode="auto", available_tools=[]))
        denied = asyncio.run(self._decide(handler, "Bash"))
        assert denied.behavior == "deny"
        host = asyncio.run(self._decide(handler, "mcp__sbxloop__watch_run"))
        assert host.behavior == "allow"


class TestResume:
    def test_missed_resume_falls_back_to_fresh(self, sdk: types.ModuleType) -> None:
        sdk.fail_on_resume = True
        sdk.script = [ResultMessage(session_id="fresh", result="ok")]
        _, emit = collect_emit()
        result = ClaudeBackend().run_session(job(resume_session_id="stale"), emit)
        assert result.session_id == "fresh"
        assert getattr(sdk.opened_with[0], "resume", None) == "stale"
        assert getattr(sdk.opened_with[1], "resume", None) is None

    def test_mid_session_failure_is_not_retried(self, sdk: types.ModuleType) -> None:
        sdk.script = [
            AssistantMessage([TextBlock("started")]),
            RuntimeError("connection lost"),
        ]
        _, emit = collect_emit()
        with pytest.raises(RuntimeError, match="connection lost"):
            ClaudeBackend().run_session(job(resume_session_id="stale"), emit)
        assert len(sdk.opened_with) == 1


class TestHostTools:
    def test_host_tools_become_an_mcp_server(
        self, sdk: types.ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        answered: list[Any] = []

        def fake_request_tool(emit: Any, tools_dir: Any, call: Any, timeout: Any) -> Any:
            answered.append(call)
            return HostToolResponse(call_id=call.call_id, ok=True, text="42")

        monkeypatch.setattr("sbxloop_worker.backends.claude.request_tool", fake_request_tool)
        sdk.script = [ResultMessage(session_id="s", result="ok")]
        _, emit = collect_emit()
        the_job = job(
            host_tools=[HostToolSpec(name="answer", description="the host answers")],
            host_tools_dir="/home/agent/.sbxloop/tools/j1",
        )
        ClaudeBackend().run_session(the_job, emit)
        server = sdk.opened_with[0].kwargs["mcp_servers"]["sbxloop"]
        assert server["name"] == "sbxloop"
        handler = server["tools"][0]
        assert handler.tool_meta[0] == "answer"
        response = asyncio.run(handler({"n": 41}))
        assert response == {"content": [{"type": "text", "text": "42"}]}
        assert answered and answered[0].name == "answer"

    def test_failed_host_tool_is_error_content(
        self, sdk: types.ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        monkeypatch.setattr(
            "sbxloop_worker.backends.claude.request_tool",
            lambda *a, **k: HostToolResponse(call_id="c", ok=False, text="", error="nope"),
        )
        sdk.script = [ResultMessage(session_id="s", result="ok")]
        _, emit = collect_emit()
        the_job = job(
            host_tools=[HostToolSpec(name="answer", description="d")],
            host_tools_dir="/home/agent/.sbxloop/tools/j1",
        )
        ClaudeBackend().run_session(the_job, emit)
        handler = sdk.opened_with[0].kwargs["mcp_servers"]["sbxloop"]["tools"][0]
        response = asyncio.run(handler({}))
        assert response["is_error"] is True
        assert response["content"][0]["text"] == "nope"


class TestAvailability:
    def test_missing_sdk_is_backend_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
        with pytest.raises(BackendUnavailableError, match="claude-agent-sdk is not installed"):
            ClaudeBackend().run_session(job(), lambda *a, **k: None)  # type: ignore[arg-type]

    def test_missing_cli_is_backend_unavailable(
        self, sdk: types.ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sbxloop_worker.backends.claude.shutil.which", lambda _: None)
        with pytest.raises(BackendUnavailableError, match="Claude Code CLI"):
            ClaudeBackend().run_session(job(), lambda *a, **k: None)  # type: ignore[arg-type]

    def test_registry_resolves_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SBXLOOP_WORKER_BACKEND", "claude")
        assert get_backend().name == "claude"


class TestDecisionLogic:
    def test_read_only_denial_polarity(self) -> None:
        assert read_only_denial("Read") is None
        assert read_only_denial("Grep") is None
        assert read_only_denial("mcp__sbxloop__anything") is None
        assert read_only_denial("Write") is not None
        assert read_only_denial("SomeNovelTool") is not None  # fail closed

    def test_unavailable_denial(self) -> None:
        assert unavailable_denial("Bash", None) is None
        assert unavailable_denial("Bash", ["Bash"]) is None
        assert unavailable_denial("Bash", []) is not None
        assert unavailable_denial("mcp__sbxloop__x", []) is None

    def test_usage_from_result_object_shape(self) -> None:
        message = types.SimpleNamespace(
            usage=types.SimpleNamespace(
                input_tokens=1,
                output_tokens=2,
                cache_read_input_tokens=3,
                cache_creation_input_tokens=4,
            )
        )
        usage = usage_from_result(message, "m")
        assert (usage.input_tokens, usage.output_tokens) == (1, 2)
        assert (usage.cache_read_tokens, usage.cache_write_tokens) == (3, 4)

    def test_auth_diagnostic_shapes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert "NOT set" in _auth_diagnostic()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sbx-cs-placeholder")
        assert "sentinel" in _auth_diagnostic()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-key-shape")
        assert "recognized format" in _auth_diagnostic()
