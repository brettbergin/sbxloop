"""Opt-in CI check of the published browser package, without inference or sbx."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import subprocess
import threading
import tomllib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.data import render_config_template

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("SBXLOOP_TEST_PLAYWRIGHT") != "1",
        reason="browser installation and execution are opt-in on CI runners only",
    ),
]


async def _exercise_server(command: list[str], url: str) -> None:
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
    )
    assert process.stdin is not None and process.stdout is not None

    async def send(message: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n")
        await process.stdin.drain()

    async def request(request_id: int, method: str, params: dict[str, Any]) -> Any:
        await send({"id": request_id, "method": method, "params": params})
        assert process.stdout is not None
        while line := await process.stdout.readline():
            message = json.loads(line)
            if message.get("id") == request_id:
                assert "error" not in message, message
                result = message["result"]
                assert not result.get("isError"), result
                return result
        raise AssertionError(f"MCP exited before answering {method}")

    try:
        await request(
            1,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "sbxloop-preset-check", "version": "1"},
            },
        )
        await send({"method": "notifications/initialized"})
        listing = await request(2, "tools/list", {})
        assert {"browser_navigate", "browser_click", "browser_evaluate"} <= {
            tool["name"] for tool in listing["tools"]
        }
        page = await request(
            3, "tools/call", {"name": "browser_navigate", "arguments": {"url": url}}
        )
        assert "Playwright preset works" in json.dumps(page), page
        await request(4, "tools/call", {"name": "browser_click", "arguments": {"target": "button"}})
        # Action replies may link to snapshot files instead of embedding the
        # accessibility tree. Read the DOM through MCP to verify the effect.
        state = await request(
            5,
            "tools/call",
            {
                "name": "browser_evaluate",
                "arguments": {"function": "() => document.querySelector('button').textContent"},
            },
        )
        assert "Button was clicked" in json.dumps(state), state
        await request(6, "tools/call", {"name": "browser_close", "arguments": {}})
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.wait()


def test_preset_installs_and_drives_its_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config.model_validate(tomllib.loads(render_config_template("playwright")))
    workspace = tmp_path / "target"
    workspace.mkdir()
    (workspace / "package.json").write_text('{"name":"target","private":true}\n')
    (workspace / "package-lock.json").write_text('{"lockfileVersion":3}\n')
    # Target repositories may have credentialed, offline dependency caches.
    monkeypatch.setenv("npm_config_offline", "true")
    monkeypatch.chdir(workspace)
    before = {path.name: path.read_bytes() for path in workspace.iterdir()}
    for command in config.sandbox.setup_commands:
        subprocess.run(["sh", "-c", command], check=True, timeout=600)
    assert {path.name: path.read_bytes() for path in workspace.iterdir()} == before
    (workspace / "index.html").write_text(
        """<title>Playwright preset works</title>
<button onclick="this.textContent='Button was clicked'">Click me</button>"""
    )
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(workspace))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(
            asyncio.wait_for(
                _exercise_server(config.mcp[0].command, f"http://127.0.0.1:{server.server_port}/"),
                timeout=90,
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
