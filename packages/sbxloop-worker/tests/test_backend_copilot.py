"""CopilotBackend provider handling against a fake ``copilot`` SDK.

The backend defers every SDK import, so stand-in modules injected into
``sys.modules`` exercise the real session path — open, resume, the event
stream, the terminal-error fold — without the SDK, the Copilot CLI runtime
or a subscription.

Event payload classes are named after the SDK's own generated dataclasses
(the backend dispatches on ``type(event.data).__name__``) and carry the
fields verified against github-copilot-sdk 1.0.13: ``SessionErrorData``'s
``error_type``/``message``/``error_code``/``status_code``/``remediation``/
``eligible_for_auto_switch``, ``AutoModeSwitchRequestedData``'s
``retry_after_seconds``, and the per-call ``ModelCallFailureData``.
Envelopes here are synthetic — no account, credential or run data.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from sbxloop_worker.backends import copilot_errors
from sbxloop_worker.backends.copilot import BACKEND_NAME, CopilotBackend
from sbxloop_worker.protocol import Event, EventTypes, JobRequest, JobResult

# -- SDK-shaped event payloads ------------------------------------------------


class SessionErrorData:
    def __init__(
        self,
        message: str,
        *,
        error_type: str = "session_error",
        error_code: str | None = None,
        status_code: int | None = None,
        remediation: Any = None,
        eligible_for_auto_switch: bool | None = None,
    ) -> None:
        self.message = message
        self.error_type = error_type
        self.error_code = error_code
        self.status_code = status_code
        self.remediation = remediation
        self.eligible_for_auto_switch = eligible_for_auto_switch


class ModelCallFailureData:
    def __init__(self, *, status_code: int | None = None, error_code: str | None = None) -> None:
        self.status_code, self.error_code = status_code, error_code
        # The SDK's internal quota snapshot rides along on a failed call;
        # the adapter must never read it as a limit.
        self._quota_snapshots = {"premium": types.SimpleNamespace(_remaining_percentage=0.0)}


class AutoModeSwitchRequestedData:
    def __init__(self, retry_after_seconds: int | None) -> None:
        self.request_id, self.retry_after_seconds = "req-1", retry_after_seconds


class SessionLimitsExhaustedRequestedData:
    def __init__(self, used: float = 5.0, cap: float = 5.0) -> None:
        self.request_id, self.used_ai_credits, self.max_ai_credits = "req-2", used, cap


class AssistantMessageData:
    def __init__(self, content: str, model: str | None = None) -> None:
        self.content, self.model = content, model


class AssistantUsageData:
    def __init__(self, input_tokens: int, output_tokens: int, model: str = "served-model") -> None:
        self.input_tokens, self.output_tokens, self.model = input_tokens, output_tokens, model


class RpcError(Exception):
    """The SDK's JSON-RPC envelope shape: numeric code, message, data."""

    def __init__(self, message: str, data: Any = None, code: int = -32603) -> None:
        super().__init__(f"JSON-RPC Error {code}: {message}")
        self.code, self.message, self.data = code, message, data


def event(data: Any) -> Any:
    return types.SimpleNamespace(data=data)


# -- the fake SDK -------------------------------------------------------------


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """A scripted ``copilot`` package.

    ``mod.script`` is replayed to the session's handlers in order; an
    ``Exception`` in it is raised from ``send_and_wait`` the way the SDK
    raises after a terminal ``SessionErrorData``. ``mod.opened`` records
    every ``create_session``/``resume_session`` call.
    """
    mod = types.ModuleType("copilot")
    mod.script = []  # type: ignore[attr-defined]
    mod.opened = []  # type: ignore[attr-defined]
    mod.resume_error = None  # type: ignore[attr-defined]
    mod.create_error = None  # type: ignore[attr-defined]

    class Session:
        def __init__(self, session_id: str) -> None:
            self.session_id = session_id
            self._handlers: list[Any] = []

        def on(self, handler: Any) -> None:
            self._handlers.append(handler)

        async def send_and_wait(self, prompt: str, timeout: float | None = None) -> Any:
            content = ""
            for item in mod.script:
                if isinstance(item, Exception):
                    raise item
                for handler in self._handlers:
                    handler(event(item))
                if isinstance(item, AssistantMessageData):
                    content = item.content
            return types.SimpleNamespace(data=types.SimpleNamespace(content=content))

        async def disconnect(self) -> None:
            return None

    class CopilotClient:
        async def __aenter__(self) -> CopilotClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def create_session(self, **kwargs: Any) -> Any:
            mod.opened.append(("create", kwargs))
            if mod.create_error is not None:
                raise mod.create_error
            return Session("fresh-session")

        async def resume_session(self, session_id: str, **kwargs: Any) -> Any:
            mod.opened.append(("resume", kwargs))
            if mod.resume_error is not None:
                raise mod.resume_error
            return Session(session_id)

    mod.CopilotClient = CopilotClient  # type: ignore[attr-defined]

    rpc = types.ModuleType("copilot.rpc")
    rpc.PermissionDecisionApproveOnce = type("PermissionDecisionApproveOnce", (), {})  # type: ignore[attr-defined]
    rpc.PermissionDecisionReject = type("PermissionDecisionReject", (), {})  # type: ignore[attr-defined]
    session_mod = types.ModuleType("copilot.session")
    session_mod.PermissionHandler = types.SimpleNamespace(approve_all=object())  # type: ignore[attr-defined]

    for name, module in {
        "copilot": mod,
        "copilot.rpc": rpc,
        "copilot.session": session_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return mod


# -- helpers ------------------------------------------------------------------


def collect_emit() -> tuple[list[Event], Any]:
    events: list[Event] = []

    def emit(type_: str, **data: Any) -> Event:
        record = Event.now(type_, "r1", job_id="j1", **data)
        events.append(record)
        return record

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


def throttle_script(retry_after_seconds: int | None = 90) -> list[Any]:
    """A terminal structured 429: the SDK delivers the error event, then
    raises the generic exception ``send_and_wait`` builds from it."""
    return [
        AutoModeSwitchRequestedData(retry_after_seconds),
        SessionErrorData(
            "Too many requests to the model endpoint",
            error_type="rate_limit",
            error_code="rate_limit_exceeded",
            status_code=429,
            eligible_for_auto_switch=True,
        ),
        Exception("Session error: Too many requests to the model endpoint"),
    ]


# -- terminal provider limits -------------------------------------------------


@pytest.mark.parametrize("expect", ["text", "json"])
def test_terminal_throttle_keeps_its_status_code_and_timing(sdk, expect, monkeypatch) -> None:
    monkeypatch.setattr(
        copilot_errors, "time", types.SimpleNamespace(time=lambda: 1_000.0), raising=True
    )
    sdk.script = throttle_script()
    _, emit = collect_emit()
    result = CopilotBackend().run_session(job(expect=expect), emit)
    assert result.failure is not None
    assert result.failure.backend == BACKEND_NAME
    assert result.failure.category == "throttle"
    assert result.failure.http_status == 429
    assert result.failure.code == "rate_limit_exceeded"
    assert result.failure.retry_at == 1_090.0
    assert not result.failure.partial_progress
    # Never the JSON-repair path, and never provider prose in the reason.
    assert result.output_json is None
    assert "Too many requests" not in result.failure.reason


def test_terminal_throttle_survives_the_worker_result_path(sdk, tmp_path: Path) -> None:
    from sbxloop_worker.runner import JobRunner

    sdk.script = [
        AssistantUsageData(120, 15),
        *throttle_script(retry_after_seconds=None),
    ]
    path = tmp_path / "result.json"
    result = JobRunner(
        job(expect="json"),
        events_path=tmp_path / "events.jsonl",
        result_path=path,
        heartbeat_s=0,
        backend_name="copilot",
    ).run()
    assert result.status == "error"
    # Not ExpectedJsonMissing: a throttled job never asks for a repair.
    assert result.error is not None
    assert result.error.type == "ProviderFailure"
    assert result.error.provider is not None
    assert result.error.provider.backend == BACKEND_NAME
    assert result.error.provider.category == "throttle"
    assert result.error.provider.http_status == 429
    # Timing the provider did not supply stays unknown.
    assert result.error.provider.retry_at is None
    # Spend already incurred rides back on the failure.
    assert result.usage is not None
    assert result.usage.input_tokens == 120
    assert JobResult.model_validate_json(path.read_text()) == result


@pytest.mark.parametrize(
    ("message", "code", "status", "category"),
    [
        ("Monthly usage limit reached for premium requests", None, 429, "quota"),
        ("You have exhausted your quota", "quota_exceeded", 403, "quota"),
        ("Your billing account requires attention", None, 402, "billing"),
        ("The model endpoint is unavailable", None, 503, "unavailable"),
    ],
)
def test_quota_and_billing_outrank_the_http_status(sdk, message, code, status, category) -> None:
    sdk.script = [
        SessionErrorData(message, error_code=code, status_code=status),
        Exception(f"Session error: {message}"),
    ]
    _, emit = collect_emit()
    result = CopilotBackend().run_session(job(), emit)
    assert result.failure is not None
    assert result.failure.category == category
    assert result.failure.http_status == status
    # A quota or billing rejection schedules no retry of its own.
    assert result.failure.retry_at is None


def test_partial_output_before_a_terminal_limit_is_reported(sdk) -> None:
    sdk.script = [
        AssistantMessageData("wrote the failing test"),
        SessionErrorData("Rate limit exceeded", status_code=429),
        Exception("Session error: Rate limit exceeded"),
    ]
    _, emit = collect_emit()
    result = CopilotBackend().run_session(job(), emit)
    assert result.failure is not None
    assert result.failure.category == "throttle"
    assert result.failure.partial_progress


# -- what is not a provider limit ---------------------------------------------


@pytest.mark.parametrize("expect", ["text", "json"])
def test_intermediate_model_call_failure_that_recovers_is_not_terminal(sdk, expect) -> None:
    sdk.script = [
        ModelCallFailureData(status_code=429, error_code="rate_limit_exceeded"),
        AssistantMessageData('{"ok": true}' if expect == "json" else "done"),
        AssistantUsageData(10, 4),
    ]
    _, emit = collect_emit()
    result = CopilotBackend().run_session(job(expect=expect), emit)
    assert result.failure is None
    assert result.output_text
    if expect == "json":
        assert result.output_json == {"ok": True}


def test_a_session_error_followed_by_output_is_not_terminal(sdk) -> None:
    sdk.script = [
        SessionErrorData("Rate limit exceeded", status_code=429),
        AssistantMessageData("recovered and finished"),
    ]
    _, emit = collect_emit()
    result = CopilotBackend().run_session(job(), emit)
    assert result.failure is None
    assert result.output_text == "recovered and finished"


def test_an_app_set_session_credit_cap_is_not_account_quota(sdk) -> None:
    sdk.script = [
        SessionLimitsExhaustedRequestedData(),
        SessionErrorData("Session AI credit limit of 5.0 reached", status_code=400),
        Exception("Session error: Session AI credit limit of 5.0 reached"),
    ]
    _, emit = collect_emit()
    with pytest.raises(RuntimeError):
        CopilotBackend().run_session(job(), emit)


def test_task_text_about_rate_limits_is_not_a_provider_rejection(sdk) -> None:
    sdk.script = [
        AssistantMessageData("The retry helper handles a 429 rate limit from the API"),
        AssistantUsageData(10, 4),
    ]
    _, emit = collect_emit()
    result = CopilotBackend().run_session(job(), emit)
    assert result.failure is None
    assert "429" in result.output_text


def test_an_auth_failure_keeps_its_diagnostic(sdk, monkeypatch) -> None:
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "")
    sdk.script = [
        SessionErrorData(
            "Session was not created with authentication info",
            error_type="authentication_failed",
            status_code=401,
            remediation=types.SimpleNamespace(value="sign_in"),
        ),
        Exception("Session error: Session was not created with authentication info"),
    ]
    _, emit = collect_emit()
    with pytest.raises(RuntimeError, match="auth diagnostic"):
        CopilotBackend().run_session(job(), emit)


def test_an_unknown_exception_is_not_classified_as_a_limit(sdk) -> None:
    sdk.script = [RuntimeError("the CLI runtime exited")]
    _, emit = collect_emit()
    with pytest.raises(RuntimeError, match="the CLI runtime exited"):
        CopilotBackend().run_session(job(), emit)


# -- resume -------------------------------------------------------------------


def test_a_throttled_resume_never_opens_a_fresh_session(sdk) -> None:
    sdk.resume_error = RpcError(
        "Too many requests", data={"statusCode": 429, "retryAfterSeconds": 30}
    )
    _, emit = collect_emit()
    result = CopilotBackend().run_session(job(resume_session_id="prior"), emit)
    assert result.failure is not None
    assert result.failure.category == "throttle"
    assert result.failure.http_status == 429
    assert result.failure.retry_at is not None
    # The resumable context is kept, not spent on a second rejection.
    assert result.session_id == "prior"
    assert [kind for kind, _ in sdk.opened] == ["resume"]


def test_a_missing_session_still_falls_back_to_a_fresh_one(sdk) -> None:
    sdk.resume_error = RpcError("Session not found", data={"statusCode": 404})
    sdk.script = [AssistantMessageData("done")]
    _, emit = collect_emit()
    result = CopilotBackend().run_session(job(resume_session_id="expired"), emit)
    assert result.failure is None
    assert result.output_text == "done"
    assert [kind for kind, _ in sdk.opened] == ["resume", "create"]


def test_a_required_resume_that_fails_parks_for_inspection(sdk) -> None:
    sdk.resume_error = RpcError("Session not found", data={"statusCode": 404})
    _, emit = collect_emit()
    result = CopilotBackend().run_session(job(resume_session_id="lost", require_resume=True), emit)
    assert result.failure is not None
    assert result.failure.category == "recovery"
    assert result.failure.partial_progress
    assert result.session_id == "lost"
    assert [kind for kind, _ in sdk.opened] == ["resume"]


def test_a_throttled_create_is_a_provider_failure(sdk) -> None:
    sdk.create_error = RpcError("Rate limit exceeded", data={"statusCode": 429})
    _, emit = collect_emit()
    result = CopilotBackend().run_session(job(), emit)
    assert result.failure is not None
    assert result.failure.category == "throttle"
    assert [kind for kind, _ in sdk.opened] == ["create"]


def test_an_unsupported_working_directory_kwarg_still_retries(sdk) -> None:
    """The pre-existing signature fallback must survive the new branches."""
    calls: list[dict[str, Any]] = []
    original = sdk.CopilotClient.create_session

    async def create_session(self: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        if "working_directory" in kwargs:
            raise TypeError("unexpected keyword argument 'working_directory'")
        return await original(self, **kwargs)

    sdk.CopilotClient.create_session = create_session
    sdk.script = [AssistantMessageData("done")]
    _, emit = collect_emit()
    result = CopilotBackend().run_session(job(cwd="/work"), emit)
    assert result.failure is None
    assert len(calls) == 2
    assert "working_directory" not in calls[-1]


def test_a_provider_limit_emits_no_agent_message(sdk) -> None:
    sdk.script = throttle_script()
    events, emit = collect_emit()
    result = CopilotBackend().run_session(job(), emit)
    assert result.failure is not None
    assert not [e for e in events if e.type == EventTypes.AGENT_MESSAGE]
