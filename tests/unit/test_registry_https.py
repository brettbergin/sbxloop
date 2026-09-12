"""Exercise fixed registry reads with real HTTPS and Git clients on loopback."""

from __future__ import annotations

import base64
import json
import ssl
import subprocess
import threading
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sbxloop_worker.registryops import CATALOGUE_ENV, RegistryFetchError, execute_fetch
from tests.fakes.gitserver import PrivateGitServer
from tests.unit.test_hostgit import git, make_repo

TOKEN = "TEST_ONLY_HTTPS_REGISTRY_CREDENTIAL_7c23"


@pytest.fixture(scope="module")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cert = tmp_path_factory.mktemp("registry-https") / "cert.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(cert.with_suffix(".key")),
            "-out",
            str(cert),
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert


def tls_context(cert: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, cert.with_suffix(".key"))
    return context


@contextmanager
def registry_server(
    cert: Path, *, redirect: str | None = None
) -> Iterator[tuple[str, list[tuple[str, str | None]]]]:
    requests: list[tuple[str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            requests.append((self.path, self.headers.get("Authorization")))
            if self.path == "/start" and redirect:
                self.send_response(302)
                self.send_header("Location", redirect)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                body = b"artifact\x00\xff"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = tls_context(cert).wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def registry_env(url: str, *, kind: str = "npm") -> dict[str, str]:
    return {
        CATALOGUE_ENV: json.dumps(
            [
                {
                    "name": "private",
                    "kind": kind,
                    "url": url,
                    "env": "REG_TOKEN",
                    "user": "reader",
                }
            ]
        ),
        "REG_TOKEN": TOKEN,
    }


def test_same_authority_https_redirect_preserves_fetching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certificate: Path
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(certificate))
    with registry_server(certificate, redirect="/artifact") as (url, requests):
        target = tmp_path / "artifact"
        execute_fetch(
            {"registry": "private", "path": "/start"}, target, timeout_s=10, env=registry_env(url)
        )
        assert target.read_bytes() == b"artifact\x00\xff"
        assert requests == [("/start", f"Bearer {TOKEN}"), ("/artifact", f"Bearer {TOKEN}")]


def test_foreign_authority_redirect_receives_no_request_or_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certificate: Path
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(certificate))
    with (
        registry_server(certificate) as (foreign, foreign_requests),
        registry_server(certificate, redirect=f"{foreign}/artifact") as (url, requests),
    ):
        with pytest.raises(RegistryFetchError, match="crosses its credential authority"):
            execute_fetch(
                {"registry": "private", "path": "/start"},
                tmp_path / "artifact",
                timeout_s=10,
                env=registry_env(url),
            )
        assert foreign_requests == []
        assert requests == [("/start", f"Bearer {TOKEN}")]
        assert not (tmp_path / "artifact").exists()


def test_git_fetch_returns_a_bundle_without_project_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certificate: Path
) -> None:
    repo = make_repo(tmp_path)
    (repo / ".gitattributes").write_text("*.txt filter=hostile\n")
    (repo / ".gitmodules").write_text('[submodule "private"]\npath=private\nurl=ext::false\n')
    git("add", ".", cwd=repo)
    git("commit", "-m", "untrusted metadata", cwd=repo)
    remotes = tmp_path / "remotes"
    remotes.mkdir()
    git("clone", "--bare", str(repo), str(remotes / "dependency.git"), cwd=tmp_path)
    marker = tmp_path / "git-hook-ran"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "reference-transaction").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (hooks / "reference-transaction").chmod(0o755)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "filter.hostile.smudge")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", f"touch {marker}")
    monkeypatch.setenv("GIT_SSL_CAINFO", str(certificate))
    server = PrivateGitServer(remotes, username="reader", token=TOKEN, tls=False)
    server._server.socket = tls_context(certificate).wrap_socket(
        server._server.socket, server_side=True
    )
    with server:
        url = server.url.replace("http://", "https://")
        bundle = tmp_path / "dependency.bundle"
        result = execute_fetch(
            {"registry": "private", "path": "/dependency.git", "operation": "git"},
            bundle,
            timeout_s=10,
            env=registry_env(url, kind="go"),
        )
        assert result["bytes"] > 0
        assert server.requests
        expected = "Basic " + base64.b64encode(f"reader:{TOKEN}".encode()).decode()
        assert all(header == expected for header in server.requests)
    assert not marker.exists()
    # Reading bundle contents happens after the service operation, in a
    # separate clone without any registry credential.
    clone = tmp_path / "agent-checkout"
    clone.mkdir()
    git("init", cwd=clone)
    git("fetch", str(bundle), "refs/sbxloop/dependency", cwd=clone)
    git("checkout", "FETCH_HEAD", cwd=clone)
    assert (clone / "hello.txt").read_text() == "hi\n"


def test_a_failing_git_operation_never_relays_the_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remote error text can echo the Authorization header, and GitPython
    puts stderr into its exception: neither may reach the error a run
    publishes. Only the operation and its exit status do — and no chained
    exception carries the rest."""
    from git import Git, GitCommandError

    original = Git._call_process

    def failing_fetch(self: Git, method: str, *args: object, **kwargs: object) -> object:
        if method == "fetch":
            raise GitCommandError(
                ["git", "fetch"], 128, stderr=f"fatal: Authorization: Basic {TOKEN} was refused"
            )
        return original(self, method, *args, **kwargs)

    monkeypatch.setattr(Git, "_call_process", failing_fetch)
    with pytest.raises(RegistryFetchError) as excinfo:
        execute_fetch(
            {"registry": "private", "path": "/dependency.git", "operation": "git"},
            tmp_path / "dependency.bundle",
            timeout_s=10,
            env=registry_env("https://registry.invalid", kind="go"),
        )
    assert str(excinfo.value) == "registry Git fetch failed (exit 128)"
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert TOKEN not in rendered
    assert "Authorization" not in rendered
