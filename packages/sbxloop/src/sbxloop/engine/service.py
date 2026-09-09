"""ServiceOps — the host's side of the service sandbox (#765, #766).

The service sandbox is the github sandbox's pattern generalized: it holds
the run's granted ``[[credentials]]`` and the credentials of the run's
``[[registries]]``, and runs nothing but the fixed ops the host submits.
This module submits them. The agent never holds a credential and never
speaks to a credential's host:

* ``call_service`` — the agent asks the host for one HTTP request; the
  host checks it against the run's grant and submits one ``service.http``
  job, and the response body — the credential's value redacted wherever
  an API echoes it — goes back to the model as the tool result.
* ``fetch_dependencies`` — catalogue discovery or a fixed authenticated
  download. The host copies the artifact into the agent; all native
  dependency resolution and project code run there without registry secrets.

The ledger records request names and result metadata, never artifact bytes
or authorization headers. No sandbox listens for another sandbox's calls.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn

from sbxloop.config import CredentialConfig, RegistryConfig, RegistryKind
from sbxloop.errors import SbxError, ServiceOpsError
from sbxloop.events import EventBus, HostEventTypes
from sbxloop.ids import new_job_id
from sbxloop.log import get_logger
from sbxloop.sbx import registries
from sbxloop.sbx.sandbox import RESULTS_DIR
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import (
    HostToolCall,
    HostToolResponse,
    HostToolSpec,
    JobRequest,
    RegistryFetchParams,
)
from sbxloop_worker.serviceops import METHODS

log = get_logger(__name__)

TOOL_NAME = "call_service"
FETCH_TOOL_NAME = "fetch_dependencies"
# Bound a large artifact download or an offline preparation command.
FETCH_TIMEOUT_S = 900.0

# What the model reads back per call. The worker already clips the body;
# this bounds the whole tool result, JSON framing included.
MAX_TOOL_TEXT = 80_000


class ServiceOps:
    """Fixed ops against the run's service sandbox.

    ``credentials`` is the run's grant — the ``[[credentials]]`` entries it
    may use, and the only names a call may name. The catalogue in the
    sandbox is built from the same list, so a name outside it is refused
    here — before a job is built — and would be refused in the sandbox too.
    ``registries_`` are the run's credentialed registries (#766), ``workdir``
    the agent sandbox's workspace for offline preparation, and
    ``workspace`` the host's (where the manifests are looked for); without
    registries there is nothing to fetch and no tool for it.
    """

    def __init__(
        self,
        client: WorkerClient,
        run_id: str,
        bus: EventBus,
        credentials: Sequence[CredentialConfig],
        registries_: Sequence[RegistryConfig] = (),
        *,
        workdir: str | None = None,
        workspace: Path | None = None,
        timeout_s: float = 120.0,
        fetch_timeout_s: float = FETCH_TIMEOUT_S,
        agent: WorkerClient | None = None,
    ) -> None:
        self.client = client
        self.run_id = run_id
        self.bus = bus
        self.catalogue: dict[str, CredentialConfig] = {c.name: c for c in credentials}
        self.credentials = tuple(self.catalogue)
        self.registries = tuple(registries_)
        self.kinds: tuple[RegistryKind, ...] = tuple(registries.kinds(self.registries))
        self.workdir = workdir
        self.workspace = workspace
        self.timeout_s = timeout_s
        self.fetch_timeout_s = fetch_timeout_s
        self.agent = agent

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

    # -- the fetch op (#766) --------------------------------------------------

    def fetch(
        self,
        ecosystem: str,
        packages: Sequence[str] = (),
        *,
        phase: str | None = None,
        task_id: str | None = None,
        path: str | None = None,
        registry: str | None = None,
        operation: str = "download",
        ref: str = "HEAD",
        sha256: str | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Return registry discovery data or copy one artifact to the agent.

        The service authenticates a fixed read against its catalogue; the
        host verifies the bytes during transfer. No manifest or command is
        ever sent to the credential-bearing worker.
        """
        event: dict[str, Any] = {
            "ecosystem": ecosystem,
            "verb": operation,
            "phase": phase,
            "task_id": task_id,
        }
        if ecosystem not in self.kinds:
            covered = ", ".join(self.kinds) or "none"
            self._refuse_fetch(
                event,
                f"no credentialed registry of kind {ecosystem!r} for run {self.run_id} "
                f"(configured: {covered})",
            )
        entries = [
            entry
            for entry in registries.catalogue_entries(self.registries)
            if entry["kind"] == ecosystem
        ]
        if path is None:
            return {
                "registries": [
                    {key: value for key, value in entry.items() if key not in ("env", "user")}
                    for entry in entries
                ],
                "cache": registries.cache_dir(ecosystem),
                "packages": list(packages),
                "instructions": (
                    "Resolve manifests and run package managers in this agent sandbox. "
                    "Call this tool with a registry name and absolute path to download "
                    "metadata or package files; operation=git fetches an HTTPS Git bundle. "
                    "Read the returned local files, recursively fetch dependencies, and "
                    "populate the ecosystem's offline cache. No code runs in the service."
                ),
            }
        if self.agent is None:
            self._refuse_fetch(event, "artifact transfer requires the agent sandbox")
        if registry is None and len(entries) == 1:
            registry = entries[0]["name"]
        if registry not in {entry["name"] for entry in entries}:
            self._refuse_fetch(event, "select a registry from this ecosystem's catalogue")
        try:
            request = RegistryFetchParams.model_validate(
                {
                    "registry": registry,
                    "path": path,
                    "operation": operation,
                    "ref": ref,
                    "sha256": sha256,
                }
            )
        except ValueError as exc:
            self._refuse_fetch(event, str(exc))
        # The output path is in the agent VM, under a fresh host-generated
        # directory. Neither worker responses nor request paths select a
        # host path or the credential-bearing VM's artifact path.
        name = filename or ("dependency.bundle" if operation == "git" else "registry-data")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,239}", name):
            self._refuse_fetch(event, "filename must be a simple artifact basename")
        event.update(registry=registry, path=path)
        job = JobRequest(
            job_id=new_job_id(),
            run_id=self.run_id,
            kind="service.fetch",
            params=request.model_dump(),
            timeout_s=self.fetch_timeout_s,
        )
        started = time.monotonic()
        result = self.client.submit(job)
        event["duration_s"] = round(time.monotonic() - started, 2)
        if result.status != "ok":
            assert result.error is not None
            event["error"] = f"{result.error.type}: {result.error.message}"
            self.bus.emit(HostEventTypes.SANDBOX_FETCH, self.run_id, job_id=job.job_id, **event)
            log.warning("service.fetch_failed", run=self.run_id, job=job.job_id, **event)
            raise ServiceOpsError(
                f"dependency fetch {ecosystem} {operation} failed: "
                f"{result.error.type}: {result.error.message}"
            )
        metadata = dict(result.output_json or {})
        remote = f"{RESULTS_DIR}/{job.job_id}.artifact"
        directory = f"/tmp/sbxloop-dependency-{job.job_id}"  # nosec B108 - agent VM path
        destination = f"{directory}/{name}"
        try:
            with tempfile.TemporaryDirectory(prefix="sbxloop-dependency-") as temporary:
                local = Path(temporary) / "artifact"
                self.client.sandbox.cp_out(remote, local)
                with local.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if local.stat().st_size != metadata.get("bytes") or digest != metadata.get(
                    "sha256"
                ):
                    raise ServiceOpsError("registry artifact changed during host transfer")
                local.chmod(0o644)
                self.agent.sandbox.mkdirs(directory)
                self.agent.sandbox.cp_in(local, destination)
        except (OSError, SbxError) as exc:
            self._refuse_fetch(event, f"registry artifact transfer failed: {exc}")
        finally:
            with contextlib.suppress(SbxError):
                self.client.sandbox.exec(["rm", "-f", remote])
        event.update(bytes=metadata["bytes"], sha256=digest, exit_code=0)
        self.bus.emit(HostEventTypes.SANDBOX_FETCH, self.run_id, job_id=job.job_id, **event)
        log.info("service.fetch", run=self.run_id, job=job.job_id, **event)
        return {"path": destination, "bytes": metadata["bytes"], "sha256": digest}

    def manifests(self, ecosystem: str) -> tuple[str, ...]:
        """The ecosystem's manifests present in the host workspace — what
        decides the fetch recipe (``npm ci`` vs ``npm install``, ``-r
        requirements.txt`` vs ``.``)."""
        if self.workspace is None or ecosystem not in self.kinds:
            return ()
        kind: RegistryKind = ecosystem
        return tuple(registries.workspace_manifests(self.workspace, kind))

    def _refuse_fetch(self, event: dict[str, Any], reason: str) -> NoReturn:
        self.bus.emit(HostEventTypes.SANDBOX_FETCH, self.run_id, error=reason, **event)
        log.warning("service.fetch_refused", run=self.run_id, error=reason, **event)
        raise ServiceOpsError(reason)

    # -- the agent's host tools ----------------------------------------------

    def tool_specs(self) -> tuple[HostToolSpec, ...]:
        """The host tools this run's service sandbox answers: ``call_service``
        when a credential was granted, ``fetch_dependencies`` when a
        registry carries one. A run with neither has no service sandbox
        and no tools."""
        specs: list[HostToolSpec] = []
        if self.credentials:
            specs.append(self.tool_spec())
        if self.kinds:
            specs.append(self.fetch_tool_spec())
        return tuple(specs)

    def fetch_tool_spec(self) -> HostToolSpec:
        """The ``fetch_dependencies`` tool as the agent session sees it: the
        credentialed ecosystems are the enum."""
        return HostToolSpec(
            name=FETCH_TOOL_NAME,
            description=(
                "Fetch private dependency metadata, archives or Git bundles as data. "
                "With ecosystem alone, discover its registry names, index URLs and cache. "
                "Then supply registry and an absolute path on that registry's host. "
                "Returns a local file path, byte count and SHA-256. Resolve dependencies, "
                "inspect metadata, unpack artifacts and populate offline caches HERE in "
                "the agent sandbox. The service only downloads bytes; it never runs "
                "package managers or reads project files. Use operation=git with an HTTPS "
                "repository path and ref to obtain a bundle for VCS dependencies. "
                "Credentials stay in the service; do not try to extract them. "
                f"Ecosystems: {', '.join(self.kinds)}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "ecosystem": {"type": "string", "enum": list(self.kinds)},
                    "packages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional package names to retain with the catalogue response; "
                            "resolution and installation are performed in this sandbox."
                        ),
                    },
                    "registry": {
                        "type": "string",
                        "description": "Registry name from the catalogue.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Absolute HTTP path; omit to discover registries.",
                    },
                    "operation": {"type": "string", "enum": ["download", "git"]},
                    "ref": {
                        "type": "string",
                        "description": "Git HEAD, full SHA, or full branch/tag ref.",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Local basename, including the archive extension.",
                    },
                    "sha256": {
                        "type": "string",
                        "description": "Expected SHA-256 from trusted dependency metadata.",
                    },
                },
                "required": ["ecosystem"],
            },
        )

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
        if call.name == FETCH_TOOL_NAME:
            return self._handle_fetch(call, phase=phase, task_id=task_id)
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

    def _handle_fetch(
        self, call: HostToolCall, *, phase: str | None, task_id: str | None
    ) -> HostToolResponse:
        args = call.arguments
        try:
            ecosystem = str(args.get("ecosystem", ""))
            packages = args.get("packages") or []
            if not isinstance(packages, list):
                raise ServiceOpsError("packages must be an array of strings")
            if args.get("filename") is not None and not isinstance(args["filename"], str):
                raise ServiceOpsError("filename must be a string")
            output = self.fetch(
                ecosystem,
                [str(p) for p in packages],
                phase=phase,
                task_id=task_id,
                path=args.get("path"),
                registry=args.get("registry"),
                operation=str(args.get("operation", "download")),
                ref=str(args.get("ref", "HEAD")),
                sha256=args.get("sha256"),
                filename=args.get("filename"),
            )
        except ServiceOpsError as exc:
            return HostToolResponse(call_id=call.call_id, ok=False, error=str(exc))
        text = json.dumps(output, ensure_ascii=False)
        if len(text) > MAX_TOOL_TEXT:
            text = text[:MAX_TOOL_TEXT] + "…"
        # Only validated discovery data or a transferred artifact reaches this point.
        return HostToolResponse(call_id=call.call_id, ok=True, text=text)
