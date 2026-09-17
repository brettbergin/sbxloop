"""People's own agents: stored beside the built-ins and `[[agents]]`, edited
through `/v1/agents`, and addressable in a conversation like any other.

Expected values come from the agent-registry contract (merge order, read-only
built-ins and toml agents, revision preconditions, no egress keys) and from
the request bodies the tests send, never from the registry under test.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from sbxloop.api.routes.meta import FEATURES
from sbxloop.backends import backend_for
from sbxloop.modelcatalog import catalog_endpoint
from tests.api.conftest import build
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_recovery import settled

SCOUT: dict[str, Any] = {
    "slug": "scout",
    "name": "Scout",
    "description": "Finds the facts a plan needs.",
    "avatar": "S",
    "color": "#123ABC",
    "instructions": "Gather the facts first and cite where each came from.",
    "roles": ["planner"],
    "interests": ["research"],
}


def _create(api: Any, headers: dict[str, str], body: dict[str, Any] | None = None) -> Any:
    return api.client.post("/v1/agents", json=body or SCOUT, headers=headers)


def test_a_person_creates_lists_reads_updates_and_archives_an_agent(api: Any) -> None:
    headers = bearer(register(api))

    created = _create(api, headers)
    assert created.status_code == 201, created.text
    scout = created.json()
    assert scout["slug"] == "scout"
    assert scout["name"] == "Scout"
    assert scout["description"] == "Finds the facts a plan needs."
    assert scout["instructions"] == "Gather the facts first and cite where each came from."
    assert scout["color"] == "#123abc"
    assert scout["avatar"] == "S"
    assert scout["roles"] == ["planner"]
    assert scout["interests"] == ["research"]
    assert scout["source"] == "user"
    assert scout["editable"] is True
    assert scout["enabled"] is True
    assert scout["revision"] == 1

    listed = api.client.get("/v1/agents", headers=headers).json()
    assert [a["slug"] for a in listed] == [
        "concierge",
        "planner",
        "builder",
        "critic",
        "operator",
        "scout",
    ]
    assert api.client.get("/v1/agents/scout", headers=headers).json() == scout

    updated = api.client.patch(
        "/v1/agents/scout",
        json={"expected_revision": 1, "description": "Checks sources.", "aliases": ["finder"]},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["description"] == "Checks sources."
    assert updated.json()["aliases"] == ["finder"]
    assert updated.json()["name"] == "Scout"
    assert updated.json()["revision"] == 2
    assert api.client.get("/v1/agents/finder", headers=headers).json()["slug"] == "scout"

    archived = api.client.post("/v1/agents/scout/archive", headers=headers)
    assert archived.status_code == 200, archived.text
    assert archived.json()["enabled"] is False
    assert archived.json()["revision"] == 3
    listed = api.client.get("/v1/agents", headers=headers).json()
    assert "scout" not in {a["slug"] for a in listed}
    # Still readable, so a saved reference can say what it was.
    assert api.client.get("/v1/agents/scout", headers=headers).json()["enabled"] is False


def test_built_in_agents_describe_themselves_and_stay_read_only(api: Any) -> None:
    headers = bearer(register(api))
    planner = api.client.get("/v1/agents/planner", headers=headers).json()
    assert planner["source"] == "builtin"
    assert planner["editable"] is False
    assert planner["color"] == "#d97706"
    assert planner["avatar"] == "P"
    assert planner["roles"] == ["planner"]
    assert planner["enabled"] is True
    assert api.client.get("/v1/agents/concierge", headers=headers).json()["aliases"] == ["angie"]

    patched = api.client.patch(
        "/v1/agents/planner",
        json={"expected_revision": 0, "description": "changed"},
        headers=headers,
    )
    assert patched.status_code == 409
    assert patched.json()["code"] == "agent_read_only"
    archived = api.client.post("/v1/agents/planner/archive", headers=headers)
    assert archived.status_code == 409
    assert archived.json()["code"] == "agent_read_only"
    assert api.client.get("/v1/agents/planner", headers=headers).json() == planner


def test_agents_from_sbxloop_toml_are_read_only(tmp_path: Path) -> None:
    built = build(tmp_path, config={"agents": [{"slug": "ranger", "name": "Ranger"}]})
    with built.client:
        headers = bearer(register(built))
        ranger = built.client.get("/v1/agents/ranger", headers=headers).json()
        assert ranger["source"] == "config"
        assert ranger["editable"] is False
        patched = built.client.patch(
            "/v1/agents/ranger",
            json={"expected_revision": ranger["revision"], "name": "Other"},
            headers=headers,
        )
        assert patched.status_code == 409
        assert patched.json()["code"] == "agent_read_only"
        # A stored agent never takes a configured slug.
        taken = _create(built, headers, {"slug": "ranger", "name": "Mine"})
        assert taken.status_code == 422
    built.ctx.close()


@pytest.mark.parametrize(
    "body",
    [
        {"slug": "planner", "name": "My planner"},
        {"slug": "angie", "name": "Not Angie"},
        {"slug": "helper", "name": "Helper", "aliases": ["angie"]},
        {"slug": "helper", "name": "Helper", "aliases": ["builder"]},
    ],
)
def test_a_slug_or_alias_already_taken_by_a_built_in_is_refused(
    api: Any, body: dict[str, Any]
) -> None:
    headers = bearer(register(api))
    response = _create(api, headers, body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_agent"
    assert api.client.get("/v1/agents/helper", headers=headers).status_code == 404


def test_a_second_agent_with_the_same_slug_conflicts(api: Any) -> None:
    headers = bearer(register(api))
    assert _create(api, headers).status_code == 201
    again = _create(api, headers)
    assert again.status_code == 409
    assert again.json()["code"] == "agent_exists"
    alias_clash = _create(api, headers, {"slug": "other", "name": "Other", "aliases": ["scout"]})
    assert alias_clash.status_code == 422


@pytest.mark.parametrize(
    "extra",
    [
        {"allow_hosts": ["exfil.example.com"]},
        {"egress": {"allow": ["*"]}},
        {"hosts": ["exfil.example.com"]},
    ],
)
def test_egress_keys_are_refused(api: Any, extra: dict[str, Any]) -> None:
    headers = bearer(register(api))
    response = _create(api, headers, {**SCOUT, **extra})
    assert response.status_code == 422
    assert api.client.get("/v1/agents/scout", headers=headers).status_code == 404

    assert _create(api, headers).status_code == 201
    patched = api.client.patch(
        "/v1/agents/scout", json={"expected_revision": 1, **extra}, headers=headers
    )
    assert patched.status_code == 422
    assert api.client.get("/v1/agents/scout", headers=headers).json()["revision"] == 1


def test_validation_messages_are_returned(api: Any) -> None:
    headers = bearer(register(api))
    response = _create(
        api,
        headers,
        {"slug": "tinker", "name": "Tinker", "tools": ["no_such_tool"], "credentials": ["vault"]},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "no_such_tool" in detail
    assert "vault" in detail

    nameless = _create(api, headers, {"slug": "tinker"})
    assert nameless.status_code == 422
    assert "needs a name" in nameless.json()["detail"]

    bad_colour = _create(api, headers, {"slug": "tinker", "name": "Tinker", "color": "red"})
    assert bad_colour.status_code == 422

    assert _create(api, headers).status_code == 201
    renamed = api.client.patch(
        "/v1/agents/scout", json={"expected_revision": 1, "slug": "other"}, headers=headers
    )
    assert renamed.status_code == 422
    broken = api.client.patch(
        "/v1/agents/scout", json={"expected_revision": 1, "tools": ["nope"]}, headers=headers
    )
    assert broken.status_code == 422
    assert "nope" in broken.json()["detail"]
    assert api.client.get("/v1/agents/scout", headers=headers).json()["revision"] == 1


def test_a_stale_revision_is_a_conflict(api: Any) -> None:
    headers = bearer(register(api))
    assert _create(api, headers).status_code == 201
    first = api.client.patch(
        "/v1/agents/scout", json={"expected_revision": 1, "name": "Scout Two"}, headers=headers
    )
    assert first.status_code == 200
    stale = api.client.patch(
        "/v1/agents/scout", json={"expected_revision": 1, "name": "Scout Three"}, headers=headers
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "agent_revision_conflict"
    current = api.client.get("/v1/agents/scout", headers=headers).json()
    assert current["name"] == "Scout Two"
    assert current["revision"] == 2

    missing = api.client.patch(
        "/v1/agents/nobody", json={"expected_revision": 1, "name": "X"}, headers=headers
    )
    assert missing.status_code == 404
    assert api.client.post("/v1/agents/nobody/archive", headers=headers).status_code == 404


def test_editing_agents_needs_collaboration_write(api: Any) -> None:
    reader = api.bearer(frozenset({"collaboration:read"}))
    assert api.client.get("/v1/agents", headers=reader).status_code == 200
    assert _create(api, reader).status_code == 403
    headers = bearer(register(api))
    assert _create(api, headers).status_code == 201
    patched = api.client.patch(
        "/v1/agents/scout", json={"expected_revision": 1, "name": "X"}, headers=reader
    )
    assert patched.status_code == 403
    assert api.client.post("/v1/agents/scout/archive", headers=reader).status_code == 403


def test_a_model_the_provider_does_not_list_is_refused(api: Any) -> None:
    headers = bearer(register(api))
    # No catalog discovered yet: the model is taken as given.
    unlisted = _create(api, headers, {"slug": "early", "name": "Early", "model": "any-model"})
    assert unlisted.status_code == 201, unlisted.text
    assert unlisted.json()["model"] == "any-model"

    config = api.ctx.config
    backend = backend_for(config)
    config.paths.model_catalogs.mkdir(parents=True, exist_ok=True)
    (config.paths.model_catalogs / f"{backend.name}.json").write_text(
        json.dumps(
            {
                "version": 1,
                "backend": backend.name,
                "endpoint": catalog_endpoint(config),
                "fetched_at": time.time(),
                "models": [{"id": "served-model", "name": "Served"}],
            }
        ),
        encoding="utf-8",
    )
    refused = _create(api, headers, {"slug": "late", "name": "Late", "model": "other-model"})
    assert refused.status_code == 422
    assert "other-model" in refused.json()["detail"]
    accepted = _create(api, headers, {"slug": "late", "name": "Late", "model": "served-model"})
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["model"] == "served-model"


def test_an_archived_agent_leaves_teams_and_mentions(api: Any) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    assert _create(api, headers).status_code == 201
    team = api.client.post(
        "/v1/teams",
        headers=headers,
        json={"name": "Scouts", "slug": "scouts", "agent_slugs": ["scout"]},
    )
    assert team.status_code == 201, team.text

    assert api.client.post("/v1/agents/scout/archive", headers=headers).status_code == 200
    refused = api.client.post(
        "/v1/teams",
        headers=headers,
        json={"name": "Again", "slug": "again", "agent_slugs": ["scout"]},
    )
    assert refused.status_code == 422
    assert refused.json()["code"] == "unknown_agent"

    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    mentioned = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@scout are you there?"},
    )
    assert mentioned.status_code == 202, mentioned.text
    assert mentioned.json()["turn"]["targets"] == []
    targeted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "hello", "target_slugs": ["scout"]},
    )
    assert targeted.status_code == 422
    assert targeted.json()["code"] == "unknown_target"


def test_a_custom_agent_answers_a_mention_in_its_own_persona(api: Any) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    assert _create(api, headers).status_code == 201
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]

    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@Scout what do we need for bread?"},
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["turn"]["targets"] == ["scout"]
    settled(api.client, headers, channel, accepted.json()["turn"]["id"])

    assert len(concierge.calls) == 1
    call = concierge.calls[0]
    assert call["session_key"] == f"{channel}:scout"
    assert call["agent_role"] == "planner"
    assert call["allow_actions"] is True
    assert call["persona"].startswith(
        "\n\n## Collaboration role\n\n"
        "You are sbxloop's **Scout**, responding in Angie as `@scout`. "
        "Gather the facts first and cite where each came from. "
    )
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    assert [m["agent_slug"] for m in messages] == [None, "scout"]
    assert messages[1]["content"] == "reply from scout"


def test_the_registry_is_advertised(api: Any) -> None:
    assert "agents.registry" in FEATURES
    response = api.client.get("/v1/capabilities", headers=api.bearer())
    assert "agents.registry" in response.json()["features"]
