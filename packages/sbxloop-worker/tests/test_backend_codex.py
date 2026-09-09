"""Codex SDK contract against an in-memory app-server client, without inference."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import types
from pathlib import Path
from typing import Any

import pytest

from sbxloop_worker.backends import BackendUnavailableError, codex_runtime, get_backend
from sbxloop_worker.backends.codex import CodexBackend
from sbxloop_worker.backends.codex_runtime import authenticated_client
from sbxloop_worker.protocol import Event, HostToolResponse, HostToolSpec, JobRequest


def job(**overrides: Any) -> JobRequest:
    return JobRequest(
        job_id="j1", run_id="r1", kind="agent.session", prompt="Do the task.", **overrides
    )


def notification(method: str, **payload: Any) -> Any:
    return types.SimpleNamespace(method=method, payload=payload)


def message(text: str, *, phase: str = "final_answer", item_id: str = "m1") -> Any:
    return notification(
        "item/completed",
        item={"type": "agentMessage", "id": item_id, "text": text, "phase": phase},
    )


def completed(status: str = "completed", error: str | None = None) -> Any:
    return notification(
        "turn/completed",
        turn={"id": "turn-1", "status": status, "error": {"message": error} if error else None},
    )


def usage(total: int, last: int, *, output: int = 3, cache: int = 2) -> Any:
    def counters(tokens: int) -> dict[str, int]:
        return {
            "inputTokens": tokens,
            "outputTokens": output,
            "cachedInputTokens": cache,
            "cacheWriteInputTokens": 0,
        }

    return notification(
        "thread/tokenUsage/updated", tokenUsage={"total": counters(total), "last": counters(last)}
    )


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    state = types.SimpleNamespace(
        clients=[],
        script=[message("Done."), completed()],
        resume_error=None,
        turn_error=None,
        login_error=None,
        blocked=False,
        calls=[],
        responses=[],
        effective_config={},
    )

    class Config:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class Client:
        def __init__(self, config: Any, approval_handler: Any = None) -> None:
            self.config = config
            self.handler = approval_handler
            self.closed = threading.Event()
            self.thread_params: list[dict[str, Any]] = []
            self.resume_params: list[dict[str, Any]] = []
            self.turn_params: list[dict[str, Any]] = []
            self.script = iter(state.script)
            state.clients.append(self)

        def start(self) -> None:
            state.calls.append("start")

        def initialize(self) -> None:
            state.calls.append("initialize")

        def request(self, method: str, params: Any, *, response_model: Any) -> Any:
            assert method == "config/read"
            assert params == {"cwd": self.config.cwd, "includeLayers": False}
            state.calls.append("config/read")
            return types.SimpleNamespace(
                config=types.SimpleNamespace(model_dump=lambda **kwargs: state.effective_config)
            )

        def account_login_start(self, params: Any) -> Any:
            state.login = params
            if state.login_error:
                raise state.login_error
            return types.SimpleNamespace(root=types.SimpleNamespace(type="apiKey"))

        def thread_start(self, params: dict[str, Any]) -> Any:
            self.thread_params.append(params)
            return types.SimpleNamespace(thread=types.SimpleNamespace(id="fresh"), model="gpt-test")

        def thread_resume(self, session_id: str, params: dict[str, Any]) -> Any:
            self.resume_params.append(params)
            if state.resume_error:
                raise state.resume_error
            return types.SimpleNamespace(
                thread=types.SimpleNamespace(id=session_id), model="gpt-test"
            )

        def turn_start(self, session_id: str, prompt: str, params: dict[str, Any]) -> Any:
            self.turn_params.append(params)
            if state.turn_error:
                raise state.turn_error
            return types.SimpleNamespace(turn=types.SimpleNamespace(id="turn-1"))

        def next_turn_notification(self, turn_id: str) -> Any:
            if state.blocked:
                assert self.closed.wait(3), "watchdog did not close the SDK"
                raise RuntimeError("transport closed")
            item = next(self.script)
            while callable(item):
                item(self)
                item = next(self.script)
            if isinstance(item, BaseException):
                raise item
            return item

        def unregister_turn_notifications(self, turn_id: str) -> None:
            state.calls.append("unregister")

        def close(self) -> None:
            self.closed.set()

    module = types.ModuleType("openai_codex.client")
    module.CodexClient, module.CodexConfig = Client, Config  # type: ignore[attr-defined]
    package = types.ModuleType("openai_codex")
    package.__path__ = []  # type: ignore[attr-defined]
    package.__version__ = "0.147.0"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai_codex", package)
    monkeypatch.setitem(sys.modules, "openai_codex.client", module)
    generated = types.ModuleType("openai_codex.generated")
    generated.__path__ = []  # type: ignore[attr-defined]
    v2 = types.ModuleType("openai_codex.generated.v2_all")
    v2.ConfigReadResponse = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai_codex.generated", generated)
    monkeypatch.setitem(sys.modules, "openai_codex.generated.v2_all", v2)
    binary = tmp_path / "codex"
    binary.write_text("runtime fixture")
    binary.with_name("codex-code-mode-host").write_text("composition fixture")
    cli = types.ModuleType("codex_cli_bin")
    cli.bundled_codex_path = lambda: binary  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "codex_cli_bin", cli)
    monkeypatch.setattr(codex_runtime.metadata, "version", lambda package: "0.147.0")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-this-is-a-fake-inference-key")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return state


@pytest.mark.parametrize("servers", [{"untrusted": {"command": "do-not-launch"}}, None, []])
def test_runtime_rejects_inherited_mcp_before_login(sdk: Any, servers: Any) -> None:
    sdk.effective_config = {"mcp_servers": servers}
    with (
        pytest.raises(RuntimeError, match="unexpected MCP configuration") as error,
        authenticated_client(),
    ):
        pytest.fail("configured MCP must be rejected before yielding a client")
    assert "do-not-launch" not in str(error.value)
    assert not hasattr(sdk, "login")
    assert sdk.clients[0].thread_params == []
    assert sdk.clients[0].closed.is_set()


@pytest.fixture
def emitted() -> tuple[list[Event], Any]:
    events: list[Event] = []

    def emit(event_type: str, **data: Any) -> Event:
        event = Event.now(event_type, "r1", "j1", **data)
        events.append(event)
        return event

    return events, emit


def tool_call(tool: str, arguments: dict[str, Any], call_id: str = "call-1") -> Any:
    def invoke(client: Any) -> None:
        response = client.handler(
            "item/tool/call",
            {
                "threadId": "fresh",
                "turnId": "turn-1",
                "callId": call_id,
                "namespace": None,
                "tool": tool,
                "arguments": arguments,
            },
        )
        client.response = response

    return invoke


def test_registered_and_text_result(sdk: Any, emitted: Any) -> None:
    events, emit = emitted
    assert isinstance(get_backend("codex"), CodexBackend)
    result = CodexBackend().run_session(job(model="auto"), emit)
    assert result.output_text == "Done."
    assert result.session_id and result.session_id.startswith("codex-v1:fresh:")
    assert result.usage is None and result.turns is None
    assert events[-1].data["backend"] == "codex"
    client = sdk.clients[0]
    assert "model" not in client.thread_params[0]
    assert client.thread_params[0]["environments"] == []
    assert client.turn_params[0]["environments"] == []
    assert client.closed.is_set()


def test_workload_replaces_persona_and_returns_final_json(sdk: Any, emitted: Any) -> None:
    sdk.script = [
        message("I will inspect it.", phase="commentary", item_id="comment"),
        message('{"ok":true}'),
        completed(),
    ]
    result = CodexBackend().run_session(
        job(system_preset=False, system_message="You are an operator.", expect="json"), emitted[1]
    )
    assert result.output_json == {"ok": True}
    assert sdk.clients[0].thread_params[0]["baseInstructions"] == "You are an operator."


def test_concierge_exposes_only_host_tools_and_denies_stale_builtins(
    sdk: Any, emitted: Any, tmp_path: Path
) -> None:
    sdk.script = [
        tool_call("shell", {"command": "do-not-execute"}),
        message("Unavailable."),
        completed(),
    ]
    result = CodexBackend().run_session(
        job(
            permission_mode="read_only",
            available_tools=[],
            host_tools=[HostToolSpec(name="inspect_run", description="Read the run")],
            host_tools_dir=str(tmp_path),
        ),
        emitted[1],
    )
    specs = sdk.clients[0].thread_params[0]["dynamicTools"]
    assert [spec["name"] for spec in specs] == ["inspect_run"]
    assert sdk.clients[0].response["success"] is False
    assert result.health and result.health.permission_denials == {"shell": 1}


def test_tool_cap_prevents_file_side_effect_and_is_not_tool_failure(
    sdk: Any, emitted: Any, tmp_path: Path
) -> None:
    sdk.script = [
        tool_call("write_file", {"path": "one.txt", "content": "first"}),
        tool_call("write_file", {"path": "two.txt", "content": "second"}, "call-2"),
        message("Finished."),
        completed(),
    ]
    result = CodexBackend().run_session(job(cwd=str(tmp_path), max_tool_calls=1), emitted[1])
    assert (tmp_path / "one.txt").read_text() == "first"
    assert not (tmp_path / "two.txt").exists()
    assert result.health and result.health.tool_cap_denials == 1
    assert result.health.tool_failures == {}
    assert len([e for e in emitted[0] if e.type == "agent.tool_cap"]) == 1
    starts = [e for e in emitted[0] if e.type == "agent.tool_start"]
    ends = [e for e in emitted[0] if e.type == "agent.tool_end"]
    assert [e.data["tool_call_id"] for e in starts] == [e.data["tool_call_id"] for e in ends]


def test_host_tool_uses_real_file_relay(sdk: Any, emitted: Any, tmp_path: Path) -> None:
    (tmp_path / "call-1.json").write_text(
        HostToolResponse(call_id="call-1", ok=True, text="Run ready.").model_dump_json()
    )
    sdk.script = [tool_call("inspect_run", {}), message("Ready."), completed()]
    CodexBackend().run_session(
        job(
            available_tools=[],
            host_tools=[HostToolSpec(name="inspect_run", description="Read the run")],
            host_tools_dir=str(tmp_path),
        ),
        emitted[1],
    )
    assert sdk.clients[0].response == {
        "contentItems": [{"type": "inputText", "text": "Run ready."}],
        "success": True,
    }
    assert any(e.type == "agent.tool_request" for e in emitted[0])
    assert any(e.type == "agent.tool_response" for e in emitted[0])


def test_resume_miss_only_retries_opening(sdk: Any, emitted: Any) -> None:
    previous = CodexBackend().run_session(job(), emitted[1]).session_id
    sdk.clients.clear()
    sdk.resume_error = RuntimeError("session no longer exists")
    result = CodexBackend().run_session(job(resume_session_id=previous), emitted[1])
    assert result.session_id and result.session_id.startswith("codex-v1:fresh:")
    assert len(sdk.clients[0].resume_params) == len(sdk.clients[0].thread_params) == 1


def test_no_fresh_retry_after_turn_start(sdk: Any, emitted: Any) -> None:
    previous = CodexBackend().run_session(job(), emitted[1]).session_id
    sdk.clients.clear()
    sdk.script = [message("Working."), RuntimeError("connection lost")]
    with pytest.raises(RuntimeError, match="connection lost"):
        CodexBackend().run_session(job(resume_session_id=previous), emitted[1])
    assert sdk.clients[0].thread_params == []
    assert sdk.clients[0].closed.is_set()


def test_changed_capabilities_start_fresh(sdk: Any, emitted: Any) -> None:
    previous = CodexBackend().run_session(job(), emitted[1]).session_id
    sdk.clients.clear()
    result = CodexBackend().run_session(
        job(resume_session_id=previous, available_tools=[]), emitted[1]
    )
    assert result.session_id != previous
    assert sdk.clients[0].resume_params == []
    assert len(sdk.clients[0].thread_params) == 1


def test_unknown_session_manifest_starts_fresh(sdk: Any, emitted: Any) -> None:
    CodexBackend().run_session(job(resume_session_id="untracked-thread"), emitted[1])
    assert sdk.clients[0].resume_params == []


def test_usage_counts_only_this_job_and_deduplicates(sdk: Any, emitted: Any) -> None:
    sdk.script = [
        usage(1000, 10),
        usage(1000, 10),
        usage(1015, 15, output=7, cache=5),
        message("Done."),
        completed(),
    ]
    result = CodexBackend().run_session(job(resume_session_id="old"), emitted[1])
    assert result.usage and result.usage.input_tokens == 25
    assert result.usage.output_tokens == 7 and result.usage.cache_read_tokens == 5
    assert result.usage.backend == "codex" and result.usage.model == "gpt-test"
    assert result.turns is None


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_noncompleted_turn_is_error(sdk: Any, emitted: Any, status: str) -> None:
    sdk.script = [message("Incomplete."), completed(status, "Provider rejected the request.")]
    with pytest.raises(RuntimeError):
        CodexBackend().run_session(job(), emitted[1])


def test_missing_json_stays_missing(sdk: Any, emitted: Any) -> None:
    result = CodexBackend().run_session(job(expect="json"), emitted[1])
    assert result.output_json is None


def test_commentary_is_not_a_final_answer(sdk: Any, emitted: Any) -> None:
    sdk.script = [message("Still working.", phase="commentary"), completed()]
    with pytest.raises(RuntimeError, match="without a final answer"):
        CodexBackend().run_session(job(), emitted[1])


def test_native_approval_is_never_accepted(sdk: Any, emitted: Any) -> None:
    def unexpected(client: Any) -> None:
        client.handler("item/commandExecution/requestApproval", {"command": "touch escaped"})

    sdk.script = [unexpected, completed()]
    with pytest.raises(RuntimeError, match=r"unexpected|unsupported|native"):
        CodexBackend().run_session(job(), emitted[1])


def test_deadline_closes_runtime_and_classifies_timeout(sdk: Any, emitted: Any) -> None:
    sdk.blocked = True
    with pytest.raises(subprocess.TimeoutExpired):
        CodexBackend().run_session(job(timeout_s=0.05), emitted[1])
    assert sdk.clients[0].closed.is_set()


def test_deadline_during_start_closes_late_process_before_initialize(
    sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_type = sys.modules["openai_codex.client"].CodexClient
    original_close = client_type.close
    watchdog_called = threading.Event()
    started = False

    def delayed_start(client: Any) -> None:
        nonlocal started
        # Match the SDK: close() does nothing until Popen has returned.
        assert watchdog_called.wait(3), "watchdog did not run during startup"
        started = True
        sdk.calls.append("start")

    def close(client: Any) -> None:
        if not started:
            watchdog_called.set()
            return
        original_close(client)

    monkeypatch.setattr(client_type, "start", delayed_start)
    monkeypatch.setattr(client_type, "close", close)
    with pytest.raises(subprocess.TimeoutExpired), authenticated_client(timeout_s=0.01):
        pass
    assert sdk.clients[0].closed.is_set()
    assert sdk.calls == ["start"], "an expired runtime must not begin a blocking RPC"


def test_runtime_isolated_auth_never_enters_argv(sdk: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", "/operator/private-codex")
    monkeypatch.setenv("CODEX_API_KEY", "wrong-key")
    original_env = dict(os.environ)
    with authenticated_client(persistent=False) as client:
        config = client.config
        assert config.env["CODEX_HOME"] != original_env["CODEX_HOME"]
        assert config.env["CODEX_API_KEY"] == ""
        assert original_env["OPENAI_API_KEY"] not in repr(config.config_overrides)
        assert sdk.login == {"type": "apiKey", "apiKey": original_env["OPENAI_API_KEY"]}
        assert any("ephemeral" in value for value in config.config_overrides)
        home = Path(config.env["CODEX_HOME"])
        assert home.exists()
    assert not home.exists()
    assert dict(os.environ) == original_env


def test_missing_key_does_not_start_sdk(sdk: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY")
    with (
        pytest.raises(BackendUnavailableError, match="OPENAI_API_KEY"),
        authenticated_client(persistent=False),
    ):
        pytest.fail("keyless client yielded")
    assert sdk.clients == []


def test_sdk_failure_redacts_entire_key(sdk: Any) -> None:
    key = os.environ["OPENAI_API_KEY"]
    sdk.login_error = RuntimeError(f"server echoed {key}")
    with pytest.raises(RuntimeError) as caught, authenticated_client(persistent=False):
        pytest.fail("failed login yielded")
    assert key not in str(caught.value)
    assert sdk.clients[0].closed.is_set()


def test_readiness_checks_pinned_sdk_without_starting(sdk: Any) -> None:
    package = sys.modules["openai_codex"]
    package.__version__ = "0.148.0"  # type: ignore[attr-defined]
    with pytest.raises(BackendUnavailableError, match=r"0\.147\.0"):
        CodexBackend().ensure_available()
    assert sdk.clients == []


def test_readiness_rejects_mismatched_runtime(sdk: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_runtime.metadata, "version", lambda package: "0.148.0")
    with pytest.raises(BackendUnavailableError, match=r"Codex runtime 0\.147\.0"):
        CodexBackend().ensure_available()
    assert sdk.clients == []


def test_readiness_rejects_missing_runtime(sdk: Any) -> None:
    sys.modules["codex_cli_bin"].bundled_codex_path().unlink()  # type: ignore[attr-defined]
    with pytest.raises(BackendUnavailableError, match="executable is missing"):
        CodexBackend().ensure_available()
    assert sdk.clients == []


def test_readiness_requires_bundled_composition_host(sdk: Any) -> None:
    sys.modules["codex_cli_bin"].bundled_codex_path().with_name("codex-code-mode-host").unlink()  # type: ignore[attr-defined]
    with pytest.raises(BackendUnavailableError, match="codex-code-mode-host executable is missing"):
        CodexBackend().ensure_available()
    assert sdk.clients == []


def test_runtime_allows_only_pure_composition_host(sdk: Any) -> None:
    with authenticated_client(persistent=False) as client:
        overrides = client.config.config_overrides
        assert "features.code_mode_host=true" in overrides
        assert "features.code_mode_host=false" not in overrides
        assert "features.shell_tool=false" in overrides
        assert "orchestrator.skills.enabled=false" in overrides
        assert "orchestrator.mcp.enabled=false" in overrides


@pytest.mark.parametrize("name", ["config.toml", "hooks.json", "AGENTS.md", "skills"])
def test_persistent_home_rejects_settings_before_runtime_start(
    sdk: Any, tmp_path: Path, name: str
) -> None:
    runtime_home = tmp_path / ".sbxloop-codex"
    runtime_home.mkdir()
    (runtime_home / name).write_text("untrusted content")
    with (
        pytest.raises(BackendUnavailableError, match="unexpected settings source"),
        authenticated_client(),
    ):
        pytest.fail("untrusted settings were loaded")
    assert sdk.clients == []


@pytest.mark.parametrize(
    "kind", ["commandExecution", "fileChange", "webSearch", "mcpToolCall", "collabAgentToolCall"]
)
def test_native_tool_event_fails_job_closed(sdk: Any, emitted: Any, kind: str) -> None:
    sdk.script = [
        notification("item/started", item={"type": kind, "id": "unexpected"}),
        completed(),
    ]
    with pytest.raises(RuntimeError, match="unexpected native Codex item"):
        CodexBackend().run_session(job(), emitted[1])
