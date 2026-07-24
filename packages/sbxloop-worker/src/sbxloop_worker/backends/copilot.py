"""GitHub Copilot SDK backend.

Runs one agentic session inside the sandbox via the ``github-copilot-sdk``
package (installed with the ``[copilot]`` extra; wheels bundle the Copilot
CLI runtime). Imports are deferred so the worker package works without the
SDK for shell/github jobs and for test environments.

Auth: the SDK auto-detects ``COPILOT_GITHUB_TOKEN``, which the sbxloop
provisioner injects into the agent sandbox (proxy-bound to the Copilot API
hosts under the default secret strategy).

This module is exercised by the real-sbx e2e workflow rather than unit
tests (it needs the SDK runtime + a Copilot subscription), and is excluded
from unit coverage accordingly.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from sbxloop_worker._json import extract_json
from sbxloop_worker.backends import BackendResult, BackendUnavailableError, EmitFn
from sbxloop_worker.protocol import EventTypes, JobRequest, Usage

READ_ONLY_DENIED_KINDS = {"shell", "write"}

_TOKEN_PREFIXES = ("gho_", "ghu_", "github_pat_")


def _auth_diagnostic() -> str:
    """Describe the COPILOT_GITHUB_TOKEN this process can actually see."""
    token = os.environ.get("COPILOT_GITHUB_TOKEN", "")
    if not token:
        return (
            "auth diagnostic: COPILOT_GITHUB_TOKEN is NOT set in the worker "
            "environment - sbx secret injection did not reach this process; "
            'try secret_strategy="plain-env"'
        )
    if token.startswith(_TOKEN_PREFIXES):
        return (
            "auth diagnostic: COPILOT_GITHUB_TOKEN is set with a recognized "
            "format; the failure is likely subscription/permissions "
            "(the PAT needs the Copilot Requests permission) or network policy"
        )
    return (
        f"auth diagnostic: COPILOT_GITHUB_TOKEN is set but has an unrecognized "
        f"format ({token[:6]}...) - likely the sbx secret-proxy sentinel. The "
        "Copilot SDK validates token format client-side and cannot use proxy "
        'sentinels; set secret_strategy="plain-env" for the agent sandbox'
    )


class CopilotBackend:
    name = "copilot"

    def run_session(self, job: JobRequest, emit: EmitFn) -> BackendResult:
        try:
            import copilot  # noqa: F401
        except ImportError as exc:
            raise BackendUnavailableError(
                "github-copilot-sdk is not installed; install sbxloop-worker[copilot]"
            ) from exc
        try:
            return asyncio.run(self._run(job, emit))
        except Exception as exc:
            # Auth failures surface as opaque SDK errors ("Session was not
            # created with authentication info..."); say what the token
            # environment actually looks like from inside the sandbox.
            raise RuntimeError(f"{exc} | {_auth_diagnostic()}") from exc

    async def _run(self, job: JobRequest, emit: EmitFn) -> BackendResult:
        from copilot import CopilotClient

        usage = Usage()
        final_text: list[str] = []

        def on_event(event: Any) -> None:
            nonlocal usage
            data = getattr(event, "data", None)
            type_name = type(data).__name__ if data is not None else type(event).__name__
            if type_name == "AssistantMessageDeltaData":
                emit(
                    EventTypes.AGENT_MESSAGE_DELTA,
                    delta=getattr(data, "delta_content", "") or "",
                )
            elif type_name == "AssistantMessageData":
                content = getattr(data, "content", "") or ""
                if content:
                    final_text.append(content)
                emit(EventTypes.AGENT_MESSAGE, content=content)
            elif type_name.startswith("ToolExecutionStart"):
                emit(
                    EventTypes.AGENT_TOOL_START,
                    tool=getattr(data, "tool_name", None) or getattr(data, "toolName", None),
                    tool_call_id=getattr(data, "tool_call_id", None)
                    or getattr(data, "toolCallId", None),
                )
            elif type_name.startswith("ToolExecutionComplete"):
                emit(
                    EventTypes.AGENT_TOOL_END,
                    tool_call_id=getattr(data, "tool_call_id", None)
                    or getattr(data, "toolCallId", None),
                    success=getattr(data, "success", None),
                )
            elif type_name == "AssistantUsageData":
                sample = Usage(
                    model=getattr(data, "model", None),
                    input_tokens=getattr(data, "input_tokens", None)
                    or getattr(data, "inputTokens", None),
                    output_tokens=getattr(data, "output_tokens", None)
                    or getattr(data, "outputTokens", None),
                )
                usage = usage.merged(sample)
                emit(EventTypes.AGENT_USAGE, **sample.model_dump(exclude_none=True))

        async with CopilotClient() as client:
            session = await self._open_session(client, job)
            try:
                session.on(on_event)
                assert job.prompt is not None
                response = await session.send_and_wait(job.prompt, timeout=job.timeout_s)
                text = self._response_text(response) or "\n".join(final_text)
                output_json = extract_json(text) if job.expect == "json" else None
                session_id = getattr(session, "session_id", None) or getattr(session, "id", None)
                return BackendResult(
                    output_text=text,
                    output_json=output_json,
                    session_id=session_id,
                    usage=usage if usage != Usage() else None,
                )
            finally:
                await self._close_session(session)

    async def _open_session(self, client: Any, job: JobRequest) -> Any:
        kwargs: dict[str, Any] = {
            "on_permission_request": self._permission_handler(job),
            "streaming": True,
        }
        if job.model and job.model != "auto":
            kwargs["model"] = job.model
        if job.system_message:
            kwargs["system_message"] = {"mode": "append", "content": job.system_message}
        if job.resume_session_id:
            return await client.resume_session(job.resume_session_id, **kwargs)
        return await client.create_session(**kwargs)

    def _permission_handler(self, job: JobRequest) -> Any:
        if job.permission_mode == "auto":
            # The microVM (network policy + secret proxy) is the security
            # boundary; inside it the agent runs unattended.
            from copilot.session import PermissionHandler

            return PermissionHandler.approve_all

        def read_only_handler(request: Any, invocation: Any = None) -> Any:
            from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

            kind = getattr(request, "kind", None)
            if kind in READ_ONLY_DENIED_KINDS:
                return PermissionDecisionReject(
                    feedback="this is a read-only review session; do not modify anything"
                )
            return PermissionDecisionApproveOnce()

        return read_only_handler

    @staticmethod
    def _response_text(response: Any) -> str:
        data = getattr(response, "data", None)
        return getattr(data, "content", None) or getattr(response, "content", None) or ""

    @staticmethod
    async def _close_session(session: Any) -> None:
        disconnect = getattr(session, "disconnect", None)
        if disconnect is not None:
            try:
                result = disconnect()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
