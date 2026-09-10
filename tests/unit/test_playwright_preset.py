"""The browser preset reaches both supported SDKs through normal init/config paths."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sbxloop.cli.app import app
from sbxloop.config import Config
from sbxloop.data import render_config_template
from sbxloop.sbx.provision import CONCIERGE_MCP_ROLES, agent_policy_allows
from sbxloop_worker.mcp import server_configs


def test_init_can_generate_a_self_contained_playwright_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["init", "--project", "--preset", "playwright"])
    assert result.exit_code == 0, result.output
    written = (tmp_path / "sbxloop.toml").read_text()
    assert written == render_config_template("playwright")
    config = Config.model_validate(tomllib.loads(written))
    assert config.agent.backend == "copilot"
    assert "javascript" in config.sandbox.effective_languages
    assert config.sandbox.setup_commands
    assert config.credentials == []


@pytest.mark.parametrize(("backend", "stdio_type"), [("copilot", "local"), ("claude", "stdio")])
def test_playwright_reaches_the_builder_on_both_backends(backend: str, stdio_type: str) -> None:
    data = tomllib.loads(render_config_template("playwright"))
    data["agent"]["backend"] = backend
    config = Config.model_validate(data)
    (spec,) = config.mcp_specs_for("builder")
    assert spec.name == "playwright"
    assert spec.transport == "stdio" and not spec.mediated
    assert not spec.env and not spec.headers
    sdk = server_configs([spec], stdio_type=stdio_type)["playwright"]
    assert sdk["type"] == stdio_type
    assert sdk["command"] == spec.command
    assert sdk["args"] == spec.args
    for role in ("planner", "operator", "critic", "concierge"):
        assert config.mcp_specs_for(role) == []
    allows = agent_policy_allows(config, config.sandbox.effective_languages)
    assert set(config.mcp[0].hosts) <= set(allows)
    assert config.mcp_hosts_for(CONCIERGE_MCP_ROLES) == []
