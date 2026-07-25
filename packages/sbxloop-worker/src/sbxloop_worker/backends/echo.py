"""Deterministic test backend.

Without a script it echoes the prompt. With ``SBXLOOP_ECHO_SCRIPT`` pointing
at a JSON file (a list of response objects), responses are consumed in order
— across worker processes — via a sidecar ``<script>.state`` cursor file, so
multi-job engine tests can script an entire run.

Response object shape (all fields optional):
``{"text": str, "json": dict|list, "session_id": str, "sleep_s": float,
"events": [{"type": str, "data": {...}}], "fail": str,
"files": {"relative/path": "content"}, "match": str}``

``files`` are written relative to the worker process cwd — modelling an
executor that produces artifacts in the run workspace.

``match`` restricts an entry to jobs whose prompt contains the substring
(e.g. a task id) — this is what makes scripting deterministic when the
engine runs tasks concurrently and job order is no longer serial.
Consumption is atomic across worker processes: the cursor file records
consumed indices and is updated under an exclusive flock.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any

from sbxloop_worker._json import extract_json
from sbxloop_worker.backends import BackendResult, EmitFn
from sbxloop_worker.protocol import EventTypes, JobRequest, Usage

SCRIPT_ENV = "SBXLOOP_ECHO_SCRIPT"


class EchoBackend:
    name = "echo"

    def run_session(self, job: JobRequest, emit: EmitFn) -> BackendResult:
        scripted = self._next_scripted_response(job.prompt or "")
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

    def _next_scripted_response(self, prompt: str) -> dict[str, Any] | None:
        raw = os.environ.get(SCRIPT_ENV)
        if not raw:
            return None
        script_path = Path(raw)
        responses = json.loads(script_path.read_text())
        state_path = script_path.with_suffix(script_path.suffix + ".state")
        lock_path = script_path.with_suffix(script_path.suffix + ".lock")
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            consumed = self._consumed(state_path)
            index = self._pick(responses, consumed, prompt)
            if index is None:
                raise RuntimeError(
                    f"echo script exhausted: {len(consumed)} responses consumed, "
                    "job wanted one more"
                )
            consumed.append(index)
            state_path.write_text(json.dumps(consumed))
        response = responses[index]
        if not isinstance(response, dict):
            raise TypeError(f"echo script entry {index} must be an object")
        return response

    @staticmethod
    def _consumed(state_path: Path) -> list[int]:
        if not state_path.is_file():
            return []
        raw_state = state_path.read_text()
        with contextlib.suppress(ValueError):
            return list(range(int(raw_state)))  # legacy linear cursor
        indices = json.loads(raw_state)
        assert isinstance(indices, list)
        return indices

    @staticmethod
    def _pick(responses: list[Any], consumed: list[int], prompt: str) -> int | None:
        """First unconsumed entry whose ``match`` (if any) is in the prompt."""
        for index, response in enumerate(responses):
            if index in consumed:
                continue
            match = response.get("match") if isinstance(response, dict) else None
            if match is None or str(match) in prompt:
                return index
        return None

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
