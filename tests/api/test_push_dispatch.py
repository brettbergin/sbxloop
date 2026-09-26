"""What becomes a push, for whom, and what happens when the relay balks.

The dispatcher reads the public chronology live — from where it stood when
it started, never from history — and turns the events a person is waiting
on into a stored notification plus a content-free ping for each of their
devices that wants it: a mention by somebody else, work they asked for
finishing or failing, a decision only they can make. Their own messages
never ping them. The ping goes to :class:`~tests.fakes.fake_relay.FakeRelay`
over HTTP; retries are driven by moving the test clock.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from sbxloop.api.chronology import DAEMON_ACTOR
from sbxloop.api.collaboration import LocalUser, _event
from sbxloop.api.publicids import run_public_id
from tests.api.conftest import Api
from tests.api.test_push_devices import (
    TOKEN_A,
    TOKEN_B,
    device,
    push_api,
    register_member,
    register_owner,
)
from tests.fakes.fake_relay import FakeRelay


@dataclass
class Room:
    api: Api
    relay: FakeRelay
    owner: LocalUser
    bob: LocalUser
    owner_headers: dict[str, str]
    bob_headers: dict[str, str]
    owner_device: str
    bob_device: str
    channel_id: str

    def step(self) -> None:
        self.api.ctx.push.dispatcher.step()

    def pushes_to(self, token: str) -> list[dict[str, Any]]:
        return [sent.payload for sent in self.relay.sent if sent.token == token]

    def notification(self, ref: str, who: str = "owner") -> dict[str, Any]:
        headers = self.owner_headers if who == "owner" else self.bob_headers
        response = self.api.client.get(f"/v1/users/me/notifications/{ref}", headers=headers)
        assert response.status_code == 200, response.text
        return dict(response.json())

    def prefs(self, who: str, **prefs: Any) -> None:
        headers = self.owner_headers if who == "owner" else self.bob_headers
        token = TOKEN_A if who == "owner" else TOKEN_B
        response = self.api.client.post(
            "/v1/users/me/devices", json=device(token, prefs=prefs), headers=headers
        )
        assert response.status_code == 200, response.text

    def say(self, user: LocalUser, content: str, targets: tuple[str, ...] = ()) -> Any:
        turn, _message, _ = self.api.ctx.collaboration.accept_turn(
            user.id,
            self.channel_id,
            content=content,
            targets=targets,
            client_turn_id=None,
            client_message_id=None,
            actor=None,
            now=self.api.clock(),
        )
        return turn

    def attention(self, kind: str, *, historical: bool = False, run_id: str | None = None) -> None:
        with self.api.harness.dstore.transaction() as session:
            _event(
                session,
                "collaboration.external_work.attention",
                self.api.clock(),
                actor=DAEMON_ACTOR,
                data={
                    "channel_id": self.channel_id,
                    "work_id": "wrk_1",
                    "run_id": run_id,
                    "attention_id": f"wrk_1:{kind}",
                    "kind": kind,
                    "title": "Nightly report",
                    "body": "Work is gated.",
                    "historical": historical,
                },
            )


@pytest.fixture
def relay() -> FakeRelay:
    return FakeRelay()


@pytest.fixture
def room(tmp_path: Path, relay: FakeRelay) -> Iterator[Room]:
    api = push_api(tmp_path, relay)
    with api.client:
        owner_headers = register_owner(api)
        bob_headers = register_member(api)
        store = api.ctx.collaboration
        owner = store.user_by_username("owner")
        bob = store.user_by_username("bob")
        assert owner is not None and bob is not None
        channel = store.create_channel(owner.id, "Plans", api.clock())
        store.add_channel_member(owner.id, channel.id, bob.id, "member", api.clock())
        owner_device = api.client.post(
            "/v1/users/me/devices", json=device(TOKEN_A), headers=owner_headers
        ).json()["id"]
        bob_device = api.client.post(
            "/v1/users/me/devices", json=device(TOKEN_B), headers=bob_headers
        ).json()["id"]
        api.ctx.push.dispatcher.prime()
        yield Room(
            api,
            relay,
            owner,
            bob,
            owner_headers,
            bob_headers,
            owner_device,
            bob_device,
            channel.id,
        )
    api.ctx.close()


def agent_name(room: Room, slug: str) -> str:
    agent = room.api.ctx.agents.get(slug)
    assert agent is not None
    return agent.spec.name


# -- mentions ----------------------------------------------------------------------------


def test_a_mention_by_somebody_else_pings_the_person_named(room: Room) -> None:
    room.say(room.bob, "  hey   @OWNER,\n\tcan you look at this?  ")
    room.step()

    [ping] = room.pushes_to(TOKEN_A)
    assert ping["k"] == "mention"
    assert ping["srv"] == "home.server-1"
    assert ping["thread"] == room.channel_id
    notice = room.notification(ping["ref"])
    assert notice["kind"] == "mention"
    assert notice["channel_id"] == room.channel_id
    assert notice["title"] == "Bob Builder mentioned you"
    assert notice["body"] == "hey @OWNER, can you look at this?"
    # The author is never pinged about their own message.
    assert room.pushes_to(TOKEN_B) == []


@pytest.mark.parametrize(
    "content",
    [
        "@ownership is shared",
        "mail owner@example.test",
        "me@owner.test",
        "@owner-team please",
        "no mention at all",
    ],
)
def test_only_a_word_bounded_handle_is_a_mention(room: Room, content: str) -> None:
    room.say(room.bob, content)
    room.step()
    assert room.pushes_to(TOKEN_A) == []


def test_a_long_mention_is_cut_to_a_notification_body(room: Room) -> None:
    room.say(room.bob, "@owner " + "x" * 300)
    room.step()
    [ping] = room.pushes_to(TOKEN_A)
    body = room.notification(ping["ref"])["body"]
    assert len(body) == 140 and body.endswith("…")
    assert body == ("@owner " + "x" * 300)[:139] + "…"


def test_mentioning_yourself_is_not_news(room: Room) -> None:
    room.say(room.owner, "note to @owner: buy milk")
    room.step()
    assert room.relay.sent == []


def test_a_mention_in_a_channel_you_cannot_see_is_not_sent(room: Room) -> None:
    store = room.api.ctx.collaboration
    secret = store.create_channel(room.bob.id, "Bob's own", room.api.clock())
    store.accept_turn(
        room.bob.id,
        secret.id,
        content="talking about @owner behind their back",
        targets=(),
        client_turn_id=None,
        client_message_id=None,
        actor=None,
        now=room.api.clock(),
    )
    room.step()
    assert room.relay.sent == []


# -- work, replies and failures ------------------------------------------------------------


def _deliver(room: Room, turn_id: str, state: str, title: str | None, slug: str | None) -> None:
    room.api.ctx.collaboration.append_work_result(
        f"msg_work_{state}",
        channel_id=room.channel_id,
        turn_id=turn_id,
        content="the result",
        agent_slug=slug,
        work={"item_id": "itm_1", "state": state, "title": title, "agent_slug": slug},
        now=room.api.clock(),
    )


@pytest.mark.parametrize(
    ("state", "kind", "title", "body"),
    [
        (
            "merged",
            "work",
            "{agent} delivered Fix the login",
            "Changes merged. The result is in the chat.",
        ),
        ("completed", "work", "{agent} delivered Fix the login", "The result is in the chat."),
        (
            "failed",
            "failure",
            "{agent} could not finish Fix the login",
            "The run ended failed. The details are in the chat.",
        ),
        (
            "blocked",
            "failure",
            "{agent} could not finish Fix the login",
            "The run ended blocked. The details are in the chat.",
        ),
        (
            "cancelled",
            "failure",
            "{agent} could not finish Fix the login",
            "The run ended cancelled. The details are in the chat.",
        ),
    ],
)
def test_delivered_work_pings_the_person_who_asked(
    room: Room, state: str, kind: str, title: str, body: str
) -> None:
    turn = room.say(room.owner, "please fix the login", ("planner",))
    room.step()
    _deliver(room, turn.id, state, "Fix the login", "planner")
    room.step()

    [ping] = room.pushes_to(TOKEN_A)
    assert ping["k"] == kind
    notice = room.notification(ping["ref"])
    assert notice["title"] == title.format(agent=agent_name(room, "planner"))
    assert notice["body"] == body
    assert notice["turn_id"] == turn.id
    # Bob can see the channel, but he did not ask for this.
    assert room.pushes_to(TOKEN_B) == []


def test_work_with_no_title_is_still_named(room: Room) -> None:
    turn = room.say(room.owner, "do the thing")
    _deliver(room, turn.id, "completed", None, None)
    room.step()
    [ping] = room.pushes_to(TOKEN_A)
    assert room.notification(ping["ref"])["title"].endswith(" delivered your work")


def test_a_finished_turn_pings_its_asker_and_a_failed_one_says_why(room: Room) -> None:
    store = room.api.ctx.collaboration
    replied = room.say(room.bob, "question", ("planner",))
    store.start_turn(replied.id, room.api.clock())
    store.finish_turn(replied.id, error=None, now=room.api.clock())
    broke = room.say(room.bob, "another", ("planner",))
    store.start_turn(broke.id, room.api.clock())
    store.finish_turn(broke.id, error="the model is unavailable", now=room.api.clock())
    room.step()

    planner = agent_name(room, "planner")
    pings = room.pushes_to(TOKEN_B)
    assert [p["k"] for p in pings] == ["work", "failure"]
    first, second = (room.notification(p["ref"], "bob") for p in pings)
    assert (first["title"], first["body"]) == (
        f"{planner} replied",
        "A new reply is waiting in the chat.",
    )
    assert (second["title"], second["body"]) == (
        f"{planner} could not reply",
        "the model is unavailable",
    )
    assert room.pushes_to(TOKEN_A) == []


# -- attention and gates ----------------------------------------------------------------


def test_a_decision_goes_only_to_people_who_can_make_it(room: Room) -> None:
    room.attention("action_required")
    room.step()
    [ping] = room.pushes_to(TOKEN_A)
    assert ping["k"] == "gate"
    notice = room.notification(ping["ref"])
    assert (notice["title"], notice["body"]) == ("Nightly report", "Work is gated.")
    # Bob is a member: he cannot approve a gate.
    assert room.pushes_to(TOKEN_B) == []


@pytest.mark.parametrize(("kind", "push_kind"), [("work", "work"), ("failure", "failure")])
def test_finished_attention_goes_to_the_channels_members(
    room: Room, kind: str, push_kind: str
) -> None:
    room.attention(kind)
    room.step()
    assert [p["k"] for p in room.pushes_to(TOKEN_A)] == [push_kind]
    assert [p["k"] for p in room.pushes_to(TOKEN_B)] == [push_kind]


def test_historical_attention_is_quiet(room: Room) -> None:
    room.attention("failure", historical=True)
    room.step()
    assert room.relay.sent == []


def test_an_opened_gate_pings_its_deciders_once(room: Room) -> None:
    room.api.ctx.chronology.record(
        "gate.opened",
        room.api.clock(),
        run_id="r_gate",
        actor=DAEMON_ACTOR,
        data={"kind": "merge", "state": "open", "pr_number": 7, "pr_url": None, "revision": 1},
    )
    # The same run, announced again through its conversation, is one ping.
    room.attention("action_required", run_id=run_public_id("r_gate"))
    room.step()
    pings = room.pushes_to(TOKEN_A)
    assert [p["k"] for p in pings] == ["gate"]
    notice = room.notification(pings[0]["ref"])
    assert notice["title"] == "Decision needed"
    assert room.pushes_to(TOKEN_B) == []


# -- preferences ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prefs", "expected"),
    [
        ({}, ["mention", "work", "gate"]),
        ({"mentions": False}, ["work", "gate"]),
        ({"work": False}, ["mention", "gate"]),
        ({"gates": False}, ["mention", "work"]),
        ({"per_channel": {"CHANNEL": "none"}}, []),
        ({"per_channel": {"CHANNEL": "mentions"}}, ["mention"]),
        ({"per_channel": {"CHANNEL": "all"}}, ["mention", "work", "gate"]),
        ({"per_channel": {"chn_other": "none"}}, ["mention", "work", "gate"]),
    ],
)
def test_a_devices_preferences_choose_what_reaches_it(
    room: Room, prefs: dict[str, Any], expected: list[str]
) -> None:
    if "per_channel" in prefs:
        prefs = {
            "per_channel": {
                (room.channel_id if key == "CHANNEL" else key): value
                for key, value in prefs["per_channel"].items()
            }
        }
    room.prefs("owner", **prefs)
    room.say(room.bob, "@owner look")
    turn = room.say(room.owner, "fix it")
    _deliver(room, turn.id, "completed", "Fix", None)
    room.attention("action_required")
    room.step()
    assert [p["k"] for p in room.pushes_to(TOKEN_A)] == expected


def test_failures_have_their_own_switch(room: Room) -> None:
    room.prefs("owner", failures=False)
    turn = room.say(room.owner, "fix it")
    _deliver(room, turn.id, "failed", "Fix", None)
    room.step()
    assert room.pushes_to(TOKEN_A) == []


# -- live only --------------------------------------------------------------------------


def test_only_what_happens_after_the_dispatcher_starts_is_sent(
    tmp_path: Path, relay: FakeRelay
) -> None:
    api = push_api(tmp_path, relay)
    with api.client:
        owner_headers = register_owner(api)
        register_member(api)
        api.client.post("/v1/users/me/devices", json=device(TOKEN_A), headers=owner_headers)
        store = api.ctx.collaboration
        owner = store.user_by_username("owner")
        bob = store.user_by_username("bob")
        assert owner is not None and bob is not None
        channel = store.create_channel(owner.id, "Plans", api.clock())
        store.add_channel_member(owner.id, channel.id, bob.id, "member", api.clock())

        def mention() -> None:
            store.accept_turn(
                bob.id,
                channel.id,
                content="@owner hello",
                targets=(),
                client_turn_id=None,
                client_message_id=None,
                actor=None,
                now=api.clock(),
            )

        mention()
        # A restart primes a fresh dispatcher at the chronology's head: the
        # mention above is history and is never sent, however often it runs.
        dispatcher = api.ctx.push.dispatcher
        dispatcher.prime()
        dispatcher.step()
        dispatcher.step()
        assert relay.sent == []
        mention()
        dispatcher.step()
        dispatcher.step()
        assert len(relay.sent) == 1
    api.ctx.close()


def test_a_dispatcher_that_was_never_primed_sends_nothing(tmp_path: Path, relay: FakeRelay) -> None:
    api = push_api(tmp_path, relay)
    with api.client:
        headers = register_owner(api)
        register_member(api)
        api.client.post("/v1/users/me/devices", json=device(TOKEN_A), headers=headers)
        owner = api.ctx.collaboration.user_by_username("owner")
        assert owner is not None
        api.ctx.push.dispatcher.step()
        assert relay.sent == []
    api.ctx.close()


# -- when the relay balks ---------------------------------------------------------------


def _mention(room: Room) -> None:
    room.say(room.bob, "@owner ping")
    room.api.ctx.push.dispatcher.scan()


def test_an_upstream_failure_is_retried_with_exponential_backoff(room: Room) -> None:
    dispatcher = room.api.ctx.push.dispatcher
    room.relay.fail_next(502, {"error": "upstream", "reason": "InternalServerError"}, times=3)
    _mention(room)
    attempts = lambda: len([r for r, _ in room.relay.requests if r == "/v1/push"])  # noqa: E731

    dispatcher.deliver_due()
    assert attempts() == 1 and room.relay.sent == []
    # Backoff doubles from `[push] backoff_s` (2 s): 2, then 4, then 8.
    for wait, expected in ((1, 1), (1, 2), (3, 2), (1, 3), (7, 3), (1, 4)):
        room.api.clock.t += wait
        dispatcher.deliver_due()
        assert attempts() == expected, (wait, expected)
    assert len(room.pushes_to(TOKEN_A)) == 1
    assert dispatcher.pending() == 0


def test_a_rate_limit_waits_as_long_as_the_relay_says(room: Room) -> None:
    dispatcher = room.api.ctx.push.dispatcher
    room.relay.fail_next(429, {"error": "rate_limited"}, headers={"Retry-After": "30"})
    _mention(room)
    dispatcher.deliver_due()
    room.api.clock.t += 29
    dispatcher.deliver_due()
    assert room.relay.sent == []
    room.api.clock.t += 1
    dispatcher.deliver_due()
    assert len(room.relay.sent) == 1


def test_an_unreachable_relay_is_retried(room: Room) -> None:
    dispatcher = room.api.ctx.push.dispatcher
    room.relay.unreachable()
    _mention(room)
    dispatcher.deliver_due()
    room.api.clock.t += 2
    dispatcher.deliver_due()
    assert len(room.relay.sent) == 1


def test_a_failure_the_relay_calls_final_is_not_retried(room: Room) -> None:
    dispatcher = room.api.ctx.push.dispatcher
    room.relay.fail_next(502, {"error": "upstream", "reason": "BadTopic", "retryable": False})
    _mention(room)
    dispatcher.deliver_due()
    assert dispatcher.pending() == 0
    room.api.clock.t += 3600
    dispatcher.deliver_due()
    assert room.relay.sent == []


def test_retries_stop_after_max_attempts(room: Room, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    dispatcher = room.api.ctx.push.dispatcher
    room.relay.fail_next(502, {"error": "upstream", "reason": "x"}, times=50)
    _mention(room)
    for _ in range(20):
        dispatcher.deliver_due()
        room.api.clock.t += 600
    pushes = [r for r, _ in room.relay.requests if r == "/v1/push"]
    assert len(pushes) == 5  # `[push] max_attempts`
    assert dispatcher.pending() == 0
    assert "push.gave_up" in caplog.text


def test_an_unregistered_device_is_pruned(room: Room) -> None:
    room.relay.unregistered.add(TOKEN_A)
    _mention(room)
    room.api.ctx.push.dispatcher.deliver_due()
    assert room.api.ctx.push.dispatcher.pending() == 0
    listed = room.api.client.get("/v1/users/me/devices", headers=room.owner_headers).json()
    assert listed == {"items": []}
    # Bob's device is untouched.
    assert (
        len(room.api.client.get("/v1/users/me/devices", headers=room.bob_headers).json()["items"])
        == 1
    )


def test_a_refused_push_is_dropped_and_the_device_kept(room: Room) -> None:
    room.relay.fail_next(400, {"error": "invalid_request"})
    _mention(room)
    room.api.ctx.push.dispatcher.deliver_due()
    room.api.clock.t += 3600
    room.api.ctx.push.dispatcher.deliver_due()
    assert room.relay.sent == []
    assert room.api.ctx.push.dispatcher.pending() == 0
    assert len(room.api.ctx.push.devices.for_user(room.owner.id)) == 1


def test_a_handle_the_relay_no_longer_knows_is_re_enrolled_on_the_next_registration(
    room: Room,
) -> None:
    room.relay.fail_next(400, {"error": "invalid_handle"})
    _mention(room)
    room.api.ctx.push.dispatcher.deliver_due()
    enrolled = len([r for r, _ in room.relay.requests if r == "/v1/enroll"])
    # Until the app registers again the device has nothing to push with.
    room.say(room.bob, "@owner again")
    room.step()
    assert room.pushes_to(TOKEN_A) == []
    again = room.api.client.post(
        "/v1/users/me/devices", json=device(TOKEN_A), headers=room.owner_headers
    )
    assert again.status_code == 200
    assert len([r for r, _ in room.relay.requests if r == "/v1/enroll"]) == enrolled + 1
    room.say(room.bob, "@owner third time")
    room.step()
    assert len(room.pushes_to(TOKEN_A)) == 1


def test_old_notifications_are_pruned_with_the_chronology(room: Room) -> None:
    room.say(room.bob, "@owner ping")
    room.step()
    [ping] = room.pushes_to(TOKEN_A)
    retention = room.api.ctx.config.api.replay_retention_s
    room.api.clock.t += retention + 1
    assert room.api.ctx.push.notification(room.owner.id, ping["ref"]) is not None
    room.api.ctx.push.dispatcher.prune()
    assert room.api.ctx.push.notification(room.owner.id, ping["ref"]) is None
