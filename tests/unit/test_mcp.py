"""External MCP servers: config, resolution, provisioning and both backends.

The extensibility point. These tests hold the properties that make it safe
to hand a third-party server to an unattended agent: no secret ever travels
in a job or an event, a server's hosts really do reach the sandbox's
allowlist, a read-only critic does not get one by default, and the two SDKs'
dialects are produced from one neutral spec.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sbxloop.config import Config
from sbxloop.sbx.provision import CONCIERGE_MCP_ROLES, agent_policy_allows
from sbxloop_worker.mcp import expand_refs, server_configs
from sbxloop_worker.protocol import JobRequest, McpServerSpec

STDIO = {
    "name": "weather",
    "transport": "stdio",
    "command": ["npx", "-y", "weather-mcp"],
    "hosts": ["api.weather.example.com"],
}
CREDENTIAL = {
    "name": "weather",
    "env": "WEATHER_API_KEY",
    "host": "api.weather.example.com",
}


def config_with(*, mcp: list[dict], credentials: list[dict] | None = None) -> Config:
    return Config.model_validate({"mcp": mcp, "credentials": credentials or []})


class TestConfigValidation:
    def test_a_stdio_server_needs_a_command(self) -> None:
        with pytest.raises(ValidationError, match="needs a command"):
            config_with(mcp=[{**STDIO, "command": []}])

    def test_an_http_server_needs_a_url(self) -> None:
        with pytest.raises(ValidationError, match="needs a url"):
            config_with(mcp=[{"name": "w", "transport": "http", "hosts": ["a.example.com"]}])

    def test_a_stdio_server_takes_no_url(self) -> None:
        with pytest.raises(ValidationError, match="takes no url"):
            config_with(mcp=[{**STDIO, "url": "https://api.weather.example.com"}])

    def test_two_servers_cannot_share_a_name(self) -> None:
        with pytest.raises(ValidationError, match="both named"):
            config_with(mcp=[STDIO, STDIO])

    def test_an_undeclared_credential_is_refused_at_load(self) -> None:
        with pytest.raises(ValidationError, match="not declared under"):
            config_with(mcp=[{**STDIO, "credential": "nope"}])

    def test_the_agent_backends_own_credential_cannot_be_reused(self) -> None:
        """sbx registers one secret per env var, so an MCP server binding
        the backend's env would fight it for the registration. Already
        impossible because those names are reserved for every
        `[[credentials]]` entry — asserted here because the MCP path is a
        new way to reach them."""
        with pytest.raises(ValidationError, match="delivered by sbxloop itself"):
            config_with(
                mcp=[{**STDIO, "credential": "weather"}],
                credentials=[{**CREDENTIAL, "env": "ANTHROPIC_API_KEY"}],
            )

    def test_two_servers_cannot_bind_the_same_env(self) -> None:
        with pytest.raises(ValidationError, match="both bind env"):
            config_with(
                mcp=[
                    {**STDIO, "credential": "weather"},
                    {**STDIO, "name": "other", "credential": "weather"},
                ],
                credentials=[CREDENTIAL],
            )

    def test_a_token_shaped_command_argument_is_refused(self) -> None:
        """argv reaches events, logs and `sbx` arguments, which is the one
        place a secret must never be."""
        with pytest.raises(ValidationError, match="looks like a secret"):
            config_with(mcp=[{**STDIO, "command": ["npx", "server", "--token", "ghp_" + "a" * 36]}])

    def test_hosts_are_normalised_and_deduped(self) -> None:
        config = config_with(
            mcp=[{**STDIO, "hosts": ["API.Weather.Example.com", "api.weather.example.com"]}]
        )
        assert config.mcp[0].hosts == ["api.weather.example.com"]


class TestRoles:
    def test_the_default_excludes_the_critic(self) -> None:
        """A read-only review session reaching a third-party service is a
        capability nobody asked for."""
        config = config_with(mcp=[STDIO])
        assert config.mcp[0].roles == ["builder", "operator"]
        assert config.mcp_specs_for("critic") == []
        assert [s.name for s in config.mcp_specs_for("builder")] == ["weather"]

    def test_a_role_can_be_asked_for_explicitly(self) -> None:
        config = config_with(mcp=[{**STDIO, "roles": ["critic"]}])
        assert [s.name for s in config.mcp_specs_for("critic")] == ["weather"]
        assert config.mcp_specs_for("builder") == []


class TestSpecResolution:
    def test_the_command_becomes_command_plus_args(self) -> None:
        (spec,) = config_with(mcp=[STDIO]).mcp_specs_for("builder")
        assert spec.command == "npx"
        assert spec.args == ["-y", "weather-mcp"]

    def test_a_credential_travels_as_a_reference_never_a_value(self) -> None:
        """The single most important property here: a job's contents reach
        events and logs."""
        config = config_with(mcp=[{**STDIO, "credential": "weather"}], credentials=[CREDENTIAL])
        (spec,) = config.mcp_specs_for("builder")
        assert spec.env == {"WEATHER_API_KEY": "${WEATHER_API_KEY}"}
        assert "secret" not in spec.model_dump_json()

    def test_an_http_server_gets_the_credentials_own_header(self) -> None:
        config = config_with(
            mcp=[
                {
                    "name": "weather",
                    "transport": "http",
                    "url": "https://api.weather.example.com/mcp",
                    "hosts": ["api.weather.example.com"],
                    "credential": "weather",
                }
            ],
            credentials=[{**CREDENTIAL, "header": "X-Api-Key", "scheme": ""}],
        )
        (spec,) = config.mcp_specs_for("builder")
        assert spec.headers == {"X-Api-Key": "${WEATHER_API_KEY}"}

    def test_a_bearer_scheme_is_spelled_out(self) -> None:
        config = config_with(
            mcp=[
                {
                    "name": "weather",
                    "transport": "sse",
                    "url": "https://api.weather.example.com/mcp",
                    "hosts": ["api.weather.example.com"],
                    "credential": "weather",
                }
            ],
            credentials=[CREDENTIAL],
        )
        (spec,) = config.mcp_specs_for("builder")
        assert spec.headers == {"Authorization": "Bearer ${WEATHER_API_KEY}"}


class TestEgress:
    def test_a_servers_hosts_reach_the_sandbox_allowlist(self) -> None:
        config = config_with(mcp=[STDIO])
        allows = agent_policy_allows(config, ["python"])
        assert "api.weather.example.com" in allows

    def test_the_allowlist_never_repeats_a_host(self) -> None:
        """`sbx policy allow` refuses a duplicate and fails the whole call,
        so a host an MCP entry shares with the baseline must be deduped."""
        config = config_with(mcp=[{**STDIO, "hosts": ["github.com", "api.weather.example.com"]}])
        allows = agent_policy_allows(config, ["python"])
        assert len(allows) == len(set(allows))

    def test_the_concierge_box_only_gets_its_own_roles_hosts(self) -> None:
        config = config_with(mcp=[STDIO])  # builder + operator, not concierge
        allows = agent_policy_allows(config, ["python"], mcp_roles=CONCIERGE_MCP_ROLES)
        assert "api.weather.example.com" not in allows


class TestSecretRegistrations:
    def test_a_credentialed_server_declares_one_binding(self) -> None:
        config = config_with(mcp=[{**STDIO, "credential": "weather"}], credentials=[CREDENTIAL])
        assert config.mcp_secrets_for() == [("WEATHER_API_KEY", "api.weather.example.com")]

    def test_a_credential_free_server_declares_none(self) -> None:
        assert config_with(mcp=[STDIO]).mcp_secrets_for() == []


class TestExpansion:
    def test_a_reference_is_resolved_from_the_environment(self) -> None:
        assert expand_refs("Bearer ${TOK}", {"TOK": "s3cret"}) == "Bearer s3cret"

    def test_an_unset_name_becomes_empty_rather_than_raising(self) -> None:
        """The server then fails its own auth, naming the service — a better
        diagnosis than the worker refusing to start a session."""
        assert expand_refs("Bearer ${TOK}", {}) == "Bearer "

    def test_shell_syntax_is_not_expansion(self) -> None:
        for text in ("$TOK", "$(echo hi)", "${TOK:-x}"):
            assert expand_refs(text, {"TOK": "s3cret"}) == text


class TestBackendDialects:
    """One neutral spec, two SDK dialects. Field-verified 2026-09-06 against
    github-copilot-sdk 1.0.8 and claude-agent-sdk 0.2.149: the shapes agree
    except that stdio is `local` to one and `stdio` to the other."""

    SPEC = McpServerSpec(
        name="weather",
        transport="stdio",
        command="npx",
        args=["-y", "weather-mcp"],
        env={"WEATHER_API_KEY": "${WEATHER_API_KEY}"},
    )

    def test_claude_spells_stdio_stdio(self) -> None:
        from sbxloop_worker.backends.claude import MCP_STDIO_TYPE

        configs = server_configs(
            [self.SPEC], stdio_type=MCP_STDIO_TYPE, environ={"WEATHER_API_KEY": "k"}
        )
        assert configs == {
            "weather": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "weather-mcp"],
                "env": {"WEATHER_API_KEY": "k"},
            }
        }

    def test_copilot_spells_stdio_local(self) -> None:
        from sbxloop_worker.backends.copilot import MCP_STDIO_TYPE

        configs = server_configs(
            [self.SPEC], stdio_type=MCP_STDIO_TYPE, environ={"WEATHER_API_KEY": "k"}
        )
        assert configs["weather"]["type"] == "local"

    def test_a_remote_server_maps_the_same_on_both(self) -> None:
        spec = McpServerSpec(
            name="w",
            transport="http",
            url="https://api.weather.example.com/mcp",
            headers={"Authorization": "Bearer ${TOK}"},
        )
        for stdio_type in ("stdio", "local"):
            configs = server_configs([spec], stdio_type=stdio_type, environ={"TOK": "k"})
            assert configs == {
                "w": {
                    "type": "http",
                    "url": "https://api.weather.example.com/mcp",
                    "headers": {"Authorization": "Bearer k"},
                }
            }

    def test_the_credential_is_only_resolved_inside_the_sandbox(self) -> None:
        """With nothing in the environment the placeholder resolves empty —
        proof the value was never carried in the spec."""
        configs = server_configs([self.SPEC], stdio_type="stdio", environ={})
        assert configs["weather"]["env"] == {"WEATHER_API_KEY": ""}


class TestProtocol:
    def test_a_non_agent_job_may_not_carry_servers(self) -> None:
        with pytest.raises(ValidationError, match="mcp_servers"):
            JobRequest(
                job_id="j1",
                run_id="r1",
                kind="shell.batch",
                commands=["true"],
                mcp_servers=[TestBackendDialects.SPEC],
            )

    def test_a_stdio_spec_needs_a_command(self) -> None:
        with pytest.raises(ValidationError, match="needs a command"):
            McpServerSpec(name="w", transport="stdio")
