"""The service sandbox end to end (#765): a run granted a credential gets a
third sandbox holding it, the builder gets one host tool that asks the host
for a call, and the host runs it as a fixed op in that sandbox. A run
granted nothing is byte-identical to today — no box, no tool, same prompt.

The fake sbx execs on the host, so the worker in every "sandbox" inherits
the test process's environment: the credential VALUE would be visible to
the agent worker here regardless. What the fake does prove is the road —
the catalogue (which only the service sandbox's job env carries) resolves
the name, the request leaves with the header attached to the pinned host,
and nothing the host records (events, tool text) carries the value."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.engine.service import ServiceOps
from sbxloop.errors import ConfigError, ProvisionError, ServiceOpsError
from sbxloop.events import EventBus, HostEventTypes
from sbxloop.worker.hosttools import HostToolCall
from sbxloop_worker.protocol import EventTypes
from sbxloop_worker.serviceops import CATALOGUE_ENV, FAKE_ENV
from tests.conftest import FakeSbx
from tests.unit.test_engine import HAPPY_TASK, Harness, task, taskgraph

VALUE = "wx-secret-value-9f8e7d"
WEATHER = {
    "name": "weather",
    "env": "WEATHER_API_KEY",
    "host": "api.weather.example.com",
    "description": "forecasts",
}
CALL = {
    "name": "call_service",
    "arguments": {
        "credential": "weather",
        "method": "GET",
        "path": "/v1/forecast",
        "query": {"city": "Oslo"},
    },
    "call_id": "c1",
}


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


@pytest.fixture
def fake_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Scripted responses for the worker's HTTP transport, plus the value
    the daemon would hold for the credential."""
    script = tmp_path / "service.json"
    script.write_text(json.dumps({"responses": [{"status": 200, "body": {"temp": 3}}]}))
    monkeypatch.setenv(FAKE_ENV, str(script))
    monkeypatch.setenv("WEATHER_API_KEY", VALUE)
    return script


def requests_sent(script: Path) -> list[dict[str, Any]]:
    path = script.with_suffix(script.suffix + ".requests.jsonl")
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def build_with_call(*calls: dict[str, Any]) -> dict[str, Any]:
    return {"text": "checked the forecast", "host_tool_calls": list(calls)}


class TestCredentialedRun:
    def test_service_box_is_provisioned_called_and_torn_down(
        self, harness: Harness, fake_service: Path
    ) -> None:
        harness.script([taskgraph(task("t1")), build_with_call(CALL), *HAPPY_TASK[1:]])
        engine = harness.engine(credentials=[WEATHER])
        result = engine.start("what's the weather", credentials=["weather"])
        assert result.succeeded
        run_id = engine.store.list_runs()[0].run_id
        assert engine.store.get_run(run_id).credentials == ["weather"]

        # Two sandboxes came up (no [github].repo, so no github sandbox —
        # the service one is the only extra); they provision on parallel
        # threads, so in no fixed order. None survive the run.
        created = {c[1].removeprefix("--name=") for c in harness.fake_sbx.invocations("create")}
        assert created == {f"sbxloop-{run_id}-agent", f"sbxloop-{run_id}-service"}
        assert harness.sandboxes_left() == []

        # The request left the service sandbox for the pinned host with the
        # credential attached, and the response came back through the tool.
        (request,) = requests_sent(fake_service)
        assert request["url"] == "https://api.weather.example.com/v1/forecast?city=Oslo"
        assert request["headers"]["Authorization"] == f"Bearer {VALUE}"
        (call,) = [e for e in harness.events if e.type == HostEventTypes.SERVICE_CALL]
        assert call.run_id == run_id
        assert (call.data["credential"], call.data["method"], call.data["path"]) == (
            "weather",
            "GET",
            "/v1/forecast",
        )
        assert call.data["status"] == 200
        assert call.data["phase"] == "build"
        assert "error" not in call.data
        # The builder read the body: the tool text is folded into its reply.
        (message,) = [
            e
            for e in harness.events
            if e.type == EventTypes.AGENT_MESSAGE and "temp" in e.data.get("content", "")
        ]
        assert message.data["agent"] == "builder"
        assert '"status": 200' in message.data["content"]
        (response,) = [e for e in harness.events if e.type == EventTypes.AGENT_TOOL_RESPONSE]
        assert (response.data["name"], response.data["ok"]) == ("call_service", True)

    def test_only_the_build_job_carries_the_tool(
        self, harness: Harness, fake_service: Path
    ) -> None:
        """The decomposer plans, the builder acts: the tool (and the prompt
        section that explains it, description included) rides on build jobs
        alone."""
        harness.script([taskgraph(task("t1")), build_with_call(CALL), *HAPPY_TASK[1:]])
        engine = harness.engine(credentials=[WEATHER], keep_sandboxes=True)
        assert engine.start("weather", credentials=["weather"]).succeeded
        run_id = engine.store.list_runs()[0].run_id
        jobs = [job for job in harness.agent_jobs(run_id) if job.get("kind") == "agent.session"]
        (build,) = [job for job in jobs if job.get("host_tools")]
        (decompose,) = [job for job in jobs if not job.get("host_tools")]
        assert [tool["name"] for tool in build["host_tools"]] == ["call_service"]
        assert build["host_tools"][0]["parameters"]["properties"]["credential"]["enum"] == [
            "weather"
        ]
        assert "## Services you may call" in build["prompt"]
        assert "weather" in build["prompt"] and "forecasts" in build["prompt"]
        assert "api.weather.example.com" in build["prompt"]
        assert "Services you may call" not in decompose["prompt"]
        # The service sandbox itself only ever saw the fixed op.
        fs = harness.fake_sbx.sandbox_fs(f"sbxloop-{run_id}-service")
        kinds = {
            json.loads(p.read_text())["kind"] for p in (fs / "home/agent/.sbxloop/jobs").iterdir()
        }
        assert kinds == {"service.http"}

    def test_service_sandbox_gets_credentials_by_the_non_proxy_road(
        self, harness: Harness, fake_service: Path
    ) -> None:
        """The value goes in through the service job env (stdin per job here),
        never through `sbx secret`; the announcement names the env variable
        and nothing more; the agent sandbox's provisioning does not mention
        it at all."""
        harness.script([taskgraph(task("t1")), build_with_call(CALL), *HAPPY_TASK[1:]])
        engine = harness.engine(credentials=[WEATHER])
        assert engine.start("weather", credentials=["weather"]).succeeded
        run_id = engine.store.list_runs()[0].run_id

        (announce,) = [e for e in harness.events if e.type == "sandbox.service_credentials"]
        assert announce.data["name"] == f"sbxloop-{run_id}-service"
        assert announce.data["envs"] == ["WEATHER_API_KEY"]
        assert announce.data["delivery"] in ("stdin", "env-file")
        assert "WEATHER_API_KEY" in announce.data["message"]

        # No `sbx secret` registration for the service role.
        for secret in harness.fake_sbx.secrets():
            assert "-service" not in json.dumps(secret)
        # The catalogue is not in the daemon's env, so the worker can only
        # have read it from what the host delivered to its sandbox.
        assert CATALOGUE_ENV not in harness.events[0].data
        # Network policy: the service sandbox is allowed exactly its hosts.
        service_rules = [
            rule
            for rule in harness.fake_sbx.policies()
            if f"sbxloop-{run_id}-service" in rule and rule[1] == "network"
        ]
        assert service_rules
        assert {rule[2] for rule in service_rules} == {"api.weather.example.com"}

    def test_value_never_reaches_the_ledger(self, harness: Harness, fake_service: Path) -> None:
        """An API that echoes the token back gets it redacted before the
        host sees it; nothing the host emits or persists carries the value."""
        fake_service.write_text(
            json.dumps(
                {
                    "responses": [
                        {
                            "status": 200,
                            "headers": {"x-echo": VALUE},
                            "body": {"you sent": f"Bearer {VALUE}"},
                        }
                    ]
                }
            )
        )
        harness.script([taskgraph(task("t1")), build_with_call(CALL), *HAPPY_TASK[1:]])
        engine = harness.engine(credentials=[WEATHER])
        assert engine.start("weather", credentials=["weather"]).succeeded
        for event in harness.events:
            assert VALUE not in json.dumps(event.data, default=str), event.type
        run_id = engine.store.list_runs()[0].run_id
        for _seq, row in engine.store.events(run_id):
            assert VALUE not in json.dumps(row.data, default=str), row.type

    def test_failed_call_reports_to_the_builder_and_the_ledger(
        self, harness: Harness, fake_service: Path
    ) -> None:
        """A 5xx is still an answer: the tool returns it (ok — the request
        completed) with the body, the ledger records the status, the build
        carries on."""
        fake_service.write_text(
            json.dumps({"responses": [{"status": 503, "body": "down for maintenance"}]})
        )
        harness.script([taskgraph(task("t1")), build_with_call(CALL), *HAPPY_TASK[1:]])
        engine = harness.engine(credentials=[WEATHER])
        assert engine.start("weather", credentials=["weather"]).succeeded
        (call,) = [e for e in harness.events if e.type == HostEventTypes.SERVICE_CALL]
        assert call.data["status"] == 503
        (message,) = [
            e
            for e in harness.events
            if e.type == EventTypes.AGENT_MESSAGE and "maintenance" in e.data["content"]
        ]
        assert '"status": 503' in message.data["content"]

    def test_ungranted_credential_is_refused_before_any_job(
        self, harness: Harness, fake_service: Path
    ) -> None:
        """Two credentials declared, one granted: asking for the other is
        refused on the host — no service job, no request."""
        other = {"name": "mail", "env": "MAIL_TOKEN", "host": "mail.example.com"}
        harness.monkeypatch.setenv("MAIL_TOKEN", "mail-secret")
        bad = {**CALL, "arguments": {**CALL["arguments"], "credential": "mail"}, "call_id": "c2"}
        harness.script([taskgraph(task("t1")), build_with_call(bad), *HAPPY_TASK[1:]])
        engine = harness.engine(credentials=[WEATHER, other])
        assert engine.start("weather", credentials=["weather"]).succeeded
        assert requests_sent(fake_service) == []
        (call,) = [e for e in harness.events if e.type == HostEventTypes.SERVICE_CALL]
        assert "not granted" in call.data["error"]
        assert "status" not in call.data
        (message,) = [
            e
            for e in harness.events
            if e.type == EventTypes.AGENT_MESSAGE and "call_service failed" in e.data["content"]
        ]
        assert "mail" in message.data["content"]

    def test_undeclared_credential_fails_before_the_run_row(
        self, harness: Harness, fake_service: Path
    ) -> None:
        engine = harness.engine(credentials=[WEATHER])
        with pytest.raises(ConfigError, match="'stocks' is not declared"):
            engine.start("stocks", credentials=["stocks"])
        assert engine.store.list_runs() == []
        assert harness.fake_sbx.invocations("create") == []

    def test_unset_value_fails_before_any_sandbox(
        self, harness: Harness, fake_service: Path
    ) -> None:
        harness.monkeypatch.delenv("WEATHER_API_KEY")
        engine = harness.engine(credentials=[WEATHER])
        with pytest.raises(ProvisionError, match=r"WEATHER_API_KEY.*not set"):
            engine.start("weather", credentials=["weather"])
        assert harness.fake_sbx.invocations("create") == []


class TestUncredentialedRun:
    def test_no_box_no_tool_same_prompt(self, harness: Harness, fake_service: Path) -> None:
        """Credentials declared but not granted: the run looks exactly like
        one on a config without the section — the builder has no host tools
        and the prompt has no services section."""
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        engine = harness.engine(credentials=[WEATHER], keep_sandboxes=True)
        assert engine.start("plain").succeeded
        run_id = engine.store.list_runs()[0].run_id
        assert engine.store.get_run(run_id).credentials == []
        created = [c[1].removeprefix("--name=") for c in harness.fake_sbx.invocations("create")]
        assert created == [f"sbxloop-{run_id}-agent"]
        assert not [e for e in harness.events if e.type == "sandbox.service_credentials"]

        jobs = [job for job in harness.agent_jobs(run_id) if job.get("kind") == "agent.session"]
        assert len(jobs) == 2  # decompose + build
        for job in jobs:
            assert job.get("host_tools", []) == []
            assert job.get("host_tools_dir") is None
            assert "Services you may call" not in job["prompt"]
            assert "call_service" not in job["prompt"]


class TestServiceOps:
    """The host-side op object on its own: the tool it offers and the
    calls it refuses without going anywhere near a sandbox."""

    def make(self, granted: list[str] = ["weather"]) -> ServiceOps:  # noqa: B006
        class NeverClient:
            def submit(self, job: Any) -> Any:
                raise AssertionError("no job should be submitted")

        config = Config.model_validate(
            {
                "credentials": [
                    WEATHER,
                    {"name": "mail", "env": "MAIL_TOKEN", "host": "mail.example.com"},
                ]
            }
        )
        return ServiceOps(
            NeverClient(),  # type: ignore[arg-type]
            "r1",
            EventBus(),
            config.credentials_named(granted),
        )

    def test_tool_spec_enumerates_the_grant(self) -> None:
        spec = self.make(["weather", "mail"]).tool_spec()
        assert spec.name == "call_service"
        props = spec.parameters["properties"]
        assert props["credential"]["enum"] == ["weather", "mail"]
        assert set(props["method"]["enum"]) >= {"GET", "POST"}
        assert spec.parameters["required"] == ["credential", "method", "path"]
        # The model learns what each name is for, and where it goes.
        assert "weather → https://api.weather.example.com (forecasts)" in spec.description
        assert "mail → https://mail.example.com" in spec.description

    def test_unknown_tool_is_not_ok(self) -> None:
        response = self.make().handle(HostToolCall(call_id="x", name="other", arguments={}))
        assert not response.ok and "unknown host tool" in (response.error or "")

    def test_bad_argument_shapes_are_refused(self) -> None:
        ops = self.make()
        for arguments in (
            {"credential": "weather", "method": "GET", "path": "/", "query": "a=1"},
            {"credential": "weather", "method": "GET", "path": "/", "headers": ["x"]},
            {"credential": "weather", "method": "BREW", "path": "/"},
            {"credential": "mail", "method": "GET", "path": "/"},
        ):
            response = ops.handle(
                HostToolCall(call_id="x", name="call_service", arguments=arguments)
            )
            assert not response.ok, arguments
            assert response.error

    def test_http_refuses_ungranted_names_with_the_granted_list(self) -> None:
        with pytest.raises(ServiceOpsError, match=r"not granted.*weather"):
            self.make().http("mail", "GET", "/")
