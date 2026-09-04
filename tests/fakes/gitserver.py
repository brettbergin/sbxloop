"""A private smart-HTTP git remote for tests: `git http-backend` behind
HTTP Basic auth that accepts exactly one username/token pair.

This is what "a private repository" means to a clone: every request is
answered 401 until the client presents the credential, and a wrong one is
401 too. Tests point :func:`sbxloop.hostgit.clone_from_remote` at
``server.url`` and prove the run's token — and nothing else — gets in.
"""

from __future__ import annotations

import base64
import os
import subprocess  # nosec B404 - drives git http-backend in tests
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class PrivateGitServer:
    """Serve every bare repo under ``root`` at ``http://127.0.0.1:<port>/<path>``.

    Use as a context manager; ``requests`` records the Authorization header
    of every request so a test can assert what the client presented.
    """

    def __init__(self, root: Path, *, username: str, token: str) -> None:
        self.root = root
        self.username = username
        self.token = token
        self.requests: list[str | None] = []
        expected = "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # quiet
                pass

            def _serve(self) -> None:
                presented = self.headers.get("Authorization")
                outer.requests.append(presented)
                if presented != expected:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="private"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                outer._backend(self)

            do_GET = _serve
            do_POST = _serve

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> PrivateGitServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _backend(self, handler: BaseHTTPRequestHandler) -> None:
        """Run one request through `git http-backend` as CGI."""
        parts = urlsplit(handler.path)
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        env = {
            **os.environ,
            "GIT_PROJECT_ROOT": str(self.root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": parts.path,
            "QUERY_STRING": parts.query,
            "REQUEST_METHOD": handler.command,
            "CONTENT_TYPE": handler.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(length),
            "REMOTE_USER": self.username,
            "GATEWAY_INTERFACE": "CGI/1.1",
            "SERVER_PROTOCOL": "HTTP/1.1",
        }
        out = subprocess.run(  # nosec B603 B607 - fixed argv, test helper
            ["git", "http-backend"], input=body, env=env, capture_output=True, check=False
        )
        head, _, payload = out.stdout.partition(b"\r\n\r\n")
        status = 200
        headers: list[tuple[str, str]] = []
        for line in head.decode().split("\r\n"):
            key, _, value = line.partition(":")
            if key.lower() == "status":
                status = int(value.strip().split()[0])
            elif key:
                headers.append((key, value.strip()))
        handler.send_response(status)
        for key, value in headers:
            handler.send_header(key, value)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)


def bare_from(seed: Path, root: Path, rel: str) -> Path:
    """Publish ``seed`` (a checkout) as a bare repository at ``root/rel``."""
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # nosec B603 B607 - fixed argv, test helper
        ["git", "clone", "--bare", "-q", str(seed), str(target)],
        check=True,
        capture_output=True,
    )
    return target
