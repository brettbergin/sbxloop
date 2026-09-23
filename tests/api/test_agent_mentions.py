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
from tests.api.test_channel_access import _invite
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_controls import Blocking
from tests.api.test_collaboration_recovery import settled
from tests.api.test_control import in_flight
from tests.unit.test_daemon_loop import gh_item

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


def test_a_chain_runs_to_the_default_depth_of_four_and_no_further(tmp_path: Any) -> None:
    """With the shipped depth cap, two agents naming each other get four
    agent-started turns after the person's, and the fifth is refused."""
    api = build(tmp_path, config={"collaboration": {"pair_cooldown_s": 0}})
    with api.client:
        assert api.ctx.config.collaboration.max_chain_depth == 4
        api.ctx.concierge = ScriptedConcierge(
            {"planner": "over to @critic", "critic": "back to @planner"}
        )
        headers = bearer(register(api))
        channel = _channel(api, headers)
        _ask(api, headers, channel, "@planner plan the bake")

        turns = _turns(api, headers, channel)
        assert [turn["chain_depth"] for turn in turns] == [0, 1, 2, 3, 4]
        assert [turn["targets"] for turn in turns[1:]] == [
            ["critic"],
            ["planner"],
            ["critic"],
            ["planner"],
        ]
        suppressed = [e for e in _events(api, headers, channel) if e["type"].endswith("suppressed")]
        assert [e["data"]["reason"] for e in suppressed] == ["chain_depth"]
        assert suppressed[0]["data"]["chain_depth"] == 5
    api.ctx.close()


class SpendingConcierge(ScriptedConcierge):
    """Answers as scripted and, as the real concierge does, charges what
    the turn spent to the workspace pool: here, more than the day's budget
    in one turn."""

    def __init__(self, api: Any, replies: dict[str, str], tokens: int) -> None:
        super().__init__(replies)
        self.api = api
        self.tokens = tokens

    def submit_turn(self, text: str, **kwargs: Any) -> Any:
        self.api.loop.dstore.record_usage(
            ts=self.api.clock(),
            source="turn",
            ref_id=kwargs.get("message_id") or "turn",
            agent_slug=kwargs.get("agent_slug"),
            channel_id=kwargs.get("channel_id"),
            input_tokens=self.tokens,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        return super().submit_turn(text, **kwargs)


def test_a_spent_token_budget_refuses_the_follow_up(tmp_path: Any) -> None:
    """The workspace budget is the last guardrail: once today's tokens are
    spent, an agent naming another agent starts nothing. The person's own
    turn is what spends them here; a budget spent before the person asks
    refuses that turn itself (``test_turn_budget``)."""
    api = build(
        tmp_path,
        config={
            "collaboration": dict(CAPS["collaboration"]),
            "daemon": {"daily_token_budget": 100},
        },
    )
    with api.client:
        api.ctx.concierge = SpendingConcierge(api, {"planner": "over to @critic"}, tokens=1000)
        headers = bearer(register(api))
        channel = _channel(api, headers)
        _ask(api, headers, channel, "@planner plan the bake")

        assert len(_turns(api, headers, channel)) == 1
        suppressed = [e for e in _events(api, headers, channel) if e["type"].endswith("suppressed")]
        assert [e["data"]["reason"] for e in suppressed] == ["token_budget"]
    api.ctx.close()


def test_an_agent_started_turn_is_a_read_only_peer_message_not_the_persons_ask(
    tmp_path: Any,
) -> None:
    """The follow-up's input is text another agent wrote. It is framed as
    that agent speaking, carries no new human approval, and answers with
    read-only tools and no handoff, so one agent's prose cannot make another
    act on the person's authority."""
    api = _api(tmp_path)
    with api.client:
        concierge = ScriptedConcierge({"planner": "Plan ready. @critic please review it."})
        api.ctx.concierge = concierge
        headers = bearer(register(api))
        channel = _channel(api, headers)
        _ask(api, headers, channel, "@planner plan the bake")

        assert len(concierge.calls) == 2, concierge.calls
        person, peer = concierge.calls
        assert person["author"] == "Local Owner"
        assert person["read_only"] is False
        assert peer["session_key"].endswith(":critic")
        assert peer["author"] != "Local Owner"
        assert "planner" in peer["author"]
        assert peer["author_id"] != person["author_id"]
        assert peer["read_only"] is True
        assert peer["handoff"] is None
        assert peer["handoff_agents"] is None
        assert "Plan ready. @critic please review it." in peer["text"]
        assert "not new human approval" in peer["text"]
    api.ctx.close()


def test_an_agent_already_answering_this_turn_gets_no_second_turn(tmp_path: Any) -> None:
    """The person asked planner and critic together. planner's reply names
    critic, who is about to answer in this same turn anyway."""
    api = _api(tmp_path)
    with api.client:
        concierge = ScriptedConcierge({"planner": "Draft done. @critic please check."})
        api.ctx.concierge = concierge
        headers = bearer(register(api))
        channel = _channel(api, headers)
        _ask(api, headers, channel, "@planner @critic plan the bake")

        assert len(_turns(api, headers, channel)) == 1
        assert [c["session_key"].rsplit(":", 1)[-1] for c in concierge.calls] == [
            "planner",
            "critic",
        ]
        assert _events(api, headers, channel) == []
    api.ctx.close()


class HandoffThenMention(ScriptedConcierge):
    """planner hands off to critic with the tool and also names critic in prose."""

    def submit_turn(self, text: str, **kwargs: Any) -> Any:
        if not self.calls and kwargs.get("handoff") is not None:
            kwargs["handoff"]("critic", "Check the proposed plan")
        return super().submit_turn(text, **kwargs)


def test_an_agent_handed_off_to_in_this_turn_gets_no_second_turn(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        concierge = HandoffThenMention({"planner": "I asked @critic to check the plan."})
        api.ctx.concierge = concierge
        headers = bearer(register(api))
        channel = _channel(api, headers)
        _ask(api, headers, channel, "@planner plan the bake")

        assert len(_turns(api, headers, channel)) == 1
        assert [c["session_key"].rsplit(":", 1)[-1] for c in concierge.calls] == [
            "planner",
            "critic",
        ]
        assert _events(api, headers, channel) == []
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


def test_stop_cancels_the_running_and_queued_turns_and_starts_nothing_after(
    tmp_path: Any,
) -> None:
    """Stop with one turn running and another queued behind it: both are
    cancelled, both settle as cancelled, and the reply still in flight when
    the stop landed starts no follow-up of its own."""
    api = _api(tmp_path)
    with api.client:
        concierge = Blocking()
        api.ctx.concierge = concierge
        headers = bearer(register(api))
        channel = _channel(api, headers)
        route = f"/v1/channels/{channel}/turns"
        running = api.client.post(
            route, json={"content": "@planner plan the bake"}, headers=headers
        ).json()["turn"]
        queued = api.client.post(route, json={"content": "@planner and then?"}, headers=headers)
        queued_turn = queued.json()["turn"]
        try:
            deadline = time.monotonic() + 5
            while not concierge.calls and time.monotonic() < deadline:
                time.sleep(0.01)
            assert concierge.calls, "the first turn never started"

            stopped = api.client.post(f"/v1/channels/{channel}/stop", headers=headers)
            assert stopped.status_code == 200, stopped.text
            body = stopped.json()
            assert set(body["cancelled_turns"]) == {running["id"], queued_turn["id"]}
            assert body["cancelled_runs"] == []
            assert body["silenced_until"] is not None
            assert (
                api.client.get(f"/v1/channels/{channel}", headers=headers).json()["silenced_until"]
                == body["silenced_until"]
            )
        finally:
            # The running reply lands after the stop and names another agent.
            if not concierge.first.done():
                concierge.first.set_result(ConciergeReply("over to @critic"))
        _quiet(api)

        turns = {turn["id"]: turn for turn in _turns(api, headers, channel)}
        assert set(turns) == {running["id"], queued_turn["id"]}
        assert turns[running["id"]]["status"] == "cancelled"
        assert turns[queued_turn["id"]]["status"] == "cancelled"
        assert [e for e in _events(api, headers, channel) if e["type"].endswith("queued")] == []
        assert len(concierge.calls) == 1

        resumed = api.client.post(f"/v1/channels/{channel}/resume", headers=headers)
        assert resumed.json()["silenced_until"] is None
    api.ctx.close()


def test_a_members_stop_cancels_the_runs_and_queued_work_the_channel_asked_for(
    tmp_path: Any,
) -> None:
    """Stop takes post, not run control: a plain workspace member who may
    post in the channel stops what that channel started. Work another
    channel asked for is untouched."""
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = FakeConcierge()
        register(api)
        member = bearer(_invite(api, "member", "guest"))
        channel = _channel(api, member)
        other = _channel(api, member)

        thread, run_id, release = in_flight(api, gh_item("1", channel_id=channel))
        try:
            now = api.clock()
            api.loop.dstore.upsert_new(gh_item("2", channel_id=channel), now)
            api.loop.dstore.upsert_new(gh_item("3", channel_id=other), now)

            stopped = api.client.post(f"/v1/channels/{channel}/stop", headers=member)
            assert stopped.status_code == 200, stopped.text
            body = stopped.json()
            assert body["cancelled_runs"] == [run_id]
            assert body["cancelled_items"] == ["gh:issue:2"]
        finally:
            release.set()
            thread.join(10)

        running = api.loop.dstore.get("gh:issue:1")
        assert running is not None and running.state == "cancelled"
        abandoned = api.loop.dstore.get("gh:issue:2")
        assert abandoned is not None and abandoned.state == "failed"
        untouched = api.loop.dstore.get("gh:issue:3")
        assert untouched is not None and untouched.state == "queued"
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


def test_a_refused_mention_does_not_add_the_agent_to_the_roster(tmp_path: Any) -> None:
    """The roster says who is in the conversation. An agent whose follow-up
    the guardrails refused never got in, so a reply naming it in a silenced
    channel leaves the roster as it was."""
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

        _ask(api, headers, channel, "@planner plan the bake")

        suppressed = [e for e in _events(api, headers, channel) if e["type"].endswith("suppressed")]
        assert [e["data"]["reason"] for e in suppressed] == ["silenced"]
        listed = api.client.get(f"/v1/channels/{channel}/participants", headers=headers)
        assert listed.status_code == 200, listed.text
        assert "critic" not in [p["agent_slug"] for p in listed.json()["data"]]
    api.ctx.close()


def test_a_roster_failure_drops_only_that_mention() -> None:
    """Joining one named agent fails; the others named in the same reply
    are still queued, and the one that failed is neither joined nor queued."""
    from sbxloop.api.mentions import MentionRouter
    from sbxloop.daemon.usagepool import Admission

    joined: list[str] = []
    queued: list[str] = []

    def join(channel_id: str, slug: str) -> None:
        if slug == "critic":
            raise RuntimeError("roster unavailable")
        joined.append(slug)

    router = MentionRouter(
        resolve=lambda slug: slug,
        participants=lambda channel_id: [],
        join=join,
        admit=lambda channel_id, **kwargs: Admission(ok=True),
        queue=lambda **kwargs: queued.append(str(kwargs["target_slug"])),
    )

    routed = router.route(
        "@critic and @planner, thoughts?",
        channel_id="ch_1",
        source_message_id="msg_1",
        author_slug="helper",
        reply_to_author=None,
        depth=1,
    )

    assert routed == ("planner",)
    assert queued == ["planner"]
    assert joined == ["planner"]
