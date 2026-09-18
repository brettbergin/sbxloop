"""A bridge surface linked to a collaboration channel.

Unlinked, a message that addresses the bot in the control channel goes to
the concierge, as it always has. Linked, it becomes a turn in the channel,
credited to whoever's account the author is mapped to; an unmapped author
is refused unless the link admits guests. Outbound, every message appended
to the channel is posted to the surface once, and a message that came from
that surface is never echoed back to it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from sbxloop.api.auth.keys import load_or_create
from sbxloop.api.auth.store import ApiAuthStore
from sbxloop.api.collaboration import CollaborationStore
from sbxloop.api.context import ApiContext
from sbxloop.daemon.channel_mirror import ChannelMirror
from tests.unit.test_daemon_discord import (
    BOT_USER,
    FakeChannel,
    FakeConcierge,
    FakeMessage,
    FakeUser,
    make_bridge,
    wait_for,
)

CONTROL = 42
SURFACE = "42"
#: An ordinary channel somebody linked: not the control channel, so nothing
#: the bot hears there used to reach it at all.
ELSEWHERE = 77
ELSEWHERE_SURFACE = "77"


class ChannelConcierge:
    """The agent runtime a channel turn runs against: answers at once."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def submit_turn(self, text: str, **kwargs: Any) -> Any:
        from concurrent.futures import Future

        from sbxloop.daemon.concierge import ConciergeReply

        self.calls.append({"text": text, **kwargs})
        future: Future[ConciergeReply] = Future()
        target = str(kwargs.get("session_key", ":angie")).rsplit(":", 1)[-1]
        future.set_result(ConciergeReply(f"reply from {target}"))
        return future


class Linked:
    """A started bridge over a real collaboration store, with an API context
    the bridge reaches through its daemon loop."""

    def __init__(self, tmp_path: Path, *, surface: int = CONTROL, **link: Any) -> None:
        self.surface = surface
        self.concierge = FakeConcierge()
        self.channel_agent = ChannelConcierge()
        self.bridge, self.client, self.loop = make_bridge(tmp_path, concierge=self.concierge)
        config = self.bridge.config
        self.ctx = ApiContext(
            config,
            loop=self.loop,
            auth=ApiAuthStore(self.bridge.dstore),
            keys=load_or_create(config.paths),
            clock=time.time,
            concierge=self.channel_agent,
        )
        self.ctx.ready.set()
        self.loop.api_ctx = self.ctx
        self.store: CollaborationStore = self.ctx.collaboration
        self.user = self.store.register_user(
            username="owner",
            email="owner@example.test",
            password="correct horse battery staple",
            full_name="Local Owner",
            timezone="UTC",
            now=time.time(),
        )
        self.channel = self.store.create_channel(self.user.id, "Plans", time.time())
        if surface not in self.client.channels:
            self.client.channels[surface] = FakeChannel(self.client, surface, name="linked")
        self.link = (
            None
            if not link
            else self.store.create_channel_link(
                None,
                self.channel.id,
                backend="discord",
                surface_id=str(surface),
                thread_id=None,
                allow_guests=bool(link.get("allow_guests")),
                created_by=self.user.id,
                now=time.time(),
            )
        )
        self.bridge.start()

    def say(self, text: str, *, author: FakeUser | None = None, mid: int = 900) -> FakeMessage:
        return self._post(f"<@{BOT_USER.id}> {text}", [BOT_USER], author, mid)

    def type(self, text: str, *, author: FakeUser | None = None, mid: int = 950) -> FakeMessage:
        """Type text verbatim — a ``!sbx`` command, with no mention of the bot."""
        return self._post(text, [], author, mid)

    def _post(
        self, content: str, mentions: list[FakeUser], author: FakeUser | None, mid: int
    ) -> FakeMessage:
        channel = self.client.channels[self.surface]
        msg = FakeMessage(content, channel, mid=mid, mentions=mentions)
        if author is not None:
            msg.author = author
        channel.messages[mid] = msg
        self.bridge._handle_message(msg)
        return msg

    def sent(self) -> list[str]:
        return self.client.channels[self.surface].sent

    def invite(self, username: str) -> Any:
        """A second workspace member, joined through an invite."""
        _invite, token = self.store.create_invite(
            "member", f"{username}@example.test", self.user.id, 3600.0, time.time()
        )
        return self.store.register_user(
            username=username,
            email=f"{username}@example.test",
            password="correct horse battery staple",
            full_name=username.title(),
            timezone="UTC",
            now=time.time(),
            invite_token=token,
        )

    def messages(self) -> list[Any]:
        return self.store.list_messages(None, self.channel.id)

    def close(self) -> None:
        self.bridge.close()
        self.ctx.close()


@pytest.fixture
def unlinked(tmp_path: Path) -> Any:
    built = Linked(tmp_path)
    yield built
    built.close()


@pytest.fixture
def linked(tmp_path: Path) -> Any:
    built = Linked(tmp_path, backend="discord")
    yield built
    built.close()


@pytest.fixture
def guests(tmp_path: Path) -> Any:
    built = Linked(tmp_path, backend="discord", allow_guests=True)
    yield built
    built.close()


def test_an_unlinked_surface_still_talks_to_the_concierge(unlinked: Any) -> None:
    unlinked.say("what is queued?")
    assert wait_for(lambda: len(unlinked.concierge.turns) == 1)
    assert unlinked.concierge.turns[0][0] == "what is queued?"
    assert unlinked.messages() == []


def test_a_linked_message_becomes_a_channel_turn_credited_to_the_account(linked: Any) -> None:
    linked.store.link_identity(
        linked.user.id, backend="discord", external_user_id="1", display_name="brett", now=1.0
    )
    linked.say("plan the bread")
    assert wait_for(lambda: len(linked.messages()) == 1)
    message = linked.messages()[0]
    assert message.content == "plan the bread"
    assert message.author.kind == "human"
    assert message.author.id == linked.user.id
    assert message.origin == {
        "backend": "discord",
        "surface_id": SURFACE,
        "external_message_id": "900",
    }
    turns = linked.store.list_turns(None, linked.channel.id)
    assert [turn.input_message_id for turn in turns] == [message.id]
    # The concierge's own control-channel route is not taken as well.
    assert linked.concierge.turns == []


def test_an_unmapped_author_is_refused_when_the_link_admits_no_guests(linked: Any) -> None:
    linked.say("plan the bread", author=FakeUser(99, "stranger"))
    control = linked.client.channels[CONTROL]
    assert wait_for(lambda: any("link" in sent for sent in control.sent))
    assert linked.messages() == []
    assert linked.concierge.turns == []


def test_a_guest_message_is_stored_under_their_display_name(guests: Any) -> None:
    guests.say("plan the bread", author=FakeUser(99, "stranger"))
    assert wait_for(lambda: len(guests.messages()) == 1)
    message = guests.messages()[0]
    assert message.author.kind == "human"
    assert message.author.id is None
    assert message.author.display_name == "stranger"
    assert message.origin["surface_id"] == SURFACE


def test_a_channel_message_is_mirrored_out_once_and_never_echoed(linked: Any) -> None:
    mirror = ChannelMirror(linked.store, lambda backend: linked.bridge)
    linked.store.add_message_observer(mirror.message_appended)
    linked.store.link_identity(
        linked.user.id, backend="discord", external_user_id="1", display_name="brett", now=1.0
    )
    control = linked.client.channels[CONTROL]
    before = len(control.sent)

    # The turn this starts is answered in the channel, and that answer is
    # what the surface must see; the message it came from must not come back.
    linked.say("plan the bread")
    assert wait_for(lambda: any("reply from" in sent for sent in control.sent[before:]))
    mirrored = [sent for sent in control.sent[before:] if sent.startswith("**")]
    assert len(mirrored) == 1
    assert "plan the bread" not in mirrored[0]
    assert "reply from" in mirrored[0]


def test_a_link_code_typed_on_the_bridge_maps_the_author(linked: Any) -> None:
    code, _expires = linked.store.create_link_code(linked.user.id, time.time())
    control = linked.client.channels[CONTROL]
    msg = FakeMessage(f"!sbx link {code}", control, mid=950)
    control.messages[950] = msg
    linked.bridge._handle_message(msg)
    assert wait_for(lambda: linked.store.identity_user("discord", "1") == linked.user.id)
    assert any("linked" in sent.casefold() for sent in control.sent)


@pytest.fixture
def elsewhere(tmp_path: Path) -> Any:
    """A link on an ordinary channel: not the control channel, and no guests."""
    built = Linked(tmp_path, surface=ELSEWHERE, backend="discord")
    yield built
    built.close()


def test_a_linked_surface_is_not_an_operator_console(elsewhere: Any) -> None:
    elsewhere.type("!sbx pause", author=FakeUser(99, "stranger"))
    assert wait_for(lambda: any(sent for sent in elsewhere.sent()))
    assert elsewhere.loop.paused is False
    assert elsewhere.loop.hold_calls == []
    assert elsewhere.messages() == []


def test_a_linked_surface_does_not_hand_out_the_daemon_log(elsewhere: Any) -> None:
    elsewhere.type("!sbx log --tail 50", author=FakeUser(99, "stranger"))
    assert wait_for(lambda: any(sent for sent in elsewhere.sent()))
    assert not any("```" in sent for sent in elsewhere.sent())


def test_the_control_channel_keeps_its_commands_when_it_is_linked(linked: Any) -> None:
    linked.type("!sbx pause")
    assert wait_for(lambda: linked.loop.paused)
    assert linked.messages() == []


def test_a_link_code_still_maps_an_author_from_a_linked_surface(elsewhere: Any) -> None:
    code, _expires = elsewhere.store.create_link_code(elsewhere.user.id, time.time())
    elsewhere.type(f"!sbx link {code}")
    assert wait_for(lambda: elsewhere.store.identity_user("discord", "1") == elsewhere.user.id)
    assert any("linked" in sent.casefold() for sent in elsewhere.sent())


def test_revoking_workspace_access_revokes_bridge_access(linked: Any) -> None:
    joiner = linked.invite("joiner")
    linked.store.link_identity(
        linked.user.id, backend="discord", external_user_id="1", display_name="brett", now=1.0
    )
    linked.store.link_identity(
        joiner.id, backend="discord", external_user_id="99", display_name="joiner", now=1.0
    )
    assert linked.store.remove_member(joiner.id) is True

    linked.say("plan the bread", author=FakeUser(99, "joiner"))
    assert wait_for(lambda: any("link" in sent for sent in linked.sent()))
    assert linked.messages() == []
    assert linked.store.list_turns(None, linked.channel.id) == []


def test_a_refusal_quotes_the_word_back_without_breaking_out_of_it(elsewhere: Any) -> None:
    elsewhere.type("!sbx `stop`" + "x" * 200, author=FakeUser(99, "stranger"))
    assert wait_for(lambda: any(sent for sent in elsewhere.sent()))
    refusal = elsewhere.sent()[0]
    assert refusal.count("`") % 2 == 0
    assert len(refusal) < 300


#: A Discord thread inside ELSEWHERE: a channel of its own, with its own id.
THREAD = 88


@pytest.fixture
def threaded(tmp_path: Path) -> Any:
    """A link to one thread inside an ordinary channel, admitting guests."""
    built = Linked(tmp_path, surface=THREAD)
    built.link = built.store.create_channel_link(
        None,
        built.channel.id,
        backend="discord",
        surface_id=ELSEWHERE_SURFACE,
        thread_id=str(THREAD),
        allow_guests=True,
        created_by=built.user.id,
        now=time.time(),
    )
    yield built
    built.close()


def test_a_message_in_a_linked_discord_thread_becomes_a_channel_turn(threaded: Any) -> None:
    threaded.say("plan the bread", author=FakeUser(99, "stranger"))
    assert wait_for(lambda: len(threaded.messages()) == 1)
    assert threaded.messages()[0].content == "plan the bread"
    assert threaded.concierge.turns == []


def test_a_linked_discord_thread_hears_the_channel(threaded: Any) -> None:
    mirror = ChannelMirror(threaded.store, lambda backend: threaded.bridge)
    threaded.store.add_message_observer(mirror.message_appended)
    threaded.say("plan the bread", author=FakeUser(99, "stranger"))
    assert wait_for(lambda: any("reply from" in sent for sent in threaded.sent()))
    assert not any("plan the bread" in sent for sent in threaded.sent() if sent.startswith("**"))
