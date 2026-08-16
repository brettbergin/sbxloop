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


class FakeEmbed:
    """Stands in for discord.Embed so the bridge's embed plumbing is
    exercised whether or not discord.py is installed (CI syncs without the
    extra)."""

    def __init__(self, spec: Any) -> None:
        self.spec = spec
        self.fields = [
            type("F", (), {"name": n, "value": v, "inline": i})() for n, v, i in spec.fields
        ]


@pytest.fixture(autouse=True)
def _discord_adapters_without_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    from sbxloop.daemon import discord as bridge_module

    monkeypatch.setattr(bridge_module, "_to_embed", lambda spec: FakeEmbed(spec.clamped()))
    monkeypatch.setattr(bridge_module, "_allowed_mentions_none", lambda: "none")


class TestClip:
    def test_clamps_bad_limits(self) -> None:
        from sbxloop.daemon.discord import DISCORD_MAX_MESSAGE, _clip

        # review: limit <= 0 or absurdly large must never yield an over-cap
        # string that Discord rejects
        assert _clip("hello", 0) == "…"
        assert _clip("hello", -5) == "…"
        assert len(_clip("x" * 5000, 10_000)) == DISCORD_MAX_MESSAGE
        assert _clip("hi", 2) == "hi"
        assert _clip("hello", 2) == "h…"


class TestFormat:
    """Bridge-facing contract only; the formatter's exact output lives in
    tests/unit/test_discord_format.py."""

    def test_returns_chunks_and_drops_noise(self) -> None:
        chunks = format_for_discord(
            ev("agent.message", content="x" * 5000, agent="planner", model="claude-sonnet-5"),
            max_chars=1900,
        )
        assert chunks and chunks[0].text.startswith("**planner** · `claude-sonnet-5`\n")
        assert all(len(c.text) <= 1900 for c in chunks)
        for t in ("agent.message_delta", "worker.heartbeat", "agent.usage", "sandbox.resources"):
            assert format_for_discord(ev(t, x=1)) == []

    def test_headline_states(self) -> None:
        item = WorkItem(
            item_id="gh:4", source="github", source_key="4", title="Fix login", url="https://x/4"
        )
        assert headline_text(item, "r1").startswith(
            "▶ run `r1` — **Fix login** · [issue #4](https://x/4)"
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
        self.author = type("A", (), {"bot": bot, "name": "brett"})()
        self.reactions: list[str] = []

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)

    async def edit(self, *, content: str | None = None, embed: Any = None) -> None:
        if content is not None:
            self.content = content
        self.embed = embed
        self.edits = getattr(self, "edits", 0) + 1

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
        self.sent_kwargs: list[dict[str, Any]] = []
        self.messages: dict[int, FakeMessage] = {}
        self._next_id = 100

    async def send(self, text: str | None = None, **kwargs: Any) -> FakeMessage:
        # discord.py signature: send(content=None, *, embed=..., allowed_mentions=..., ...)
        self.sent.append(text or "")
        self.sent_kwargs.append(kwargs)
        self._next_id += 1
        msg = FakeMessage(text or "", self, mid=self._next_id)
        msg.embed = kwargs.get("embed")
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
        self.cancel_calls: list[tuple[str | None, bool]] = []
        self.retried: list[tuple[str, str | None]] = []

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

    def cancel_current(self, requester: str | None = None, *, retry: bool = False) -> bool:
        self.cancelled += 1
        self.cancel_calls.append((requester, retry))
        return True

    # #229 item controls: the real loop wraps DaemonStore; the fake exposes
    # the store's own transitions so error text flows through unchanged.
    def abandon_item(self, item_id: str, reason: str | None = None) -> WorkItem:
        return self.dstore.abandon(item_id, reason or "abandoned by operator", 1.0)

    def retry_item(self, item_id: str, by: str | None = None) -> WorkItem:
        self.retried.append((item_id, by))
        return self.dstore.retry(item_id, 1.0, f"re-queued by {by or 'operator'}")

    def requeue_item(self, item_id: str) -> WorkItem:
        return self.dstore.requeue(item_id, 1.0)


def make_bridge(
    tmp_path: Path, *, channel_id: int = 42, **discord: Any
) -> tuple[DiscordBridge, FakeClient, FakeLoop]:
    config = Config.model_validate(
        {"state_dir": str(tmp_path / "state"), "discord": {"channel_id": channel_id, **discord}}
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
            thread_id = bridge.dstore.discord_thread("r1").thread_id  # type: ignore[union-attr]
            thread = client.channels[thread_id]
            assert control.sent and control.sent[0].startswith("▶ run `r1`")
            bus.emit("agent.message", "r1", content="Planning now", agent="planner", model="m")
            bus.emit("run.deliver", "r1", repo="o/r", pr=3, url="https://x/pull/3")
            assert wait_for(lambda: any("planner" in s for s in thread.sent))
            assert wait_for(
                lambda: any("PR [#3 · o/r](https://x/pull/3)" in s for s in thread.sent)
            )
            # every send disables mentions; the headline carries an embed card
            assert all("allowed_mentions" in k for k in control.sent_kwargs + thread.sent_kwargs)
            assert control.sent_kwargs[0].get("embed") is not None
            bridge.run_finished(
                item,
                RunReport("r1", "completed", "1/1 tasks done", delivery=(3, "https://x/pull/3")),
            )
            assert wait_for(
                lambda: any(s.startswith("**finished: completed**") for s in thread.sent)
            )
            headline = control.messages[bridge.dstore.discord_thread("r1").headline_id]  # type: ignore[union-attr]
            assert wait_for(lambda: headline.content.startswith("✅"))
            # the finished card is an embed with the PR field
            finish_kwargs = [
                k
                for t, k in zip(thread.sent, thread.sent_kwargs, strict=True)
                if t.startswith("**finished")
            ]
            assert finish_kwargs and finish_kwargs[0].get("embed") is not None
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
            thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
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
            thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
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
            assert "commands:" in joined and "abandon <item> [reason]" in joined
        finally:
            bridge.close()

    def test_item_commands_abandon_retry_requeue(self, tmp_path: Path) -> None:
        """#229: `!sbx items|abandon|retry|requeue` mirror the CLI; a refused
        transition answers with the store's reason instead of a traceback."""
        bridge, client, floop = make_bridge(tmp_path)
        floop.dstore.upsert_new(
            WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A"), 1.0
        )
        floop.dstore.mark_running("inbox:a.md", "r1", 1.0)
        bridge.start()
        try:
            control = client.channels[42]

            def ask(cmd: str) -> str:
                # Item commands run off the gateway loop (executor), so
                # replies to a burst may interleave: send one, await one.
                n = len(control.sent)
                bridge._handle_message(FakeMessage(cmd, control))
                assert wait_for(lambda: len(control.sent) > n), cmd
                return control.sent[n]

            reply = ask("!sbx items")
            assert "`inbox:a.md` running" in reply and "run `r1`" in reply
            assert ask("!sbx abandon").startswith("usage: abandon")
            reply = ask("!sbx retry inbox:a.md")
            assert "retry failed:" in reply and "abandon it first" in reply
            reply = ask("!sbx abandon inbox:a.md plan spiraled")
            assert "abandoned" in reply and "`r1`" in reply
            reply = ask("!sbx requeue inbox:a.md")
            assert "requeue failed:" in reply and "use retry" in reply
            assert "attempts reset" in ask("!sbx retry inbox:a.md")
            reply = ask("!sbx abandon gh:404")
            assert "abandon failed:" in reply and "gh:404" in reply
            assert "`inbox:a.md` queued" in ask("!sbx items")
            item = floop.dstore.get("inbox:a.md")
            assert item is not None and item.state == "queued" and item.attempts == 0
        finally:
            bridge.close()

    def test_cancel_and_retry_are_attributed_to_the_author(self, tmp_path: Path) -> None:
        """#246: the loop settles a cancel by who asked (GitHub comment,
        finish card); --retry re-queues instead; retry reruns a settled item
        under the author's name."""
        bridge, client, floop = make_bridge(tmp_path)
        floop.dstore.upsert_new(
            WorkItem(item_id="gh:8", source="github", source_key="8", title="Eight"), 1.0
        )
        floop.dstore.mark_running("gh:8", "r1", 1.0)
        floop.dstore.mark_cancelled("gh:8", "cancelled by op", 2.0)
        bridge.start()
        try:
            control = client.channels[42]
            for cmd in ("!sbx cancel", "!sbx cancel --retry"):
                bridge._handle_message(FakeMessage(cmd, control))
            assert wait_for(lambda: len(control.sent) >= 2)
            assert floop.cancel_calls == [
                ("Discord user `brett`", False),
                ("Discord user `brett`", True),
            ]
            for cmd in ("!sbx retry gh:8", "!sbx retry"):
                n = len(control.sent)
                bridge._handle_message(FakeMessage(cmd, control))
                assert wait_for(lambda n=n: len(control.sent) > n), cmd
            assert floop.retried == [("gh:8", "Discord user `brett`")]
            joined = "\n".join(control.sent)
            assert "settles as cancelled" in joined and "run again fresh" in joined
            assert "`gh:8` re-queued" in joined and "usage: retry" in joined
            item = floop.dstore.get("gh:8")
            assert item is not None and item.state == "queued"
            assert item.last_error == "re-queued by Discord user `brett`"
        finally:
            bridge.close()

    def test_plain_control_channel_message_gets_steering_hint(self, tmp_path: Path) -> None:
        """Field: both of Brett's steering attempts landed in the control
        channel, not the run's thread. A plain message there must answer
        with where to type, naming the live run's thread."""
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            control = client.channels[42]
            # nothing running: generic hint
            bridge._handle_message(FakeMessage("hello there", control))
            assert wait_for(lambda: any("Nothing is running" in s for s in control.sent))
            # live run: hint names its thread
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            engine = FakeEngine()
            bridge.run_started(item, "r1", engine, EventBus())  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread_id = bridge.dstore.discord_thread("r1").thread_id  # type: ignore[union-attr]
            bridge._handle_message(FakeMessage("also add a docstring", control))
            assert wait_for(lambda: any(f"<#{thread_id}>" in s and "r1" in s for s in control.sent))
            assert engine.posted == []  # never treated as steering
        finally:
            bridge.close()

    def test_gateway_failure_keeps_thread_alive_and_drains_queue(self, tmp_path: Path) -> None:
        """Review: if the gateway task exits (bad token / network), the
        bridge thread used to end while the loop kept enqueuing events —
        unbounded growth. Now it stays alive in degraded mode and drains."""
        bridge, client, _ = make_bridge(tmp_path)

        async def failing_start(token: str) -> None:
            raise RuntimeError("401 Unauthorized: bad token")

        client.start = failing_start  # type: ignore[method-assign]
        bridge.start(connect_wait_s=2.0)
        try:
            assert bridge._thread is not None and bridge._thread.is_alive()
            assert wait_for(lambda: bridge._degraded)
            # events keep flowing in from a run and must be consumed
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            bus = EventBus()
            bridge.run_started(item, "r1", FakeEngine(), bus)  # type: ignore[arg-type]
            for _ in range(200):
                bus.emit("agent.tool_start", "r1", tool="bash", args="ls")
            assert wait_for(lambda: bridge._events.qsize() < 50, timeout=10)
            assert bridge._thread.is_alive()
        finally:
            bridge.close()
        assert bridge._thread is not None and not bridge._thread.is_alive()

    def test_daemon_events_go_to_control_channel(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            bridge.daemon_event("circuit breaker opened")
            assert wait_for(lambda: "circuit breaker opened" in client.channels[42].sent)
            # URLs are masked so notices do not unfurl; an item we ran points at its thread
            item = WorkItem(item_id="gh:8", source="github", source_key="8", title="T")
            bridge.run_started(item, "r1", FakeEngine(), EventBus())  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            tid = bridge.dstore.discord_thread("r1").thread_id  # type: ignore[union-attr]
            bridge.daemon_event("✅ gh:8 done (1/1 tasks done) · PR https://x/pull/9")
            assert wait_for(
                lambda: any(
                    "<https://x/pull/9>" in s and f"<#{tid}>" in s for s in client.channels[42].sent
                )
            )
        finally:
            bridge.close()

    def test_tool_calls_are_batched_into_one_block_when_verbose(self, tmp_path: Path) -> None:
        # verbose keeps the stream-everything behaviour normal had before #235
        bridge, client, _ = make_bridge(tmp_path, chronology_level="verbose")
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            bus = EventBus()
            bridge.run_started(item, "r1", FakeEngine(), bus)  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
            for i in range(3):
                bus.emit(
                    "agent.tool_start", "r1", tool="bash", args=f"ls {i}", tool_call_id=f"c{i}"
                )
                bus.emit("agent.tool_end", "r1", tool="bash", tool_call_id=f"c{i}", success=True)
            bus.emit("agent.tool_start", "r1", tool="bash", args="pytest -q", tool_call_id="c9")
            bus.emit(
                "agent.tool_end",
                "r1",
                tool="bash",
                tool_call_id="c9",
                success=False,
                exit_code=1,
                error="FAILED test_x\n1 failed",
            )
            assert wait_for(lambda: any("✗ `bash` failed (exit 1)" in s for s in thread.sent))
            batch = next(s for s in thread.sent if s.startswith("```text\n$ bash  ls 0"))
            assert batch.count("$ bash") == 4 and "pytest -q   ✗ exit 1" in batch
            assert batch.endswith("```")
            detail = next(s for s in thread.sent if s.startswith("✗ `bash` failed"))
            assert "```text\nFAILED test_x\n1 failed\n```" in detail
        finally:
            bridge.close()

    def test_normal_level_digests_tool_bursts_into_one_edited_line(self, tmp_path: Path) -> None:
        """#235: hundreds of ⚙ lines drowned the human channel. At the
        normal level a burst is ONE message edited in place; agent
        messages close it; a failed call keeps its own detail block; the
        next burst is a fresh message."""
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            bus = EventBus()
            bridge.run_started(item, "r1", FakeEngine(), bus)  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
            bus.emit("agent.message", "r1", content="Looking around", agent="executor")
            for i in range(12):
                bus.emit(
                    "agent.tool_start", "r1", tool="bash", args=f"ls {i}", tool_call_id=f"c{i}"
                )
                bus.emit("agent.tool_end", "r1", tool="bash", tool_call_id=f"c{i}", success=True)
            bus.emit("agent.tool_start", "r1", tool="view", args="README.md", tool_call_id="v1")
            bus.emit("agent.tool_start", "r1", tool="bash", args="pytest -q", tool_call_id="c9")
            bus.emit(
                "agent.tool_end",
                "r1",
                tool="bash",
                tool_call_id="c9",
                success=False,
                exit_code=1,
                error="FAILED test_x\n1 failed",
            )
            failed = "✗ `bash` failed (exit 1)"
            assert wait_for(lambda: any(s.startswith(failed) for s in thread.sent))
            # the burst message went out once, after the agent message it follows
            digests = [m for m in thread.messages.values() if m.content.startswith("⚙ ")]
            assert wait_for(lambda: "14 tool calls (bash x13, view)" in digests[0].content, 8)
            assert len(digests) == 1
            assert "last: `pytest -q`" in digests[0].content and "✗ 1 failed" in digests[0].content
            assert sum(1 for s in thread.sent if s.startswith("⚙ ")) == 1
            assert not any("$ bash" in s for s in thread.sent)  # no streamed tool lines
            order = [s[:12] for s in thread.sent]
            first, second, third = (
                order.index("**executor**"),
                order.index("⚙ 1 tool cal"),
                order.index("✗ `bash` fai"),
            )
            assert first < second < third
            # an agent message closes the burst; the next tool call is a new message
            bus.emit("agent.message", "r1", content="Now fixing", agent="executor")
            bus.emit("agent.tool_start", "r1", tool="edit", args="x.py", tool_call_id="e1")
            assert wait_for(lambda: sum(1 for s in thread.sent if s.startswith("⚙ ")) == 2)
            assert thread.sent[-1] == "⚙ 1 tool call (edit) — last: `x.py`"
            bridge.run_finished(item, RunReport("r1", "completed", "1/1 tasks done"))
            assert wait_for(lambda: any(s.startswith("**finished") for s in thread.sent))
        finally:
            bridge.close()

    def test_digest_flags_repetitive_burst_and_surfaces_tool_cap(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path, command_prefix="!loop")
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            bus = EventBus()
            bridge.run_started(item, "r1", FakeEngine(), bus)  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
            for i in range(8):
                bus.emit(
                    "agent.tool_start",
                    "r1",
                    tool="bash",
                    args=f"grep -n 'exit {i}' /tmp/out.txt | od -c | head",
                    tool_call_id=f"c{i}",
                )
            bus.emit("phase.end", "r1", task_id="t1", phase="execute", status="ok")
            msgs = thread.messages
            assert wait_for(
                lambda: any("bash x8 similar commands" in m.content for m in msgs.values()), 8
            )
            digest = next(m for m in thread.messages.values() if m.content.startswith("⚙ "))
            assert "may be stuck; `!loop cancel` stops the run" in digest.content
            bus.emit("agent.tool_cap", "r1", cap=40)
            assert wait_for(lambda: any("⛔ tool-call ceiling (40)" in s for s in thread.sent))
        finally:
            bridge.close()

    def test_status_line_is_one_message_edited_in_place(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            bus = EventBus()
            bridge.run_started(item, "r1", FakeEngine(), bus)  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
            bus.emit(
                "task.state",
                "r1",
                task_id="t1",
                title="Add tests",
                state="pending",
                revisions=0,
                replans=0,
            )
            bus.emit(
                "task.state",
                "r1",
                task_id="t2",
                title="Wire CLI",
                state="pending",
                revisions=0,
                replans=0,
            )
            bus.emit("task.start", "r1", task_id="t1", title="Add tests")
            assert wait_for(lambda: bridge.dstore.discord_thread("r1").status_id is not None)  # type: ignore[union-attr]
            sid = bridge.dstore.discord_thread("r1").status_id  # type: ignore[union-attr]
            status_msg = thread.messages[sid]
            assert wait_for(lambda: status_msg.content.startswith("⏳ task 1/2 · **Add tests**"), 8)
            # rapid transitions coalesce into edits of the SAME message
            for st in ("executing", "verifying"):
                bus.emit("task.state", "r1", task_id="t1", state=st, revisions=0, replans=0)
            bus.emit("task.end", "r1", task_id="t1", title="Add tests", state="done")
            bus.emit("task.start", "r1", task_id="t2", title="Wire CLI")
            assert wait_for(lambda: "task 2/2" in status_msg.content, timeout=8)
            assert "✅ 1 done" in status_msg.content
            assert sum(1 for s in thread.sent if s.startswith("⏳")) == 1
            bridge.run_finished(item, RunReport("r1", "completed", "2/2 tasks done"))
            assert wait_for(lambda: status_msg.content.startswith("✅ finished"), timeout=8)
        finally:
            bridge.close()

    def test_headline_card_gains_branch_and_pr(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(
                item_id="gh:4", source="github", source_key="4", title="Fix", url="https://x/4"
            )
            bus = EventBus()
            bridge.run_started(item, "r1", FakeEngine(), bus)  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            control = client.channels[42]
            headline = control.messages[bridge.dstore.discord_thread("r1").headline_id]  # type: ignore[union-attr]
            bus.emit("sandbox.workspace_clone", "r1", source="/p", target="/t", branch="sbxloop/r1")
            assert wait_for(lambda: getattr(headline, "edits", 0) >= 1)
            bus.emit("run.deliver", "r1", repo="o/r", pr=3, url="https://x/pull/3")
            assert wait_for(lambda: getattr(headline, "edits", 0) >= 2)
            # the edited card lists the branch and PR (embed converted when discord.py present)
            embed = headline.embed
            assert embed is not None
            names = [f.name for f in embed.fields]
            assert "Branch" in names and "PR" in names
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
