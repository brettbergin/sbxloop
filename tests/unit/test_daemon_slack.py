"""Slack bridge: Socket Mode events in, Web API calls out — with a fake
client, no network, slack_sdk not required. The service-agnostic behaviour
(pump, digest, status line, watches) is covered once in
test_daemon_discord.py; this file covers the Slack seams: event
normalisation and filtering, threads as ``thread_ts``, mrkdwn at the send
seam, attachments, reactions by name, the permalink thread pointer, token
checks and the once-logged channel error."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.daemon.chat import build_bridge
from sbxloop.daemon.discord import DiscordBridge
from sbxloop.daemon.discord_format import agent_model_label, format_for_discord
from sbxloop.daemon.model import DaemonNotice, RunReport, WorkItem
from sbxloop.daemon.slack import SlackBridge, SlackMessage, SlackTarget
from sbxloop.daemon.store import ChatThread, DaemonStore
from sbxloop.errors import DaemonError
from sbxloop.events import Event, EventBus
from tests.unit.test_daemon_discord import FakeConcierge, FakeEngine, FakeLoop, wait_for

CHANNEL = "C0123ABCDEF"
BOT = "UBOT"


def ev(type: str, **data: Any) -> Event:
    return Event.now(type, "r1", **data)


class FakeApiError(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.response = {"ok": False, "error": error}


class FakeWeb:
    """The slice of ``AsyncWebClient`` the bridge calls, recording everything."""

    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.reactions: list[dict[str, Any]] = []
        self.lookups: list[str] = []
        self.users = {"U1": "brett", "U2": "ana"}
        self.fail_post: Exception | None = None
        self._seq = 0

    def _ts(self) -> str:
        self._seq += 1
        return f"1700000000.{self._seq:06d}"

    async def auth_test(self) -> dict[str, Any]:
        return {"ok": True, "user_id": BOT}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_post is not None:
            raise self.fail_post
        ts = self._ts()
        self.posted.append({**kwargs, "ts": ts})
        return {"ok": True, "channel": kwargs["channel"], "ts": ts}

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updated.append(kwargs)
        return {"ok": True}

    async def reactions_add(self, **kwargs: Any) -> dict[str, Any]:
        self.reactions.append(kwargs)
        return {"ok": True}

    async def users_info(self, user: str) -> dict[str, Any]:
        self.lookups.append(user)
        if user not in self.users:
            raise FakeApiError("user_not_found")
        return {"ok": True, "user": {"id": user, "name": self.users[user]}}


class FakeSlackClient:
    def __init__(self, bridge: SlackBridge) -> None:
        self.bridge = bridge
        self.web = FakeWeb()
        self.connected = False
        self.closed = False
        self.connect_error: Exception | None = None

    async def connect(self) -> str:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True
        return BOT

    async def close(self) -> None:
        self.closed = True

    def deliver(self, event: dict[str, Any]) -> None:
        """What the Socket Mode listener does with an Events API event."""
        self.bridge._handle_event(event)


def make_bridge(
    tmp_path: Path,
    *,
    concierge: Any = None,
    tokens: tuple[str, str] = ("xoxb", "xapp"),
    **slack: Any,
) -> tuple[SlackBridge, FakeSlackClient, FakeLoop]:
    config = Config.model_validate(
        {"state_dir": str(tmp_path / "state"), "slack": {"channel_id": CHANNEL, **slack}}
    )
    dstore = DaemonStore(config.state_dir / "state.db")
    floop = FakeLoop(dstore)
    holder: dict[str, FakeSlackClient] = {}

    def factory(b: SlackBridge) -> FakeSlackClient:
        holder["client"] = FakeSlackClient(b)
        return holder["client"]

    bridge = SlackBridge(
        config,
        dstore,
        loop_ref=floop,
        client_factory=factory,
        bot_token=tokens[0],
        app_token=tokens[1],
        concierge=concierge,
    )
    # The factory runs inside start(), which is why the client is fetched after it.
    bridge.start()
    return bridge, holder["client"], floop


def message(
    text: str, *, user: str = "U1", ts: str = "1700000001.000001", thread_ts: str | None = None
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "message",
        "channel": CHANNEL,
        "user": user,
        "text": text,
        "ts": ts,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return event


def start_run(bridge: SlackBridge, run_id: str = "r1") -> tuple[WorkItem, EventBus, FakeEngine]:
    item = WorkItem(item_id="gh:issue:4", source_key="4", title="Fix login", url="https://x/4")
    bus = EventBus()
    engine = FakeEngine()
    bridge.run_started(item, run_id, engine, bus)  # type: ignore[arg-type]
    assert wait_for(lambda: bridge.dstore.chat_thread(run_id) is not None)
    return item, bus, engine


def thread_of(bridge: SlackBridge, run_id: str = "r1") -> ChatThread:
    known = bridge.dstore.chat_thread(run_id)
    assert known is not None
    return known


class TestCredentials:
    def test_both_tokens_are_required(self, tmp_path: Path) -> None:
        config = Config.model_validate(
            {"state_dir": str(tmp_path / "state"), "slack": {"channel_id": CHANNEL}}
        )
        dstore = DaemonStore(config.state_dir / "state.db")
        with pytest.raises(DaemonError, match="SLACK_BOT_TOKEN is not set"):
            SlackBridge(config, dstore, bot_token="", app_token="xapp").start()
        with pytest.raises(DaemonError, match="SLACK_BOT_TOKEN and SLACK_APP_TOKEN are not set"):
            SlackBridge(config, dstore, bot_token="", app_token="").start()

    def test_tokens_come_from_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-env")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-env")
        config = Config.model_validate(
            {"state_dir": str(tmp_path / "state"), "slack": {"channel_id": CHANNEL}}
        )
        bridge = SlackBridge(config, DaemonStore(config.state_dir / "state.db"))
        assert (bridge.bot_token, bridge.app_token) == ("xoxb-env", "xapp-env")

    def test_tokens_never_reach_the_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        bridge, client, _ = make_bridge(tmp_path, tokens=("xoxb-SECRET-1", "xapp-SECRET-2"))
        try:
            start_run(bridge)
            bridge.daemon_notice(DaemonNotice("daemon.started", "hello"))
            assert wait_for(lambda: len(client.web.posted) >= 2)
        finally:
            bridge.close()
        assert "SECRET" not in caplog.text


class TestBackendSelection:
    def test_build_bridge_picks_by_config(self, tmp_path: Path) -> None:
        dstore = DaemonStore(tmp_path / "state.db")
        assert build_bridge(Config(), dstore) is None
        discord = build_bridge(Config.model_validate({"discord": {"channel_id": 42}}), dstore)
        assert isinstance(discord, DiscordBridge) and discord.backend == "discord"
        slack = build_bridge(Config.model_validate({"slack": {"channel_id": CHANNEL}}), dstore)
        assert isinstance(slack, SlackBridge) and slack.backend == "slack"
        assert slack.chat is slack.config.slack


class TestRunThreads:
    def test_headline_thread_chronology_and_finish(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        try:
            item, bus, _ = start_run(bridge)
            known = thread_of(bridge)
            headline = client.web.posted[0]
            # the thread IS the headline's ts; the row records the Slack shape
            assert known == ChatThread(CHANNEL, headline["ts"], headline["ts"], None, "slack")
            assert "thread_ts" not in headline
            assert headline["unfurl_links"] is False and headline["unfurl_media"] is False
            assert headline["attachments"][0]["blocks"], "headline card as an attachment"
            assert "*Fix login*" in headline["text"]  # mrkdwn, not **bold**
            bus.publish(
                ev("agent.message", content="I will **fix** [it](https://x/4)", agent="builder")
            )
            assert wait_for(lambda: any(p.get("thread_ts") for p in client.web.posted))
            post = next(p for p in client.web.posted if p.get("thread_ts"))
            assert post["thread_ts"] == headline["ts"]
            assert "*builder*" in post["text"] and "<https://x/4|it>" in post["text"]
            bridge.run_finished(
                item, RunReport("r1", "merged", "1/1 tasks done", pr=(7, "https://x/pull/7"))
            )
            assert wait_for(lambda: any("finished: merged" in p["text"] for p in client.web.posted))
            finish = next(p for p in client.web.posted if "finished: merged" in p["text"])
            assert finish["thread_ts"] == headline["ts"]
            # the headline card was edited in place with the final state
            assert wait_for(
                lambda: any(
                    u["ts"] == headline["ts"] and u["text"].startswith("🎉")
                    for u in client.web.updated
                )
            )
            edit = next(u for u in client.web.updated if u["text"].startswith("🎉"))
            assert edit["attachments"][0]["color"] == "#2ECC71"
        finally:
            bridge.close()

    def test_no_thread_per_run_posts_top_level(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path, thread_per_run=False)
        try:
            _, bus, _ = start_run(bridge)
            known = thread_of(bridge)
            assert known.thread_id == CHANNEL and known.channel_id == CHANNEL
            bus.publish(ev("agent.message", content="hi", agent="builder"))
            assert wait_for(lambda: len(client.web.posted) >= 2)
            assert all("thread_ts" not in p for p in client.web.posted)
        finally:
            bridge.close()

    def test_status_line_reattaches_by_ts_after_restart(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        try:
            _, bus, _ = start_run(bridge)
            bus.publish(ev("task.start", task_id="t1", title="Add tests", index=1, total=2))
            assert wait_for(lambda: thread_of(bridge).status_id is not None)
            status_ts = thread_of(bridge).status_id
            bus.publish(ev("task.end", task_id="t1", state="done", index=1, total=2))
            assert wait_for(lambda: any(u["ts"] == status_ts for u in client.web.updated))
        finally:
            bridge.close()

    def test_embeds_false_sends_text_twins(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path, embeds=False)
        try:
            start_run(bridge)
            headline = client.web.posted[0]
            assert "attachments" not in headline and "*Fix login*" in headline["text"]
        finally:
            bridge.close()

    def test_daemon_notice_points_at_the_thread_by_permalink(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        try:
            start_run(bridge)
            known = thread_of(bridge)
            bridge.daemon_notice(DaemonNotice("run.done", "🎉 gh:issue:4 merged", run_id="r1"))
            link = (
                f"<https://slack.com/archives/{CHANNEL}/p{known.thread_id.replace('.', '')}|thread>"
            )
            assert wait_for(
                lambda: any(
                    "thread_ts" not in p and link in p["text"] and "merged" in p["text"]
                    for p in client.web.posted
                )
            )
        finally:
            bridge.close()


class TestInbound:
    def test_command_in_the_control_channel(self, tmp_path: Path) -> None:
        bridge, client, floop = make_bridge(tmp_path)
        try:
            client.deliver(message("!sbx pause"))
            assert wait_for(lambda: floop.hold_calls)
            assert floop.hold_calls[0] == ("pause", "operator", "Slack user `brett`")
            assert client.web.lookups == ["U1"]
            client.deliver(message("!sbx status", ts="1700000001.000002"))
            assert wait_for(
                lambda: any(
                    p.get("attachments") and "thread_ts" not in p for p in client.web.posted
                )
            )
            assert client.web.lookups == ["U1"], "the handle is cached"
        finally:
            bridge.close()

    def test_command_in_a_run_thread_answers_there(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        try:
            start_run(bridge)
            known = thread_of(bridge)
            client.deliver(message("!sbx queue", thread_ts=known.thread_id))
            assert wait_for(
                lambda: any(
                    p.get("thread_ts") == known.thread_id and "queue" in p["text"].lower()
                    for p in client.web.posted
                )
            )
        finally:
            bridge.close()

    def test_mention_in_a_run_thread_steers(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        try:
            _, bus, engine = start_run(bridge)
            known = thread_of(bridge)
            client.deliver(
                message(
                    f"<@{BOT}> focus on the tests first",
                    ts="1700000005.000001",
                    thread_ts=known.thread_id,
                )
            )
            assert wait_for(lambda: engine.posted == ["focus on the tests first"])
            assert wait_for(lambda: client.web.reactions)
            assert client.web.reactions[0] == {
                "channel": CHANNEL,
                "timestamp": "1700000005.000001",
                "name": "hourglass_flowing_sand",
            }
            # the "⏳ steer queued" note lands in the thread
            assert wait_for(
                lambda: any(
                    p.get("thread_ts") == known.thread_id and "steer" in p["text"]
                    for p in client.web.posted
                )
            )
            bus.publish(ev("chat.reply", message_id="m1", text="ok"))
            assert wait_for(
                lambda: any(r["name"] == "white_check_mark" for r in client.web.reactions)
            )
        finally:
            bridge.close()

    def test_plain_thread_message_is_chatter(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        try:
            _, _, engine = start_run(bridge)
            known = thread_of(bridge)
            client.deliver(message("looks fine to me", thread_ts=known.thread_id))
            client.deliver(message("<@U2> what do you think?", thread_ts=known.thread_id))
            bridge.daemon_notice(DaemonNotice("daemon.started", "tick"))  # a later post to wait on
            assert wait_for(lambda: any("tick" in p["text"] for p in client.web.posted))
            assert engine.posted == []
        finally:
            bridge.close()

    def test_steer_after_finish_is_refused_in_the_thread(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        try:
            item, _, engine = start_run(bridge)
            known = thread_of(bridge)
            bridge.run_finished(item, RunReport("r1", "completed", "1/1"))
            assert wait_for(
                lambda: any("finished: completed" in p["text"] for p in client.web.posted)
            )
            client.deliver(message(f"<@{BOT}> too late", thread_ts=known.thread_id))
            assert wait_for(
                lambda: any(
                    "steering is no longer possible" in p["text"]
                    and p.get("thread_ts") == known.thread_id
                    for p in client.web.posted
                )
            )
            assert engine.posted == []
        finally:
            bridge.close()

    def test_mention_in_the_control_channel_is_a_concierge_turn(self, tmp_path: Path) -> None:
        concierge = FakeConcierge()
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge)
        try:
            client.deliver(message(f"<@{BOT}> what's running?"))
            assert wait_for(lambda: concierge.turns == [("what's running?", "Slack user `brett`")])
            assert wait_for(
                lambda: any("hello from the concierge" in p["text"] for p in client.web.posted)
            )
            reply = next(p for p in client.web.posted if "hello from the concierge" in p["text"])
            assert "thread_ts" not in reply, "answered top-level, where the bot listens"
            assert wait_for(
                lambda: (
                    [r["name"] for r in client.web.reactions]
                    == ["hourglass_flowing_sand", "white_check_mark"]
                )
            )
        finally:
            bridge.close()

    def test_without_a_concierge_a_mention_gets_the_off_hint(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        try:
            client.deliver(message(f"<@{BOT}> hello?"))
            assert wait_for(lambda: any("chat is off" in p["text"] for p in client.web.posted))
        finally:
            bridge.close()

    def test_noise_is_ignored(self, tmp_path: Path) -> None:
        bridge, client, floop = make_bridge(tmp_path)
        try:
            client.deliver({**message("!sbx pause"), "type": "app_mention"})
            client.deliver({**message("!sbx pause"), "channel": "C0OTHER"})
            client.deliver({**message("!sbx pause"), "subtype": "message_changed"})
            client.deliver({**message("!sbx pause"), "bot_id": "B1"})
            client.deliver({**message("!sbx pause"), "user": BOT})
            client.deliver(
                {**message("!sbx pause"), "channel": CHANNEL, "thread_ts": "1.2"}
            )  # a human thread
            bridge.daemon_notice(DaemonNotice("daemon.started", "tick"))
            assert wait_for(lambda: any("tick" in p["text"] for p in client.web.posted))
            assert floop.hold_calls == []
        finally:
            bridge.close()

    def test_unknown_user_is_attributed_by_id(self, tmp_path: Path) -> None:
        bridge, client, floop = make_bridge(tmp_path)
        try:
            client.deliver(message("!sbx pause", user="U9"))
            assert wait_for(lambda: floop.hold_calls)
            assert floop.hold_calls[0][2] == "Slack user `U9`"
        finally:
            bridge.close()


class TestWatches:
    def test_watch_notice_mentions_the_requester_and_links_the_thread(self, tmp_path: Path) -> None:
        concierge = FakeConcierge()
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge)
        try:
            client.deliver(message(f"<@{BOT}> watch r1"))
            assert wait_for(lambda: concierge.turns)
            assert wait_for(lambda: bridge._requester_ids.get("Slack user `brett`") == "U1")
            assert bridge.on_watch("r1", "Slack user `brett` (via concierge)") is None
            item, _, _ = start_run(bridge)
            known = thread_of(bridge)
            bridge.run_finished(item, RunReport("r1", "completed", "1/1 tasks done"))
            assert wait_for(lambda: any("<@U1>" in p["text"] for p in client.web.posted))
            notice = next(p for p in client.web.posted if "<@U1>" in p["text"])
            assert "thread_ts" not in notice
            assert (
                f"<https://slack.com/archives/{CHANNEL}/p{known.thread_id.replace('.', '')}|thread>"
                in notice["text"]
            )
        finally:
            bridge.close()


class TestFailures:
    def test_channel_unreachable_is_logged_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING)
        bridge, client, _ = make_bridge(tmp_path)
        try:
            client.web.fail_post = FakeApiError("not_in_channel")
            bridge.daemon_notice(DaemonNotice("daemon.started", "one"))
            bridge.daemon_notice(DaemonNotice("daemon.started", "two"))
            assert wait_for(lambda: "slack.channel_unreachable" in caplog.text)
        finally:
            bridge.close()
        assert caplog.text.count("slack.channel_unreachable") == 1
        assert "/invite" in caplog.text and "posting resumes" in caplog.text

    def test_posting_resumes_once_the_channel_is_reachable(self, tmp_path: Path) -> None:
        """The channel error is a one-time log line, not a degraded state:
        the next send after the app is invited goes through."""
        bridge, client, _ = make_bridge(tmp_path)
        try:
            client.web.fail_post = FakeApiError("not_in_channel")
            bridge.daemon_notice(DaemonNotice("daemon.started", "lost"))
            assert wait_for(lambda: bridge._channel_error_logged)
            client.web.fail_post = None
            bridge.daemon_notice(DaemonNotice("daemon.started", "back"))
            assert wait_for(lambda: any("back" in p["text"] for p in client.web.posted))
            assert not any("lost" in p["text"] for p in client.web.posted)
        finally:
            bridge.close()

    def test_attachment_rejected_falls_back_to_text(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        try:
            original = client.web.chat_postMessage

            async def flaky(**kwargs: Any) -> dict[str, Any]:
                if "attachments" in kwargs:
                    raise FakeApiError("invalid_attachments")
                return await original(**kwargs)

            client.web.chat_postMessage = flaky  # type: ignore[method-assign]
            start_run(bridge)
            headline = client.web.posted[0]
            assert "attachments" not in headline and "*Fix login*" in headline["text"]
        finally:
            bridge.close()

    def test_connect_failure_degrades_without_crashing(self, tmp_path: Path) -> None:
        config = Config.model_validate(
            {"state_dir": str(tmp_path / "state"), "slack": {"channel_id": CHANNEL}}
        )
        dstore = DaemonStore(config.state_dir / "state.db")

        def factory(b: SlackBridge) -> FakeSlackClient:
            client = FakeSlackClient(b)
            client.connect_error = FakeApiError("invalid_auth")
            return client

        bridge = SlackBridge(
            config, dstore, client_factory=factory, bot_token="xoxb", app_token="xapp"
        )
        bridge.start(connect_wait_s=0.5)
        try:
            assert wait_for(lambda: bridge._degraded)
            bridge.daemon_notice(DaemonNotice("daemon.started", "dropped"))
        finally:
            bridge.close(drain_wait_s=0.5)

    def test_already_reacted_is_not_an_error(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        try:

            async def dup(**kwargs: Any) -> dict[str, Any]:
                raise FakeApiError("already_reacted")

            client.web.reactions_add = dup  # type: ignore[method-assign]
            asyncio.run(bridge._add_reaction(SlackMessage(CHANNEL, "1.1"), "✅"))
            with pytest.raises(ValueError, match="no Slack reaction name"):
                asyncio.run(bridge._add_reaction(SlackMessage(CHANNEL, "1.1"), "🦄"))
        finally:
            bridge.close()


class TestSeams:
    def test_handles(self, tmp_path: Path) -> None:
        bridge, _, _ = make_bridge(tmp_path)
        try:
            assert asyncio.run(bridge._control_channel()) == SlackTarget(CHANNEL)
            assert asyncio.run(bridge._thread_handle("1.5")) == SlackTarget(CHANNEL, "1.5")
            assert asyncio.run(bridge._thread_handle(CHANNEL)) == SlackTarget(CHANNEL)
            assert asyncio.run(
                bridge._fetch_message(SlackTarget(CHANNEL, "1.5"), "1.6")
            ) == SlackMessage(CHANNEL, "1.6", "1.5")
            assert (
                bridge.thread_link(ChatThread(CHANNEL, CHANNEL, None, None, "slack"))
                == f"<#{CHANNEL}>"
            )
            assert bridge.mention_user("U1") == "<@U1>"
            assert bridge._message_id(SlackMessage(CHANNEL, "1.7")) == "1.7"
            assert bridge._handle_id(SlackTarget(CHANNEL, "1.8")) == "1.8"
        finally:
            bridge.close()

    def test_edit_rewrites_mrkdwn_and_card(self, tmp_path: Path) -> None:
        from sbxloop.daemon.discord_format import EmbedSpec

        bridge, client, _ = make_bridge(tmp_path)
        try:
            asyncio.run(
                bridge._edit(
                    SlackMessage(CHANNEL, "1.9"),
                    "**done** <https://x/1>",
                    embed=EmbedSpec(title="t", color=1),
                )
            )
            (edit,) = client.web.updated
            assert edit["channel"] == CHANNEL and edit["ts"] == "1.9"
            assert edit["text"] == "*done* <https://x/1>"
            # an edit never grows a preview the post did not have
            assert edit["unfurl_links"] is False and edit["unfurl_media"] is False
            assert edit["attachments"][0]["color"] == "#000001"
        finally:
            bridge.close()


class TestAgentBackendLabel:
    """Slack renders through the same formatter as Discord, so the backend +
    model label must come out identical on both transports (#601)."""

    def test_agent_message_and_headline_name_backend_and_model(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        try:
            bridge.config = bridge.config.model_copy(
                update={
                    "model": "gpt-5",
                    "agent": bridge.config.agent.model_copy(update={"backend": "copilot"}),
                }
            )
            _, bus, _ = start_run(bridge)
            headline = client.web.posted[0]
            assert "copilot · gpt-5" in headline["text"]
            assert "copilot · gpt-5" in str(headline["attachments"][0]["blocks"])
            bus.publish(
                ev("agent.message", content="hi", agent="builder", model="gpt-5", backend="copilot")
            )
            assert wait_for(
                lambda: any(
                    p.get("thread_ts") and "copilot · gpt-5" in p["text"] for p in client.web.posted
                )
            )
            post = next(
                p
                for p in client.web.posted
                if p.get("thread_ts") and "copilot · gpt-5" in p["text"]
            )
            assert "*builder*" in post["text"]
            # the identical label the Discord path renders
            assert agent_model_label("copilot", "gpt-5") == "copilot · gpt-5"
            discord_chunks = format_for_discord(
                ev("agent.message", content="hi", agent="builder", model="gpt-5", backend="copilot")
            )
            assert discord_chunks[0].text.startswith("**builder** · `copilot · gpt-5`")
        finally:
            bridge.close()

    def test_claude_backend_and_missing_or_unknown_backend(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        try:
            _, bus, _ = start_run(bridge)
            bus.publish(
                ev(
                    "agent.message",
                    content="a",
                    agent="builder",
                    model="claude-sonnet-5",
                    backend="claude",
                )
            )
            # a historical event with no backend recorded at all
            bus.publish(ev("agent.message", content="b", agent="builder", model="gpt-5"))
            # ... and one with no model either
            bus.publish(ev("agent.message", content="c", agent="builder"))
            assert wait_for(lambda: sum(1 for p in client.web.posted if p.get("thread_ts")) >= 3)
            texts = [p["text"] for p in client.web.posted if p.get("thread_ts")]
            assert any("claude · claude-sonnet-5" in t for t in texts)
            assert any("unknown · gpt-5" in t for t in texts)
            assert any(t.strip().startswith("*builder*") and "·" not in t for t in texts)
        finally:
            bridge.close()
