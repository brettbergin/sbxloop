"""Agents mentioning agents, and the guardrails around it.

An agent's reply is prose in a shared channel, so `@another-agent` in it is
an address, not decoration. The router turns one into a follow-up turn by
that agent; the guardrails are what keeps a pair of agents from talking to
each other forever: a chain depth, a per-channel and per-agent rate cap
within a window, a cooldown per ordered pair, the channel's silence, and
the workspace token budget. Every decision, allowed or refused, is an
audit event carrying the reason and never the message text.

A human keeps the last word: stop cancels the channel's turns and silences
it, resume lifts the silence, and read state says what has been seen.

Expected values come from the configured caps and the scripted replies,
never from the code under test.
"""

from __future__ import annotations

import time
from typing import Any

from sbxloop.daemon.concierge import ConciergeReply
from tests.api.conftest import build
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_recovery import settled

CAPS = {
    "collaboration": {
        "max_chain_depth": 4,
        "window_s": 600,
        "channel_turns_per_window": 20,
        "agent_turns_per_window": 6,
        "pair_cooldown_s": 60,
    }
}


class ScriptedConcierge(FakeConcierge):
    """Answers with the text scripted for the agent being addressed."""

    def __init__(self, replies: dict[str, str]) -> None:
        super().__init__()
        self.replies = replies

    def submit_turn(self, text: str, **kwargs: Any) -> Any:
        from concurrent.futures import Future

        self.calls.append({"text": text, **kwargs})
        target = kwargs["session_key"].rsplit(":", 1)[-1]
        future: Future[ConciergeReply] = Future()
        future.set_result(ConciergeReply(self.replies.get(target, f"reply from {target}")))
        return future


def _api(tmp_path: Any, **collaboration: Any) -> Any:
    section = dict(CAPS["collaboration"])
    section.update(collaboration)
    return build(tmp_path, config={"collaboration": section})


def _channel(api: Any, headers: dict[str, str]) -> str:
    return str(api.client.post("/v1/channels", json={}, headers=headers).json()["id"])


def _turns(api: Any, headers: dict[str, str], channel: str) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 5
    turns: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        turns = api.client.get(f"/v1/channels/{channel}/turns", headers=headers).json()
        if turns and all(turn["status"] not in {"accepted", "running"} for turn in turns):
            return turns
        time.sleep(0.02)
    return turns


def _quiet(api: Any) -> None:
    """Wait for the whole chain the ask started, not just its first turn."""
    assert api.ctx.turns.wait_idle(timeout=10), "the channel never went quiet"


def _events(api: Any, headers: dict[str, str], channel: str) -> list[dict[str, Any]]:
    page = api.client.get(
        "/v1/events",
        params={"type_prefix": "collaboration.followup", "channel_id": channel, "limit": 100},
        headers=headers,
    )
    assert page.status_code == 200, page.text
    return list(page.json()["data"])


def _ask(api: Any, headers: dict[str, str], channel: str, content: str) -> dict[str, Any]:
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns", json={"content": content}, headers=headers
    )
    assert accepted.status_code == 202, accepted.text
    turn = accepted.json()["turn"]
    settled(api.client, headers, channel, turn["id"])
    _quiet(api)
    return dict(turn)


def test_an_agent_reply_mentioning_an_agent_queues_one_follow_up_turn(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = ScriptedConcierge(
            {"planner": "Here is the plan. @critic please review it."}
        )
        headers = bearer(register(api))
        channel = _channel(api, headers)
        first = _ask(api, headers, channel, "@planner plan the bake")

        turns = _turns(api, headers, channel)
        assert len(turns) == 2, turns
        follow_up = turns[1]
        assert follow_up["trigger"] == "mention"
        assert follow_up["parent_turn_id"] == first["id"]
        assert follow_up["chain_depth"] == 1
        assert follow_up["targets"] == ["critic"]
        assert follow_up["author_id"] == "planner"

        queued = [e for e in _events(api, headers, channel) if e["type"].endswith("queued")]
        assert len(queued) == 1
        assert queued[0]["data"]["agent_slug"] == "critic"
        assert queued[0]["data"]["trigger"] == "mention"
        # The audit line never carries what was said.
        assert "please review" not in repr(queued[0])

        # The critic's own reply is an ordinary channel message.
        messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
        assert [m["agent_slug"] for m in messages if m["role"] == "assistant"] == [
            "planner",
            "critic",
        ]
    api.ctx.close()


def test_a_mention_inside_a_code_fence_or_a_quote_is_not_an_address(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = ScriptedConcierge(
            {"planner": "Run this:\n\n```\n@critic --check\n```\n\n> @operator said so earlier."}
        )
        headers = bearer(register(api))
        channel = _channel(api, headers)
        _ask(api, headers, channel, "@planner plan the bake")

        assert len(_turns(api, headers, channel)) == 1
        assert _events(api, headers, channel) == []
    api.ctx.close()


def test_an_agent_never_addresses_itself(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = ScriptedConcierge({"planner": "@planner will keep going."})
        headers = bearer(register(api))
        channel = _channel(api, headers)
        _ask(api, headers, channel, "@planner plan the bake")

        assert len(_turns(api, headers, channel)) == 1
        assert _events(api, headers, channel) == []
    api.ctx.close()


def test_the_chain_stops_at_the_configured_depth(tmp_path: Any) -> None:
    """Two agents that keep naming each other would never stop on their
    own. At the depth cap the follow-up is refused and the refusal says so."""
    api = _api(tmp_path, max_chain_depth=1, pair_cooldown_s=0)
    with api.client:
        api.ctx.concierge = ScriptedConcierge(
            {"planner": "over to @critic", "critic": "back to @planner"}
        )
        headers = bearer(register(api))
        channel = _channel(api, headers)
        _ask(api, headers, channel, "@planner plan the bake")

        turns = _turns(api, headers, channel)
        assert len(turns) == 2, turns
        assert turns[1]["chain_depth"] == 1
        events = _events(api, headers, channel)
        suppressed = [e for e in events if e["type"].endswith("suppressed")]
        assert [e["data"]["reason"] for e in suppressed] == ["chain_depth"]
        assert suppressed[0]["data"]["agent_slug"] == "planner"
    api.ctx.close()


def test_a_pair_that_just_spoke_waits_out_its_cooldown(tmp_path: Any) -> None:
    api = _api(tmp_path, max_chain_depth=8, pair_cooldown_s=60)
    with api.client:
        api.ctx.concierge = ScriptedConcierge(
            {"planner": "over to @critic", "critic": "a note for @operator"}
        )
        headers = bearer(register(api))
        channel = _channel(api, headers)
        _ask(api, headers, channel, "@planner plan the bake")
        _turns(api, headers, channel)
        # planner -> critic again, inside the cooldown for that ordered pair.
        api.ctx.concierge.replies["planner"] = "over to @critic"
        _ask(api, headers, channel, "@planner plan it again")

        suppressed = [e for e in _events(api, headers, channel) if e["type"].endswith("suppressed")]
        assert "pair_cooldown" in [e["data"]["reason"] for e in suppressed]
    api.ctx.close()


def test_a_channel_over_its_window_cap_refuses_further_agent_turns(tmp_path: Any) -> None:
    api = _api(tmp_path, max_chain_depth=8, pair_cooldown_s=0, channel_turns_per_window=1)
    with api.client:
        api.ctx.concierge = ScriptedConcierge(
            {"planner": "over to @critic", "critic": "over to @operator"}
        )
        headers = bearer(register(api))
        channel = _channel(api, headers)
        _ask(api, headers, channel, "@planner plan the bake")

        suppressed = [e for e in _events(api, headers, channel) if e["type"].endswith("suppressed")]
        assert [e["data"]["reason"] for e in suppressed] == ["channel_rate"]
    api.ctx.close()


def test_one_agent_over_its_window_cap_is_refused_by_name(tmp_path: Any) -> None:
    """The channel is well under its own cap; the agent being addressed for
    a second time in the window is not."""
    api = _api(tmp_path, max_chain_depth=8, pair_cooldown_s=0, agent_turns_per_window=1)
    with api.client:
        api.ctx.concierge = ScriptedConcierge(
            {
                "planner": "over to @critic",
                "critic": "over to @operator",
                "operator": "back to @critic",
            }
        )
        headers = bearer(register(api))
        channel = _channel(api, headers)
        _ask(api, headers, channel, "@planner plan the bake")

        suppressed = [e for e in _events(api, headers, channel) if e["type"].endswith("suppressed")]
        assert "agent_rate" in [e["data"]["reason"] for e in suppressed]
    api.ctx.close()


def test_a_silenced_channel_refuses_follow_ups_until_it_is_resumed(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = ScriptedConcierge({"planner": "over to @critic"})
        headers = bearer(register(api))
        channel = _channel(api, headers)

        silenced = api.client.put(
            f"/v1/channels/{channel}/silence",
            json={"until": api.clock() + 300},
            headers=headers,
        )
        assert silenced.status_code == 200, silenced.text
        assert silenced.json()["silenced_until"] == api.clock() + 300

        _ask(api, headers, channel, "@planner plan the bake")
        suppressed = [e for e in _events(api, headers, channel) if e["type"].endswith("suppressed")]
        assert [e["data"]["reason"] for e in suppressed] == ["silenced"]

        resumed = api.client.post(f"/v1/channels/{channel}/resume", headers=headers)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["silenced_until"] is None
    api.ctx.close()


def test_stop_cancels_the_channels_turns_and_silences_it(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = _channel(api, headers)
        # Accept a turn, then stop before it can settle.
        accepted = api.client.post(
            f"/v1/channels/{channel}/turns",
            json={"content": "@planner plan the bake"},
            headers=headers,
        )
        assert accepted.status_code == 202, accepted.text

        stopped = api.client.post(f"/v1/channels/{channel}/stop", headers=headers)
        assert stopped.status_code == 200, stopped.text
        body = stopped.json()
        assert set(body) >= {"cancelled_turns", "cancelled_runs", "silenced_until"}
        assert body["silenced_until"] is not None
        assert (
            api.client.get(f"/v1/channels/{channel}", headers=headers).json()["silenced_until"]
            == body["silenced_until"]
        )

        resumed = api.client.post(f"/v1/channels/{channel}/resume", headers=headers)
        assert resumed.json()["silenced_until"] is None
    api.ctx.close()


def test_read_state_is_recorded_and_the_channel_reports_what_is_unread(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = _channel(api, headers)
        _ask(api, headers, channel, "hello")

        listing = api.client.get("/v1/channels", headers=headers).json()["items"][0]
        assert listing["unread_count"] == 2

        messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
        last = max(int(message["sequence"]) for message in messages)
        read = api.client.put(
            f"/v1/channels/{channel}/read", json={"sequence": last}, headers=headers
        )
        assert read.status_code == 200, read.text
        assert read.json()["last_read_sequence"] == last

        assert (
            api.client.get(f"/v1/channels/{channel}", headers=headers).json()["unread_count"] == 0
        )
        members = api.client.get(f"/v1/channels/{channel}/members", headers=headers).json()["data"]
        assert members[0]["last_read_sequence"] == last
    api.ctx.close()


def test_the_new_controls_are_advertised(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
        for feature in (
            "collaboration.channel_stop",
            "collaboration.silence",
            "collaboration.read_state",
        ):
            assert feature in features
    api.ctx.close()
