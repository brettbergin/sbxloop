"""Fixed authenticated MCP tool operations; no agent, subprocess, or listener.

Streamable HTTP with JSON or SSE responses. Session identifiers stay in the
service sandbox; the host supplies only an opaque local session key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO

from sbxloop_worker.protocol import McpOpParams
from sbxloop_worker.serviceops import FAKE_ENV, FakeTransport, _NoRedirect, load_catalogue

CATALOGUE_ENV = "SBXLOOP_MCP_SERVERS"
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
MAX_BYTES = 2_000_000


class McpOpError(RuntimeError):
    """A refused or unsuccessful operation, without remote error bodies."""


def _sse(stream: BinaryIO, request_id: str, deadline: float) -> dict[str, Any]:
    total = 0
    data: list[str] = []
    while time.monotonic() < deadline:
        line = stream.readline(MAX_BYTES - total + 1)
        total += len(line)
        if total > MAX_BYTES:
            raise McpOpError("MCP response exceeds the size limit")
        if not line:
            break
        text = line.decode("utf-8").rstrip("\r\n")
        if text.startswith("data:"):
            data.append(text[5:].lstrip(" "))
        elif not text and data:
            payload = "\n".join(data)
            if not payload:
                data.clear()
                continue
            message = json.loads(payload)
            data.clear()
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
            if isinstance(message, dict) and "id" in message and "method" in message:
                raise McpOpError("MCP server requested an unsupported client capability")
    raise McpOpError("MCP stream ended without the requested result")


def _exchange(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
    env: Mapping[str, str],
    deadline: float,
    *,
    method: str = "POST",
) -> tuple[dict[str, str], dict[str, Any]]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise McpOpError("MCP operation exceeded its deadline")
    payload = json.dumps(body).encode() if body is not None else None
    request_id = str(body["id"]) if body is not None and "id" in body else None
    if env.get(FAKE_ENV):
        status, response_headers, raw = FakeTransport(Path(env[FAKE_ENV])).send(
            method, url, headers, payload, remaining
        )
        if status not in (200, 202, 204) and not (method == "DELETE" and status == 405):
            raise McpOpError(f"MCP HTTP request failed ({status})")
        if len(raw) > MAX_BYTES:
            raise McpOpError("MCP response exceeds the size limit")
        message = json.loads(raw) if raw and request_id else {}
        return response_headers, message
    request = urllib.request.Request(url, data=payload, method=method, headers=headers)
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=min(remaining, 30.0)) as response:  # nosec B310 - catalogue HTTPS URL validated below; redirects refused
            response_headers = {k.lower(): v for k, v in response.headers.items()}
            if request_id is None:
                return response_headers, {}
            content_type = response_headers.get("content-type", "").split(";", 1)[0]
            if content_type == "text/event-stream":
                return response_headers, _sse(response, request_id, deadline)
            if content_type != "application/json":
                raise McpOpError("MCP response must be JSON or an SSE stream")
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise McpOpError("MCP response exceeds the size limit")
            return response_headers, json.loads(raw)
    except urllib.error.HTTPError as exc:
        if method == "DELETE" and exc.code == 405:
            return {}, {}
        raise McpOpError(
            f"MCP HTTP request failed ({exc.code}); request was not replayed"
        ) from None
    except (urllib.error.URLError, TimeoutError):
        raise McpOpError("MCP HTTP transport failed; request was not replayed") from None


def _execute(
    params: Mapping[str, Any],
    state_dir: Path,
    env: Mapping[str, str] | None = None,
    *,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    env = os.environ if env is None else env
    request = McpOpParams.model_validate(params)
    servers = {entry["name"]: entry for entry in json.loads(env.get(CATALOGUE_ENV, "[]"))}
    server = servers.get(request.server)
    if server is None:
        raise McpOpError("MCP server is not in the service catalogue")
    credential = load_catalogue(env).get(server["credential"])
    if credential is None or not env.get(credential.env):
        raise McpOpError("MCP service credential is unavailable")
    url = str(server["url"])
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != credential.host
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise McpOpError("MCP URL is outside the credential's HTTPS host")
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        credential.header: credential.header_value(env[credential.env]),
    }
    deadline = time.monotonic() + min(timeout_s, 120.0)
    key = hashlib.sha256(f"{request.server}:{request.session}".encode()).hexdigest()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_file = state_dir / f"{key}.json"

    def rpc(method: str, arguments: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
        ident = uuid.uuid4().hex
        response_headers, message = _exchange(
            url,
            headers,
            {
                "jsonrpc": "2.0",
                "id": ident,
                "method": method,
                "params": arguments,
            },
            env,
            deadline,
        )
        # Fake responses use the explicit placeholder so tests need not
        # predict random request IDs; real transports never accept it.
        if env.get(FAKE_ENV) and message.get("id") == "$request":
            message["id"] = ident
        if message.get("jsonrpc") != "2.0" or message.get("id") != ident:
            raise McpOpError("MCP response identity does not match the request")
        if "error" in message or not isinstance(message.get("result"), dict):
            raise McpOpError("MCP server returned an error or invalid result")
        return response_headers, message["result"]

    if state_file.exists():
        state = json.loads(state_file.read_text())
        headers["MCP-Protocol-Version"] = state["version"]
        if state.get("session"):
            headers["MCP-Session-Id"] = state["session"]
    elif request.action == "close":
        return {}
    elif request.action != "tools/list":
        raise McpOpError("MCP session is not initialized")
    else:
        response_headers, result = rpc(
            "initialize",
            {
                "protocolVersion": VERSIONS[0],
                "capabilities": {},
                "clientInfo": {"name": "sbxloop", "version": "1"},
            },
        )
        version = result.get("protocolVersion")
        if version not in VERSIONS:
            raise McpOpError("MCP server negotiated an unsupported protocol version")
        headers["MCP-Protocol-Version"] = version
        session = response_headers.get("mcp-session-id", "")
        if any(not 0x21 <= ord(char) <= 0x7E for char in session) or len(session) > 4096:
            raise McpOpError("invalid MCP session identifier")
        if session:
            headers["MCP-Session-Id"] = session
        with state_file.open("x") as out:
            state_file.chmod(0o600)
            json.dump({"session": session, "version": version}, out)
        _exchange(
            url, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"}, env, deadline
        )
    if request.action == "close":
        try:
            if "MCP-Session-Id" in headers:
                _exchange(url, headers, None, env, deadline, method="DELETE")
        finally:
            state_file.unlink(missing_ok=True)
        return {}
    if request.action == "tools/call":
        if not request.tool:
            raise McpOpError("MCP tools/call requires a tool name")
        _, result = rpc("tools/call", {"name": request.tool, "arguments": request.arguments})
    else:
        tools: list[Any] = []
        cursor: str | None = None
        for _ in range(20):
            _, page = rpc("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(page.get("tools"), list):
                raise McpOpError("MCP tools/list did not return tools")
            tools.extend(page["tools"])
            if len(tools) > 256:
                raise McpOpError("MCP server exposes more than 256 tools")
            cursor = page.get("nextCursor")
            if not cursor:
                break
        else:
            raise McpOpError("MCP tool pagination exceeded its limit")
        result = {"tools": tools}
    encoded = json.dumps(result)
    values = [env.get(entry.env, "") for entry in load_catalogue(env).values()]
    values.append(headers.get("MCP-Session-Id", ""))
    for value in values:
        if value:
            for secret in (value, base64.b64encode(value.encode()).decode()):
                encoded = encoded.replace(json.dumps(secret)[1:-1], "[REDACTED]")
    return dict(json.loads(encoded))


def execute(
    params: Mapping[str, Any],
    state_dir: Path,
    env: Mapping[str, str] | None = None,
    *,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    try:
        return _execute(params, state_dir, env, timeout_s=timeout_s)
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        # Invalid remote bytes, protocol fields and session files must not
        # leak response fragments through the runner's error traceback.
        raise McpOpError("MCP transport or protocol failed") from None
