"""Claude Agent SDK backend (#533).

Runs one agentic session inside the sandbox via the ``claude-agent-sdk``
package (installed with the ``[claude]`` extra). The SDK is the Claude Code
harness as a library: it spawns the Claude Code CLI — which must be on PATH
in the sandbox (provisioning installs Node plus ``@anthropic-ai/claude-code``
when the backend is selected) — and supplies the built-in coding tools.
Imports are deferred so the worker package works without the SDK for
shell/github jobs and for test environments; unit tests exercise this module
against a fake ``claude_agent_sdk`` injected into ``sys.modules``.

Auth: the SDK reads ``ANTHROPIC_API_KEY``, which the sbxloop provisioner
injects into the agent sandbox alone (proxy-bound to ``api.anthropic.com``
under the default secret strategy, per-job stdin or the env file otherwise).
The credential split is unchanged: the agent sandbox never holds a GitHub
token, whichever agent backend it runs.

Contract parity with the Copilot backend:

- the same ``agent.*`` event stream (message, tool_start/tool_end, usage,
  permission_denied, tool_cap), so chronology, chat bridges and checkpoints
  are backend-agnostic;
- ``permission_mode="auto"`` approves everything (the microVM is the
  security boundary) under the tool-call governor; ``"read_only"`` allows
  only the read-shaped built-ins and fails closed on anything unknown;
- host tools are registered as an in-process MCP server whose handlers
  relay to the host through ``sbxloop_worker.hosttools``;
- resume is an optimisation, never a requirement: a session the CLI cannot
  resume costs a fresh session, nothing more;
- usage lands in the protocol :class:`Usage` (tokens + model), so
  ``run_usage`` / ``usage_today`` report Claude spend exactly like Copilot.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Any

from sbxloop_worker._json import extract_json
from sbxloop_worker.backends import BackendResult, BackendUnavailableError, EmitFn
from sbxloop_worker.backends.copilot import (
    SessionHealthTracker,
    ToolCallGovernor,
    ToolCallRegistry,
    excerpt_output,
)
from sbxloop_worker.hosttools import HostToolTimeout, request_tool, safe_call_id
from sbxloop_worker.mcp import server_configs
from sbxloop_worker.protocol import (
    EventTypes,
    HostToolCall,
    HostToolSpec,
    JobRequest,
    Usage,
)
from sbxloop_worker.secrets import is_sbx_sentinel

# The in-process MCP server host tools are registered under; the SDK exposes
# each tool to the model as ``mcp__<server>__<tool>``.
# Backend identity stamped onto agent events and usage samples, so chat
# can name provider+model.
BACKEND_NAME = "claude"

HOST_TOOL_SERVER = "sbxloop"
_HOST_TOOL_PREFIX = f"mcp__{HOST_TOOL_SERVER}__"

# The read-only critic barrier, on Claude Code's built-in tool vocabulary —
# an allowlist with default-deny, exactly like the Copilot backend's
# READ_ONLY_ALLOWED_KINDS: an unknown or novel tool fails closed, so the
# worst case is the critic losing a capability and saying so — never the
# critic silently editing the work under review. Read/Glob/Grep are true
# reads; WebFetch/WebSearch cannot mutate the workspace and stay inside the
# sandbox network policy; Bash is allowed so the critic can run inspection
# commands, trading the hard mutation guarantee for utility the same way the
# Copilot backend's ``shell`` allowance does.
READ_ONLY_ALLOWED_TOOLS = frozenset({"Read", "Glob", "Grep", "WebFetch", "WebSearch", "Bash"})

ANTHROPIC_TOKEN_ENV = "ANTHROPIC_API_KEY"  # nosec B105 - env var name
ANTHROPIC_TOKEN_PREFIX = "sk-ant-"  # nosec B105 - shape marker, not a credential

TOOL_ARGS_CLIP = 400


#: This SDK's spelling of the stdio transport (the Copilot SDK says
#: ``local``); field-verified against claude-agent-sdk 0.2.149.
MCP_STDIO_TYPE = "stdio"


def read_only_denial(tool_name: str) -> str | None:
    """Rejection feedback for ``tool_name`` in a read-only session, or None.

    Host tools are always allowed — the host decided which exist, and each
    call is answered (and can be refused) host-side anyway. Pure decision
    logic so the default-deny polarity is unit-testable without the SDK.
    """
    if tool_name.startswith(_HOST_TOOL_PREFIX):
        return None
    if tool_name in READ_ONLY_ALLOWED_TOOLS:
        return None
    return (
        f"this is a read-only review session; the {tool_name!r} tool is not in "
        "the read allowlist — do not modify anything, report findings instead"
    )


def unavailable_denial(tool_name: str, available: list[str] | None) -> str | None:
    """Rejection feedback when the job restricts built-in tools, or None.

    ``available_tools=[]`` means host tools only (the concierge shape); host
    tool names are always allowed, mirroring the Copilot backend's
    ``available_tools`` handling.
    """
    if available is None or tool_name.startswith(_HOST_TOOL_PREFIX):
        return None
    if tool_name in available:
        return None
    return f"the {tool_name!r} tool is not available in this session"


def _auth_diagnostic() -> str:
    """Describe the ANTHROPIC_API_KEY this process can actually see."""
    token = os.environ.get(ANTHROPIC_TOKEN_ENV, "")
    if not token:
        return (
            f"auth diagnostic: {ANTHROPIC_TOKEN_ENV} is NOT set in the worker "
            "environment - sbx secret injection did not reach this process; "
            'try secret_strategy="plain-env"'
        )
    if is_sbx_sentinel(token):
        return (
            f"auth diagnostic: {ANTHROPIC_TOKEN_ENV} holds an sbx secret-proxy "
            "sentinel; the Claude Code CLI sends the value directly and cannot "
            'authenticate with a placeholder - set secret_strategy="plain-env"'
        )
    if token.startswith(ANTHROPIC_TOKEN_PREFIX):
        return (
            f"auth diagnostic: {ANTHROPIC_TOKEN_ENV} is set with a recognized "
            "format; the failure is likely account access, model availability, "
            "or network policy (the sandbox must reach api.anthropic.com)"
        )
    return (
        f"auth diagnostic: {ANTHROPIC_TOKEN_ENV} is set but has an unrecognized "
        f"format ({token[:6]}...)"
    )


def _render_args(raw: Any) -> str | None:
    """A ToolUseBlock's input as one displayable string (best-effort)."""
    if not isinstance(raw, dict) or not raw:
        return None
    for key in ("command", "cmd", "input", "query", "pattern", "file_path", "path"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value[:TOOL_ARGS_CLIP]
    try:
        return json.dumps(raw, separators=(",", ":"))[:TOOL_ARGS_CLIP]
    except (TypeError, ValueError):
        return str(raw)[:TOOL_ARGS_CLIP]


def _result_text(content: Any) -> str | None:
    """A ToolResultBlock's content as plain text (None when absent/blank).

    The SDK delivers either a string or a list of content dicts/blocks."""
    if isinstance(content, str):
        return content if content.strip() else None
    if isinstance(content, list):
        parts: list[str] = []
        for entry in content:
            text = entry.get("text") if isinstance(entry, dict) else getattr(entry, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(text)
        if parts:
            return "\n".join(parts)
    return None


def _usage_value(usage: Any, name: str) -> Any:
    """One usage counter from the SDK's usage payload (dict or object)."""
    if isinstance(usage, dict):
        return usage.get(name)
    return getattr(usage, name, None)


def usage_from_result(message: Any, model: str | None) -> Usage:
    """A ResultMessage's usage as a protocol :class:`Usage`.

    The SDK reports the Messages-API counter names
    (``cache_creation_input_tokens`` / ``cache_read_input_tokens``); the
    protocol carries them as ``cache_write_tokens`` / ``cache_read_tokens``.
    """
    usage = getattr(message, "usage", None)
    return Usage(
        model=model,
        backend=BACKEND_NAME,
        input_tokens=_usage_value(usage, "input_tokens"),
        output_tokens=_usage_value(usage, "output_tokens"),
        cache_read_tokens=_usage_value(usage, "cache_read_input_tokens"),
        cache_write_tokens=_usage_value(usage, "cache_creation_input_tokens"),
    )


class ClaudeBackend:
    name = "claude"

    def ensure_available(self) -> None:
        """What this backend needs before a session can start (see
        ``backends.ensure_available``): the SDK from the worker's
        ``[claude]`` extra, and the CLI it spawns, which the pip package
        does not bundle."""
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError as exc:
            raise BackendUnavailableError(
                "claude-agent-sdk is not installed; install sbxloop-worker[claude]"
            ) from exc
        if shutil.which("claude") is None:
            raise BackendUnavailableError(
                "the Claude Code CLI is not on PATH in this sandbox; the claude "
                "backend needs it (provisioning installs Node and "
                "@anthropic-ai/claude-code — re-provision, or bake a template "
                "with the claude backend configured)"
            )

    def run_session(self, job: JobRequest, emit: EmitFn) -> BackendResult:
        self.ensure_available()
        try:
            return asyncio.run(self._run(job, emit))
        except BackendUnavailableError:
            raise
        except Exception as exc:
            # Auth failures surface as opaque CLI/process errors; say what
            # the token environment actually looks like from inside the
            # sandbox, exactly like the Copilot backend does.
            raise RuntimeError(f"{exc} | {_auth_diagnostic()}") from exc

    async def _run(self, job: JobRequest, emit: EmitFn) -> BackendResult:
        tracker = SessionHealthTracker()
        governor = ToolCallGovernor(job.max_tool_calls)
        state = _SessionState(job, emit, tracker, governor)

        options = self._options(job, emit, tracker, governor, resume=job.resume_session_id)
        try:
            result = await asyncio.wait_for(self._session(job, options, state), job.timeout_s)
        except TimeoutError:
            raise
        except Exception:
            # Resume is an optimisation, never a requirement: a session the
            # CLI has expired or never heard of must cost a fresh session
            # and nothing more. Only a session that produced nothing yet is
            # retried — a mid-session failure is a real failure. The host
            # sees the miss for free: a fresh session comes back with a
            # different id than the one it asked to resume
            # (`phase.resume_missed`).
            if not job.resume_session_id or state.saw_output:
                raise
            state = _SessionState(job, emit, tracker, governor)
            options = self._options(job, emit, tracker, governor, resume=None)
            result = await asyncio.wait_for(self._session(job, options, state), job.timeout_s)
        return result

    async def _session(self, job: JobRequest, options: Any, state: _SessionState) -> BackendResult:
        from claude_agent_sdk import ClaudeSDKClient

        async with ClaudeSDKClient(options=options) as client:
            assert job.prompt is not None
            await client.query(job.prompt)
            async for message in client.receive_response():
                state.handle(message)
        text = state.result_text or "\n".join(state.final_text)
        return BackendResult(
            output_text=text,
            output_json=extract_json(text) if job.expect == "json" else None,
            session_id=state.session_id,
            usage=(
                state.usage.merged(Usage(backend=BACKEND_NAME)) if state.usage != Usage() else None
            ),
            turns=state.turns,
            health=state.tracker.health(state.governor),
        )

    def _options(
        self,
        job: JobRequest,
        emit: EmitFn,
        tracker: SessionHealthTracker,
        governor: ToolCallGovernor,
        *,
        resume: str | None,
    ) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        kwargs: dict[str, Any] = {
            # The Claude Code system prompt is opt-in for SDK sessions; the
            # coding personas are written against a Claude Code-shaped
            # agent, so the preset is requested and the job's system
            # message appended after it (the Copilot backend's `append`
            # mode, same semantics). A job that declines the preset is not
            # a coding agent: its system message is the whole system
            # prompt, and with none the SDK's own default stands.
            "system_prompt": self._system_prompt(job),
            # No filesystem settings — the SDK's own default, declared
            # rather than inherited (#688): the repository's CLAUDE.md
            # reaches every phase through the prompt's repository
            # conventions block, capped by the host, and a target repo's
            # `.claude/settings.json` (hooks, permission rules) must not
            # reconfigure an unattended session under it.
            "setting_sources": [],
        }
        if job.model and job.model != "auto":
            kwargs["model"] = job.model
        if job.cwd:
            kwargs["cwd"] = job.cwd
        if resume:
            kwargs["resume"] = resume
        servers: dict[str, Any] = {}
        if job.host_tools:
            if not job.host_tools_dir:
                raise RuntimeError("host_tools need host_tools_dir")
            servers[HOST_TOOL_SERVER] = self._host_tool_server(job, emit)
        # The operator's servers alongside the in-process one, never
        # instead of it: the host tools ARE an MCP server here, and
        # replacing the dict would silently take the run's own tools away.
        servers.update(server_configs(job.mcp_servers, stdio_type=MCP_STDIO_TYPE))
        if servers:
            kwargs["mcp_servers"] = servers
        if job.permission_mode == "auto" and governor.cap is None and job.available_tools is None:
            # The microVM (network policy + secret proxy) is the security
            # boundary; inside it the agent runs unattended. Any of a
            # tool-call cap, a tool restriction, or read-only mode needs the
            # permission callback instead.
            kwargs["permission_mode"] = "bypassPermissions"
        else:
            kwargs["can_use_tool"] = self._permission_handler(job, emit, tracker, governor)
        return ClaudeAgentOptions(**kwargs)

    def _permission_handler(
        self,
        job: JobRequest,
        emit: EmitFn,
        tracker: SessionHealthTracker,
        governor: ToolCallGovernor,
    ) -> Any:
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        async def can_use_tool(tool_name: str, input_data: Any, context: Any) -> Any:
            nudge = governor.decide()
            if nudge is not None:
                if governor.denied == 1:
                    emit(
                        EventTypes.AGENT_TOOL_CAP,
                        cap=governor.cap,
                        calls=governor.calls,
                        tool=tool_name,
                    )
                return PermissionResultDeny(message=nudge)
            feedback = unavailable_denial(tool_name, job.available_tools)
            if feedback is None and job.permission_mode == "read_only":
                feedback = read_only_denial(tool_name)
            if feedback is not None:
                # Denials must leave a trace: the session's health tally and
                # the event stream both record them, so a critic that lost
                # capabilities is auditable after the fact (#123).
                tracker.record_denial(tool_name, tool_call_id=getattr(context, "tool_use_id", None))
                emit(EventTypes.AGENT_PERMISSION_DENIED, kind=tool_name, feedback=feedback)
                return PermissionResultDeny(message=feedback)
            return PermissionResultAllow()

        return can_use_tool

    @staticmethod
    def _system_prompt(job: JobRequest) -> Any:
        if not job.system_preset:
            return job.system_message
        if job.system_message:
            return {"type": "preset", "preset": "claude_code", "append": job.system_message}
        return {"type": "preset", "preset": "claude_code"}

    def _host_tool_server(self, job: JobRequest, emit: EmitFn) -> Any:
        """An in-process MCP server whose tools round-trip to the host."""
        from claude_agent_sdk import create_sdk_mcp_server
        from claude_agent_sdk import tool as sdk_tool

        assert job.host_tools_dir is not None
        tools = [
            self._host_tool(spec, job.host_tools_dir, job, emit, sdk_tool)
            for spec in job.host_tools
        ]
        return create_sdk_mcp_server(name=HOST_TOOL_SERVER, version="1.0.0", tools=tools)

    @staticmethod
    def _host_tool(
        spec: HostToolSpec, tools_dir: str, job: JobRequest, emit: EmitFn, sdk_tool: Any
    ) -> Any:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            call = HostToolCall(
                call_id=safe_call_id(None),
                name=spec.name,
                arguments=dict(args or {}),
            )
            try:
                response = await asyncio.to_thread(
                    request_tool, emit, tools_dir, call, job.host_tool_timeout_s
                )
            except HostToolTimeout as exc:
                return {"content": [{"type": "text", "text": str(exc)}], "is_error": True}
            if response.ok:
                return {"content": [{"type": "text", "text": response.text}]}
            return {
                "content": [
                    {
                        "type": "text",
                        "text": response.text or response.error or f"{spec.name} failed",
                    }
                ],
                "is_error": True,
            }

        return sdk_tool(spec.name, spec.description, spec.parameters)(handler)


class _SessionState:
    """Folds the SDK's message stream into events and the final result."""

    def __init__(
        self,
        job: JobRequest,
        emit: EmitFn,
        tracker: SessionHealthTracker,
        governor: ToolCallGovernor,
    ) -> None:
        self.job = job
        self.emit = emit
        self.tracker = tracker
        self.governor = governor
        self.registry = ToolCallRegistry()
        self.final_text: list[str] = []
        self.result_text: str | None = None
        self.session_id: str | None = None
        self.usage = Usage()
        self.turns: int | None = None
        self.saw_output = False
        self.model_slug = job.model if job.model and job.model != "auto" else None

    def handle(self, message: Any) -> None:
        name = type(message).__name__
        if name == "AssistantMessage":
            self.saw_output = True
            model = getattr(message, "model", None) or self.model_slug
            if isinstance(model, str):
                self.model_slug = model
            for block in getattr(message, "content", None) or []:
                self._assistant_block(block)
        elif name == "UserMessage":
            for block in self._blocks(message):
                if type(block).__name__ == "ToolResultBlock":
                    self._tool_result(block)
        elif name == "ResultMessage":
            self._result(message)

    @staticmethod
    def _blocks(message: Any) -> list[Any]:
        content = getattr(message, "content", None)
        return content if isinstance(content, list) else []

    def _assistant_block(self, block: Any) -> None:
        kind = type(block).__name__
        if kind == "TextBlock":
            text = getattr(block, "text", "") or ""
            if text:
                self.final_text.append(text)
                self.emit(
                    EventTypes.AGENT_MESSAGE,
                    content=text,
                    model=self.model_slug,
                    backend=BACKEND_NAME,
                )
        elif kind == "ToolUseBlock":
            tool = getattr(block, "name", None)
            call_id = getattr(block, "id", None)
            args = _render_args(getattr(block, "input", None))
            self.registry.start(call_id, tool, args)
            self.emit(EventTypes.AGENT_TOOL_START, tool=tool, tool_call_id=call_id, args=args)

    def _tool_result(self, block: Any) -> None:
        call_id = getattr(block, "tool_use_id", None)
        tool, args, duration_ms = self.registry.end(call_id)
        is_error = getattr(block, "is_error", None)
        success = None if is_error is None else not is_error
        raw = _result_text(getattr(block, "content", None))
        output = None if raw is None else excerpt_output(raw)
        self.tracker.record_tool_end(tool, success, tool_call_id=call_id)
        self.emit(
            EventTypes.AGENT_TOOL_END,
            tool_call_id=call_id,
            tool=tool,
            args=args,
            success=success,
            exit_code=None,
            output=output,
            error=output if success is False else None,
            output_lines=None if raw is None else len(raw.splitlines()),
            duration_ms=duration_ms,
        )

    def _result(self, message: Any) -> None:
        self.saw_output = True
        self.session_id = getattr(message, "session_id", None) or self.session_id
        result = getattr(message, "result", None)
        if isinstance(result, str) and result.strip():
            self.result_text = result
        turns = getattr(message, "num_turns", None)
        if isinstance(turns, int):
            self.turns = turns
        sample = usage_from_result(message, self.model_slug)
        if sample != Usage():
            self.usage = self.usage.merged(sample)
            payload = sample.model_dump(exclude_none=True)
            payload["backend"] = BACKEND_NAME
            self.emit(EventTypes.AGENT_USAGE, **payload)
