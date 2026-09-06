"""MCP must not degrade service credentials into arbitrary agent access."""

import sys
from pathlib import Path

import pytest

from sbxloop.config import Config
from sbxloop.daemon.agentbox import DaemonAgent
from sbxloop.events import EventBus
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner, agent_policy_allows
from tests.conftest import FakeSbx


def configuration(tmp_path: Path, strategy: str, *, credentialed: bool = True) -> Config:
    server = {
        "name": "weather",
        "transport": "http",
        "url": "https://api.weather.example.com/mcp",
        "hosts": ["api.weather.example.com"],
        "roles": ["operator"],
    }
    if credentialed:
        server["credential"] = "weather"
    return Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "secret_strategy": strategy,
            "mcp": [server],
            "credentials": [
                {"name": "weather", "env": "WEATHER_API_KEY", "host": "api.weather.example.com"}
            ],
        }
    )


@pytest.mark.parametrize("strategy", ["proxy", "plain-env"])
@pytest.mark.parametrize("stdin_ok", [False, True])
def test_provision_keeps_mcp_keys_in_service_under_fallback(
    fake_sbx: FakeSbx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    strategy: str,
    stdin_ok: bool,
) -> None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN", "WEATHER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    if stdin_ok:
        monkeypatch.setenv("SBX_FAKE_EXEC_STDIN", "1")
    marker = "audit-synthetic-mcp-credential"
    provisioner = Provisioner(
        SbxCLI(binary=str(fake_sbx.binary)),
        configuration(tmp_path, strategy),
        env={"COPILOT_GITHUB_TOKEN": "audit-inference-key", "WEATHER_API_KEY": marker},
    )
    pair = provisioner.ensure_pair("audit", workspace=tmp_path / "workspace", expects_mount=False)

    agent_fs = fake_sbx.sandbox_fs("sbxloop-audit-agent")
    assert marker not in "".join(p.read_text(errors="replace") for p in agent_fs.rglob("env.sh"))
    agent_env = provisioner.job_env("agent", sandbox=pair.agent)
    assert marker not in str(agent_env() if agent_env else {})
    assert pair.service is not None
    service_env = provisioner.job_env("service", sandbox=pair.service)
    if stdin_ok:
        assert service_env is not None and service_env()["WEATHER_API_KEY"] == marker
    else:
        service_fs = fake_sbx.sandbox_fs("sbxloop-audit-service")
        assert marker in (service_fs / "home/agent/.sbxloop/env.sh").read_text()
    assert "api.weather.example.com" not in agent_policy_allows(provisioner.config, ["python"])
    for record in fake_sbx.raw_invocations():
        # Custom-secret registration carries names; raw values must never
        # become a command or stdin payload for arbitrary agent processes.
        assert marker not in str(record.get("args", []))
        if "sbxloop-audit-agent" in record.get("args", []):
            assert marker not in str(record.get("stdin", ""))


def test_credential_free_mcp_preserves_non_proxy_agent_auth(
    fake_sbx: FakeSbx,
    tmp_path: Path,
) -> None:
    provisioner = Provisioner(
        SbxCLI(binary=str(fake_sbx.binary)),
        configuration(tmp_path, "plain-env", credentialed=False),
        env={"COPILOT_GITHUB_TOKEN": "audit-inference-key", "WEATHER_API_KEY": "not-granted"},
    )
    provisioner.ensure_pair("audit")
    env_file = fake_sbx.sandbox_fs("sbxloop-audit-agent") / "home/agent/.sbxloop/env.sh"
    assert "audit-inference-key" in env_file.read_text()
    assert "not-granted" not in env_file.read_text()


def test_concierge_mcp_service_is_lazy_scoped_and_removed(
    fake_sbx: FakeSbx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEATHER_API_KEY", "concierge-only-key")
    config = configuration(tmp_path, "plain-env")
    server = config.mcp[0].model_copy(update={"roles": ["concierge"]})
    other = server.model_copy(update={"name": "other", "credential": "other", "roles": ["builder"]})
    credential = config.credentials[0].model_copy(
        update={"name": "other", "env": "MISSING_BUILDER_KEY"}
    )
    config = config.model_copy(
        update={"mcp": [server, other], "credentials": [*config.credentials, credential]}
    )
    daemon = DaemonAgent(
        config,
        SbxCLI(binary=str(fake_sbx.binary)),
        EventBus(),
        worker_python=sys.executable,
        install_workers=False,
    )
    assert fake_sbx.invocations("create") == []
    client = daemon._mcp_service()
    assert daemon._mcp_service() is client
    env_file = fake_sbx.sandbox_fs(client.sandbox.name) / "home/agent/.sbxloop/env.sh"
    assert "concierge-only-key" in env_file.read_text()
    assert "MISSING_BUILDER_KEY" not in env_file.read_text()
    daemon.close()
    assert not fake_sbx.sandbox_fs(client.sandbox.name).exists()
