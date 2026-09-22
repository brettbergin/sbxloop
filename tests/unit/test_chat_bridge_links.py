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
from tests.unit import test_daemon_mattermost as mattermost_tests, test_daemon_slack as slack_tests
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


def open_store(
    bridge: Any, loop: Any, channel_agent: Any
) -> tuple[ApiContext, CollaborationStore, Any, Any]:
    """The API context a bridge reaches through its daemon loop, over a real
    collaboration store, with the workspace owner and one channel of theirs."""
    config = bridge.config
    ctx = ApiContext(
        config,
        loop=loop,
        auth=ApiAuthStore(bridge.dstore),
        keys=load_or_create(config.paths),
        clock=time.time,
        concierge=channel_agent,
    )
    ctx.ready.set()
    loop.api_ctx = ctx
    store: CollaborationStore = ctx.collaboration
    user = store.register_user(
        username="owner",
        email="owner@example.test",
        password="correct horse battery staple",
        full_name="Local Owner",
        timezone="UTC",
        now=time.time(),
    )
    channel = store.create_channel(user.id, "Plans", time.time())
    return ctx, store, user, channel


class Linked:
    """A started bridge over a real collaboration store, with an API context
    the bridge reaches through its daemon loop."""

    def __init__(self, tmp_path: Path, *, surface: int = CONTROL, **link: Any) -> None:
        self.surface = surface
        self.concierge = FakeConcierge()
        self.channel_agent = ChannelConcierge()
        self.bridge, self.client, self.loop = make_bridge(tmp_path, concierge=self.concierge)
        self.ctx, self.store, self.user, self.channel = open_store(
            self.bridge, self.loop, self.channel_agent
        )
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
    assert wait_for(lambda: len(linked.messages()) >= 1)
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
    assert wait_for(lambda: len(guests.messages()) >= 1)
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
    assert wait_for(lambda: any("linked" in sent.casefold() for sent in control.sent))


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
    assert wait_for(lambda: any("linked" in sent.casefold() for sent in elsewhere.sent()))


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
    # The channel agent answers at once, so the input may already have a reply.
    assert wait_for(lambda: len(threaded.messages()) >= 1)
    assert threaded.messages()[0].content == "plan the bread"
    assert threaded.concierge.turns == []


def test_a_linked_discord_thread_hears_the_channel(threaded: Any) -> None:
    mirror = ChannelMirror(threaded.store, lambda backend: threaded.bridge)
    threaded.store.add_message_observer(mirror.message_appended)
    threaded.say("plan the bread", author=FakeUser(99, "stranger"))
    assert wait_for(lambda: any("reply from" in sent for sent in threaded.sent()))
    assert not any("plan the bread" in sent for sent in threaded.sent() if sent.startswith("**"))


def test_a_mirrored_guest_post_is_marked_as_a_guest_from_its_bridge(guests: Any) -> None:
    """A guest's name is whatever they call themselves on the other service,
    so a post of theirs mirrored to a second surface must say so, or a guest
    named after a member reads as that member."""
    guests.client.channels[ELSEWHERE] = FakeChannel(guests.client, ELSEWHERE, name="elsewhere")
    guests.store.create_channel_link(
        None,
        guests.channel.id,
        backend="discord",
        surface_id=ELSEWHERE_SURFACE,
        thread_id=None,
        allow_guests=False,
        created_by=guests.user.id,
        now=time.time(),
    )
    mirror = ChannelMirror(guests.store, lambda backend: guests.bridge)
    guests.store.add_message_observer(mirror.message_appended)
    elsewhere = guests.client.channels[ELSEWHERE]

    guests.say("plan the bread", author=FakeUser(99, "stranger"))
    assert wait_for(lambda: any("plan the bread" in sent for sent in elsewhere.sent))
    post = next(sent for sent in elsewhere.sent if "plan the bread" in sent)
    assert post == "**stranger (guest, via discord)**\nplan the bread"
    # The channel's own answer came over no bridge, and its header says nothing of one.
    assert wait_for(lambda: any("reply from" in sent for sent in elsewhere.sent))
    reply = next(sent for sent in elsewhere.sent if "reply from" in sent)
    assert "via" not in reply.splitlines()[0]


def test_a_mirrored_post_from_a_mapped_author_names_the_bridge_it_came_over(linked: Any) -> None:
    linked.store.link_identity(
        linked.user.id, backend="discord", external_user_id="1", display_name="brett", now=1.0
    )
    linked.client.channels[ELSEWHERE] = FakeChannel(linked.client, ELSEWHERE, name="elsewhere")
    linked.store.create_channel_link(
        None,
        linked.channel.id,
        backend="discord",
        surface_id=ELSEWHERE_SURFACE,
        thread_id=None,
        allow_guests=False,
        created_by=linked.user.id,
        now=time.time(),
    )
    mirror = ChannelMirror(linked.store, lambda backend: linked.bridge)
    linked.store.add_message_observer(mirror.message_appended)
    elsewhere = linked.client.channels[ELSEWHERE]

    linked.say("plan the bread")
    assert wait_for(lambda: any("plan the bread" in sent for sent in elsewhere.sent))
    header = next(sent for sent in elsewhere.sent if "plan the bread" in sent).splitlines()[0]
    author = linked.messages()[0].author
    assert header == f"**{author.display_name or author.id} (via discord)**"
    assert "guest" not in header


# -- Slack and Mattermost: a thread is a surface of its own, inside a channel --

#: The ts of a post in the linked Slack channel that a thread hangs under.
SLACK_THREAD = "1700000001.000001"
#: The root post of a Mattermost thread in the linked channel.
MATTERMOST_ROOT = "r" * 26


class LinkedSurface:
    """A started Slack or Mattermost bridge whose control channel is linked
    to a channel, with the one member mapped to the service account the
    harness's messages come from."""

    def __init__(self, tmp_path: Path, backend: str) -> None:
        self.backend = backend
        self.concierge = FakeConcierge()
        harness = slack_tests if backend == "slack" else mattermost_tests
        self.surface = harness.CHANNEL
        self.bridge, self.client, self.loop = harness.make_bridge(
            tmp_path, concierge=self.concierge
        )
        assert wait_for(lambda: self.client.connected)
        self.ctx, self.store, self.user, self.channel = open_store(
            self.bridge, self.loop, ChannelConcierge()
        )
        self.link = self.link_to(None)
        external = "U1" if backend == "slack" else mattermost_tests.USER_ID
        self.store.link_identity(
            self.user.id, backend=backend, external_user_id=external, display_name="me", now=1.0
        )

    def link_to(self, thread_id: str | None) -> Any:
        return self.store.create_channel_link(
            None,
            self.channel.id,
            backend=self.backend,
            surface_id=self.surface,
            thread_id=thread_id,
            allow_guests=False,
            created_by=self.user.id,
            now=time.time(),
        )

    def messages(self) -> list[Any]:
        return self.store.list_messages(None, self.channel.id)

    def close(self) -> None:
        self.bridge.close()
        self.ctx.close()


@pytest.fixture
def slack_linked(tmp_path: Path) -> Any:
    built = LinkedSurface(tmp_path, "slack")
    yield built
    built.close()


@pytest.fixture
def mattermost_linked(tmp_path: Path) -> Any:
    built = LinkedSurface(tmp_path, "mattermost")
    yield built
    built.close()


def test_a_thread_reply_under_a_linked_slack_channel_reaches_the_channel(
    slack_linked: Any,
) -> None:
    """Whoever replies in a thread under a mirrored post is talking to the
    channel the surface is linked to, not to the daemon's concierge."""
    slack_linked.client.deliver(
        {
            "type": "message",
            "channel": slack_tests.CHANNEL,
            "user": "U1",
            "text": f"<@{slack_tests.BOT}> plan the bread",
            "ts": "1700000002.000002",
            "thread_ts": SLACK_THREAD,
        }
    )
    assert wait_for(lambda: len(slack_linked.messages()) >= 1)
    message = slack_linked.messages()[0]
    assert message.content == "plan the bread"
    assert message.author.id == slack_linked.user.id
    assert message.origin == {
        "backend": "slack",
        "surface_id": slack_tests.CHANNEL,
        "external_message_id": "1700000002.000002",
    }
    assert slack_linked.concierge.turns == []


def test_a_thread_reply_under_a_linked_mattermost_channel_reaches_the_channel(
    mattermost_linked: Any,
) -> None:
    mattermost_linked.client.deliver(
        mattermost_tests.posted(
            f"@{mattermost_tests.BOT_NAME} plan the bread",
            post_id="q" * 26,
            root_id=MATTERMOST_ROOT,
        )
    )
    assert wait_for(lambda: len(mattermost_linked.messages()) >= 1)
    message = mattermost_linked.messages()[0]
    assert message.content == "plan the bread"
    assert message.author.id == mattermost_linked.user.id
    assert message.origin == {
        "backend": "mattermost",
        "surface_id": mattermost_tests.CHANNEL,
        "external_message_id": "q" * 26,
    }
    assert mattermost_linked.concierge.turns == []


def test_a_message_in_a_linked_thread_is_still_mirrored_to_the_channel_link(
    mattermost_linked: Any,
) -> None:
    """A channel linked to both a Mattermost channel and one thread in it:
    what is typed in the thread reaches the channel link's readers at the
    top level, and is never echoed back into the thread it came from."""
    mattermost_linked.link_to(MATTERMOST_ROOT)
    mirror = ChannelMirror(mattermost_linked.store, lambda backend: mattermost_linked.bridge)
    mattermost_linked.store.add_message_observer(mirror.message_appended)
    posts = mattermost_linked.client.posts

    mattermost_linked.client.deliver(
        mattermost_tests.posted(
            f"@{mattermost_tests.BOT_NAME} plan the bread",
            post_id="q" * 26,
            root_id=MATTERMOST_ROOT,
        )
    )
    assert wait_for(lambda: any("reply from" in p["message"] for p in posts))
    message = mattermost_linked.messages()[0]
    assert message.origin.get("surface_id") == mattermost_tests.CHANNEL
    assert message.origin.get("thread_id") == MATTERMOST_ROOT
    top_level = [p for p in posts if "plan the bread" in p["message"] and not p.get("root_id")]
    assert len(top_level) == 1
    assert not any(
        "plan the bread" in p["message"] and p.get("root_id") == MATTERMOST_ROOT for p in posts
    )


def test_a_linked_message_addresses_the_agents_it_mentions(linked: Any) -> None:
    # "@software-dev review this" typed on the surface reaches software-dev
    # exactly as it would typed in Angie: the turn targets the agent, the
    # agent joins the channel, and the reply posted is the agent's own.
    linked.store.link_identity(
        linked.user.id, backend="discord", external_user_id="1", display_name="brett", now=1.0
    )
    linked.say("@software-dev review this")
    assert wait_for(lambda: len(linked.messages()) >= 2)
    turns = linked.store.list_turns(None, linked.channel.id)
    assert [turn.targets for turn in turns] == [("software-dev",)]
    assert [call["session_key"] for call in linked.channel_agent.calls] == [
        f"{linked.channel.id}:software-dev"
    ]
    assert linked.messages()[1].agent_slug == "software-dev"
    joined = linked.store.list_participants(None, linked.channel.id)
    assert "software-dev" in {participant.agent_slug for participant in joined}


def test_a_failed_linked_turn_keeps_the_exception_out_of_the_surface(
    linked: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A database or OS error is for the daemon log. The surface, which the
    # link may open to strangers, hears only that the message did not land.
    linked.store.link_identity(
        linked.user.id, backend="discord", external_user_id="1", display_name="brett", now=1.0
    )
    detail = "database is locked (/srv/sbxloop/state/state.db)"

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"sqlite3.OperationalError: {detail}")

    monkeypatch.setattr(linked.ctx, "accept_bridge_turn", explode)
    linked.say("plan the bread")
    control = linked.client.channels[CONTROL]
    assert wait_for(lambda: any("daemon log" in sent for sent in control.sent))
    assert not any("database is locked" in sent or "state.db" in sent for sent in control.sent)
    assert linked.messages() == []


def test_a_refused_linked_turn_still_says_why(linked: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # A refusal the store words for people is worth repeating.
    from sbxloop.api.collaboration import CollaborationError

    linked.store.link_identity(
        linked.user.id, backend="discord", external_user_id="1", display_name="brett", now=1.0
    )

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise CollaborationError("channel_not_found", "channel not found")

    monkeypatch.setattr(linked.ctx, "accept_bridge_turn", refuse)
    linked.say("plan the bread")
    control = linked.client.channels[CONTROL]
    assert wait_for(lambda: any("channel not found" in sent for sent in control.sent))
