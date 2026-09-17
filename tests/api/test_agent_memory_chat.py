"""An agent's memory in a chat turn (plan S-A5).

The contract: a mentioned agent's chat persona gains its memory block only
when it has memories visible in the channel; the memory tools are offered to
an agent whose ``tools`` name ``memory`` and to a person's own agent that
names no tool list, never to a built-in, and never when ``[memory]`` is off;
what an agent remembers in a turn is written as that agent, in that channel,
from that turn's message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sbxloop.agents.memory import PROMPT_HEADING
from tests.api.conftest import build
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_recovery import settled
from tests.unit.test_daemon_concierge import make

SCOUT: dict[str, Any] = {
    "slug": "scout",
    "name": "Scout",
    "instructions": "Gather the facts first.",
    "roles": ["planner"],
}


def _turn(api: Any, headers: dict[str, str], channel: str, content: str) -> dict[str, Any]:
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns", headers=headers, json={"content": content}
    )
    assert accepted.status_code == 202, accepted.text
    done = settled(api.client, headers, channel, accepted.json()["turn"]["id"])
    return dict(done)


def _channel(api: Any, headers: dict[str, str]) -> str:
    return str(api.client.post("/v1/channels", json={}, headers=headers).json()["id"])


def _tool_names(call: dict[str, Any]) -> set[str]:
    return {tool.spec.name for tool in call.get("agent_tools") or ()}


def test_the_persona_gains_the_memory_block_only_when_memories_exist(api: Any) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel = _channel(api, headers)

    _turn(api, headers, channel, "@planner what next?")
    assert PROMPT_HEADING not in concierge.calls[0]["persona"]

    created = api.client.post(
        "/v1/agents/planner/memories",
        headers=headers,
        json={"content": "The person plans in two-week cycles."},
    )
    assert created.status_code == 201, created.text
    _turn(api, headers, channel, "@planner what next?")
    persona = concierge.calls[1]["persona"]
    assert PROMPT_HEADING in persona
    assert "The person plans in two-week cycles." in persona
    # Exactly the block was added, set off from the persona by a blank line.
    block = "\n\n## What you remember\n- (fact) The person plans in two-week cycles.\n"
    assert block in persona
    assert persona.replace(block, "") == concierge.calls[0]["persona"]


def test_another_channel_s_private_memory_stays_out_of_the_persona(api: Any) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    here, elsewhere = _channel(api, headers), _channel(api, headers)
    created = api.client.post(
        "/v1/agents/planner/memories",
        headers=headers,
        json={"content": "Only for the other room.", "channel_id": elsewhere},
    )
    assert created.status_code == 201, created.text
    _turn(api, headers, here, "@planner hello")
    assert "Only for the other room." not in concierge.calls[0]["persona"]
    _turn(api, headers, elsewhere, "@planner hello")
    assert "Only for the other room." in concierge.calls[1]["persona"]


def test_who_is_offered_the_memory_tools(api: Any) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    for body in (SCOUT, {**SCOUT, "slug": "lister", "name": "Lister", "tools": ["memory"]}):
        assert api.client.post("/v1/agents", json=body, headers=headers).status_code == 201
    assert (
        api.client.post(
            "/v1/agents",
            json={**SCOUT, "slug": "narrow", "name": "Narrow", "tools": ["list_runs"]},
            headers=headers,
        ).status_code
        == 201
    )
    channel = _channel(api, headers)
    for mention in ("@planner", "@scout", "@lister", "@narrow", ""):
        _turn(api, headers, channel, f"{mention} hi".strip())
    offered = [_tool_names(call) for call in concierge.calls]
    memory = {"remember", "recall", "forget"}
    assert offered == [set(), memory, memory, set(), set()]


def test_disabled_memory_offers_no_tools_and_no_block(tmp_path: Path) -> None:
    built = build(tmp_path, config={"memory": {"enabled": False}})
    with built.client:
        concierge = FakeConcierge()
        built.ctx.concierge = concierge
        headers = bearer(register(built))
        assert built.client.post("/v1/agents", json=SCOUT, headers=headers).status_code == 201
        channel = _channel(built, headers)
        _turn(built, headers, channel, "@scout hi")
        assert _tool_names(concierge.calls[0]) == set()
        assert PROMPT_HEADING not in concierge.calls[0]["persona"]
    built.ctx.close()


def test_an_agent_remembers_in_a_turn_and_recalls_later(api: Any, tmp_path: Path) -> None:
    concierge, client, _, _, _ = make(
        tmp_path / "concierge",
        [
            {
                "calls": [
                    ("remember", {"content": "Bread needs a warm place to rise.", "kind": "fact"})
                ],
                "text": "Noted.",
            },
            {"calls": [("recall", {"query": "bread"})], "text": "I remember."},
        ],
    )
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    assert api.client.post("/v1/agents", json=SCOUT, headers=headers).status_code == 201
    channel = _channel(api, headers)
    try:
        first = _turn(api, headers, channel, "@scout remember how bread rises")
        assert first["status"] == "completed", first
        (memory,) = api.client.get(
            "/v1/agents/scout/memories", headers=headers, params={"channel_id": channel}
        ).json()
        assert memory["content"] == "Bread needs a warm place to rise."
        assert memory["author"] == "agent:scout"
        assert memory["source_channel_id"] == channel
        assert {tool.name for tool in client.jobs[0].host_tools} >= {"remember", "recall"}

        second = _turn(api, headers, channel, "@scout what about bread?")
        assert second["status"] == "completed", second
        # The second turn's persona carries the memory it kept.
        assert "Bread needs a warm place to rise." in (client.jobs[1].system_message or "")
    finally:
        concierge.close()
