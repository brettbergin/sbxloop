"""Deterministic test backend.

Without a script it echoes the prompt. With ``SDXLOOP_ECHO_SCRIPT`` pointing
at a JSON file (a list of response objects), responses are consumed in order
— across worker processes — via a sidecar ``<script>.state`` cursor file, so
multi-job engine tests can script an entire run.

Response object shape (all fields optional):
``{"text": str, "json": dict|list, "session_id": str, "sleep_s": float,
"events": [{"type": str, "data": {...}}], "fail": str}``
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from sdxloop_worker._json import extract_json
from sdxloop_worker.backends import BackendResult, EmitFn
from sdxloop_worker.protocol import EventTypes, JobRequest, Usage

SCRIPT_ENV = "SDXLOOP_ECHO_SCRIPT"


class EchoBackend:
    name = "echo"

    def run_session(self, job: JobRequest, emit: EmitFn) -> BackendResult:
        scripted = self._next_scripted_response()
        if scripted is None:
            return self._echo(job, emit)
        return self._scripted(job, emit, scripted)

    # -- default echo mode -------------------------------------------------

    def _echo(self, job: JobRequest, emit: EmitFn) -> BackendResult:
        assert job.prompt is not None
        text = f"echo: {job.prompt}"
        emit(EventTypes.AGENT_MESSAGE, content=text)
        output_json = {"echo": job.prompt} if job.expect == "json" else None
        return BackendResult(
            output_text=text,
            output_json=output_json,
            session_id=f"echo-{job.job_id}",
            usage=Usage(model="echo", input_tokens=len(job.prompt.split()), output_tokens=2),
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
        for scripted_event in response.get("events", []):
            emit(scripted_event["type"], **scripted_event.get("data", {}))
        text = str(response.get("text", ""))
        if text:
            emit(EventTypes.AGENT_MESSAGE, content=text)
        output_json = response.get("json")
        if output_json is None and job.expect == "json":
            output_json = extract_json(text)
        return BackendResult(
            output_text=text,
            output_json=output_json,
            session_id=str(response.get("session_id", f"echo-{job.job_id}")),
            usage=Usage(model="echo"),
        )
