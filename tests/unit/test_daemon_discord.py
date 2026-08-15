"""Discord bridge: pure formatter, steering/command routing with a fake
client, non-blocking bus subscription. No network, discord.py not required."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.daemon.discord import DiscordBridge, format_for_discord, headline_text
from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.errors import DaemonError
from sbxloop.events import Event, EventBus


def ev(type: str, **data: Any) -> Event:
    return Event.now(type, "r1", **data)


class TestFormat:
    def test_agent_message_carries_attribution_and_clips(self) -> None:
        text = format_for_discord(
            ev("agent.message", content="x" * 5000, agent="planner", model="claude-sonnet-5"),
            max_chars=1900,
        )
        assert text is not None
        assert text.startswith("**planner** · `claude-sonnet-5`\n")
        assert len(text) <= 1900

    def test_skipped_noise_returns_none(self) -> None:
        for t in ("agent.message_delta", "worker.heartbeat", "agent.usage", "sandbox.resources"):
            assert format_for_discord(ev(t, x=1)) is None

    def test_url_carriers_become_link_lines(self) -> None:
        assert format_for_discord(ev("run.report", repo="o/r", issue=3, url="https://x/3")) == (
            "📋 tracking issue #3 https://x/3"
        )
        assert format_for_discord(ev("run.deliver", repo="o/r", pr=9, url="https://x/pull/9")) == (
            "🔀 PR #9 https://x/pull/9"
        )
        assert "delivery failed" in (
            format_for_discord(ev("run.deliver", repo="o/r", error="409")) or ""
        )
        assert "created repository" in (
            format_for_discord(ev("run.deliver", repo="o/r", created=True)) or ""
        )
        assert "branch `sbxloop/r1`" in (
            format_for_discord(
                ev("sandbox.workspace_clone", branch="sbxloop/r1", source="/p", target="/t")
            )
            or ""
        )

    def test_tool_lines_and_quiet_level(self) -> None:
        start = ev("agent.tool_start", tool="bash", args="ls -la\n  extra")
        assert format_for_discord(start) == "⚙ `bash` ls -la extra"
        assert format_for_discord(start, level="quiet") is None
        assert format_for_discord(ev("agent.tool_end", tool="bash", success=True)) is None
        failed = format_for_discord(
            ev("agent.tool_end", tool="bash", success=False, error="boom\nline2")
        )
        assert failed is not None and failed.startswith("✗ `bash` failed")

    def test_chat_events(self) -> None:
        assert "answering at the next checkpoint" in (
            format_for_discord(ev("chat.message", message_id="m1", text="hi")) or ""
        )
        assert (
            format_for_discord(ev("chat.reply", message_id="m1", reply="Sure.", action="continue"))
            == "🧭 **steering:** Sure."
        )
        assert "steering failed" in (
            format_for_discord(ev("chat.reply", message_id="m1", error="worker down")) or ""
        )
        assert "applied `steer_task`" in (
            format_for_discord(ev("chat.action", action="steer_task", guidance="focus auth")) or ""
        )

    def test_headline_states(self) -> None:
        item = WorkItem(
            item_id="gh:4", source="github", source_key="4", title="Fix login", url="https://x/4"
        )
        assert headline_text(item, "r1").startswith(
            "▶ run `r1` — **Fix login** · issue #4 (https://x/4)"
        )
        assert headline_text(item, "r1", "completed").startswith("✅")
        assert headline_text(item, "r1", "delivery_failed").startswith("⚠")


# -- fake discord objects --------------------------------------------------------------


class FakeMessage:
    def __init__(
        self, content: str, channel: FakeChannel, *, bot: bool = False, mid: int = 500
    ) -> None:
        self.content = content
        self.channel = channel
        self.id = mid
        self.author = type("A", (), {"bot": bot})()
        self.reactions: list[str] = []

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)

    async def edit(self, *, content: str) -> None:
        self.content = content

    async def create_thread(self, name: str) -> FakeChannel:
        thread = FakeChannel(self.channel.client, self.channel.id * 10 + 1, name=name)
        self.channel.client.channels[thread.id] = thread
        return thread


class FakeChannel:
    def __init__(self, client: FakeClient, cid: int, name: str = "control") -> None:
        self.client = client
        self.id = cid
        self.name = name
        self.sent: list[str] = []
        self.messages: dict[int, FakeMessage] = {}
        self._next_id = 100

    async def send(self, text: str) -> FakeMessage:
        self.sent.append(text)
        self._next_id += 1
        msg = FakeMessage(text, self, mid=self._next_id)
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, mid: int) -> FakeMessage:
        return self.messages[mid]


class FakeClient:
    """Mirrors the real client's readiness contract: ready is signalled from
    within start() (like on_ready), never before — so a pump that waits on
    the wrong thing parks forever here too (regression for the field bug
    where wait_until_ready() was awaited before client.start())."""

    def __init__(self, control_id: int = 42) -> None:
        self.channels: dict[int, FakeChannel] = {control_id: FakeChannel(self, control_id)}
        self.closed = False
        self.bridge: DiscordBridge | None = None

    def get_channel(self, cid: int) -> FakeChannel | None:
        return self.channels.get(cid)

    async def fetch_channel(self, cid: int) -> FakeChannel:
        return self.channels[cid]

    async def start(self, token: str) -> None:
        if self.bridge is not None:
            self.bridge.mark_ready()
        while not self.closed:
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        self.closed = True


class FakeEngine:
    def __init__(self) -> None:
        self.posted: list[str] = []

    def post_user_message(self, text: str) -> str:
        self.posted.append(text)
        return f"m{len(self.posted)}"


class FakeLoop:
    def __init__(self, dstore: DaemonStore) -> None:
        self.dstore = dstore
        self.paused = False
        self.cancelled = 0

    def status(self) -> dict[str, Any]:
        return {
            "current": None,
            "queued": 2,
            "runs_today": 1,
            "max_runs_per_day": 12,
            "breaker_open": False,
            "paused": self.paused,
            "stopping": False,
        }

    def pause(self) -> None:
        self.paused = True

    def unpause(self) -> None:
        self.paused = False

    def cancel_current(self) -> bool:
        self.cancelled += 1
        return True


def make_bridge(
    tmp_path: Path, *, channel_id: int = 42
) -> tuple[DiscordBridge, FakeClient, FakeLoop]:
    config = Config.model_validate(
        {"state_dir": str(tmp_path / "state"), "discord": {"channel_id": channel_id}}
    )
    dstore = DaemonStore(config.state_dir / "state.db")
    client = FakeClient(channel_id)
    floop = FakeLoop(dstore)

    def factory(b: DiscordBridge) -> FakeClient:
        client.bridge = b
        return client

    bridge = DiscordBridge(config, dstore, loop_ref=floop, client_factory=factory, token="tok")
    return bridge, client, floop


def wait_for(pred: Any, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class TestBridge:
    def test_missing_token_is_a_daemon_error(self, tmp_path: Path) -> None:
        config = Config.model_validate(
            {"state_dir": str(tmp_path / "state"), "discord": {"channel_id": 1}}
        )
        bridge = DiscordBridge(
            config,
            DaemonStore(config.state_dir / "state.db"),
            client_factory=lambda b: None,
            token="",
        )
        with pytest.raises(DaemonError, match="DISCORD_BOT_TOKEN"):
            bridge.start()

    def test_run_lifecycle_creates_thread_posts_chronology_edits_headline(
        self, tmp_path: Path
    ) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            bus = EventBus()
            bridge.run_started(item, "r1", FakeEngine(), bus)  # type: ignore[arg-type]
            control = client.channels[42]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread_id = bridge.dstore.discord_thread("r1")[1]  # type: ignore[index]
            thread = client.channels[thread_id]
            assert control.sent and control.sent[0].startswith("▶ run `r1`")
            bus.emit("agent.message", "r1", content="Planning now", agent="planner", model="m")
            bus.emit("run.deliver", "r1", repo="o/r", pr=3, url="https://x/pull/3")
            assert wait_for(lambda: any("planner" in s for s in thread.sent))
            assert wait_for(lambda: any("PR #3" in s for s in thread.sent))
            bridge.run_finished(
                item,
                RunReport("r1", "completed", "1/1 tasks done", delivery=(3, "https://x/pull/3")),
            )
            assert wait_for(
                lambda: any(s.startswith("**finished: completed**") for s in thread.sent)
            )
            headline = control.messages[bridge.dstore.discord_thread("r1")[2]]  # type: ignore[index]
            assert wait_for(lambda: headline.content.startswith("✅"))
        finally:
            bridge.close()

    def test_bus_subscriber_never_blocks(self, tmp_path: Path) -> None:
        bridge, _client, _ = make_bridge(tmp_path)
        # do NOT start the bridge: nothing drains the queue; emits must still return instantly
        item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
        bus = EventBus()
        bridge.run_started(item, "r1", FakeEngine(), bus)  # type: ignore[arg-type]
        t0 = time.time()
        for _ in range(500):
            bus.emit("agent.tool_start", "r1", tool="bash", args="ls")
        assert time.time() - t0 < 1.0

    def test_steering_in_live_thread_posts_message_and_reacts(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            engine = FakeEngine()
            bus = EventBus()
            bridge.run_started(item, "r1", engine, bus)  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread = client.channels[bridge.dstore.discord_thread("r1")[1]]  # type: ignore[index]
            msg = FakeMessage("focus on auth first", thread, mid=777)
            thread.messages[777] = msg
            bridge._handle_message(msg)
            assert engine.posted == ["focus on auth first"]
            assert wait_for(lambda: "⏳" in msg.reactions)
            # the matching chat.reply resolves it: ✅ reaction + reply text in thread
            bus.emit("chat.reply", "r1", message_id="m1", reply="Will do.", action="steer_task")
            assert wait_for(lambda: "✅" in msg.reactions)
            assert wait_for(lambda: any("Will do." in s for s in thread.sent))
        finally:
            bridge.close()

    def test_message_in_finished_thread_gets_finished_reply(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            engine = FakeEngine()
            bridge.run_started(item, "r1", engine, EventBus())  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread = client.channels[bridge.dstore.discord_thread("r1")[1]]  # type: ignore[index]
            bridge.run_finished(item, RunReport("r1", "completed", "done"))
            bridge._handle_message(FakeMessage("too late?", thread))
            assert engine.posted == []
            assert wait_for(lambda: any("has finished" in s for s in thread.sent))
        finally:
            bridge.close()

    def test_bot_messages_and_unknown_channels_are_ignored(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        engine = FakeEngine()
        bridge._engine = engine  # type: ignore[attr-defined]
        other = FakeChannel(client, 999)
        bridge._handle_message(FakeMessage("hi", other))
        bridge._handle_message(FakeMessage("hi", client.channels[42], bot=True))
        assert engine.posted == []

    def test_commands_dispatch(self, tmp_path: Path) -> None:
        bridge, client, floop = make_bridge(tmp_path)
        bridge.start()
        try:
            control = client.channels[42]
            for cmd in (
                "!sbx status",
                "!sbx pause",
                "!sbx resume",
                "!sbx cancel",
                "!sbx queue",
                "!sbx bogus",
            ):
                bridge._handle_message(FakeMessage(cmd, control))
            assert wait_for(lambda: len(control.sent) >= 6)
            joined = "\n".join(control.sent)
            assert "**queued:** 2" in joined and "runs today:** 1/12" in joined
            assert "paused" in joined and "resumed." in joined
            assert floop.cancelled == 1 and "cancel requested" in joined
            assert "queue is empty." in joined
            assert "commands:" in joined
        finally:
            bridge.close()

    def test_daemon_events_go_to_control_channel(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            bridge.daemon_event("circuit breaker opened")
            assert wait_for(lambda: "circuit breaker opened" in client.channels[42].sent)
        finally:
            bridge.close()

    def test_recovery_reattaches_to_existing_thread(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        # a previous process recorded the thread; make the channel exist
        prior = FakeChannel(client, 4242, name="r1 · Do A")
        client.channels[4242] = prior
        bridge.dstore.record_discord_thread("r1", 42, 4242, None)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            bus = EventBus()
            bridge.run_started(item, "r1", FakeEngine(), bus)  # type: ignore[arg-type]
            bus.emit("agent.message", "r1", content="resumed work", agent="executor")
            assert wait_for(lambda: any("resumed work" in s for s in prior.sent))
            assert client.channels[42].sent == []  # no duplicate headline
        finally:
            bridge.close()


def test_threading_sanity() -> None:
    # guard: the module must not require an event loop at import/construct time
    assert threading.current_thread() is threading.main_thread()
