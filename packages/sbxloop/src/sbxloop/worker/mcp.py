"""Expose credentialed MCP tools through host-initiated service jobs."""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Callable, Iterator
from typing import Any

from sbxloop.errors import WorkerError
from sbxloop.ids import new_job_id
from sbxloop.worker.client import WorkerClient
from sbxloop.worker.hosttools import HostToolHandler
from sbxloop_worker.protocol import HostToolCall, HostToolResponse, HostToolSpec, JobRequest


class McpBroker:
    def __init__(self, client: Callable[[], WorkerClient]) -> None:
        self.client = client

    @contextlib.contextmanager
    def prepare(
        self, job: JobRequest, previous: HostToolHandler | None
    ) -> Iterator[tuple[JobRequest, HostToolHandler | None]]:
        client = self.client()
        session = new_job_id()
        opened: list[str] = []
        mapping: dict[str, tuple[str, str]] = {}
        specs = list(job.host_tools)
        occupied = {spec.name for spec in specs}

        def operation(server: str, action: str, **params: Any) -> dict[str, Any]:
            result = client.submit(
                JobRequest(
                    job_id=new_job_id(),
                    run_id=job.run_id,
                    kind="service.mcp",
                    params={"server": server, "session": session, "action": action, **params},
                    timeout_s=120,
                )
            )
            if result.status != "ok":
                raise WorkerError("MCP service operation failed")
            return dict(result.output_json or {})

        def handle(call: HostToolCall) -> HostToolResponse:
            target = mapping.get(call.name)
            if target is None:
                if previous is not None and call.name in occupied:
                    return previous(call)
                return HostToolResponse(call_id=call.call_id, ok=False, error="Unknown host tool")
            server, tool = target
            try:
                result = operation(server, "tools/call", tool=tool, arguments=call.arguments)
            except WorkerError:
                return HostToolResponse(
                    call_id=call.call_id, ok=False, error="MCP tool call failed"
                )
            return HostToolResponse(
                call_id=call.call_id,
                ok=not bool(result.get("isError")),
                text=json.dumps(result),
            )

        try:
            for server in job.mcp_servers:
                if not server.mediated:
                    continue
                opened.append(server.name)
                result = operation(server.name, "tools/list")
                for tool in result.get("tools", []):
                    if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                        raise WorkerError("MCP server returned an invalid tool")
                    original = tool["name"]
                    digest = hashlib.sha256(f"{server.name}:{original}".encode()).hexdigest()[:24]
                    name = f"mcp_{digest}"
                    if name in occupied or name in mapping:
                        raise WorkerError("MCP server returned duplicate tool names")
                    schema = tool.get("inputSchema")
                    if not isinstance(schema, dict) or schema.get("type") != "object":
                        raise WorkerError("MCP tool requires an object input schema")
                    mapping[name] = (server.name, original)
                    specs.append(
                        HostToolSpec(
                            name=name,
                            description=f"{server.name}/{original}: {tool.get('description', '')}",
                            parameters=schema,
                        )
                    )
            prepared = job.model_copy(
                update={
                    "mcp_servers": [server for server in job.mcp_servers if not server.mediated],
                    "host_tools": specs,
                    "host_tool_timeout_s": max(job.host_tool_timeout_s, 150),
                }
            )
            yield prepared, handle if specs else None
        finally:
            for server_name in opened:
                with contextlib.suppress(WorkerError):
                    operation(server_name, "close")
