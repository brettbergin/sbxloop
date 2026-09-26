"""The product agent answers to the name an operator gives it.

A ``[[agents]]`` entry for ``concierge`` may rename the agent that speaks as
the product and choose the ``@`` aliases people address it by. The slug,
the chat session keys and everything stored stay what they were; the name
is what the agent says it is, what the agent directory lists, what the
preference prompts and refusals call it, and what a signed-out client is
told before anyone signs in. With no such entry nothing changes.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sbxloop.agents.registry import ConfigAgentRegistry
from sbxloop.api.agents import ANGIE_MENTIONED, ANGIE_PERSONA
from sbxloop.config import Config
from tests.api.conftest import Api, build
from tests.api.test_collaboration import FakeConcierge, bearer, register

RENAMED: dict[str, Any] = {
    "agents": [{"slug": "concierge", "name": "Lantern", "aliases": ["lantern", "angie"]}]
}


@pytest.fixture
def renamed(tmp_path: Path) -> Iterator[Api]:
    # Its own home, so a test may serve it beside the shipped ``api``.
    home = tmp_path / "renamed"
    home.mkdir()
    built = build(home, config=RENAMED)
    with built.client:
        yield built
    built.ctx.close()


def _wait_for_calls(concierge: FakeConcierge, count: int = 1) -> None:
    deadline = time.monotonic() + 5
    while len(concierge.calls) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(concierge.calls) >= count


# -- the registry and the persona ---------------------------------------------------


def test_the_shipped_persona_is_unchanged_without_a_rename() -> None:
    concierge = ConfigAgentRegistry(Config()).get("concierge")
    assert concierge is not None
    assert concierge.chat_persona() == ANGIE_PERSONA + ANGIE_MENTIONED
    assert "You are Angie," in ANGIE_PERSONA
    assert "`@concierge` (or `@angie`)" in ANGIE_MENTIONED


def test_a_renamed_concierge_speaks_under_its_name_and_aliases() -> None:
    registry = ConfigAgentRegistry(Config.model_validate(RENAMED))
    concierge = registry.get("concierge")
    assert concierge is not None
    assert concierge.spec.name == "Lantern"
    for selector in ("lantern", "LANTERN", "angie", "concierge"):
        found = registry.get(selector)
        assert found is not None and found.slug == "concierge", selector

    persona = concierge.chat_persona()
    assert "You are Lantern, a concise personal assistant" in persona
    assert "`@concierge` (or `@lantern` or `@angie`)" in persona
    assert "You are still Lantern" in persona
    assert "Angie" not in persona


def test_a_rename_alone_keeps_the_shipped_alias() -> None:
    config = Config.model_validate({"agents": [{"slug": "concierge", "name": "Lantern"}]})
    concierge = ConfigAgentRegistry(config).get("concierge")
    assert concierge is not None
    # The shipped alias stays unless the entry names others.
    assert concierge.spec.aliases == ["angie"]


def test_other_roles_respond_within_the_configured_name() -> None:
    planner = ConfigAgentRegistry(Config.model_validate(RENAMED)).get("planner")
    assert planner is not None
    assert "responding in Lantern as `@planner`" in planner.chat_persona("Lantern")
    assert "responding in Angie as `@planner`" in planner.chat_persona()


# -- the API ------------------------------------------------------------------------


def test_the_directory_lists_the_configured_name(renamed: Api) -> None:
    headers = bearer(register(renamed))

    listed = renamed.client.get("/v1/agents", headers=headers).json()
    concierge = next(agent for agent in listed if agent["slug"] == "concierge")
    assert concierge["name"] == "Lantern"
    assert concierge["aliases"] == ["lantern", "angie"]
    assert "You are Lantern" in concierge["system_prompt"]
    planner = next(agent for agent in listed if agent["slug"] == "planner")
    assert "responding in Lantern as `@planner`" in planner["system_prompt"]

    one = renamed.client.get("/v1/agents/concierge", headers=headers).json()
    assert one == concierge
    assert renamed.client.get("/v1/agents/lantern", headers=headers).json()["slug"] == "concierge"


def test_the_directory_keeps_its_old_listing_without_a_rename(api: Api) -> None:
    headers = bearer(register(api))
    concierge = api.client.get("/v1/agents/concierge", headers=headers).json()
    assert concierge["name"] == "Concierge"
    assert concierge["aliases"] == ["angie"]
    assert "You are Angie" in concierge["system_prompt"]


def test_a_signed_out_client_learns_the_name(renamed: Api, api: Api) -> None:
    assert renamed.client.get("/v1/auth/providers").json()["assistant_name"] == "Lantern"
    assert api.client.get("/v1/auth/providers").json()["assistant_name"] == "Angie"


def test_preference_prompts_name_the_configured_assistant(renamed: Api, api: Api) -> None:
    def descriptions(served: Api, headers: dict[str, str]) -> dict[str, str]:
        found = served.client.get("/v1/prompts/definitions", headers=headers).json()
        return {item["name"]: item["description"] for item in found}

    lantern = descriptions(renamed, bearer(register(renamed)))
    assert lantern["personality"] == "How would you like Lantern to communicate with you?"
    assert lantern["style"] == "How detailed should Lantern's responses be? Any tone preferences?"
    assert not any("Angie" in text for text in lantern.values())

    angie = descriptions(api, bearer(register(api)))
    assert angie["personality"] == "How would you like Angie to communicate with you?"


def test_a_mention_by_a_configured_alias_reaches_the_concierge(renamed: Api) -> None:
    concierge = FakeConcierge()
    renamed.ctx.concierge = concierge
    headers = bearer(register(renamed))
    channel_id = renamed.client.post("/v1/channels", json={}, headers=headers).json()["id"]

    response = renamed.client.post(
        f"/v1/channels/{channel_id}/turns",
        headers=headers,
        json={"content": "@lantern what is running?"},
    )
    assert response.status_code == 202, response.text

    _wait_for_calls(concierge)
    (call,) = concierge.calls
    # Stored identifiers keep the shipped names: the session and the author.
    assert call["session_key"] == f"{channel_id}:angie"
    assert call["agent_slug"] == "concierge"
    assert "You are Lantern" in call["persona"]


def test_refusals_name_the_configured_assistant(renamed: Api) -> None:
    headers = bearer(register(renamed))
    channel_id = renamed.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    renamed.ctx.concierge = FakeConcierge()

    conflict = renamed.client.post(
        f"/v1/channels/{channel_id}/turns",
        headers=headers,
        json={"content": "fix it", "intent": "code", "target_slugs": ["planner"]},
    )
    assert conflict.status_code == 422, conflict.text
    assert "coordinated by Lantern" in conflict.json()["detail"]

    machine = renamed.client.get("/v1/users/me", headers=renamed.bearer())
    assert machine.status_code == 403
    assert machine.json()["code"] == "local_profile_required"
    assert machine.json()["detail"] == "this client is not the local Lantern user"
