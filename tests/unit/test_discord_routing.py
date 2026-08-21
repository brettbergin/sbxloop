"""route_message: every inbound-message rule, without a Discord client."""

from __future__ import annotations

from sbxloop.daemon.discord_routing import Route, route_message, strip_mentions

BOT = 777
CONTROL = 42
THREAD = 4242


def route(content: str, *, channel_id: int | None = CONTROL, **facts):  # type: ignore[no-untyped-def]
    base = {
        "author_is_bot": False,
        "mentioned_ids": frozenset(),
        "reply_to_bot": False,
        "control_channel_id": CONTROL,
        "prefix": "!sbx",
        "bot_user_id": BOT,
        "is_run_thread": False,
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


class TestRunThread:
    """A run thread is gated exactly like the control channel: steering
    pauses the agent and can rewrite the running task's plan, so it takes a
    deliberate @mention — not any message that lands in the thread."""

    def thread(self, content: str, **facts):  # type: ignore[no-untyped-def]
        return route(content, channel_id=THREAD, is_run_thread=True, **facts)

    def test_plain_thread_message_is_ignored(self) -> None:
        # The bug: watching a run and talking about it used to steer it.
        assert self.thread("  huh, that retry looks wrong  ") == Route("ignore", "")

    def test_mention_steers_and_the_token_is_stripped(self) -> None:
        assert self.thread(f"<@{BOT}> focus on tests", mentioned_ids={BOT}) == Route(
            "steer", "focus on tests"
        )
        assert self.thread(f"hurry <@!{BOT}> up", mentioned_ids={BOT}) == Route("steer", "hurry up")

    def test_someone_elses_mention_is_kept_and_is_not_a_trigger(self) -> None:
        assert self.thread("<@123> look at this", mentioned_ids={123}) == Route("ignore", "")
        assert self.thread(f"<@{BOT}> ask <@123>", mentioned_ids={BOT, 123}) == Route(
            "steer", "ask <@123>"
        )

    def test_reply_to_bot_steers(self) -> None:
        assert self.thread("do that again") == Route("ignore", "")
        assert self.thread("do that again", reply_to_bot=True) == Route("steer", "do that again")

    def test_bare_mention_is_ignored(self) -> None:
        assert self.thread(f"<@{BOT}>", mentioned_ids={BOT}) == Route("ignore", "")

    def test_bot_author_is_ignored(self) -> None:
        # The bridge's own chronology posts land in the thread it steers.
        assert self.thread(f"<@{BOT}> go", mentioned_ids={BOT}, author_is_bot=True) == Route(
            "ignore", ""
        )

    def test_unknown_bot_id_never_matches(self) -> None:
        assert self.thread(f"<@{BOT}> go", mentioned_ids={BOT}, bot_user_id=None) == Route(
            "ignore", ""
        )

    def test_commands_work_in_a_run_thread(self) -> None:
        assert self.thread("!sbx status") == Route("command", "status")
        assert self.thread(f"<@{BOT}> !sbx cancel --retry", mentioned_ids={BOT}) == Route(
            "command", "cancel --retry"
        )


class TestOtherChannels:
    def test_no_channel_is_ignored(self) -> None:
        assert route("x", channel_id=None) == Route("ignore", "")

    def test_a_channel_that_is_neither_is_never_ours(self) -> None:
        # A DM or an unrelated guild channel: not even a mention or a command.
        assert route("hello", channel_id=99) == Route("ignore", "")
        assert route(f"<@{BOT}> hello", channel_id=99, mentioned_ids={BOT}) == Route("ignore", "")
        assert route("!sbx status", channel_id=99) == Route("ignore", "")
        assert route("x", channel_id=99, reply_to_bot=True) == Route("ignore", "")

    def test_control_channel_unset_leaves_only_run_threads(self) -> None:
        assert route("x", control_channel_id=None) == Route("ignore", "")
        assert route(
            f"<@{BOT}> x", control_channel_id=None, mentioned_ids={BOT}, is_run_thread=True
        ) == Route("steer", "x")


def test_strip_mentions() -> None:
    assert strip_mentions("<@1>  hi <@!1> there  <@2>", 1) == "hi there <@2>"
    assert strip_mentions("<@1> hi <@2>", None) == "hi"
    assert strip_mentions("no mentions", 1) == "no mentions"
