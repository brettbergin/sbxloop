"""Native role discovery and ordered, individually configured collaboration."""

from typing import Any, get_args

from sbxloop.engine.harness import Role
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_recovery import settled


def test_catalog_uses_native_roles_and_operator_model_configuration(api: Any) -> None:
    headers = bearer(register(api))
    api.ctx.config.agent.models.build = "builder-model"
    api.ctx.config.agent.models.review = "critic-model"
    response = api.client.get("/v1/agents", headers=headers)
    assert response.status_code == 200
    agents = {a["slug"]: a for a in response.json()}
    assert set(agents) == set(get_args(Role))
    assert agents["builder"]["model"] == "builder-model"
    assert agents["critic"]["model"] == "critic-model"
    assert agents["builder"]["backend"] == api.ctx.config.agent.backend
    assert agents["builder"]["execution_mode"] == "chat_and_managed_runs"
    assert agents["critic"]["read_only"] is True
    assert api.client.get("/v1/agents/builder", headers=headers).json() == agents["builder"]
    # Saved legacy teams and callers remain usable, but no longer populate discovery.
    assert api.client.get("/v1/agents/software-dev", headers=headers).status_code == 200


def test_native_team_hands_prior_replies_to_the_next_role(api: Any) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    team = api.client.post(
        "/v1/teams",
        headers=headers,
        json={
            "name": "Delivery",
            "slug": "delivery",
            "agent_slugs": ["planner", "builder", "critic"],
        },
    )
    assert team.status_code == 201, team.text
    response = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@delivery assess the proposed change"},
    )
    assert response.status_code == 202, response.text
    settled(api.client, headers, channel, response.json()["turn"]["id"])
    assert [c["agent_role"] for c in concierge.calls] == ["planner", "builder", "critic"]
    assert "reply from planner" in concierge.calls[1]["history"]
    assert "reply from builder" in concierge.calls[2]["history"]
    assert len({c["session_key"] for c in concierge.calls}) == 3
