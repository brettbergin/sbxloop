"""Codex SDK backend with every executable tool governed by the worker.

The pinned Python SDK owns the model loop, streaming and session state.
Native environment/hosted capabilities are disabled: only explicit dynamic
tools can act, so read-only sessions, concierge capabilities and tool ceilings
are enforced before side effects. Models may compose these tools in Codex's
pure JavaScript Code Mode; the same callback governs every nested action.
No SDK listener or MCP server is needed.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from typing import Any

from sbxloop_worker._json import extract_json
from sbxloop_worker.backends import BackendResult, EmitFn
from sbxloop_worker.backends.codex_runtime import SDK_VERSION, authenticated_client, ensure_runtime
from sbxloop_worker.backends.codex_tools import LocalTool, local_tools
from sbxloop_worker.backends.copilot import (
    SessionHealthTracker,
    ToolCallGovernor,
    ToolCallRegistry,
    excerpt_output,
)
from sbxloop_worker.hosttools import HostToolTimeout, request_tool, safe_call_id
from sbxloop_worker.protocol import EventTypes, HostToolCall, HostToolSpec, JobRequest, Usage
from sbxloop_worker.secrets import redact_secrets

BACKEND_NAME = "codex"


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(by_alias=True, mode="json")
        if isinstance(dumped, dict):
            return dumped
    return {}


def _dynamic_spec(spec: HostToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "name": spec.name,
        "description": spec.description,
        "inputSchema": spec.parameters,
        "deferLoading": False,
    }


def _clean(text: str) -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        text = text.replace(key, "[REDACTED]")
    return redact_secrets(text)


def _session_fingerprint(job: JobRequest, specs: list[HostToolSpec]) -> str:
    """Bind persisted SDK tools to the capabilities currently authorized.

    Codex cannot replace dynamic tool definitions when resuming a thread.
    The protocol's opaque session handle carries their fingerprint so a
    changed tool roster/schema, persona or workspace starts fresh instead.
    """
    manifest = {
        "sdk": SDK_VERSION,
        "tools": [spec.model_dump(mode="json") for spec in specs],
        "permission_mode": job.permission_mode,
        "system_preset": job.system_preset,
        "system_message": job.system_message,
        "cwd": job.cwd,
    }
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def _resume_thread_id(handle: str | None, fingerprint: str) -> str | None:
    if handle is None:
        return None
    parts = handle.split(":")
    if len(parts) != 3 or parts[0] != "codex-v1" or parts[2] != fingerprint or not parts[1]:
        return None
    return parts[1]


class CodexBackend:
    name = BACKEND_NAME

    def ensure_available(self) -> None:
        ensure_runtime()

    def run_session(self, job: JobRequest, emit: EmitFn) -> BackendResult:
        if job.mcp_servers:
            raise RuntimeError(
                "Codex does not support native MCP servers; remove native [[mcp]] entries "
                "or select a backend that supports them. Credentialed HTTP MCP must be "
                "mediated into host tools before dispatch."
            )
        self.ensure_available()
        deadline = time.monotonic() + job.timeout_s
        state = _SessionState(job, emit, deadline)
        fingerprint = _session_fingerprint(job, state.specs)
        resume_id = _resume_thread_id(job.resume_session_id, fingerprint)
        with authenticated_client(
            job.cwd,
            timeout_s=max(0.0, deadline - time.monotonic()),
            approval_handler=state.handle_request,
        ) as client:
            params: dict[str, Any] = {
                "modelProvider": "openai",
                "approvalPolicy": "never",
                "sandbox": "read-only",
            }
            if job.cwd:
                params["cwd"] = job.cwd
            if job.model and job.model != "auto":
                params["model"] = job.model
            if job.system_preset:
                if job.system_message:
                    params["developerInstructions"] = job.system_message
            else:
                params["baseInstructions"] = job.system_message or ""
            opened = None
            if resume_id:
                try:
                    opened = client.thread_resume(resume_id, params)
                except Exception:
                    # No turn was started and no tool could execute. Expired
                    # histories are an optimization miss, never lost work.
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired("codex", job.timeout_s) from None
            if opened is None:
                opened = client.thread_start(
                    {
                        **params,
                        "environments": [],
                        "dynamicTools": [_dynamic_spec(spec) for spec in state.specs],
                        "ephemeral": False,
                    }
                )
            state.model = getattr(opened, "model", None) or state.model
            session_id = opened.thread.id
            assert job.prompt is not None
            turn = client.turn_start(session_id, job.prompt, {"environments": []})
            turn_id = turn.turn.id
            try:
                while True:
                    event = client.next_turn_notification(turn_id)
                    if state.handle_event(event):
                        break
            finally:
                client.unregister_turn_notifications(turn_id)
            return BackendResult(
                output_text=state.final_text,
                output_json=extract_json(state.final_text) if job.expect == "json" else None,
                session_id=f"codex-v1:{session_id}:{fingerprint}",
                usage=state.usage if state.samples else None,
                # Token notifications can also accompany compaction. They
                # do not establish a count of model turns.
                turns=None,
                health=state.tracker.health(state.governor),
            )


class _SessionState:
    def __init__(self, job: JobRequest, emit: EmitFn, deadline: float) -> None:
        self.job, self.emit, self.deadline = job, emit, deadline
        self.tracker = SessionHealthTracker()
        self.governor = ToolCallGovernor(job.max_tool_calls)
        self.registry = ToolCallRegistry()
        local = local_tools(job, deadline=deadline)
        self.local: dict[str, LocalTool] = {tool.spec.name: tool for tool in local}
        self.host = {spec.name: spec for spec in job.host_tools}
        if len(self.host) != len(job.host_tools) or self.local.keys() & self.host.keys():
            raise ValueError("Codex tool names must be unique across local and host tools")
        if self.host and not job.host_tools_dir:
            raise ValueError("host_tools need host_tools_dir")
        self.specs = [*(tool.spec for tool in local), *job.host_tools]
        self.model = job.model if job.model and job.model != "auto" else None
        self.usage = Usage(backend=BACKEND_NAME)
        self.samples = 0
        self.last_total: dict[str, Any] | None = None
        self.final_text = ""
        self.saw_final_answer = False
        self.messages: set[str] = set()
        self.responses: dict[str, tuple[str, dict[str, Any]]] = {}

    def handle_request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        if method != "item/tool/call" or not isinstance(params, dict):
            raise RuntimeError(f"unexpected native Codex server request: {method}")
        if time.monotonic() >= self.deadline:
            raise subprocess.TimeoutExpired("codex", self.job.timeout_s)
        name = params.get("tool")
        arguments = params.get("arguments")
        original_id = params.get("callId")
        if (
            not isinstance(name, str)
            or not isinstance(arguments, dict)
            or not isinstance(original_id, str)
        ):
            raise RuntimeError("malformed Codex dynamic tool request")
        signature = json.dumps([params.get("namespace"), name, arguments], sort_keys=True)
        cached = self.responses.get(original_id)
        if cached is not None:
            if cached[0] != signature:
                raise RuntimeError("Codex reused a tool call id for different arguments")
            return cached[1]
        call_id = safe_call_id(original_id)
        args = self._args(arguments)
        self.registry.start(call_id, name, args)
        self.emit(EventTypes.AGENT_TOOL_START, tool=name, tool_call_id=call_id, args=args)
        nudge = self.governor.decide()
        exit_code: int | None = None
        count_failure = True
        if nudge is not None:
            text, success, count_failure = nudge, False, False
            if self.governor.denied == 1:
                self.emit(
                    EventTypes.AGENT_TOOL_CAP,
                    cap=self.governor.cap,
                    calls=self.governor.calls,
                    tool=name,
                )
        elif params.get("namespace") not in (None, "functions") or (
            name not in self.local and name not in self.host
        ):
            text, success = f"The {name!r} tool is not available in this session.", False
            self.tracker.record_denial(name, call_id)
            self.emit(EventTypes.AGENT_PERMISSION_DENIED, kind=name, feedback=text)
        else:
            try:
                if name in self.host:
                    assert self.job.host_tools_dir is not None
                    response = request_tool(
                        self.emit,
                        self.job.host_tools_dir,
                        HostToolCall(call_id=call_id, name=name, arguments=arguments),
                        min(
                            self.job.host_tool_timeout_s, max(0.0, self.deadline - time.monotonic())
                        ),
                    )
                    text, success = response.text or response.error or "", response.ok
                else:
                    text, success = self.local[name].invoke(arguments), True
                    if name == "shell":
                        exit_code = json.loads(text)["exit_code"]
                        success = exit_code == 0
            except HostToolTimeout as exc:
                if time.monotonic() >= self.deadline:
                    raise subprocess.TimeoutExpired("codex", self.job.timeout_s) from None
                text, success = str(exc), False
            except TimeoutError as exc:
                if time.monotonic() >= self.deadline:
                    raise subprocess.TimeoutExpired("codex", self.job.timeout_s) from None
                text, success = str(exc), False
            except Exception as exc:
                text, success = str(exc), False
        text = _clean(text)
        _, _, duration_ms = self.registry.end(call_id)
        if count_failure:
            self.tracker.record_tool_end(name, success, call_id)
        output = excerpt_output(text)
        self.emit(
            EventTypes.AGENT_TOOL_END,
            tool=name,
            tool_call_id=call_id,
            args=args,
            success=success,
            exit_code=exit_code,
            output=output,
            error=output if not success else None,
            output_lines=len(text.splitlines()),
            duration_ms=duration_ms,
        )
        result = {"contentItems": [{"type": "inputText", "text": text}], "success": success}
        self.responses[original_id] = (signature, result)
        return result

    @staticmethod
    def _args(arguments: dict[str, Any]) -> str | None:
        if not arguments:
            return None
        for key in ("command", "path", "pattern"):
            if isinstance(arguments.get(key), str):
                return _clean(arguments[key])[:400]
        return _clean(json.dumps(arguments, separators=(",", ":")))[:400]

    def handle_event(self, event: Any) -> bool:
        method = getattr(event, "method", "")
        payload = _object(getattr(event, "payload", None))
        if method == "item/agentMessage/delta":
            delta = payload.get("delta")
            if isinstance(delta, str):
                self.emit(EventTypes.AGENT_MESSAGE_DELTA, delta=_clean(delta), backend=BACKEND_NAME)
        elif method in ("item/started", "item/completed"):
            item = _object(payload.get("item"))
            self._check_item(item)
            if method == "item/completed":
                self._message(item)
        elif method == "thread/tokenUsage/updated":
            self._usage(_object(payload.get("tokenUsage")))
        elif method == "turn/completed":
            turn = _object(payload.get("turn"))
            if turn.get("status") != "completed":
                detail = _object(turn.get("error")).get("message") or turn.get("status")
                raise RuntimeError(f"Codex turn did not complete: {_clean(str(detail))}")
            for item in turn.get("items") or []:
                obj = _object(item)
                self._check_item(obj)
                self._message(obj)
            if not self.final_text:
                raise RuntimeError("Codex completed the turn without a final answer")
            return True
        return False

    @staticmethod
    def _check_item(item: dict[str, Any]) -> None:
        allowed = {
            "userMessage",
            "agentMessage",
            "reasoning",
            "contextCompaction",
            "dynamicToolCall",
        }
        if item.get("type") not in allowed:
            raise RuntimeError(
                f"unexpected native Codex item: {item.get('type')!r}; "
                "the configured tool boundary was not honored"
            )

    def _message(self, item: dict[str, Any]) -> None:
        if item.get("type") != "agentMessage":
            return
        item_id, text = item.get("id"), item.get("text")
        if not isinstance(text, str) or not text or item_id in self.messages:
            return
        if isinstance(item_id, str):
            self.messages.add(item_id)
        text = _clean(text)
        self.emit(EventTypes.AGENT_MESSAGE, content=text, model=self.model, backend=BACKEND_NAME)
        if item.get("phase") == "final_answer":
            self.saw_final_answer = True
            self.final_text = text
        elif item.get("phase") is None and not self.saw_final_answer:
            self.final_text = text

    def _usage(self, payload: dict[str, Any]) -> None:
        total, last = _object(payload.get("total")), _object(payload.get("last"))
        if not total or not last or total == self.last_total:
            return

        def count(key: str) -> int | None:
            current = total.get(key)
            previous = self.last_total.get(key) if self.last_total else None
            if type(current) is int and type(previous) is int and current >= previous:
                return current - previous
            value = last.get(key)
            return value if type(value) is int and value >= 0 else None

        sample = Usage(
            backend=BACKEND_NAME,
            model=self.model,
            input_tokens=count("inputTokens"),
            output_tokens=count("outputTokens"),
            cache_read_tokens=count("cachedInputTokens"),
            cache_write_tokens=count("cacheWriteInputTokens"),
        )
        self.last_total = total
        self.samples += 1
        self.usage = self.usage.merged(sample)
        self.emit(EventTypes.AGENT_USAGE, **sample.model_dump(exclude_none=True))
