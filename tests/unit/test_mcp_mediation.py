"""Successful MCP use crosses only the fixed, credential-bearing service path."""

import io
import json
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sbxloop.config import Config
from sbxloop.errors import WorkerError
from sbxloop.worker.mcp import McpBroker
from sbxloop_worker.mcpops import CATALOGUE_ENV, McpOpError, _sse, execute
from sbxloop_worker.protocol import HostToolCall, JobRequest, JobResult, McpServerSpec
from sbxloop_worker.serviceops import FAKE_ENV

SECRET = "synthetic-mcp-secret"
SESSION = "synthetic-remote-session"


def environment(tmp_path: Path, responses: list[dict[str, Any]]) -> dict[str, str]:
    script = tmp_path / "http.json"
    script.write_text(json.dumps({"responses": responses}))
    return {
        CATALOGUE_ENV: json.dumps(
            [{"name": "weather", "url": "https://weather.example/mcp", "credential": "weather"}]
        ),
        "SBXLOOP_SERVICE_CREDENTIALS": json.dumps(
            [
                {
                    "name": "weather",
                    "env": "WEATHER_KEY",
                    "host": "weather.example",
                    "header": "Authorization",
                    "scheme": "Bearer",
                }
            ]
        ),
        "WEATHER_KEY": SECRET,
        FAKE_ENV: str(script),
    }


def reply(result: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"body": {"jsonrpc": "2.0", "id": "$request", "result": result}, **extra}


def responses() -> list[dict[str, Any]]:
    return [
        reply(
            {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}},
            headers={"Mcp-Session-Id": SESSION},
        ),
        {"status": 202},
        reply(
            {
                "tools": [
                    {
                        "name": "forecast",
                        "description": "Get weather",
                        "inputSchema": {"type": "object"},
                    }
                ]
            }
        ),
        reply({"content": [{"type": "text", "text": "sunny " + SECRET + SESSION}]}),
        {"status": 204},
    ]


def test_discover_call_and_close_keep_credentials_and_session_in_service(tmp_path: Path) -> None:
    env = environment(tmp_path, responses())
    jobs: list[JobRequest] = []

    class Service:
        def submit(self, job: JobRequest) -> JobResult:
            jobs.append(job)
            return JobResult(
                job_id=job.job_id,
                status="ok",
                output_json=execute(job.params, tmp_path / "sessions", env),
            )

    broker = McpBroker(lambda: Service())  # type: ignore[arg-type, return-value]
    job = JobRequest(
        job_id="agent",
        run_id="run",
        kind="agent.session",
        prompt="weather",
        mcp_servers=[
            McpServerSpec(
                name="weather", transport="http", url="https://weather.example/mcp", mediated=True
            ),
        ],
    )
    with broker.prepare(job, None) as (prepared, handler):
        assert prepared.mcp_servers == []
        assert len(prepared.host_tools) == 1 and handler is not None
        rejected = handler(HostToolCall(call_id="bad", name="mcp_not_granted"))
        assert not rejected.ok and len(jobs) == 1
        result = handler(
            HostToolCall(
                call_id="call", name=prepared.host_tools[0].name, arguments={"city": "London"}
            )
        )
        assert result.ok and "sunny" in result.text and "[REDACTED]" in result.text
        staged = (
            prepared.model_dump_json()
            + result.model_dump_json()
            + "".join(j.model_dump_json() for j in jobs)
        )
        assert SECRET not in staged and SESSION not in staged
    assert [j.params["action"] for j in jobs] == ["tools/list", "tools/call", "close"]
    requests = [
        json.loads(line)
        for line in (tmp_path / "http.json.requests.jsonl").read_text().splitlines()
    ]
    assert all(r["headers"]["Authorization"] == "Bearer " + SECRET for r in requests)
    assert all(r["headers"]["MCP-Session-Id"] == SESSION for r in requests[1:])
    assert requests[-1]["method"] == "DELETE"
    assert list((tmp_path / "sessions").glob("*.json")) == []


@pytest.mark.parametrize(
    "extra",
    [
        {"argv": ["sh"]},
        {"prompt": "run code"},
        {"cwd": "/repo"},
        {"commands": ["true"]},
        {"op": "shell"},
    ],
)
def test_mcp_job_cannot_execute_code(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        JobRequest(
            job_id="j",
            run_id="r",
            kind="service.mcp",
            params={"server": "weather", "session": "s", "action": "tools/list"},
            **extra,
        )


def test_unknown_server_cannot_borrow_a_service_credential(tmp_path: Path) -> None:
    env = environment(tmp_path, [])
    with pytest.raises(McpOpError, match="not in the service catalogue"):
        execute(
            {"server": "other", "session": "s", "action": "tools/list"}, tmp_path / "sessions", env
        )
    assert not (tmp_path / "http.json.requests.jsonl").exists()


def test_sse_ignores_notifications_and_returns_matching_result() -> None:
    stream = io.BytesIO(
        b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
        b'data: {"jsonrpc":"2.0","id":"wanted","result":{"tools":[]}}\n\n'
    )
    assert _sse(stream, "wanted", time.monotonic() + 1)["result"] == {"tools": []}


@pytest.mark.parametrize("transport", ["stdio", "sse"])
def test_credentialed_executable_and_legacy_transports_are_refused(transport: str) -> None:
    server = {
        "name": "weather",
        "transport": transport,
        "credential": "weather",
        "hosts": ["weather.example"],
    }
    server.update(
        {"command": ["node", "server.js"]}
        if transport == "stdio"
        else {"url": "https://weather.example/mcp"}
    )
    with pytest.raises(ValidationError, match=r"requires.*http"):
        Config.model_validate(
            {
                "mcp": [server],
                "credentials": [
                    {"name": "weather", "env": "WEATHER_KEY", "host": "weather.example"}
                ],
            }
        )


def test_failed_discovery_closes_remote_session(tmp_path: Path) -> None:
    env = environment(tmp_path, [*responses()[:2], {"status": 500}, {"status": 204}])

    class Service:
        def submit(self, job: JobRequest) -> JobResult:
            try:
                output = execute(job.params, tmp_path / "sessions", env)
            except McpOpError:
                raise WorkerError("MCP failed") from None
            return JobResult(job_id=job.job_id, status="ok", output_json=output)

    job = JobRequest(
        job_id="j",
        run_id="r",
        kind="agent.session",
        prompt="x",
        mcp_servers=[
            McpServerSpec(
                name="weather", transport="http", url="https://weather.example/mcp", mediated=True
            )
        ],
    )
    with pytest.raises(WorkerError), McpBroker(lambda: Service()).prepare(job, None):  # type: ignore[arg-type, return-value]
        pytest.fail("discovery should fail")
    assert list((tmp_path / "sessions").glob("*.json")) == []
