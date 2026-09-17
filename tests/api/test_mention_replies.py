"""A mention means reply, not queued work.

Mentioning an agent used to rewrite the turn's intent to ``delegate`` while
the agent's own instructions said that anything it could not produce in
chat is a workload to queue with one call and no confirmation. A plain
"@planner give me a list of items to make bread" therefore queued a run
instead of answering. This module pins the contract that replaces it:

- the caller's intent survives a mention, so a conversation stays a
  conversation while the mentioned agent is still recorded as a target and
  joins the channel;
- ``auto`` exists as an intent, so a client can hand the choice to the lead;
- every chat turn carries the rule that an ask answerable in the reply is
  answered inline, and only a runner turn carries a runner's instruction;
- a runner turn records the run roles of the agents it mentions, so
  admission can assign them.

Expected values come from the request bodies and the built-in agents'
declared roles, never from the code under test.
"""

from __future__ import annotations

import time
from typing import Any

from sbxloop.agents.definition import AgentSpec
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_recovery import settled

#: The sentence a turn whose handling was not delegated to a runner carries.
MENTION_RULE = "Being mentioned is a request to reply, not a request to queue work."
#: The rule every turn that may answer carries, whatever its intent.
ANSWER_RULE = "answer it inline and in full, and start nothing"
#: The sentence only an explicit workload turn carries.
WORKLOAD_RULE = "Call start_workload once with their request"
#: The sentence only an `auto` turn carries.
AUTO_RULE = "The person left this turn's handling to you."


def _save(api: Any, slug: str, roles: list[str]) -> None:
    api.ctx.agents.create(
        AgentSpec.model_validate(
            {"slug": slug, "name": slug.title(), "instructions": f"Be {slug}.", "roles": roles}
        ),
        by="test",
    )


def _channel(api: Any, headers: dict[str, str]) -> str:
    return str(api.client.post("/v1/channels", json={}, headers=headers).json()["id"])


def _messages(api: Any, headers: dict[str, str], channel: str, count: int) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 5
    messages: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
        if len(messages) >= count:
            break
        time.sleep(0.01)
    return messages


def test_a_mention_keeps_the_turn_a_conversation_and_answers_in_the_channel(api: Any) -> None:
    """The reported failure: naming an agent asked it a question, and the
    answer belongs in the reply. The mention still records the target and
    joins the agent to the channel."""
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel = _channel(api, headers)

    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@planner give me a list of items to make bread"},
    )
    assert accepted.status_code == 202, accepted.text
    turn = accepted.json()["turn"]
    assert turn["targets"] == ["planner"]
    assert turn["intent"] == "conversation"
    assert turn["participants"][0]["assignees"] is None

    settled(api.client, headers, channel, turn["id"])
    (call,) = concierge.calls
    assert MENTION_RULE in call["persona"]
    assert ANSWER_RULE in call["persona"]
    assert WORKLOAD_RULE not in call["persona"]
    assert AUTO_RULE not in call["persona"]

    messages = _messages(api, headers, channel, 2)
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["agent_slug"] == "planner"

    joined = api.client.get(f"/v1/channels/{channel}/participants", headers=headers)
    assert [entry["agent_slug"] for entry in joined.json()["data"]] == ["planner"]


def test_an_explicit_workload_turn_still_carries_the_runner_instruction(api: Any) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel = _channel(api, headers)

    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "write up the bread market", "intent": "workload"},
    )
    assert accepted.status_code == 202, accepted.text
    turn = accepted.json()["turn"]
    assert turn["intent"] == "workload"

    settled(api.client, headers, channel, turn["id"])
    (call,) = concierge.calls
    assert WORKLOAD_RULE in call["persona"]
    assert call["allow_actions"] is True


def test_the_auto_intent_hands_the_choice_to_the_lead(api: Any) -> None:
    """``auto`` is what a client sends when it does not know whether the ask
    is a question or a piece of work. The lead is told both branches and
    keeps its action tools."""
    capabilities = api.client.get("/v1/capabilities", headers=api.bearer()).json()
    assert "collaboration.lead_orchestrator" in capabilities["features"]

    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel = _channel(api, headers)

    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "give me a list of items to make bread", "intent": "auto"},
    )
    assert accepted.status_code == 202, accepted.text
    turn = accepted.json()["turn"]
    assert turn["intent"] == "auto"

    settled(api.client, headers, channel, turn["id"])
    (call,) = concierge.calls
    assert AUTO_RULE in call["persona"]
    assert ANSWER_RULE in call["persona"]
    assert "start_workload" in call["persona"]
    assert call["allow_actions"] is True


def test_a_runner_turn_assigns_the_run_roles_of_the_agents_it_mentions(api: Any) -> None:
    """A code turn naming an agent that declares a run role hands that role
    to admission, without the mention becoming a parallel chat target."""
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    _save(api, "baker", ["planner", "builder"])
    channel = _channel(api, headers)

    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@baker fix the crash in the mailer", "intent": "code"},
    )
    assert accepted.status_code == 202, accepted.text
    turn = accepted.json()["turn"]
    assert turn["targets"] == []
    assert turn["participants"][0]["assignees"] == {"planner": "baker", "builder": "baker"}

    settled(api.client, headers, channel, turn["id"])
    (call,) = concierge.calls
    assert call["work_roles"] == {"planner": "baker", "builder": "baker"}

    joined = api.client.get(f"/v1/channels/{channel}/participants", headers=headers)
    assert [entry["agent_slug"] for entry in joined.json()["data"]] == ["baker"]


def test_an_auto_turn_assigns_the_run_roles_of_the_agents_it_mentions(api: Any) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel = _channel(api, headers)

    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@builder ship the fix", "intent": "auto"},
    )
    assert accepted.status_code == 202, accepted.text
    turn = accepted.json()["turn"]
    assert turn["participants"][0]["assignees"] == {"builder": "builder"}
