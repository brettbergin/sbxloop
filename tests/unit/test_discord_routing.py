"""route_message: every inbound-message rule, without a Discord client."""

from __future__ import annotations

from sbxloop.daemon.discord_routing import Route, route_message, strip_mentions

BOT = 777
CONTROL = 42


def route(content: str, *, channel_id: int | None = CONTROL, **facts):  # type: ignore[no-untyped-def]
    base = {
        "author_is_bot": False,
        "mentioned_ids": frozenset(),
        "reply_to_bot": False,
        "control_channel_id": CONTROL,
        "prefix": "!sbx",
        "bot_user_id": BOT,
    }
    base.update(facts)
    return route_message(content=content, channel_id=channel_id, **base)


class TestControlChannel:
    def test_command_with_or_without_mention(self) -> None:
        assert route("!sbx status") == Route("command", "status")
        assert route(f"<@{BOT}> !sbx cancel --retry", mentioned_ids={BOT}) == Route(
            "command", "cancel --retry"
        )
        assert route(f"!sbx <@!{BOT}> queue", mentioned_ids={BOT}) == Route("command", "queue")

    def test_mention_forms_go_to_the_concierge(self) -> None:
        assert route(f"<@{BOT}> what's running?", mentioned_ids={BOT}) == Route(
            "concierge", "what's running?"
        )
        assert route(f"hey <@!{BOT}>, status?", mentioned_ids={BOT}) == Route(
            "concierge", "hey , status?"
        )
        # someone else's mention is left in the text and is not a trigger
        assert route("<@123> look at this", mentioned_ids={123}) == Route("ignore", "")

    def test_reply_to_bot_goes_to_the_concierge(self) -> None:
        assert route("and then?", reply_to_bot=True) == Route("concierge", "and then?")

    def test_plain_message_is_ignored(self) -> None:
        assert route("hello there") == Route("ignore", "")

    def test_bare_mention_is_ignored(self) -> None:
        assert route(f"<@{BOT}>", mentioned_ids={BOT}) == Route("ignore", "")

    def test_bot_author_is_ignored(self) -> None:
        assert route("!sbx status", author_is_bot=True) == Route("ignore", "")
        assert route(f"<@{BOT}> hi", author_is_bot=True, mentioned_ids={BOT}) == Route("ignore", "")

    def test_unknown_bot_id_never_matches(self) -> None:
        # Before on_ready client.user is None: mentions cannot be ours.
        assert route(f"<@{BOT}> hi", mentioned_ids={BOT}, bot_user_id=None) == Route("ignore", "")
        assert route("!sbx status", bot_user_id=None) == Route("command", "status")


class TestOtherChannels:
    def test_thread_message_is_a_steer_verbatim(self) -> None:
        assert route("  focus on tests  ", channel_id=4242) == Route("steer", "focus on tests")
        # mentions are NOT stripped for steers: the agent sees what was typed
        assert route(f"<@{BOT}> hurry", channel_id=4242, mentioned_ids={BOT}) == Route(
            "steer", f"<@{BOT}> hurry"
        )

    def test_no_channel_is_ignored(self) -> None:
        assert route("x", channel_id=None) == Route("ignore", "")

    def test_control_channel_unset_means_every_channel_is_a_thread(self) -> None:
        assert route("x", control_channel_id=None) == Route("steer", "x")


def test_strip_mentions() -> None:
    assert strip_mentions("<@1>  hi <@!1> there  <@2>", 1) == "hi there <@2>"
    assert strip_mentions("<@1> hi <@2>", None) == "hi"
    assert strip_mentions("no mentions", 1) == "no mentions"
