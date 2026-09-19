"""The resident worker, spoken to directly: jobs on stdin, events and
results on stdout, one forked child per job."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

READY_S = 20.0


class Server:
    """A `python -m sbxloop_worker serve` and a line reader over its stdout."""

    def __init__(self, tmp_path: Path, env: dict[str, str]) -> None:
        self.home = tmp_path / "home"
        self.home.mkdir()
        full = {**os.environ, **env, "HOME": str(self.home), "SBXLOOP_WORKER_BACKEND": "echo"}
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "sbxloop_worker",
                "serve",
                "--env-file",
                str(self.home / "env.sh"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=full,
        )
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.stdin = self.proc.stdin
        self.stdout = self.proc.stdout

    def send(self, message: dict[str, Any]) -> None:
        self.stdin.write(json.dumps(message) + "\n")
        self.stdin.flush()

    def read(self, timeout: float = READY_S) -> dict[str, Any]:
        """The next line as an object (a control message or an event)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.stdout.readline()
            if line == "":
                raise AssertionError(
                    f"server exited: {self.proc.stderr.read() if self.proc.stderr else ''}"
                )
            line = line.strip()
            if line:
                return json.loads(line)
        raise AssertionError("no line from the server in time")

    def read_until(
        self, kind: str, *, timeout: float = READY_S
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Lines up to and including the control message of ``kind``."""
        seen: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            obj = self.read(timeout=max(0.1, deadline - time.monotonic()))
            if obj.get("t") == kind:
                return obj, seen
            seen.append(obj)
        raise AssertionError(
            f"no {kind!r} message in time; saw {[o.get('t') or o.get('type') for o in seen]}"
        )

    def paths(self, job_id: str) -> dict[str, str]:
        """Nothing: the server roots a job's files at its own home."""
        return {}

    def tools_dir(self, job_id: str) -> Path:
        return self.home / ".sbxloop" / "tools" / job_id

    def close(self) -> int:
        self.stdin.close()
        return self.proc.wait(timeout=READY_S)


def job(job_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "job_id": job_id,
        "run_id": "r1",
        "kind": "agent.session",
        "prompt": "hi",
    }
    base.update(overrides)
    return base


@pytest.fixture
def server(tmp_path: Path) -> Server:
    s = Server(tmp_path, {})
    yield s  # type: ignore[misc]
    if s.proc.poll() is None:
        s.proc.kill()


def test_reports_ready_before_reading_anything(server: Server) -> None:
    ready = server.read()
    assert ready["t"] == "ready" and isinstance(ready["pid"], int)
    assert server.close() == 0


def test_runs_a_job_and_returns_its_result_with_its_events(server: Server) -> None:
    server.read()
    server.send({"t": "job", "job": job("j1"), **server.paths("j1")})
    result, events = server.read_until("result")
    assert result["job_id"] == "j1"
    assert result["result"]["status"] == "ok"
    assert result["result"]["output_text"] == "echo: hi"
    types = [e["type"] for e in events]
    assert types[0] == "worker.start" and "worker.end" in types
    assert all(e["job_id"] == "j1" for e in events)
    assert server.close() == 0


def test_jobs_run_concurrently_in_their_own_children(server: Server, tmp_path: Path) -> None:
    server.read()
    slow = tmp_path / "slow.json"
    slow.write_text(json.dumps([{"text": "slow", "sleep_s": 1.0}, {"text": "fast"}]))
    # Two agent jobs share the echo script; the first sleeps, the second
    # finishes first and its result arrives first.
    server.send({"t": "env", "exports": {"SBXLOOP_ECHO_SCRIPT": str(slow)}})
    server.send({"t": "job", "job": job("slow"), **server.paths("slow")})
    time.sleep(0.2)
    server.send({"t": "job", "job": job("fast"), **server.paths("fast")})
    first, _ = server.read_until("result")
    second, _ = server.read_until("result")
    assert (first["job_id"], second["job_id"]) == ("fast", "slow")
    assert server.close() == 0


def test_env_message_reaches_later_jobs(server: Server, tmp_path: Path) -> None:
    server.read()
    marker = tmp_path / "seen"
    server.send({"t": "env", "exports": {"SBXLOOP_TEST_TOKEN": "first"}})
    argv = ["sh", "-c", f"printenv SBXLOOP_TEST_TOKEN > {marker}"]
    server.send(
        {
            "t": "job",
            "job": job("j1", kind="shell.check", argv=argv, prompt=None),
            **server.paths("j1"),
        }
    )
    server.read_until("result")
    assert marker.read_text().strip() == "first"
    server.send({"t": "env", "exports": {"SBXLOOP_TEST_TOKEN": "rotated"}})
    server.send(
        {
            "t": "job",
            "job": job("j2", kind="shell.check", argv=argv, prompt=None),
            **server.paths("j2"),
        }
    )
    server.read_until("result")
    assert marker.read_text().strip() == "rotated"
    assert server.close() == 0


def test_a_tool_response_is_written_atomically_where_the_job_polls(
    server: Server, tmp_path: Path
) -> None:
    server.read()
    script = tmp_path / "script.json"
    script.write_text(
        json.dumps(
            [
                {
                    "text": "asked",
                    "host_tool_calls": [{"name": "answer", "arguments": {}, "call_id": "c1"}],
                }
            ]
        )
    )
    server.send({"t": "env", "exports": {"SBXLOOP_ECHO_SCRIPT": str(script)}})
    server.send(
        {
            "t": "job",
            "job": job("j1", host_tools=[{"name": "answer", "description": "d", "parameters": {}}]),
        }
    )
    request, _ = _read_event(server, "agent.tool_request")
    assert request["data"]["call_id"] == "c1"
    assert server.tools_dir("j1").is_dir()
    server.send(
        {
            "t": "tool",
            "job_id": "j1",
            "response": {"call_id": "c1", "ok": True, "text": "forty-two"},
        }
    )
    result, _ = server.read_until("result")
    assert result["result"]["output_text"] == "asked\nforty-two"
    # The server cleaned the job's tools directory before announcing the result.
    assert not server.tools_dir("j1").exists()
    assert server.close() == 0


def test_cancel_ends_the_job_and_reports_it_lost(server: Server, tmp_path: Path) -> None:
    server.read()
    script = tmp_path / "script.json"
    script.write_text(json.dumps([{"text": "never", "sleep_s": 30}]))
    server.send({"t": "env", "exports": {"SBXLOOP_ECHO_SCRIPT": str(script)}})
    server.send({"t": "job", "job": job("j1"), **server.paths("j1")})
    _read_event(server, "worker.start")
    started = time.monotonic()
    server.send({"t": "cancel", "job_id": "j1"})
    lost, _ = server.read_until("lost")
    assert lost["job_id"] == "j1"
    assert time.monotonic() - started < 10
    assert server.close() == 0


def test_a_bad_line_is_reported_and_the_server_keeps_serving(server: Server) -> None:
    server.read()
    server.stdin.write("this is not json\n")
    server.send({"t": "nonsense"})
    server.send(
        {"t": "tool", "job_id": "absent", "response": {"call_id": "c", "ok": True, "text": ""}}
    )
    server.stdin.flush()
    errors = [server.read()["message"] for _ in range(3)]
    assert any("JSONDecodeError" in e or "Expecting" in e for e in errors)
    assert any("unknown message type" in e for e in errors)
    assert any("no running job" in e for e in errors)
    server.send({"t": "job", "job": job("j1"), **server.paths("j1")})
    result, _ = server.read_until("result")
    assert result["result"]["status"] == "ok"
    assert server.close() == 0


def test_closing_stdin_cancels_what_is_left_and_exits(server: Server, tmp_path: Path) -> None:
    server.read()
    script = tmp_path / "script.json"
    script.write_text(json.dumps([{"text": "never", "sleep_s": 30}]))
    server.send({"t": "env", "exports": {"SBXLOOP_ECHO_SCRIPT": str(script)}})
    server.send({"t": "job", "job": job("j1"), **server.paths("j1")})
    _read_event(server, "worker.start")
    started = time.monotonic()
    assert server.close() == 0
    assert time.monotonic() - started < 10


def _read_event(server: Server, event_type: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []
    deadline = time.monotonic() + READY_S
    while time.monotonic() < deadline:
        obj = server.read()
        if obj.get("type") == event_type:
            return obj, seen
        seen.append(obj)
    raise AssertionError(f"no {event_type} event; saw {seen}")
