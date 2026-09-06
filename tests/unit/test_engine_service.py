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

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.engine.service import ServiceOps
from sbxloop.engine.skilltools import SKILL_TOOL_NAME
from sbxloop.errors import ConfigError, ProvisionError, ServiceOpsError
from sbxloop.events import EventBus, HostEventTypes
from sbxloop.worker.hosttools import HostToolCall
from sbxloop_worker.protocol import EventTypes
from sbxloop_worker.serviceops import CATALOGUE_ENV, FAKE_ENV
from tests.conftest import FakeSbx
from tests.unit.test_engine import HAPPY_TASK, Harness, task, taskgraph
from tests.unit.test_hostgit import git, make_repo

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


def service_tools(job: dict[str, Any]) -> list[str]:
    """The job's host tool names minus `load_skill`, which rides on every
    agent session whatever the run was granted. These tests are about the
    tools a service grant adds, so the skill door is not one of them."""
    return [t["name"] for t in job.get("host_tools", []) if t["name"] != SKILL_TOOL_NAME]


def tool_named(job: dict[str, Any], name: str) -> dict[str, Any]:
    (spec,) = [t for t in job["host_tools"] if t["name"] == name]
    return spec


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
        (build,) = [job for job in jobs if service_tools(job)]
        (decompose,) = [job for job in jobs if not service_tools(job)]
        assert service_tools(build) == ["call_service"]
        assert tool_named(build, "call_service")["parameters"]["properties"]["credential"][
            "enum"
        ] == ["weather"]
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
        one on a config without the section — the builder gets no service
        tool beyond the skill door every session carries, and the prompt has
        no services section."""
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
            assert service_tools(job) == []
            assert "Services you may call" not in job["prompt"]
            assert "call_service" not in job["prompt"]


NPM_SECRET = "npm-registry-token-1a2b3c"  # nosec B105 - test fixture
NPM_REGISTRY = {
    "kind": "npm",
    "host": "npm.example.com",
    "url": "https://npm.example.com/npm/",
    "auth_env": "NPM_TOKEN",
}
FETCH = {
    "name": "fetch_dependencies",
    "arguments": {"ecosystem": "npm", "packages": ["left-pad@1.3.0"]},
    "call_id": "f1",
}


def build_with_fetch(*calls: dict[str, Any]) -> dict[str, Any]:
    return {"text": "fetched what I needed", "host_tool_calls": list(calls)}


@pytest.fixture
def fake_npm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An `npm` on the fake sandbox's PATH that records what it was asked
    (argv, cwd, the environment it saw) and echoes a registry URL with the
    token in it — what a real npm does in verbose output."""
    bin_dir = tmp_path / "npm-bin"
    bin_dir.mkdir()
    log = tmp_path / "npm-calls.jsonl"
    npm = bin_dir / "npm"
    npm.write_text(
        "#!/bin/sh\n"
        f'printf \'{{"argv": "%s", "cwd": "%s", "cache": "%s", "token": "%s"}}\\n\' '
        f'"$*" "$PWD" "$npm_config_cache" "$NPM_TOKEN" >> "{log}"\n'
        'echo "npm http fetch GET https://u:$NPM_TOKEN@npm.example.com/npm/left-pad"\n'
        'echo "added 1 package"\n'
    )
    npm.chmod(0o755)
    monkeypatch.setenv("SBX_FAKE_PROFILE", f'export PATH="{bin_dir}:$PATH"\nunset NPM_TOKEN\n')
    monkeypatch.setenv("NPM_TOKEN", NPM_SECRET)
    return log


def npm_calls(log: Path) -> list[dict[str, Any]]:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


class TestCredentialedRegistryRun:
    """`[[registries]]` with `auth_env` (#766): the service sandbox holds the
    credential and fetches; the agent sandbox is offline for the ecosystem
    and asks through `fetch_dependencies`."""

    def workspace(self, harness: Harness, *, lockfile: bool = False) -> Path:
        source = make_repo(harness.tmp_path)
        (source / "package.json").write_text('{"name": "app", "dependencies": {}}\n')
        if lockfile:
            (source / "package-lock.json").write_text("{}\n")
        git("add", ".", cwd=source)
        git("commit", "-m", "npm project", cwd=source)
        return source

    def test_setup_downloads_data_and_verifies_in_the_agent(
        self, harness: Harness, fake_npm: Path, fake_service: Path
    ) -> None:
        source = self.workspace(harness, lockfile=True)
        artifact = b"opaque dependency data\x00\xff"
        fake_service.write_text(
            json.dumps(
                {"responses": [{"status": 200, "body_base64": base64.b64encode(artifact).decode()}]}
            )
        )
        download = {
            **FETCH,
            "arguments": {
                "ecosystem": "npm",
                "path": "/npm/left-pad/-/left-pad-1.3.0.tgz",
                "filename": "left-pad.tgz",
            },
        }
        harness.script(
            [
                {
                    "json": {"ready": True, "reason": "cache prepared"},
                    "host_tool_calls": [download],
                },
                taskgraph(task("t1")),
                build_with_fetch(FETCH),
                *HAPPY_TASK[1:],
            ]
        )
        engine = harness.engine(
            registries=[NPM_REGISTRY], sandbox={"workspace": str(source)}, keep_sandboxes=True
        )
        result = engine.start("add left-pad")
        assert result.succeeded, result.reason
        run_id = result.run_id
        (preparation_spend,) = [
            row for row in engine.store.phase_attempts(run_id) if row["phase"] == "dependencies"
        ]
        assert preparation_spend["turns"] == 1
        assert preparation_spend["input_tokens"] > 0
        calls = npm_calls(fake_npm)
        assert [c["argv"] for c in calls] == ["ci --ignore-scripts"]
        assert all(c["token"] == "" for c in calls)
        clone = result.workspace
        assert clone is not None
        assert Path(calls[0]["cwd"]).resolve() == clone.resolve()
        service_fs = harness.fake_sbx.sandbox_fs(f"sbxloop-{run_id}-service")
        service_jobs = [
            json.loads(p.read_text()) for p in (service_fs / "home/agent/.sbxloop/jobs").iterdir()
        ]
        assert [j["kind"] for j in service_jobs] == ["service.fetch"]
        assert all(j.get("argv") is None and j.get("cwd") is None for j in service_jobs)
        assert not (service_fs / "home/agent/.npmrc").exists()
        assert not (service_fs / "home/agent/.sbxloop/deps").exists()
        assert not list((service_fs / "home/agent/.sbxloop/results").glob("*.artifact"))
        agent_fs = harness.fake_sbx.sandbox_fs(f"sbxloop-{run_id}-agent")
        (copied,) = list((agent_fs / "tmp").rglob("left-pad.tgz"))
        assert copied.read_bytes() == artifact
        (request,) = requests_sent(fake_service)
        assert request["headers"]["Authorization"] == f"Bearer {NPM_SECRET}"
        agent_home = agent_fs / "home/agent"
        agent_sh = (agent_home / ".sbxloop/env.sh").read_text()
        assert "export npm_config_offline=true\n" in agent_sh
        assert "NPM_TOKEN" not in agent_sh
        assert not (agent_home / ".npmrc").exists()
        jobs = [job for job in harness.agent_jobs(run_id) if job.get("kind") == "agent.session"]
        preparation, decompose, build = jobs
        assert service_tools(preparation) == ["fetch_dependencies"]
        assert service_tools(decompose) == []
        assert service_tools(build) == ["fetch_dependencies"]
        assert preparation["host_tool_timeout_s"] > service_jobs[0]["timeout_s"]
        assert build["host_tool_timeout_s"] > service_jobs[0]["timeout_s"]
        assert "## Dependencies" in build["prompt"]
        fetches = [e for e in harness.events if e.type == HostEventTypes.SANDBOX_FETCH]
        assert [e.data["verb"] for e in fetches] == ["prepare", "download", "verify-offline"]
        assert fetches[-1].data["exit_code"] == 0
        for event in harness.events:
            assert NPM_SECRET not in json.dumps(event.data, default=str), event.type
        assert (clone / ".sbxloop/deps").is_dir()
        assert ".sbxloop/" in (clone / ".git/info/exclude").read_text()

    def test_catalogue_query_does_not_run_a_package_manager(
        self, harness: Harness, fake_npm: Path
    ) -> None:
        source = make_repo(harness.tmp_path)
        harness.script([taskgraph(task("t1")), build_with_fetch(FETCH), *HAPPY_TASK[1:]])
        engine = harness.engine(registries=[NPM_REGISTRY], sandbox={"workspace": str(source)})
        assert engine.start("inspect dependencies").succeeded
        assert npm_calls(fake_npm) == []
        messages = [e.data["content"] for e in harness.events if e.type == EventTypes.AGENT_MESSAGE]
        assert any('"registries"' in m and '"cache"' in m for m in messages)
        assert all(NPM_SECRET not in m for m in messages)

    def test_no_manifest_no_setup_fetch(self, harness: Harness, fake_npm: Path) -> None:
        source = make_repo(harness.tmp_path)  # no package.json
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        engine = harness.engine(registries=[NPM_REGISTRY], sandbox={"workspace": str(source)})
        assert engine.start("plain").succeeded
        assert npm_calls(fake_npm) == []
        assert not [e for e in harness.events if e.type == HostEventTypes.SANDBOX_FETCH]

    @pytest.mark.parametrize("ready", [False, True])
    def test_incomplete_preparation_fails_closed(
        self, harness: Harness, fake_npm: Path, ready: bool
    ) -> None:
        npm = fake_npm.parent / "npm-bin/npm"
        npm.write_text('#!/bin/sh\necho "offline cache incomplete" >&2\nexit 1\n')
        source = self.workspace(harness)
        harness.script([{"json": {"ready": ready, "reason": "cache incomplete"}}])
        engine = harness.engine(registries=[NPM_REGISTRY], sandbox={"workspace": str(source)})
        with pytest.raises(ProvisionError, match=r"dependency (preparation|verification)"):
            engine.start("prepare dependencies")
        assert harness.consumed() == 1
        assert harness.run_states() == ["provisioning"]
        assert harness.sandboxes_left() == []

    def test_ungranted_registry_is_refused_without_a_service_job(
        self, harness: Harness, fake_npm: Path
    ) -> None:
        source = make_repo(harness.tmp_path)
        bad = {
            **FETCH,
            "arguments": {"ecosystem": "npm", "registry": "foreign", "path": "/package"},
        }
        other = {**FETCH, "arguments": {"ecosystem": "pypi"}, "call_id": "f3"}
        harness.script([taskgraph(task("t1")), build_with_fetch(bad, other), *HAPPY_TASK[1:]])
        engine = harness.engine(registries=[NPM_REGISTRY], sandbox={"workspace": str(source)})
        assert engine.start("try").succeeded
        assert npm_calls(fake_npm) == []
        refused = [e for e in harness.events if e.type == HostEventTypes.SANDBOX_FETCH]
        assert "select a registry" in refused[0].data["error"]
        assert "no credentialed registry" in refused[1].data["error"]
        responses = [e for e in harness.events if e.type == EventTypes.AGENT_TOOL_RESPONSE]
        assert [r.data["ok"] for r in responses] == [False, False]

    def test_no_workspace_view_in_the_service_box_fails_closed(
        self, harness: Harness, fake_npm: Path
    ) -> None:
        harness.monkeypatch.setenv("SBX_FAKE_NO_MOUNT", "1")
        source = self.workspace(harness)
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        engine = harness.engine(registries=[NPM_REGISTRY], sandbox={"workspace": str(source)})
        with pytest.raises(ProvisionError, match=r"was not visible inside the .* sandbox"):
            engine.start("add left-pad")
        assert harness.consumed() == 0

    def test_credentials_and_registries_share_the_one_service_box(
        self, harness: Harness, fake_npm: Path, fake_service: Path
    ) -> None:
        source = self.workspace(harness)
        harness.script(
            [
                {"json": {"ready": True}},
                taskgraph(task("t1")),
                build_with_call(CALL),
                *HAPPY_TASK[1:],
            ]
        )
        engine = harness.engine(
            registries=[NPM_REGISTRY],
            credentials=[WEATHER],
            sandbox={"workspace": str(source)},
            keep_sandboxes=True,
        )
        result = engine.start("weather", credentials=["weather"])
        assert result.succeeded, result.reason
        run_id = result.run_id
        created = {c[1].removeprefix("--name=") for c in harness.fake_sbx.invocations("create")}
        assert len(created) == 2
        assert [c["argv"] for c in npm_calls(fake_npm)] == ["install --ignore-scripts"]
        assert len(requests_sent(fake_service)) == 1
        jobs = [job for job in harness.agent_jobs(run_id) if job.get("kind") == "agent.session"]
        (build,) = [job for job in jobs if "call_service" in service_tools(job)]
        assert service_tools(build) == [
            "call_service",
            "fetch_dependencies",
        ]
        rules = {
            rule[2]
            for rule in harness.fake_sbx.policies()
            if f"sbxloop-{run_id}-service" in rule and rule[1] == "network"
        }
        assert {"api.weather.example.com", "npm.example.com", "registry.npmjs.org"} <= rules


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
