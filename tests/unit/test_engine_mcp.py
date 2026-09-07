"""An ordinary run uses MCP through real worker jobs and the host-tool wire."""

import hashlib
import json
from pathlib import Path

import pytest

from sbxloop_worker.serviceops import FAKE_ENV
from tests.conftest import FakeSbx
from tests.unit.test_engine import HAPPY_TASK, Harness, task, taskgraph
from tests.unit.test_mcp_mediation import SECRET, SESSION, responses


@pytest.mark.slow
def test_builder_mcp_uses_only_service_jobs_and_does_not_grant_generic_http(
    fake_sbx: FakeSbx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(fake_sbx, tmp_path, monkeypatch)
    script = tmp_path / "http.json"
    script.write_text(json.dumps({"responses": responses()}))
    monkeypatch.setenv(FAKE_ENV, str(script))
    monkeypatch.setenv("WEATHER_KEY", SECRET)
    tool = "mcp_" + hashlib.sha256(b"weather:forecast").hexdigest()[:24]
    harness.script(
        [
            taskgraph(task("t1")),
            {
                "text": "forecast",
                "host_tool_calls": [
                    {"call_id": "forecast", "name": tool, "arguments": {"city": "London"}},
                    {
                        "call_id": "ungranted",
                        "name": "call_service",
                        "arguments": {"credential": "weather", "method": "GET", "path": "/admin"},
                    },
                ],
            },
            *HAPPY_TASK[1:],
        ]
    )
    engine = harness.engine(
        keep_sandboxes=True,
        credentials=[{"name": "weather", "env": "WEATHER_KEY", "host": "weather.example"}],
        mcp=[
            {
                "name": "weather",
                "transport": "http",
                "url": "https://weather.example/mcp",
                "hosts": ["weather.example"],
                "credential": "weather",
                "roles": ["builder"],
            }
        ],
    )
    assert engine.start("use the forecast").succeeded
    run_id = engine.store.list_runs()[0].run_id
    jobs = [job for job in harness.agent_jobs(run_id) if job["kind"] == "agent.session"]
    assert all(job["mcp_servers"] == [] for job in jobs)
    assert sum(any(t["name"] == tool for t in job["host_tools"]) for job in jobs) == 1
    assert all(all(t["name"] != "call_service" for t in job["host_tools"]) for job in jobs)
    service = fake_sbx.sandbox_fs(f"sbxloop-{run_id}-service")
    service_jobs = [
        json.loads(p.read_text()) for p in (service / "home/agent/.sbxloop/jobs").iterdir()
    ]
    assert {job["kind"] for job in service_jobs} == {"service.mcp"}
    assert {job["params"]["action"] for job in service_jobs} == {
        "tools/list",
        "tools/call",
        "close",
    }
    public = json.dumps(jobs) + "".join(e.model_dump_json() for e in harness.events)
    assert SECRET not in public and SESSION not in public
    assert "sunny" in public
