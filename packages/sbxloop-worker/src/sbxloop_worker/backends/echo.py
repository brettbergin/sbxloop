"""Deterministic test backend.

Without a script it echoes the prompt. With ``SBXLOOP_ECHO_SCRIPT`` pointing
at a JSON file (a list of response objects), responses are consumed in order
— across worker processes — via a sidecar ``<script>.state`` cursor file, so
multi-job engine tests can script an entire run.

Response object shape (all fields optional):
``{"text": str, "json": dict|list, "session_id": str, "sleep_s": float,
"events": [{"type": str, "data": {...}}], "fail": str,
"files": {"relative/path": "content"},
"host_tool_calls": [{"name": str, "arguments": {...}, "call_id": str}],
"health": {"permission_denials": {...}, "tool_failures": {...}}}``

``files`` are written relative to the worker process cwd — modelling an
executor that produces artifacts in the run workspace. ``host_tool_calls``
go through the real host-tool round trip (``sbxloop_worker.hosttools``):
each response's text is appended to the output, so a host round-trip test
can assert what the "model" saw; a host timeout fails the job like any
other backend error.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from sbxloop_worker._json import extract_json
from sbxloop_worker.backends import BackendResult, EmitFn
from sbxloop_worker.hosttools import request_tool, safe_call_id
from sbxloop_worker.protocol import EventTypes, HostToolCall, JobRequest, SessionHealth, Usage

# Backend identity stamped onto agent events and usage samples, so chat
# can name provider+model.
BACKEND_NAME = "echo"

SCRIPT_ENV = "SBXLOOP_ECHO_SCRIPT"


class EchoBackend:
    name = "echo"

    def ensure_available(self) -> None:
        """Nothing to install: the point of this backend is that it runs
        wherever the worker itself does."""

    def run_session(self, job: JobRequest, emit: EmitFn) -> BackendResult:
        scripted = self._next_scripted_response()
        if scripted is None:
            return self._echo(job, emit)
        return self._scripted(job, emit, scripted)

    # -- default echo mode -------------------------------------------------

    def _echo(self, job: JobRequest, emit: EmitFn) -> BackendResult:
        assert job.prompt is not None
        text = f"echo: {job.prompt}"
        emit(EventTypes.AGENT_MESSAGE, content=text, model="echo", backend=BACKEND_NAME)
        output_json = {"echo": job.prompt} if job.expect == "json" else None
        return BackendResult(
            output_text=text,
            output_json=output_json,
            session_id=f"echo-{job.job_id}",
            usage=Usage(
                model="echo",
                backend=BACKEND_NAME,
                input_tokens=len(job.prompt.split()),
                output_tokens=2,
            ),
            turns=1,
        )

    # -- scripted mode -----------------------------------------------------

    def _next_scripted_response(self) -> dict[str, Any] | None:
        raw = os.environ.get(SCRIPT_ENV)
        if not raw:
            return None
        script_path = Path(raw)
        responses = json.loads(script_path.read_text())
        state_path = script_path.with_suffix(script_path.suffix + ".state")
        cursor = int(state_path.read_text()) if state_path.is_file() else 0
        if cursor >= len(responses):
            raise RuntimeError(
                f"echo script exhausted: {cursor} responses consumed, job wanted one more"
            )
        state_path.write_text(str(cursor + 1))
        response = responses[cursor]
        if not isinstance(response, dict):
            raise TypeError(f"echo script entry {cursor} must be an object")
        return response

    def _scripted(self, job: JobRequest, emit: EmitFn, response: dict[str, Any]) -> BackendResult:
        if "fail" in response:
            raise RuntimeError(str(response["fail"]))
        if sleep_s := float(response.get("sleep_s", 0)):
            time.sleep(sleep_s)
        for relative, content in response.get("files", {}).items():
            target = Path.cwd() / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content))
        for scripted_event in response.get("events", []):
            emit(scripted_event["type"], **scripted_event.get("data", {}))
        text = str(response.get("text", ""))
        for tool_text in self._host_tool_calls(job, emit, response.get("host_tool_calls", [])):
            text = f"{text}\n{tool_text}" if text else tool_text
        if text:
            emit(EventTypes.AGENT_MESSAGE, content=text, model="echo", backend=BACKEND_NAME)
        output_json = response.get("json")
        if output_json is None and job.expect == "json":
            output_json = extract_json(text)
        health = response.get("health")
        return BackendResult(
            output_text=text,
            output_json=output_json,
            session_id=str(response.get("session_id", f"echo-{job.job_id}")),
            usage=Usage(
                model="echo",
                backend=BACKEND_NAME,
                input_tokens=len(job.prompt.split()) if job.prompt else 0,
                output_tokens=len(text.split()),
            ),
            turns=1,
            health=SessionHealth.model_validate(health) if health is not None else None,
        )

    @staticmethod
    def _host_tool_calls(job: JobRequest, emit: EmitFn, calls: list[dict[str, Any]]) -> list[str]:
        if not calls:
            return []
        if not job.host_tools_dir:
            raise RuntimeError("echo script has host_tool_calls but the job has no host_tools_dir")
        texts: list[str] = []
        for entry in calls:
            call = HostToolCall(
                call_id=safe_call_id(entry.get("call_id")),
                name=str(entry["name"]),
                arguments=dict(entry.get("arguments", {})),
            )
            response = request_tool(emit, job.host_tools_dir, call, job.host_tool_timeout_s)
            if response.ok:
                texts.append(response.text)
            else:
                texts.append(f"[{call.name} failed: {response.error}]")
        return texts
