"""ServiceOps — the host's side of the service sandbox (#765).

The service sandbox is the github sandbox's pattern generalized: it holds
the run's granted ``[[credentials]]`` and runs nothing but the fixed ops
the host submits. This module submits them. The agent never holds a
credential and never speaks to a credential's host: it asks the host
through the ``call_service`` host tool, the host checks the request
against the run's grant and submits one ``service.http`` job to the
service sandbox, and the response body — the credential's value redacted
wherever an API echoes it — goes back to the model as the tool result.

What the ledger sees is one ``service.call`` event per request: the
credential's name, the method, the path, the status and the duration.
Never a body, never a header, never a value.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from typing import Any, NoReturn

from sbxloop.config import CredentialConfig
from sbxloop.errors import ServiceOpsError
from sbxloop.events import EventBus, HostEventTypes
from sbxloop.ids import new_job_id
from sbxloop.log import get_logger
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import HostToolCall, HostToolResponse, HostToolSpec, JobRequest
from sbxloop_worker.serviceops import METHODS

log = get_logger(__name__)

TOOL_NAME = "call_service"

# What the model reads back per call. The worker already clips the body;
# this bounds the whole tool result, JSON framing included.
MAX_TOOL_TEXT = 80_000


class ServiceOps:
    """Fixed ops against the run's service sandbox.

    ``credentials`` is the run's grant — the ``[[credentials]]`` entries it
    may use, and the only names a call may name. The catalogue in the
    sandbox is built from the same list, so a name outside it is refused
    here — before a job is built — and would be refused in the sandbox too.
    """

    def __init__(
        self,
        client: WorkerClient,
        run_id: str,
        bus: EventBus,
        credentials: Sequence[CredentialConfig],
        *,
        timeout_s: float = 120.0,
    ) -> None:
        self.client = client
        self.run_id = run_id
        self.bus = bus
        self.catalogue: dict[str, CredentialConfig] = {c.name: c for c in credentials}
        self.credentials = tuple(self.catalogue)
        self.timeout_s = timeout_s

    # -- the op -------------------------------------------------------------

    def http(
        self,
        credential: str,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: Any = None,
        timeout_s: float | None = None,
        phase: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """One authenticated request; the worker's result dict
        (``status``, ``headers``, ``body``, ``truncated``, ``elapsed_s``).
        Raises :class:`ServiceOpsError` when the run was not granted
        ``credential`` or the sandbox refused / could not make the call."""
        method = str(method).upper()
        event: dict[str, Any] = {
            "credential": credential,
            "method": method,
            "path": path,
            "phase": phase,
            "task_id": task_id,
        }
        # A refusal is part of the run's chronology too: the model asked for
        # something the run was not granted, and the ledger says so.
        if credential not in self.credentials:
            granted = ", ".join(self.credentials) or "none"
            self._refuse(
                event,
                f"credential {credential!r} is not granted to run {self.run_id} "
                f"(granted: {granted})",
            )
        if method not in METHODS:
            self._refuse(event, f"unsupported method {method!r}; one of {sorted(METHODS)}")
        params: dict[str, Any] = {"credential": credential, "method": method, "path": path}
        if query:
            params["query"] = query
        if headers:
            params["headers"] = headers
        if body is not None and body != "":
            params["body"] = body
        if timeout_s is not None:
            params["timeout_s"] = timeout_s
        job = JobRequest(
            job_id=new_job_id(),
            run_id=self.run_id,
            kind="service.http",
            params=params,
            timeout_s=(timeout_s or 0) + self.timeout_s,
        )
        started = time.monotonic()
        result = self.client.submit(job)
        event["duration_s"] = round(time.monotonic() - started, 2)
        if result.status != "ok":
            assert result.error is not None
            event["error"] = f"{result.error.type}: {result.error.message}"
            self.bus.emit(HostEventTypes.SERVICE_CALL, self.run_id, job_id=job.job_id, **event)
            log.warning("service.call_failed", run=self.run_id, job=job.job_id, **event)
            raise ServiceOpsError(
                f"service call {method} {path} with {credential!r} failed: "
                f"{result.error.type}: {result.error.message}"
            )
        output = dict(result.output_json or {})
        event["status"] = output.get("status")
        self.bus.emit(HostEventTypes.SERVICE_CALL, self.run_id, job_id=job.job_id, **event)
        log.info("service.call", run=self.run_id, job=job.job_id, **event)
        return output

    def _refuse(self, event: dict[str, Any], reason: str) -> NoReturn:
        self.bus.emit(HostEventTypes.SERVICE_CALL, self.run_id, error=reason, **event)
        log.warning("service.call_refused", run=self.run_id, error=reason, **event)
        raise ServiceOpsError(reason)

    # -- the agent's host tool -----------------------------------------------

    def tool_spec(self) -> HostToolSpec:
        """The ``call_service`` tool as the agent session sees it: the
        granted credential names are the enum, so the model cannot even
        spell one it was not given."""
        names = list(self.credentials)
        granted = "; ".join(
            f"{c.name} → https://{c.host}" + (f" ({c.description})" if c.description else "")
            for c in self.catalogue.values()
        )
        return HostToolSpec(
            name=TOOL_NAME,
            description=(
                "Make one authenticated HTTP request through the run's service sandbox. "
                "You never hold the credential: name it, and the request is sent to "
                "that credential's own host with the credential attached. Returns the "
                "status, response headers and body (clipped). Credentials granted to "
                f"this run: {granted}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "credential": {
                        "type": "string",
                        "enum": names,
                        "description": "Which granted credential to send the request with.",
                    },
                    "method": {"type": "string", "enum": sorted(METHODS)},
                    "path": {
                        "type": "string",
                        "description": "Absolute path on the credential's host, e.g. /v1/items.",
                    },
                    "query": {
                        "type": "object",
                        "description": "Query parameters (string values).",
                        "additionalProperties": {"type": "string"},
                    },
                    "headers": {
                        "type": "object",
                        "description": (
                            "Extra request headers; the credential's own header is set for you."
                        ),
                        "additionalProperties": {"type": "string"},
                    },
                    "body": {
                        "description": (
                            "Request body: an object/array is sent as JSON, a string as-is."
                        ),
                    },
                },
                "required": ["credential", "method", "path"],
            },
        )

    def handler(
        self, *, phase: str | None = None, task_id: str | None = None
    ) -> Callable[[HostToolCall], HostToolResponse]:
        """A host-tool handler bound to the phase that runs it."""

        def handle(call: HostToolCall) -> HostToolResponse:
            return self.handle(call, phase=phase, task_id=task_id)

        return handle

    def handle(
        self, call: HostToolCall, *, phase: str | None = None, task_id: str | None = None
    ) -> HostToolResponse:
        if call.name != TOOL_NAME:
            return HostToolResponse(
                call_id=call.call_id, ok=False, error=f"unknown host tool {call.name!r}"
            )
        args = call.arguments
        try:
            credential = str(args.get("credential", ""))
            method = str(args.get("method", ""))
            path = str(args.get("path", ""))
            query = args.get("query")
            headers = args.get("headers")
            if query is not None and not isinstance(query, dict):
                raise ServiceOpsError("query must be an object")
            if headers is not None and not isinstance(headers, dict):
                raise ServiceOpsError("headers must be an object")
            output = self.http(
                credential,
                method,
                path,
                query=query,
                headers={str(k): str(v) for k, v in headers.items()} if headers else None,
                body=args.get("body"),
                phase=phase,
                task_id=task_id,
            )
        except ServiceOpsError as exc:
            return HostToolResponse(call_id=call.call_id, ok=False, error=str(exc))
        text = json.dumps(
            {
                "status": output.get("status"),
                "headers": output.get("headers", {}),
                "body": output.get("body", ""),
                "truncated": bool(output.get("truncated")),
            },
            ensure_ascii=False,
        )
        if len(text) > MAX_TOOL_TEXT:
            text = text[:MAX_TOOL_TEXT] + "…"
        # A 4xx/5xx is an answer the model needs to read (the body says
        # why), so the tool call itself succeeded; only a request that never
        # completed is not ok.
        return HostToolResponse(call_id=call.call_id, ok=True, text=text)
