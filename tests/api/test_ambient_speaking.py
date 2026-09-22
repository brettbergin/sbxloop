"""A participant that speaks without being addressed.

An agent in `ambient` mode is listening in, not waiting to be named. When
something it cares about is said it may answer on its own. That is only
tolerable if it is cheap to decide and hard to abuse, so a message reaches
an ambient agent through three gates in order:

1. its `interests`, matched against the recent messages without calling
   anything;
2. the same guardrails an agent-to-agent mention passes, plus its own
   hourly cap;
3. a short relevance call on a cheap model, which answers RELEVANT or PASS.

A PASS posts nothing and says so in the audit trail. `ambient = false`,
the shipped default, turns the whole thing off.

Expected values come from the agents' declared interests, the scripted
classifier verdicts and the configured caps, never from the code under test.
"""

from __future__ import annotations

import time
from concurrent.futures import Future
from typing import Any

from sbxloop.agents.definition import AgentSpec
from sbxloop.daemon.concierge import ConciergeReply
from sbxloop.daemon.usagepool import Admission
from tests.api.conftest import build
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_recovery import settled


class ClassifyingConcierge(FakeConcierge):
    """Answers the relevance question with a scripted verdict and records
    which agents were ever asked one."""

    def __init__(
        self, verdicts: dict[str, str] | None = None, replies: dict[str, str] | None = None
    ) -> None:
        super().__init__()
        self.verdicts = verdicts or {}
        self.replies = replies or {}
        self.classified: list[str] = []

    def submit_turn(self, text: str, **kwargs: Any) -> Future[ConciergeReply]:
        key = str(kwargs.get("session_key") or "")
        future: Future[ConciergeReply] = Future()
        if ":ambient:" in key:
            slug = key.rsplit(":", 1)[-1]
            self.classified.append(slug)
            self.calls.append({"text": text, **kwargs})
            future.set_result(ConciergeReply(self.verdicts.get(slug, "PASS")))
            return future
        speaker = key.rsplit(":", 1)[-1]
        if speaker in self.replies:
            self.calls.append({"text": text, **kwargs})
            future.set_result(ConciergeReply(self.replies[speaker]))
            return future
        return super().submit_turn(text, **kwargs)

    def answered(self, slug: str) -> list[dict[str, Any]]:
        """The calls in which ``slug`` spoke in the channel, not the
        classifier's calls about it."""
        return [
            call
            for call in self.calls
            if ":ambient:" not in str(call.get("session_key") or "")
            and str(call.get("session_key") or "").endswith(f":{slug}")
        ]


def _classifier_calls(api: Any) -> list[dict[str, Any]]:
    return [c for c in api.ctx.concierge.calls if ":ambient:" in str(c.get("session_key") or "")]


def _api(tmp_path: Any, **collaboration: Any) -> Any:
    section = {"ambient": True, "max_chain_depth": 4, **collaboration}
    return build(tmp_path, config={"collaboration": section})


def _agent(api: Any, slug: str, interests: list[str]) -> None:
    api.ctx.agents.create(
        AgentSpec.model_validate(
            {
                "slug": slug,
                "name": slug.title(),
                "instructions": f"Be {slug}.",
                "interests": interests,
            }
        ),
        by="test",
    )


def _listening(api: Any, headers: dict[str, str], channel: str, slug: str) -> None:
    response = api.client.put(
        f"/v1/channels/{channel}/participants/{slug}",
        json={"mode": "ambient"},
        headers=headers,
    )
    assert response.status_code in {200, 201}, response.text


def _say(api: Any, headers: dict[str, str], channel: str, content: str) -> dict[str, Any]:
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns", json={"content": content}, headers=headers
    )
    assert accepted.status_code == 202, accepted.text
    turn = accepted.json()["turn"]
    settled(api.client, headers, channel, turn["id"])
    assert api.ctx.turns.wait_idle(timeout=10), "the channel never went quiet"
    return dict(turn)


def _turns(api: Any, headers: dict[str, str], channel: str) -> list[dict[str, Any]]:
    return list(api.client.get(f"/v1/channels/{channel}/turns", headers=headers).json())


def _events(api: Any, headers: dict[str, str], channel: str) -> list[dict[str, Any]]:
    page = api.client.get(
        "/v1/events",
        params={"type_prefix": "collaboration.followup", "channel_id": channel, "limit": 100},
        headers=headers,
    )
    assert page.status_code == 200, page.text
    return list(page.json()["data"])


def test_an_ambient_agent_whose_interests_match_speaks_without_being_named(
    tmp_path: Any,
) -> None:
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = ClassifyingConcierge({"baker": "RELEVANT"})
        headers = bearer(register(api))
        _agent(api, "baker", ["bread", "sourdough"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")

        _say(api, headers, channel, "I want to make sourdough this weekend")

        assert api.ctx.concierge.classified == ["baker"]
        turns = _turns(api, headers, channel)
        ambient = [turn for turn in turns if turn["trigger"] == "ambient"]
        assert len(ambient) == 1
        assert ambient[0]["targets"] == ["baker"]
        assert ambient[0]["chain_depth"] == 1
        messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
        assert "baker" in [message["agent_slug"] for message in messages]
    api.ctx.close()


def test_nothing_it_cares_about_costs_nothing(tmp_path: Any) -> None:
    """The prefilter is the point: an agent with no matching interest is
    never classified, so an idle channel never calls a model."""
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = ClassifyingConcierge({"baker": "RELEVANT"})
        headers = bearer(register(api))
        _agent(api, "baker", ["bread", "sourdough"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")

        _say(api, headers, channel, "what time is the meeting tomorrow")

        assert api.ctx.concierge.classified == []
        assert [turn["trigger"] for turn in _turns(api, headers, channel)] == ["human"]
    api.ctx.close()


def test_a_pass_posts_nothing_and_says_why(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = ClassifyingConcierge({"baker": "PASS"})
        headers = bearer(register(api))
        _agent(api, "baker", ["bread"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")

        _say(api, headers, channel, "the bread is in the oven")

        assert api.ctx.concierge.classified == ["baker"]
        assert [turn["trigger"] for turn in _turns(api, headers, channel)] == ["human"]
        events = _events(api, headers, channel)
        suppressed = [e for e in events if e["type"].endswith("suppressed")]
        assert [e["data"]["reason"] for e in suppressed] == ["ambient_pass"]
        assert suppressed[0]["data"]["agent_slug"] == "baker"
        # One decision, one record: nothing claims a turn was queued.
        assert [e for e in events if e["type"].endswith("queued")] == []
    api.ctx.close()


def test_an_agent_never_answers_its_own_message(tmp_path: Any) -> None:
    """The mention that starts the conversation makes the agent a
    participant; its own reply must not then be something it answers."""
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = ClassifyingConcierge({"baker": "RELEVANT"})
        headers = bearer(register(api))
        _agent(api, "baker", ["reply", "bread"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")

        _say(api, headers, channel, "@baker how is the bread")

        # The turn it was named in, and nothing it started for itself.
        assert "baker" not in api.ctx.concierge.classified
        assert [turn["trigger"] for turn in _turns(api, headers, channel)] == ["human"]
    api.ctx.close()


def test_a_silenced_channel_keeps_an_ambient_agent_quiet(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = ClassifyingConcierge({"baker": "RELEVANT"})
        headers = bearer(register(api))
        _agent(api, "baker", ["bread"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")
        silenced = api.client.put(
            f"/v1/channels/{channel}/silence",
            json={"until": api.clock() + 300},
            headers=headers,
        )
        assert silenced.status_code == 200, silenced.text

        _say(api, headers, channel, "the bread is rising")

        # Refused before anything was spent on classifying it.
        assert api.ctx.concierge.classified == []
        suppressed = [e for e in _events(api, headers, channel) if e["type"].endswith("suppressed")]
        assert [e["data"]["reason"] for e in suppressed] == ["silenced"]
        assert [turn["trigger"] for turn in _turns(api, headers, channel)] == ["human"]
    api.ctx.close()


def test_an_ambient_agent_has_an_hourly_cap(tmp_path: Any) -> None:
    api = _api(tmp_path, ambient_max_per_hour=1, pair_cooldown_s=0)
    with api.client:
        api.ctx.concierge = ClassifyingConcierge({"baker": "RELEVANT"})
        headers = bearer(register(api))
        _agent(api, "baker", ["bread"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")

        _say(api, headers, channel, "the bread is rising")
        _say(api, headers, channel, "the bread is still rising")

        ambient = [t for t in _turns(api, headers, channel) if t["trigger"] == "ambient"]
        assert len(ambient) == 1
        suppressed = [e for e in _events(api, headers, channel) if e["type"].endswith("suppressed")]
        assert "ambient_cap" in [e["data"]["reason"] for e in suppressed]
    api.ctx.close()


def test_ambient_off_is_the_whole_feature_off(tmp_path: Any) -> None:
    api = _api(tmp_path, ambient=False)
    with api.client:
        api.ctx.concierge = ClassifyingConcierge({"baker": "RELEVANT"})
        headers = bearer(register(api))
        _agent(api, "baker", ["bread"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")

        _say(api, headers, channel, "the bread is rising")

        assert api.ctx.concierge.classified == []
        assert _events(api, headers, channel) == []
        assert [turn["trigger"] for turn in _turns(api, headers, channel)] == ["human"]
    api.ctx.close()


def test_each_listener_looks_at_a_message_once(tmp_path: Any) -> None:
    """Two listeners, one of which passes. The turn the other one takes
    answers the same message and must not put it in front of the listener
    that already passed on it."""
    api = _api(tmp_path, ambient_window_messages=1, pair_cooldown_s=0)
    with api.client:
        # Angie's own answer keeps the topic newest, so anything that looks
        # at the conversation again would still find something to match.
        api.ctx.concierge = ClassifyingConcierge(
            {"baker": "RELEVANT", "miller": "PASS"}, replies={"angie": "bread takes a while"}
        )
        headers = bearer(register(api))
        _agent(api, "baker", ["bread"])
        _agent(api, "miller", ["bread"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")
        _listening(api, headers, channel, "miller")

        _say(api, headers, channel, "the bread is rising")

        assert sorted(api.ctx.concierge.classified) == ["baker", "miller"]
        ambient = [t for t in _turns(api, headers, channel) if t["trigger"] == "ambient"]
        assert [t["targets"] for t in ambient] == [["baker"]]
    api.ctx.close()


def test_an_unprompted_answer_carries_no_authority(tmp_path: Any) -> None:
    """Nobody asked the listener anything, so it answers without tools,
    without handing off, and is told the message was not a request."""
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = ClassifyingConcierge({"baker": "RELEVANT"})
        headers = bearer(register(api))
        _agent(api, "baker", ["bread"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")

        _say(api, headers, channel, "the bread is rising")

        (call,) = api.ctx.concierge.answered("baker")
        assert call["allow_actions"] is False
        assert call["read_only"] is True
        assert call["handoff"] is None
        assert call["handoff_agents"] is None
        assert "the bread is rising" in call["text"]
        assert call["text"] != "the bread is rising"
        assert "not a request" in call["text"]
    api.ctx.close()


def test_the_classifier_starts_fresh_every_time(tmp_path: Any) -> None:
    """Each relevance call stands alone: no earlier verdict or transcript
    rides along in a resumed session."""
    api = _api(tmp_path, ambient_max_per_hour=10, pair_cooldown_s=0)
    with api.client:
        api.ctx.concierge = ClassifyingConcierge({"baker": "PASS"})
        headers = bearer(register(api))
        _agent(api, "baker", ["bread"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")

        _say(api, headers, channel, "the bread is rising")
        _say(api, headers, channel, "the bread is baked")

        asks = _classifier_calls(api)
        assert len(asks) == 2
        assert all(call.get("stateless") is True for call in asks)
    api.ctx.close()


def test_the_classifier_runs_on_the_ambient_model(tmp_path: Any) -> None:
    api = _api(tmp_path, ambient_model="small-cheap-model")
    with api.client:
        api.ctx.concierge = ClassifyingConcierge({"baker": "PASS"})
        headers = bearer(register(api))
        _agent(api, "baker", ["bread"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")

        _say(api, headers, channel, "the bread is rising")

        (ask,) = _classifier_calls(api)
        assert ask["model"] == "small-cheap-model"
    api.ctx.close()


def test_without_an_ambient_model_the_classifier_uses_the_concierges(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = ClassifyingConcierge({"baker": "PASS"})
        headers = bearer(register(api))
        _agent(api, "baker", ["bread"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")

        _say(api, headers, channel, "the bread is rising")

        (ask,) = _classifier_calls(api)
        # No override: the concierge resolves its own configured model.
        assert ask["model"] is None
    api.ctx.close()


def test_an_agents_reply_can_draw_an_unprompted_answer(tmp_path: Any) -> None:
    api = _api(tmp_path, ambient_window_messages=1)
    with api.client:
        api.ctx.concierge = ClassifyingConcierge(
            {"baker": "RELEVANT"}, replies={"helper": "you will need flour for the bread"}
        )
        headers = bearer(register(api))
        _agent(api, "helper", [])
        _agent(api, "baker", ["bread"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")

        _say(api, headers, channel, "@helper what do I need this weekend")

        assert api.ctx.concierge.classified == ["baker"]
        ambient = [t for t in _turns(api, headers, channel) if t["trigger"] == "ambient"]
        assert len(ambient) == 1
        assert ambient[0]["targets"] == ["baker"]
        assert ambient[0]["author_id"] == "helper"
    api.ctx.close()


def test_a_listener_an_agent_names_is_answered_by_the_mention_alone(tmp_path: Any) -> None:
    """A reply that names a listening agent queues that agent through the
    mention. The ambient path stays out of it: no second look, no second
    turn, no stray refusal in the audit trail."""
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = ClassifyingConcierge(
            {"baker": "RELEVANT"}, replies={"helper": "@baker how long should it proof"}
        )
        headers = bearer(register(api))
        _agent(api, "helper", [])
        _agent(api, "baker", ["proof"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")

        _say(api, headers, channel, "@helper ask about the dough")

        assert api.ctx.concierge.classified == []
        baker = [t for t in _turns(api, headers, channel) if t["targets"] == ["baker"]]
        assert [t["trigger"] for t in baker] == ["mention"]
        events = _events(api, headers, channel)
        assert [e for e in events if e["type"].endswith("suppressed")] == []
    api.ctx.close()


class _EmptyPool:
    """A workspace budget with nothing left in it."""

    def admit_turn(self, channel_id: str | None, agent_slug: str | None, now: float) -> Admission:
        return Admission(ok=False, reason="budget", retry_at=now + 60)


def test_a_spent_budget_keeps_an_ambient_agent_quiet(tmp_path: Any) -> None:
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = ClassifyingConcierge({"baker": "RELEVANT"})
        headers = bearer(register(api))
        _agent(api, "baker", ["bread"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")
        api.ctx.guardrails.pool = _EmptyPool()

        _say(api, headers, channel, "the bread is rising")

        assert api.ctx.concierge.classified == []
        suppressed = [e for e in _events(api, headers, channel) if e["type"].endswith("suppressed")]
        assert [e["data"]["reason"] for e in suppressed] == ["budget"]
        assert [turn["trigger"] for turn in _turns(api, headers, channel)] == ["human"]
    api.ctx.close()


def test_an_ambient_turn_a_person_provoked_counts_against_the_rate_caps(
    tmp_path: Any,
) -> None:
    """The caps count turns agents started, whoever wrote the message that
    started them: an ambient answer to a person is still an agent turn."""
    api = _api(tmp_path, channel_turns_per_window=1, ambient_max_per_hour=10, pair_cooldown_s=0)
    with api.client:
        api.ctx.concierge = ClassifyingConcierge({"baker": "RELEVANT"})
        headers = bearer(register(api))
        _agent(api, "baker", ["bread"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")

        _say(api, headers, channel, "the bread is rising")
        _say(api, headers, channel, "the bread is baked")

        turns = _turns(api, headers, channel)
        person = next(t["author_id"] for t in turns if t["trigger"] == "human")
        ambient = [t for t in turns if t["trigger"] == "ambient"]
        assert len(ambient) == 1
        # Provoked by the person's message, not by another agent.
        assert ambient[0]["author_id"] == person
        suppressed = [e for e in _events(api, headers, channel) if e["type"].endswith("suppressed")]
        assert [e["data"]["reason"] for e in suppressed] == ["channel_rate"]
    api.ctx.close()


def _until(condition: Any, what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert condition(), what


class Holding(ClassifyingConcierge):
    """Holds one call open until the test releases it: the first answer
    asked of ``slug``, or, with ``classifier``, the first relevance question
    asked about it. Everything else is answered as scripted."""

    def __init__(
        self,
        slug: str,
        *,
        classifier: bool = False,
        verdicts: dict[str, str] | None = None,
        replies: dict[str, str] | None = None,
    ) -> None:
        super().__init__(verdicts, replies)
        self.slug = slug
        self.classifier = classifier
        self.held: Future[ConciergeReply] = Future()

    def submit_turn(self, text: str, **kwargs: Any) -> Future[ConciergeReply]:
        key = str(kwargs.get("session_key") or "")
        relevance = ":ambient:" in key
        if (
            key.rsplit(":", 1)[-1] == self.slug
            and relevance == self.classifier
            and not self.held.done()
        ):
            if relevance:
                self.classified.append(self.slug)
            self.calls.append({"text": text, **kwargs})
            return self.held
        return super().submit_turn(text, **kwargs)


def test_a_stop_that_lands_during_classification_keeps_the_listener_quiet(
    tmp_path: Any,
) -> None:
    """Stop is the person's kill switch. A listener whose relevance call was
    already in flight when the channel was stopped and silenced does not get
    a turn out of it, whatever the classifier then answers."""
    api = _api(tmp_path)
    with api.client:
        concierge = Holding("baker", classifier=True)
        api.ctx.concierge = concierge
        headers = bearer(register(api))
        _agent(api, "baker", ["bread"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")

        accepted = api.client.post(
            f"/v1/channels/{channel}/turns",
            json={"content": "the bread is rising"},
            headers=headers,
        )
        assert accepted.status_code == 202, accepted.text
        turn = accepted.json()["turn"]
        try:
            _until(lambda: concierge.classified == ["baker"], "the classifier was never asked")
            stopped = api.client.post(f"/v1/channels/{channel}/stop", headers=headers)
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["silenced_until"] is not None
        finally:
            if not concierge.held.done():
                concierge.held.set_result(ConciergeReply("RELEVANT"))
        settled(api.client, headers, channel, turn["id"])
        assert api.ctx.turns.wait_idle(timeout=10), "the channel never went quiet"

        turns = _turns(api, headers, channel)
        assert [(t["trigger"], t["status"]) for t in turns] == [("human", "cancelled")]
        assert concierge.answered("baker") == []
        assert [e for e in _events(api, headers, channel) if e["type"].endswith("queued")] == []
    api.ctx.close()


def test_a_cancelled_turns_late_reply_draws_no_unprompted_answer(tmp_path: Any) -> None:
    """A person cancels the turn while the addressed agent is still
    answering. The reply that lands afterwards is kept, but it is not an
    invitation: no listener is classified on it and none gets a turn."""
    api = _api(tmp_path, ambient_window_messages=1)
    with api.client:
        concierge = Holding("helper", verdicts={"baker": "RELEVANT"})
        api.ctx.concierge = concierge
        headers = bearer(register(api))
        _agent(api, "helper", [])
        _agent(api, "baker", ["bread"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")

        accepted = api.client.post(
            f"/v1/channels/{channel}/turns",
            json={"content": "@helper what do I need this weekend"},
            headers=headers,
        )
        assert accepted.status_code == 202, accepted.text
        turn = accepted.json()["turn"]
        try:
            _until(lambda: bool(concierge.answered("helper")), "helper was never asked")
            cancelled = api.client.post(
                f"/v1/channels/{channel}/turns/{turn['id']}/cancel", headers=headers
            )
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["status"] == "cancelling"
        finally:
            if not concierge.held.done():
                concierge.held.set_result(ConciergeReply("you will need flour for the bread"))
        settled(api.client, headers, channel, turn["id"])
        assert api.ctx.turns.wait_idle(timeout=10), "the channel never went quiet"

        turns = _turns(api, headers, channel)
        assert [(t["trigger"], t["status"]) for t in turns] == [("human", "cancelled")]
        assert concierge.classified == []
        assert concierge.answered("baker") == []
        assert [e for e in _events(api, headers, channel) if e["type"].endswith("queued")] == []
    api.ctx.close()


def test_an_unprompted_answer_to_a_guest_is_recorded_as_the_agents_own(tmp_path: Any) -> None:
    """A guest on a linked surface has no account. The turn a listener
    takes on the guest's message is recorded as the listener's own turn,
    never as a person whose id happens to be the agent's slug."""
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = ClassifyingConcierge({"baker": "RELEVANT"})
        headers = bearer(register(api))
        owner_id = api.client.get("/v1/users/me", headers=headers).json()["id"]
        _agent(api, "baker", ["bread"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")
        store = api.ctx.collaboration
        link = store.create_channel_link(
            None,
            channel,
            backend="slack",
            surface_id="C1",
            thread_id=None,
            allow_guests=True,
            created_by=owner_id,
            now=api.clock(),
        )

        turn, message = api.ctx.accept_bridge_turn(
            link,
            content="the bread is rising",
            author_user_id=None,
            display_name="stranger",
            external_message_id="m1",
        )
        settled(api.client, headers, channel, turn.id)
        assert api.ctx.turns.wait_idle(timeout=10), "the channel never went quiet"

        assert api.ctx.concierge.classified == ["baker"]
        ambient = [t for t in _turns(api, headers, channel) if t["trigger"] == "ambient"]
        assert len(ambient) == 1
        assert ambient[0]["targets"] == ["baker"]
        assert ambient[0]["input_message_id"] == message.id
        recorded = store.get_turn(None, channel, ambient[0]["id"])
        assert recorded is not None and recorded.author is not None
        assert (recorded.author.kind, recorded.author.id) == ("agent", "baker")
        assert ambient[0]["author_id"] == "baker"
        assert ambient[0]["status"] == "completed"
        messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
        assert "baker" in [m["agent_slug"] for m in messages]
    api.ctx.close()


def test_only_the_new_message_is_matched_against_interests(tmp_path: Any) -> None:
    """One mention of an interest a few messages ago is not a reason to
    classify every later message: the prefilter reads the message that
    just arrived, and an unrelated one costs nothing."""
    api = _api(tmp_path)
    with api.client:
        api.ctx.concierge = ClassifyingConcierge({"baker": "PASS"})
        headers = bearer(register(api))
        _agent(api, "baker", ["bread"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")

        _say(api, headers, channel, "the bread is rising")
        _say(api, headers, channel, "what time is the meeting tomorrow")

        assert api.ctx.concierge.classified == ["baker"]
    api.ctx.close()


def test_the_person_is_answered_before_any_listener_is_classified(tmp_path: Any) -> None:
    """The agents the person addressed answer first. Deciding whether a
    listener has something to add happens after them, so a slow relevance
    call never holds up the person's own turn; the listener still answers
    the person's message."""
    api = _api(tmp_path)
    with api.client:
        concierge = Holding("baker", classifier=True, replies={"helper": "flour, mostly"})
        api.ctx.concierge = concierge
        headers = bearer(register(api))
        _agent(api, "helper", [])
        _agent(api, "baker", ["weekend"])
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        _listening(api, headers, channel, "baker")

        accepted = api.client.post(
            f"/v1/channels/{channel}/turns",
            json={"content": "@helper what do I need this weekend"},
            headers=headers,
        )
        assert accepted.status_code == 202, accepted.text
        turn = accepted.json()["turn"]
        try:
            _until(lambda: concierge.classified == ["baker"], "the classifier was never asked")
            # The relevance question is still open; the person has been answered.
            assert len(concierge.answered("helper")) == 1
            messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
            assert "flour, mostly" in [m["content"] for m in messages]
        finally:
            if not concierge.held.done():
                concierge.held.set_result(ConciergeReply("RELEVANT"))
        settled(api.client, headers, channel, turn["id"])
        assert api.ctx.turns.wait_idle(timeout=10), "the channel never went quiet"

        ambient = [t for t in _turns(api, headers, channel) if t["trigger"] == "ambient"]
        assert len(ambient) == 1
        assert ambient[0]["targets"] == ["baker"]
        assert ambient[0]["parent_turn_id"] == turn["id"]
        assert ambient[0]["input_message_id"] == turn["input_message_id"]
        assert len(concierge.answered("baker")) == 1
    api.ctx.close()
