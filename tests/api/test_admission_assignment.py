"""Admitting work for named agents, over HTTP.

`POST /v1/items` accepts an optional lead, an agent per run role and the
channel the work belongs to; an item reads back with its lead and its role
assignment. A chat turn hands the concierge its channel and the agents it
mentioned for the roles they declare, and finished work is delivered to the
channel the item names.

Expected values come from the request bodies and the agents the tests save,
never from the code under test.
"""

from __future__ import annotations

from typing import Any

from sbxloop.agents.definition import AgentSpec
from sbxloop.daemon.model import WorkItem
from sbxloop.ghids import api_item_id
from tests.api.conftest import build
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_recovery import settled

KEY = {"Idempotency-Key": "assign-1"}


def _save(api: Any, slug: str, roles: list[str]) -> None:
    api.ctx.agents.create(
        AgentSpec.model_validate(
            {"slug": slug, "name": slug.title(), "instructions": f"Be {slug}.", "roles": roles}
        ),
        by="test",
    )


def _api(tmp_path: Any) -> Any:
    return build(tmp_path, config={"workloads": [{"name": "research", "sinks": ["chat"]}]})


def test_an_admitted_workload_names_its_lead_and_roles(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        headers = bearer(register(api))
        _save(api, "baker", ["planner"])
        _save(api, "chef", ["lead"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        response = api.client.post(
            "/v1/items",
            json={
                "kind": "workload",
                "ask": "Bake bread",
                "lead": "chef",
                "roles": {"planner": "baker"},
                "channel_id": channel,
            },
            headers={**headers, **KEY},
        )
        assert response.status_code == 201, response.text
        item = response.json()["item"]
        assert item["lead_agent"] == "chef"
        assert item["assignment"] == {"planner": "baker"}
        stored = api.harness.dstore.items()[0]
        assert stored.channel_id == channel
        # Once dispatched, the item reads back with the whole planned team.
        api.harness.source.items = [stored]
        api.harness.outcomes = ["completed"]
        api.clock.t += 10
        api.loop.tick()
        detail = api.client.get(f"/v1/items/{item['id']}", headers=headers).json()
        assert detail["lead_agent"] == "chef"
        assert detail["assignment"] == {
            "planner": "baker",
            "builder": "builder",
            "critic": "critic",
            "operator": "operator",
        }
    api.ctx.close()


def test_unusable_agents_and_unknown_channels_are_refused(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        headers = bearer(register(api))
        _save(api, "baker", ["planner"])
        body = {"kind": "workload", "ask": "Bake bread"}
        cases = [
            ({"roles": {"planner": "nobody"}}, "nobody"),
            ({"roles": {"critic": "baker"}}, "critic"),
            ({"lead": "baker"}, "lead"),
        ]
        for index, (extra, fragment) in enumerate(cases):
            refused = api.client.post(
                "/v1/items",
                json={**body, **extra},
                headers={**headers, "Idempotency-Key": f"bad-{index}"},
            )
            assert refused.status_code == 422, refused.text
            assert refused.json()["code"] == "invalid_argument"
            assert fragment in refused.json()["detail"]
        missing = api.client.post(
            "/v1/items",
            json={**body, "channel_id": "chn_missing"},
            headers={**headers, "Idempotency-Key": "bad-channel"},
        )
        assert missing.status_code == 404, missing.text
        assert api.harness.dstore.items() == []
    api.ctx.close()


def test_an_item_admitted_without_agents_reads_as_before(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        headers = bearer(register(api))
        response = api.client.post(
            "/v1/items",
            json={"kind": "workload", "ask": "Bake bread"},
            headers={**headers, **KEY},
        )
        assert response.status_code == 201, response.text
        item = response.json()["item"]
        assert item["lead_agent"] is None and item["assignment"] is None
    api.ctx.close()


def test_a_chat_turn_hands_its_channel_and_mentioned_roles_to_the_concierge(
    tmp_path: Any,
) -> None:
    api = _api(tmp_path)
    with api.client:
        concierge = FakeConcierge()
        api.ctx.concierge = concierge
        headers = bearer(register(api))
        _save(api, "baker", ["planner", "builder"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        url = f"/v1/channels/{channel}/turns"
        mentioned = api.client.post(
            url, json={"content": "bake", "target_slugs": ["baker"]}, headers=headers
        ).json()
        settled(api.client, headers, channel, mentioned["turn"]["id"])
        plain = api.client.post(url, json={"content": "hello"}, headers=headers).json()
        settled(api.client, headers, channel, plain["turn"]["id"])
        first, second = concierge.calls
        assert first["channel_id"] == channel
        assert first["work_roles"] == {"planner": "baker", "builder": "baker"}
        assert first["work_lead"] is None
        assert second["channel_id"] == channel
        assert second["work_roles"] == {}
        assert second["work_lead"] == "concierge"
    api.ctx.close()


def test_work_is_delivered_to_the_channel_the_item_names(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        accepted = api.client.post(
            f"/v1/channels/{channel}/turns", json={"content": "report"}, headers=headers
        ).json()
        settled(api.client, headers, channel, accepted["turn"]["id"])
        # Keyed by nothing the channel said: only the item's channel links it.
        item = WorkItem(
            item_id=api_item_id("detached"),
            source_key="detached",
            title="Report",
            body="Prepare a report",
            kind="workload",
            channel_id=channel,
            lead_agent="concierge",
        )
        api.harness.dstore.upsert_new(item, api.clock())
        work = api.client.get(f"/v1/channels/{channel}/work", headers=headers)
        assert work.status_code == 200, work.text
        (snapshot,) = work.json()
        assert snapshot["state"] == "queued"
        assert snapshot["agent_slug"] == "concierge"
        api.harness.source.items = [item]
        api.harness.outcomes = ["completed"]
        api.clock.t += 10
        api.loop.tick()
        messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
        results = [m for m in messages if m["kind"] == "work_result"]
        assert len(results) == 1
        assert "the answer is 42" in results[0]["content"]
        # Another channel of the same person does not receive it.
        other = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        assert api.client.get(f"/v1/channels/{other}/work", headers=headers).json() == []
    api.ctx.close()


def test_the_lead_on_the_item_is_credited_for_its_result(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        _save(api, "chef", ["lead"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        accepted = api.client.post(
            f"/v1/channels/{channel}/turns",
            json={"content": "report", "target_slugs": ["operator"]},
            headers=headers,
        ).json()
        settled(api.client, headers, channel, accepted["turn"]["id"])
        key = accepted["turn"]["input_message_id"]
        item = WorkItem(
            item_id=f"chat:{key}",
            source_key=key,
            title="Report",
            kind="workload",
            channel_id=channel,
            lead_agent="chef",
        )
        api.harness.dstore.upsert_new(item, api.clock())
        (snapshot,) = api.client.get(f"/v1/channels/{channel}/work", headers=headers).json()
        assert snapshot["agent_slug"] == "chef"
    api.ctx.close()


def test_the_feature_is_advertised(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        headers = bearer(register(api))
        features = api.client.get("/v1/capabilities", headers=headers).json()["features"]
        assert "intake.assignment" in features
    api.ctx.close()
