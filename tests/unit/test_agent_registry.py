"""The agent registry: today's built-in catalogue, plus `[[agents]]` from config.

Expected values here are the catalogue as it shipped before the registry
existed (names, descriptions, instructions, persona text) and the shared
contract's identity choices (colour, avatar, role, alias), written out
literally rather than read back from the module under test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sbxloop.agents.definition import AgentDefinition, AgentSpec
from sbxloop.agents.registry import AgentRegistryReadOnly, ConfigAgentRegistry, default_registry
from sbxloop.agents.tools import TOOL_CATALOG
from sbxloop.config import DEFAULT_CONFIG_LOCKED, Config

ANGIE_TEXT = """

## Product persona

You are Angie, a concise personal assistant backed by sbxloop. Answer the
person in the current channel and preserve context only within that channel.
Treat conversation as conversation. Do not claim that a person approved an
action, and explain any sbxloop operation you actually perform.

The person addressed you as `@concierge` (or `@angie`), which is how they let
you use your sbxloop tools in this turn. You are still Angie: speak as
yourself, never as a separate "Concierge" agent, and say what you did.
"""

INSTRUCTIONS = {
    "planner": "Plan within the ask. Use earlier team replies as context. "
    "Do not claim to have built the result.",
    "builder": "Help implement the ask through sbxloop's managed code runs. "
    "This chat session has no checkout, editor or shell; actual file changes "
    "and verification occur in a managed run. Report its status honestly.",
    "critic": "Inspect and judge the evidence and prior team replies. This role is read-only. "
    "Never modify work or dispatch a run. State any evidence you cannot access.",
    "operator": "Use managed workload runs for execution and deliverables. "
    "This chat session has host tools but no editor or shell. Report what the run "
    "actually did and distinguish it from advice.",
}


def _role_persona(name: str, slug: str) -> str:
    return (
        "\n\n## Collaboration role\n\n"
        f"You are sbxloop's **{name}**, responding in Angie as `@{slug}`. "
        f"{INSTRUCTIONS[slug]} Keep the answer useful in a shared chat, state any "
        "action you took, and never imply that another agent or person approved it."
    )


CREDENTIAL = {"name": "weather", "env": "WEATHER_API_KEY", "host": "api.weather.example.com"}
MCP = {
    "name": "forecasts",
    "transport": "http",
    "url": "https://api.weather.example.com/mcp",
    "hosts": ["api.weather.example.com"],
    "credential": "weather",
}


def _config(agents: list[dict[str, Any]], **extra: Any) -> Config:
    return Config.model_validate({"agents": agents, **extra})


class TestBuiltins:
    def test_listing_is_todays_five_agents_in_order(self) -> None:
        registry = ConfigAgentRegistry(Config())
        assert [a.slug for a in registry.list()] == [
            "concierge",
            "planner",
            "builder",
            "critic",
            "operator",
        ]
        assert all(a.source == "builtin" and a.read_only for a in registry.list())

    @pytest.mark.parametrize(
        ("slug", "name", "color", "avatar", "roles"),
        [
            ("concierge", "Angie", "#84cc16", "A", ["lead"]),
            ("planner", "Planner", "#d97706", "P", ["planner"]),
            ("builder", "Builder", "#ea580c", "B", ["builder"]),
            ("critic", "Critic", "#e11d48", "C", ["critic"]),
            ("operator", "Operator", "#0284c7", "O", ["operator"]),
        ],
    )
    def test_identity(
        self, slug: str, name: str, color: str, avatar: str, roles: list[str]
    ) -> None:
        agent = ConfigAgentRegistry(Config()).get(slug)
        assert agent is not None
        assert agent.spec.name == name
        assert agent.spec.color == color
        assert agent.spec.avatar == avatar
        assert agent.spec.roles == roles
        assert agent.spec.enabled is True
        # None means "what the role gets today": a built-in narrows nothing.
        assert agent.spec.tools is None and agent.spec.mcp is None and agent.spec.skills is None
        assert agent.spec.credentials == [] and agent.spec.model is None

    def test_descriptions_and_instructions_are_todays(self) -> None:
        registry = ConfigAgentRegistry(Config())
        descriptions = {
            "concierge": "Chat with sbxloop and direct its managed runs.",
            "planner": "Scope work and prepare a plan for the builder.",
            "builder": "Discuss implementation and dispatch code work through sbxloop.",
            "critic": "Review plans, results, and evidence without changing work.",
            "operator": "Discuss and dispatch research, data, and document workloads.",
        }
        for slug, description in descriptions.items():
            agent = registry.get(slug)
            assert agent is not None and agent.spec.description == description
        for slug, instructions in INSTRUCTIONS.items():
            agent = registry.get(slug)
            assert agent is not None and agent.spec.instructions == instructions
        concierge = registry.get("concierge")
        assert concierge is not None
        assert (
            concierge.spec.instructions
            == "Help the person direct the loop through the available tools."
        )

    def test_chat_persona_is_byte_identical_to_today(self) -> None:
        registry = ConfigAgentRegistry(Config())
        concierge = registry.get("concierge")
        assert concierge is not None and concierge.chat_persona() == ANGIE_TEXT
        for slug, name in (
            ("planner", "Planner"),
            ("builder", "Builder"),
            ("critic", "Critic"),
            ("operator", "Operator"),
        ):
            agent = registry.get(slug)
            assert agent is not None
            assert agent.chat_persona() == _role_persona(name, slug)

    def test_builtins_add_nothing_to_a_run(self) -> None:
        for agent in ConfigAgentRegistry(Config()).list():
            assert agent.run_persona() == ""

    def test_angie_alias_and_case_resolve_to_concierge(self) -> None:
        registry = ConfigAgentRegistry(Config())
        for selector in ("angie", "Angie", "CONCIERGE"):
            agent = registry.get(selector)
            assert agent is not None and agent.slug == "concierge"
        assert registry.get("nobody") is None

    def test_legacy_agents_resolve_but_are_not_listed(self) -> None:
        registry = ConfigAgentRegistry(Config())
        legacy = registry.get("software-dev")
        assert legacy is not None and legacy.spec.name == "Software Developer"
        assert "software-dev" not in {a.slug for a in registry.list()}
        assert "software-dev" in {a.slug for a in registry.list(include_disabled=True)}

    def test_for_role(self) -> None:
        registry = ConfigAgentRegistry(Config())
        assert [a.slug for a in registry.for_role("lead")] == ["concierge"]
        assert [a.slug for a in registry.for_role("critic")] == ["critic"]

    def test_writes_are_refused(self) -> None:
        registry = ConfigAgentRegistry(Config())
        spec = AgentSpec(slug="scout", name="Scout")
        with pytest.raises(AgentRegistryReadOnly):
            registry.create(spec, by="u1")
        with pytest.raises(AgentRegistryReadOnly):
            registry.update("planner", {"color": "#000000"}, expected_revision=0, by="u1")
        with pytest.raises(AgentRegistryReadOnly):
            registry.archive("planner", by="u1")

    def test_default_registry_is_the_config_registry(self) -> None:
        registry = default_registry(Config())
        agent = registry.get("planner")
        assert isinstance(agent, AgentDefinition)


class TestApiShims:
    """`sbxloop.api.agents` keeps its exports, now derived from the registry."""

    def test_shim_catalogue_is_todays(self) -> None:
        from sbxloop.api.agents import AGENTS, AGENTS_BY_SLUG, LEGACY_AGENTS

        assert [(a.slug, a.name, a.role, a.category) for a in AGENTS] == [
            ("concierge", "Concierge", "concierge", "SBXLOOP Agents"),
            ("planner", "Planner", "planner", "SBXLOOP Agents"),
            ("builder", "Builder", "builder", "SBXLOOP Agents"),
            ("critic", "Critic", "critic", "SBXLOOP Agents"),
            ("operator", "Operator", "operator", "SBXLOOP Agents"),
        ]
        assert AGENTS_BY_SLUG["critic"].capabilities == ("review", "evidence", "read only")
        assert AGENTS_BY_SLUG["planner"].phase == "decompose"
        assert [a.slug for a in LEGACY_AGENTS] == [
            "cron",
            "task-manager",
            "workflow-manager",
            "event-manager",
            "github",
            "software-dev",
            "web",
            "weather",
        ]
        assert AGENTS_BY_SLUG["weather"].capabilities == (
            "weather",
            "forecast",
            "temperature",
            "alerts",
        )
        assert AGENTS_BY_SLUG["concierge"].persona == ANGIE_TEXT
        assert AGENTS_BY_SLUG["operator"].persona == _role_persona("Operator", "operator")

    def test_api_context_exposes_the_registry(self, tmp_path: Path) -> None:
        from sbxloop.api.context import ApiContext

        config = _config([{"slug": "scout", "name": "Scout"}], home=str(tmp_path))
        ctx = ApiContext(config, loop=None, auth=None, keys=None)  # type: ignore[arg-type]
        try:
            scout = ctx.agents.get("scout")
            assert scout is not None and scout.source == "config"
        finally:
            ctx.executor.shutdown()
            ctx.turn_executor.shutdown()


class TestConfigAgents:
    def test_toml_agent_is_added_and_resolves_by_alias(self, tmp_path: Path) -> None:
        from sbxloop.config import load_config

        (tmp_path / "sbxloop.toml").write_text(
            "[[agents]]\n"
            'slug = "scout"\n'
            'name = "Scout"\n'
            'color = "#123abc"\n'
            'avatar = "S"\n'
            'roles = ["planner"]\n'
            'aliases = ["recon"]\n'
            'instructions = "Look before you leap."\n'
        )
        config = load_config(cwd=tmp_path, env={})
        registry = ConfigAgentRegistry(config)
        scout = registry.get("recon")
        assert scout is not None and scout.slug == "scout"
        assert scout.source == "config" and scout.read_only
        assert scout.spec.color == "#123abc"
        assert [a.slug for a in registry.list()][-1] == "scout"
        assert [a.slug for a in registry.for_role("planner")] == ["planner", "scout"]
        assert "Look before you leap." in scout.run_persona()
        assert "Look before you leap." in scout.chat_persona()
        assert "`@scout`" in scout.chat_persona()

    def test_toml_agent_merges_into_a_builtin_by_slug(self) -> None:
        registry = ConfigAgentRegistry(
            _config([{"slug": "planner", "color": "#000000", "model": "planner-model"}])
        )
        planner = registry.get("planner")
        assert planner is not None
        assert planner.source == "config"
        assert planner.spec.color == "#000000" and planner.spec.model == "planner-model"
        # Everything the entry does not say stays the built-in's.
        assert planner.spec.name == "Planner" and planner.spec.roles == ["planner"]
        assert planner.chat_persona() == _role_persona("Planner", "planner")
        assert planner.run_persona() == ""
        assert [a.slug for a in registry.list()] == [
            "concierge",
            "planner",
            "builder",
            "critic",
            "operator",
        ]

    def test_toml_agent_can_disable_a_builtin(self) -> None:
        registry = ConfigAgentRegistry(_config([{"slug": "operator", "enabled": False}]))
        assert "operator" not in {a.slug for a in registry.list()}
        assert "operator" in {a.slug for a in registry.list(include_disabled=True)}
        assert registry.for_role("operator") == []

    def test_credentials_and_mcp_by_name(self) -> None:
        config = _config(
            [
                {
                    "slug": "forecaster",
                    "name": "Forecaster",
                    "roles": ["operator"],
                    "credentials": ["weather"],
                    "mcp": ["forecasts"],
                    "tools": ["call_service", "forecasts", "memory"],
                    "can_start": ["workload"],
                    "max_runs_per_day": 3,
                }
            ],
            credentials=[CREDENTIAL],
            mcp=[MCP],
        )
        agent = ConfigAgentRegistry(config).get("forecaster")
        assert agent is not None and agent.spec.credentials == ["weather"]

    @pytest.mark.parametrize(
        "entry",
        [
            {"slug": "x", "name": "X", "egress": ["*.example.com"]},
            {"slug": "x", "name": "X", "hosts": ["example.com"]},
            {"slug": "x", "name": "X", "allow": ["example.com"]},
        ],
    )
    def test_egress_keys_are_refused(self, entry: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            _config([entry])

    def test_unknown_credential_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"credential.*nope"):
            _config([{"slug": "x", "name": "X", "credentials": ["nope"]}])

    def test_undeclared_mcp_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"mcp.*ghost"):
            _config([{"slug": "x", "name": "X", "mcp": ["ghost"]}])

    def test_unknown_tool_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"tool.*rm_rf"):
            _config([{"slug": "x", "name": "X", "tools": ["rm_rf"]}])

    def test_can_start_needs_a_role(self) -> None:
        with pytest.raises(ValueError, match="can_start"):
            _config([{"slug": "x", "name": "X", "can_start": ["code"]}])

    @pytest.mark.parametrize("color", ["red", "#fff", "#12345g", "#1234567", "10b981"])
    def test_bad_colors_are_refused(self, color: str) -> None:
        with pytest.raises(ValueError, match="color"):
            AgentSpec(slug="x", name="X", color=color)

    @pytest.mark.parametrize(
        "avatar",
        ["https://example.com/a.png", "a/b", "x:y", "ABC", "\u00e9\u00e9\u00e9", " ", "\n"],
    )
    def test_bad_avatars_are_refused(self, avatar: str) -> None:
        with pytest.raises(ValueError, match="avatar"):
            AgentSpec(slug="x", name="X", avatar=avatar)

    @pytest.mark.parametrize(
        "avatar", ["", "A", "AB", "\U0001f35e", "\U0001f469\u200d\U0001f52c", "\u2b50\ufe0f"]
    )
    def test_emoji_and_short_avatars_are_accepted(self, avatar: str) -> None:
        assert AgentSpec(slug="x", name="X", avatar=avatar).avatar == avatar

    @pytest.mark.parametrize("slug", ["", "Planner", "-x", "a b", "x" * 65, "a/b"])
    def test_bad_slugs_are_refused(self, slug: str) -> None:
        with pytest.raises(ValueError, match="slug"):
            AgentSpec(slug=slug, name="X")

    def test_duplicate_slugs_are_refused(self) -> None:
        with pytest.raises(ValueError, match=r"two \[\[agents\]\] entries.*scout"):
            _config([{"slug": "scout", "name": "A"}, {"slug": "scout", "name": "B"}])

    def test_alias_may_not_shadow_an_agent(self) -> None:
        with pytest.raises(ValueError, match=r"alias.*planner"):
            _config([{"slug": "scout", "name": "Scout", "aliases": ["planner"]}])
        with pytest.raises(ValueError, match=r"alias.*angie"):
            _config([{"slug": "scout", "name": "Scout", "aliases": ["angie"]}])
        with pytest.raises(ValueError, match="angie"):
            _config([{"slug": "angie", "name": "Imposter"}])

    def test_a_new_agent_needs_a_name(self) -> None:
        with pytest.raises(ValueError, match="name"):
            _config([{"slug": "scout"}])

    def test_validate_reports_problems_without_raising(self) -> None:
        registry = ConfigAgentRegistry(Config())
        spec = AgentSpec(
            slug="scout",
            name="Scout",
            credentials=["nope"],
            tools=["rm_rf"],
            aliases=["builder"],
            can_start=["code"],
        )
        problems = registry.validate(spec)
        assert len(problems) == 4
        assert registry.validate(AgentSpec(slug="scout", name="Scout")) == []

    def test_agents_are_locked_from_chat_by_default(self) -> None:
        assert "agents" in DEFAULT_CONFIG_LOCKED
        assert "agents" in Config().concierge.config_locked

    def test_agents_are_not_run_config_drift(self) -> None:
        from sbxloop.engine.engine import LoopEngine

        stored = Config()
        current = _config([{"slug": "scout", "name": "Scout"}])
        assert LoopEngine._config_drift(stored, current) == []


class TestToolCatalog:
    def test_reserved_names(self) -> None:
        assert {"memory", "start_run", "file_issue"} <= TOOL_CATALOG

    def test_run_host_tools_are_in_the_catalog(self) -> None:
        from sbxloop.engine.issue_lookup import TOOL_NAME as LOOKUP
        from sbxloop.engine.service import FETCH_TOOL_NAME, TOOL_NAME
        from sbxloop.engine.skilltools import SKILL_TOOL_NAME

        assert {LOOKUP, FETCH_TOOL_NAME, TOOL_NAME, SKILL_TOOL_NAME} <= TOOL_CATALOG

    def test_every_concierge_tool_is_in_the_catalog(self, tmp_path: Path) -> None:
        from tests.unit.test_daemon_concierge import FakeGithub, make

        concierge, _, _, _, _ = make(tmp_path, [], github=FakeGithub())
        try:
            names = set(concierge.tool_names) | {"handoff_agent"}
            assert "create_issue" in names
            assert names <= TOOL_CATALOG, names - TOOL_CATALOG
        finally:
            concierge.close()
