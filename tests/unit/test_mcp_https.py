"""Exercise the real HTTPS client against a synthetic Streamable HTTP server."""

import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sbxloop_worker.mcpops import CATALOGUE_ENV, McpOpError, execute


@pytest.mark.parametrize("sse", [False, True])
def test_real_https_session_and_no_redirect_replay(
    tmp_path: Path,
    git_server_certificate: Path,
    monkeypatch: pytest.MonkeyPatch,
    sse: bool,
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(git_server_certificate))
    records: list[tuple[str, str | None, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = request["method"]
            records.append(
                (method, self.headers.get("Authorization"), self.headers.get("Mcp-Session-Id"))
            )
            if method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if method == "tools/call":
                self.send_response(307)
                self.send_header("Location", "/leak")
                self.end_headers()
                return
            result = (
                {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}
                if method == "initialize"
                else {"tools": []}
            )
            body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
            self.send_header("Mcp-Session-Id", "private-session")
            self.end_headers()
            self.wfile.write((f"data: {body}\n\n" if sse else body).encode())

        def do_DELETE(self) -> None:
            records.append(
                ("DELETE", self.headers.get("Authorization"), self.headers.get("Mcp-Session-Id"))
            )
            self.send_response(405)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(git_server_certificate, git_server_certificate.with_suffix(".key"))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {
        CATALOGUE_ENV: json.dumps(
            [
                {
                    "name": "weather",
                    "url": f"https://127.0.0.1:{server.server_port}/mcp",
                    "credential": "weather",
                }
            ]
        ),
        "SBXLOOP_SERVICE_CREDENTIALS": json.dumps(
            [
                {
                    "name": "weather",
                    "env": "KEY",
                    "host": "127.0.0.1",
                    "header": "Authorization",
                    "scheme": "Bearer",
                }
            ]
        ),
        "KEY": "synthetic-key",
    }
    params = {"server": "weather", "session": "host-key"}
    try:
        assert execute({**params, "action": "tools/list"}, tmp_path, env) == {"tools": []}
        with pytest.raises(McpOpError, match="307"):
            execute({**params, "action": "tools/call", "tool": "forecast"}, tmp_path, env)
        execute({**params, "action": "close"}, tmp_path, env)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert [r[0] for r in records] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
        "DELETE",
    ]
    assert all(r[1] == "Bearer synthetic-key" for r in records)
    assert all(r[2] == "private-session" for r in records[1:])
    assert not list(tmp_path.glob("*.json"))
