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
from sbxloop.daemon.discord import DiscordBridge, _Pending, format_for_discord, headline_text
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


class FakeUser:
    def __init__(self, uid: int, name: str = "brett", *, bot: bool = False) -> None:
        self.id = uid
        self.name = name
        self.bot = bot


BOT_USER = FakeUser(777, "sbxloop", bot=True)


class FakeMessage:
    def __init__(
        self,
        content: str,
        channel: FakeChannel,
        *,
        bot: bool = False,
        mid: int = 500,
        mentions: list[FakeUser] | None = None,
        reply_to: FakeMessage | None = None,
    ) -> None:
        self.content = content
        self.channel = channel
        self.id = mid
        self.author = BOT_USER if bot else FakeUser(1, "brett")
        self.reactions: list[str] = []
        self.mentions = list(mentions or [])
        # discord.py: message.reference.resolved is the replied-to Message
        self.reference = type("Ref", (), {"resolved": reply_to})() if reply_to else None

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
        msg = FakeMessage(text or "", self, bot=True, mid=self._next_id)
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
        self.user = BOT_USER

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
            "run_cap_timezone": "UTC",
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
    tmp_path: Path, *, channel_id: int = 42, concierge: Any = None, **discord: Any
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

    bridge = DiscordBridge(
        config, dstore, loop_ref=floop, client_factory=factory, token="tok", concierge=concierge
    )
    return bridge, client, floop


class FakeConcierge:
    """Concierge stand-in: answers from a script; can call on_tool first."""

    def __init__(self, replies: list[Any] | None = None) -> None:
        from sbxloop.daemon.concierge import ConciergeReply

        self.replies = list(replies or [ConciergeReply("hello from the concierge")])
        self.turns: list[tuple[str, str]] = []
        self.pending = 0
        self.tool_calls: list[tuple[str, dict[str, Any], bool]] = []
        self.gate = threading.Event()
        self.gate.set()

    def submit_turn(self, text: str, *, author: str, on_tool: Any = None) -> Any:
        import concurrent.futures

        from sbxloop_worker.protocol import HostToolResponse

        self.turns.append((text, author))
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()

        def run() -> None:
            self.gate.wait(5)
            for name, args, ok in self.tool_calls:
                if on_tool is not None:
                    on_tool(name, args, HostToolResponse(call_id="c", ok=ok, text="t"))
            reply = self.replies.pop(0)
            if isinstance(reply, BaseException):
                future.set_exception(reply)
            else:
                future.set_result(reply)

        threading.Thread(target=run, daemon=True).start()
        return future


def steer_msg(content: str, thread: FakeChannel, *, mid: int = 777) -> FakeMessage:
    """A message that steers: it @mentions the bot, exactly as the control
    channel requires. Plain thread chatter is ignored, so every steering test
    goes through here. The bridge strips the token before relaying."""
    msg = FakeMessage(f"<@{BOT_USER.id}> {content}", thread, mid=mid, mentions=[BOT_USER])
    thread.messages[mid] = msg
    return msg


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
                RunReport(
                    "r1",
                    "completed",
                    "1/1 tasks done",
                    delivery=(3, "https://x/pull/3"),
                    filed=("gh:12",),
                ),
            )
            assert wait_for(
                lambda: any(s.startswith("**finished: completed**") for s in thread.sent)
            )
            # what the run filed rides on the finish text (no repo configured → plain #12)
            finish_text = next(s for s in thread.sent if s.startswith("**finished"))
            assert "\n🔀 PR #3 <https://x/pull/3>\n🔎 filed #12" in finish_text
            headline = control.messages[bridge.dstore.discord_thread("r1").headline_id]  # type: ignore[union-attr]
            assert wait_for(lambda: headline.content.startswith("✅"))
            # the finished card is an embed with the PR field
            finish_kwargs = [
                k
                for t, k in zip(thread.sent, thread.sent_kwargs, strict=True)
                if t.startswith("**finished")
            ]
            assert finish_kwargs and finish_kwargs[0].get("embed") is not None
            assert [f.name for f in finish_kwargs[0]["embed"].fields] == ["PR", "Filed"]
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
            msg = steer_msg("focus on auth first", thread)
            bridge._handle_message(msg)
            assert engine.posted == ["focus on auth first"]  # mention token stripped
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
            bridge._handle_message(steer_msg("too late?", thread))
            assert engine.posted == []
            assert wait_for(lambda: any("has finished" in s for s in thread.sent))
        finally:
            bridge.close()

    def test_plain_thread_message_never_steers(self, tmp_path: Path) -> None:
        """The bug: a run thread used to steer on *every* message, so two
        people discussing the run they were watching kept derailing it.
        Steering takes an @mention now, exactly like the control channel."""
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            engine = FakeEngine()
            bridge.run_started(item, "r1", engine, EventBus())  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
            before = len(thread.sent)
            chatter = FakeMessage("huh, that retry looks wrong", thread, mid=900)
            bridge._handle_message(chatter)
            bridge._handle_message(
                FakeMessage(f"<@{BOT_USER.id}>", thread, mid=901, mentions=[BOT_USER])
            )  # bare mention
            # A mention right after proves the run was steerable all along.
            bridge._handle_message(steer_msg("focus on auth first", thread))
            assert wait_for(lambda: engine.posted == ["focus on auth first"])
            assert chatter.reactions == []
            assert not any("has finished" in s for s in thread.sent[before:])
        finally:
            bridge.close()

    def test_reply_to_a_bot_message_in_a_thread_steers(self, tmp_path: Path) -> None:
        """The chronology is all bot messages, so replying to one is the
        other natural way to address the run — same rule as the concierge."""
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            engine = FakeEngine()
            bridge.run_started(item, "r1", engine, EventBus())  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
            chronology = FakeMessage("running tests…", thread, bot=True, mid=910)
            reply = FakeMessage("skip the slow ones", thread, mid=911, reply_to=chronology)
            thread.messages[911] = reply
            bridge._handle_message(reply)
            assert wait_for(lambda: engine.posted == ["skip the slow ones"])
        finally:
            bridge.close()

    def test_a_reply_discord_only_left_in_the_cache_still_steers(self, tmp_path: Path) -> None:
        """discord.py leaves reference.resolved None when the gateway payload
        omitted the referenced message; the cache is the fallback."""
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            engine = FakeEngine()
            bridge.run_started(item, "r1", engine, EventBus())  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
            chronology = FakeMessage("running tests…", thread, bot=True, mid=920)
            reply = FakeMessage("skip the slow ones", thread, mid=921)
            reply.reference = type("Ref", (), {"resolved": None, "cached_message": chronology})()
            thread.messages[921] = reply
            bridge._handle_message(reply)
            assert wait_for(lambda: engine.posted == ["skip the slow ones"])
            # A deleted referenced message has neither, and must not steer.
            gone = FakeMessage("and this?", thread, mid=922)
            gone.reference = type("Ref", (), {"resolved": None, "cached_message": None})()
            bridge._handle_message(gone)
            assert engine.posted == ["skip the slow ones"]
        finally:
            bridge.close()

    def test_commands_work_in_a_run_thread(self, tmp_path: Path) -> None:
        """`!sbx <verb>` is answered wherever the bot listens, and answers
        in the channel it was typed in."""
        bridge, client, floop = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            engine = FakeEngine()
            bridge.run_started(item, "r1", engine, EventBus())  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
            bridge._handle_message(FakeMessage("!sbx pause", thread, mid=930))
            assert wait_for(lambda: any("paused" in s for s in thread.sent))
            assert floop.paused is True
            assert engine.posted == []  # a command is never relayed as steering
        finally:
            bridge.close()

    def test_bot_messages_and_unknown_channels_are_ignored(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        engine = FakeEngine()
        bridge._engine = engine
        other = FakeChannel(client, 999)
        bridge._handle_message(FakeMessage("hi", other))
        bridge._handle_message(FakeMessage("hi", client.channels[42], bot=True))
        # Not the control channel and not a run thread: not ours, mention or not.
        bridge._handle_message(FakeMessage(f"<@{BOT_USER.id}> steer", other, mentions=[BOT_USER]))
        bridge._handle_message(FakeMessage("!sbx cancel", other))
        assert engine.posted == []
        assert other.sent == []

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
            assert "**queued:** 2" in joined
            assert "**runs today (UTC):** 1/12, resets at 00:00 UTC" in joined
            assert "olling" not in joined and "24h" not in joined
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

    def test_plain_control_channel_message_is_ignored(self, tmp_path: Path) -> None:
        """People talk among themselves in the control channel; the bot
        answers only commands and mentions (the old canned "steer in the
        thread" hint is gone — the concierge explains that when asked)."""
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            control = client.channels[42]
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            engine = FakeEngine()
            bridge.run_started(item, "r1", engine, EventBus())  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            before = len(control.sent)
            bridge._handle_message(FakeMessage("hello there", control))
            bridge._handle_message(FakeMessage("also add a docstring", control))
            time.sleep(0.3)
            assert len(control.sent) == before
            assert engine.posted == []  # never treated as steering
        finally:
            bridge.close()

    def test_mention_without_concierge_says_chat_is_off(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            control = client.channels[42]
            bridge._handle_message(FakeMessage("<@777> status?", control, mentions=[BOT_USER]))
            assert wait_for(lambda: any("chat is off" in s for s in control.sent))
            assert "`!sbx status`" in control.sent[-1]
        finally:
            bridge.close()

    def test_mention_goes_to_the_concierge_and_the_reply_is_posted(self, tmp_path: Path) -> None:
        from sbxloop.daemon.concierge import ConciergeReply

        concierge = FakeConcierge([ConciergeReply("two runs today; `r1` is live")])
        concierge.tool_calls = [("sbx_control", {"command": "status"}, True)]
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge)
        bridge.start()
        try:
            control = client.channels[42]
            msg = FakeMessage("<@777> what's running?", control, mentions=[BOT_USER], mid=900)
            bridge._handle_message(msg)
            assert wait_for(lambda: "✅" in msg.reactions)
            assert msg.reactions == ["⏳", "✅"]
            # the mention token was stripped; attribution names the user
            assert concierge.turns == [("what's running?", "Discord user `brett`")]
            # tool-note audit line, then the reply threaded under the question
            assert any(s.startswith("🛠 concierge: sbx_control(status)") for s in control.sent)
            idx = control.sent.index("two runs today; `r1` is live")
            assert control.sent_kwargs[idx].get("reference") is msg
            assert control.sent_kwargs[idx].get("mention_author") is False
        finally:
            bridge.close()

    def test_reply_to_bot_message_goes_to_the_concierge(self, tmp_path: Path) -> None:
        concierge = FakeConcierge()
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge)
        bridge.start()
        try:
            control = client.channels[42]
            asyncio.run(control.send("earlier bot message"))
            bot_msg = control.messages[max(control.messages)]
            bridge._handle_message(FakeMessage("and then?", control, reply_to=bot_msg))
            assert wait_for(lambda: concierge.turns == [("and then?", "Discord user `brett`")])
        finally:
            bridge.close()

    def test_concierge_error_reply_gets_a_warning(self, tmp_path: Path) -> None:
        from sbxloop.daemon.concierge import ConciergeReply

        concierge = FakeConcierge(
            [ConciergeReply("", ok=False, error="that took longer than 180s")]
        )
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge)
        bridge.start()
        try:
            control = client.channels[42]
            msg = FakeMessage("<@777> hi", control, mentions=[BOT_USER])
            bridge._handle_message(msg)
            assert wait_for(lambda: "⚠" in msg.reactions)
            assert any("⚠ concierge: that took longer than 180s" in s for s in control.sent)
        finally:
            bridge.close()

    def test_long_concierge_reply_is_split(self, tmp_path: Path) -> None:
        from sbxloop.daemon.concierge import ConciergeReply

        long = "\n\n".join(f"paragraph {i} " + "x" * 300 for i in range(12))
        concierge = FakeConcierge([ConciergeReply(long)])
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge, max_message_chars=1000)
        bridge.start()
        try:
            control = client.channels[42]
            msg = FakeMessage("<@777> tell me everything", control, mentions=[BOT_USER])
            bridge._handle_message(msg)
            assert wait_for(lambda: "✅" in msg.reactions)
            chunks = [s for s in control.sent if s.startswith("paragraph")]
            assert len(chunks) >= 3 and all(len(c) <= 1000 for c in chunks)
            # only the first chunk is a threaded reply
            refs = [
                control.sent_kwargs[i].get("reference")
                for i, s in enumerate(control.sent)
                if s.startswith("paragraph")
            ]
            assert refs[0] is msg and all(r is None for r in refs[1:])
        finally:
            bridge.close()

    def test_queued_turn_says_so(self, tmp_path: Path) -> None:
        concierge = FakeConcierge()
        concierge.pending = 2
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge)
        bridge.start()
        try:
            control = client.channels[42]
            bridge._handle_message(FakeMessage("<@777> hi", control, mentions=[BOT_USER]))
            assert wait_for(lambda: any("queued behind 2" in s for s in control.sent))
        finally:
            bridge.close()

    def test_unknown_verb_mentions_the_concierge(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path, concierge=FakeConcierge())
        bridge.start()
        try:
            control = client.channels[42]
            bridge._handle_message(FakeMessage("!sbx dance", control))
            assert wait_for(lambda: any("@mention me" in s for s in control.sent))
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
            assert batch.count("$ bash") == 4 and "pytest -q  ✗ exit 1" in batch
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

    def test_short_run_that_ends_before_the_gateway_is_up_is_posted_in_full(
        self, tmp_path: Path
    ) -> None:
        """--once (#236): a one-tick daemon can start AND finish its run
        before the gateway connects. Nothing may be dropped — headline,
        chronology and the finished card all land once the bridge is ready,
        and close() waits for them instead of exiting mid-queue."""
        bridge, client, _ = make_bridge(tmp_path)
        gate = threading.Event()

        async def slow_start(token: str) -> None:
            while not gate.is_set():
                await asyncio.sleep(0.02)
            client.bridge.mark_ready()  # type: ignore[union-attr]
            while not client.closed:
                await asyncio.sleep(0.05)

        client.start = slow_start  # type: ignore[method-assign]
        bridge.start(connect_wait_s=0.2)
        item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
        bus = EventBus()
        bridge.run_started(item, "r1", FakeEngine(), bus)  # type: ignore[arg-type]
        bus.emit("agent.message", "r1", content="all done quickly", agent="executor")
        bridge.run_finished(item, RunReport("r1", "completed", "1/1 tasks done"))
        assert client.channels[42].sent == []  # still connecting: buffered, not lost
        gate.set()
        bridge.close()  # drains before returning
        control = client.channels[42]
        assert control.sent and control.sent[0].startswith("▶ run `r1`")
        thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
        assert any("all done quickly" in s for s in thread.sent)
        assert any(s.startswith("**finished: completed**") for s in thread.sent)
        assert control.messages[bridge.dstore.discord_thread("r1").headline_id].content.startswith(  # type: ignore[union-attr]
            "✅"
        )

    def test_close_is_bounded_when_the_gateway_never_connects(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)

        async def never_ready(token: str) -> None:
            while not client.closed:
                await asyncio.sleep(0.05)

        client.start = never_ready  # type: ignore[method-assign]
        bridge.start(connect_wait_s=0.1)
        bridge.daemon_event("queued something")
        t0 = time.time()
        bridge.close(drain_wait_s=0.5)
        assert time.time() - t0 < 5
        assert bridge._thread is not None and not bridge._thread.is_alive()

    def test_steer_gets_a_live_wait_note_edited_in_place(self, tmp_path: Path) -> None:
        """#236: the ⏳ reaction says "received"; the note under the steer
        says where the agent is (phase, task, tool calls vs the #228
        ceiling) and is edited as that moves, then resolved with the reply."""
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            engine = FakeEngine()
            bus = EventBus()
            bridge.run_started(item, "r1", engine, bus)  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
            bus.emit("task.start", "r1", task_id="t2", title="Wire CLI")
            bus.emit("task.state", "r1", task_id="t2", state="executing", revisions=0, replans=0)
            for _ in range(3):
                bus.emit("agent.tool_start", "r1", tool="bash", args="ls")
            assert wait_for(
                lambda: (
                    bridge._progress.get("r1") is not None
                    and bridge._progress["r1"].tool_calls == 3
                )
            )
            msg = steer_msg("focus on auth first", thread)
            bridge._handle_message(msg)
            assert wait_for(lambda: any(s.startswith("⏳ steer queued") for s in thread.sent))
            note_id = next(
                m.id for m in thread.messages.values() if m.content.startswith("⏳ steer queued")
            )
            note = thread.messages[note_id]
            assert "mid-**execute** on `t2` · Wire CLI (3/40 tool calls so far)" in note.content
            assert note.content.endswith("answered at the next checkpoint")
            # more tool calls -> the SAME note is edited, not a new one
            for _ in range(2):
                bus.emit("agent.tool_start", "r1", tool="bash", args="ls")
            assert wait_for(lambda: "5/40 tool calls" in note.content, timeout=8)
            bus.emit("agent.tool_cap", "r1", cap=40, calls=40, tool="bash")
            assert wait_for(lambda: "ceiling reached" in note.content, timeout=8)
            # the engine picks it up at the checkpoint, then answers
            bus.emit("chat.message", "r1", message_id="m1", text="focus on auth first")
            assert wait_for(lambda: note.content.startswith("🧭 steer picked up"), timeout=8)
            bus.emit("chat.reply", "r1", message_id="m1", reply="Will do.", action="steer_task")
            assert wait_for(lambda: note.content == "✅ steer answered", timeout=8)
            assert wait_for(lambda: "✅" in msg.reactions)
            assert sum(1 for s in thread.sent if s.startswith("⏳ steer queued")) == 1
        finally:
            bridge.close()

    def test_steer_note_says_so_when_the_run_ends_first(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            bridge.run_started(item, "r1", FakeEngine(), EventBus())  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
            msg = steer_msg("late thought", thread, mid=778)
            bridge._handle_message(msg)
            assert wait_for(lambda: any(s.startswith("⏳ steer queued") for s in thread.sent))
            note = next(m for m in thread.messages.values() if m.content.startswith("⏳ steer"))
            assert note.content == "⏳ steer queued; answered at the next checkpoint"
            bridge.run_finished(item, RunReport("r1", "completed", "done"))
            assert wait_for(lambda: note.content.startswith("⚠ steer not answered"))
            assert any("1 steering message(s) were not answered" in s for s in thread.sent)
        finally:
            bridge.close()

    def test_reply_queued_before_the_finish_still_counts_as_answered(self, tmp_path: Path) -> None:
        """A short run can emit chat.reply and finish before the pump has
        drained either. Which steers went unanswered is decided by the pump
        after it drains what was queued ahead of the finish marker, so the
        queued reply still resolves its steer — no false "not answered"."""
        bridge, client, _ = make_bridge(tmp_path)
        gate = threading.Event()

        async def slow_start(token: str) -> None:
            while not gate.is_set():
                await asyncio.sleep(0.02)
            client.bridge.mark_ready()  # type: ignore[union-attr]
            while not client.closed:
                await asyncio.sleep(0.05)

        client.start = slow_start  # type: ignore[method-assign]
        bridge.start(connect_wait_s=0.2)
        item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
        bus = EventBus()
        engine = FakeEngine()
        bridge.run_started(item, "r1", engine, bus)  # type: ignore[arg-type]
        # The steer itself needs a live thread to be posted from Discord; the
        # bridge's own bookkeeping for it is what the finish path consults.
        mid = engine.post_user_message("focus on auth first")
        with bridge._lock:
            bridge._pending[mid] = _Pending("r1", 4242, 778)
        bus.emit("chat.reply", "r1", message_id=mid, reply="Will do.", action="steer_task")
        bridge.run_finished(item, RunReport("r1", "completed", "done"))
        gate.set()
        bridge.close()  # drains: chat.reply first, then the finish marker
        thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
        assert any(s.startswith("**finished: completed**") for s in thread.sent)
        assert not any("were not answered" in s for s in thread.sent)
        assert bridge._pending == {}


def test_threading_sanity() -> None:
    # guard: the module must not require an event loop at import/construct time
    assert threading.current_thread() is threading.main_thread()


class TestRunWatches:
    """#335: `watch_run` registers a Discord id on the bridge and the run's
    finish posts an @mention notice in the control channel."""

    def test_author_id_is_the_mentionable_form(self) -> None:
        from sbxloop.daemon.discord import _author_id, _author_name

        control = FakeChannel(FakeClient(), 42)
        msg = FakeMessage("hi", control)
        assert _author_id(msg) == "1"
        assert _author_name(msg) == "Discord user `brett`"
        msg.author = type("A", (), {"name": "webhook"})()
        assert _author_id(msg) is None

    def test_concierge_turn_records_the_id_and_on_watch_registers(self, tmp_path: Path) -> None:
        concierge = FakeConcierge()
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge)
        bridge.start()
        try:
            control = client.channels[42]
            bridge._handle_message(
                FakeMessage("<@777> watch r1", control, mentions=[BOT_USER], mid=901)
            )
            assert wait_for(lambda: concierge.turns != [])
            assert bridge.on_watch("r1", "Discord user `brett`") is None
            assert bridge._watchers == {"r1": ["1"]}
            # a second registration by the same person is not duplicated
            assert bridge.on_watch("r1", "Discord user `brett`") is None
            assert bridge._watchers == {"r1": ["1"]}
        finally:
            bridge.close()

    def test_watch_registers_through_the_real_concierge_seam(self, tmp_path: Path) -> None:
        """Regression for the author/`by` mismatch: `_remember_requester`
        stores a watcher's id under the bare author string, but a real
        concierge turn's tool calls arrive tagged with
        `concierge.VIA_CONCIERGE_SUFFIX` (`Concierge._tool_handler` builds
        `by = f"{author}{VIA_CONCIERGE_SUFFIX}"`) before reaching `on_watch`.
        Drive an actual `Concierge` — not `FakeConcierge`, which never adds
        the tag — wired to `bridge.on_watch`, so the seam the two test files
        used to test in contradictory isolation is now actually crossed."""
        from sbxloop.engine.store import StateStore
        from tests.unit.test_daemon_concierge import make, turn

        bridge, _client, _floop = make_bridge(tmp_path)
        author = "Discord user `brett`"
        bridge._remember_requester(author, "555")
        concierge, _, _, _, _ = make(
            tmp_path,
            [{"calls": [("watch_run", {"run_id_or_item_id": "r1"})]}],
            on_watch=bridge.on_watch,
        )
        store = StateStore(tmp_path / "state" / "state.db")
        store.create_run("r1", "Ship it")
        store.set_run_state("r1", "running")
        turn(concierge, author=author)
        assert bridge._watchers == {"r1": ["555"]}

    def test_unknown_requester_registers_nothing_and_says_so(self, tmp_path: Path) -> None:
        bridge, _, _ = make_bridge(tmp_path)
        note = bridge.on_watch("r1", "Discord user `nobody`")
        assert note is not None and "mentionable id" in note
        assert bridge._watchers == {}

    def test_finish_pings_watchers_once_with_the_outcome(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            bridge.run_started(item, "r1", FakeEngine(), EventBus())  # type: ignore[arg-type]
            control = client.channels[42]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            bridge._remember_requester("Discord user `brett`", "1")
            assert bridge.on_watch("r1", "Discord user `brett`") is None
            bridge.run_finished(
                item,
                RunReport(
                    "r1",
                    "completed",
                    "1/1 tasks done",
                    delivery=(3, "https://x/pull/3"),
                ),
            )
            assert wait_for(lambda: any(s.startswith("<@1> run `r1`") for s in control.sent))
            notice = next(s for s in control.sent if s.startswith("<@1> run `r1`"))
            assert "**completed**" in notice
            assert "1/1 tasks done" in notice
            assert "🔀 PR #3 <https://x/pull/3>" in notice
            tid = bridge.dstore.discord_thread("r1").thread_id  # type: ignore[union-attr]
            assert f"<#{tid}>" in notice
            assert bridge._watchers == {}
            # a second finish for the same run pings nobody (the pop is final)
            bridge.run_finished(item, RunReport("r1", "completed", "1/1 tasks done"))
            assert not wait_for(
                lambda: sum(1 for s in control.sent if s.startswith("<@1>")) > 1, timeout=1.0
            )
        finally:
            bridge.close()

    def test_no_watchers_posts_no_mention(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            bridge.run_started(item, "r1", FakeEngine(), EventBus())  # type: ignore[arg-type]
            control = client.channels[42]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
            bridge.run_finished(item, RunReport("r1", "completed", "1/1 tasks done"))
            assert wait_for(lambda: any("finished: completed" in s for s in thread.sent))
            assert not any("<@" in s for s in control.sent)
        finally:
            bridge.close()

    def test_two_watchers_share_one_message(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            bridge.run_started(item, "r1", FakeEngine(), EventBus())  # type: ignore[arg-type]
            control = client.channels[42]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            bridge._remember_requester("brett", "1")
            bridge._remember_requester("dana", "2")
            assert bridge.on_watch("r1", "brett") is None
            assert bridge.on_watch("r1", "dana") is None
            bridge.run_finished(item, RunReport("r1", "failed", "0/1 tasks done"))
            assert wait_for(lambda: any(s.startswith("<@1> <@2>") for s in control.sent))
            notices = [s for s in control.sent if "<@1>" in s]
            assert len(notices) == 1
            assert "**failed**" in notices[0]
        finally:
            bridge.close()

    def test_a_restart_forgets_every_watch(self, tmp_path: Path) -> None:
        # watches (and the requester-id map) live in bridge memory only: a
        # freshly constructed bridge over the same state dir knows nothing,
        # which is what the concierge's confirmation warns about.
        bridge, _, _ = make_bridge(tmp_path)
        bridge._remember_requester("Discord user `brett`", "1")
        assert bridge.on_watch("r1", "Discord user `brett`") is None
        assert bridge._watchers == {"r1": ["1"]}

        restarted, _, _ = make_bridge(tmp_path)
        assert restarted._watchers == {}
        note = restarted.on_watch("r1", "Discord user `brett`")
        assert note is not None and "mentionable id" in note
        assert restarted._watchers == {}

    def test_post_failure_is_not_fatal(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A")
            bridge.run_started(item, "r1", FakeEngine(), EventBus())  # type: ignore[arg-type]
            control = client.channels[42]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            bridge._remember_requester("brett", "1")
            assert bridge.on_watch("r1", "brett") is None

            async def boom(*a: Any, **k: Any) -> None:
                raise RuntimeError("discord is on fire")

            thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
            bridge._send_channel = boom  # type: ignore[method-assign]
            bridge.run_finished(item, RunReport("r1", "completed", "1/1 tasks done"))
            # the finish path still completes: the card lands and state is cleared
            assert wait_for(lambda: any("finished: completed" in s for s in thread.sent))
            assert wait_for(lambda: "r1" not in bridge._items)
            assert bridge._watchers == {}
            assert not any("<@1>" in s for s in control.sent)
        finally:
            bridge.close()


class TestToolOutputExcerptWiring:
    """The configured excerpt caps reach both render paths (t5/#403)."""

    def test_verbose_batcher_path_uses_configured_caps(self, tmp_path: Path) -> None:
        bridge, _, _ = make_bridge(
            tmp_path,
            chronology_level="verbose",
            tool_output_lines=2,
            tool_fail_output_lines=6,
        )
        detail = "\n".join(f"L{i}" for i in range(50))
        bridge._render("r1", ev("agent.tool_start", tool="bash", args="ls", tool_call_id="c1"))
        ok = bridge._render(
            "r1",
            ev(
                "agent.tool_end",
                tool="bash",
                tool_call_id="c1",
                success=True,
                exit_code=0,
                output=detail,
                output_lines=50,
            ),
        )
        excerpt = next(c for c in ok if c.text.startswith("✓"))
        assert "… 48 lines elided …" in excerpt.text
        assert len(excerpt.text) <= 2000

        bad = bridge._render(
            "r1",
            ev(
                "agent.tool_end",
                tool="bash",
                tool_call_id="c2",
                success=False,
                exit_code=2,
                error="boom: it broke",
                output=detail,
                output_lines=50,
            ),
        )
        fail = next(c for c in bad if c.text.startswith("✗"))
        assert "(exit 2)" in fail.text and "boom: it broke" in fail.text

    def test_normal_digest_path_surfaces_failure_only(self, tmp_path: Path) -> None:
        bridge, _, _ = make_bridge(tmp_path, tool_fail_output_lines=6)
        detail = "\n".join(f"E{i}" for i in range(100))
        bridge._render("r1", ev("agent.tool_start", tool="bash", args="ls", tool_call_id="c1"))
        assert (
            bridge._render(
                "r1",
                ev(
                    "agent.tool_end",
                    tool="bash",
                    tool_call_id="c1",
                    success=True,
                    exit_code=0,
                    output=detail,
                    output_lines=100,
                ),
            )
            == []
        )
        chunks = bridge._render(
            "r1",
            ev(
                "agent.tool_end",
                tool="bash",
                tool_call_id="c2",
                success=False,
                exit_code=1,
                error=detail,
                output_lines=100,
            ),
        )
        assert len(chunks) == 1
        assert "… 94 lines elided …" in chunks[0].text
        assert len(chunks[0].text) <= 2000


class TestToolOutputRedactionWiring:
    """event -> _render -> chunk: nothing published carries a credential (#403 t6)."""

    PAT = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    JWT = "eyJhbGciOiJIUzI1NiJ9.payload.signature"
    KEY = "sk-live-abcdef0123456789"

    @property
    def command(self) -> str:
        return (
            f"gh auth login --with-token {self.PAT} && "
            f"curl -H 'Authorization: Bearer {self.JWT}' -X GET https://api && "
            f"API_KEY={self.KEY} deploy"
        )

    @property
    def output(self) -> str:
        return (
            f"error: bad credentials\ntoken was {self.PAT}\n"
            f"Authorization: Bearer {self.JWT}\nAPI_KEY={self.KEY}\n"
        )

    @pytest.mark.parametrize("level", ["verbose", "normal"])
    def test_no_secret_reaches_any_chunk(self, tmp_path: Path, level: str) -> None:
        bridge, _, _ = make_bridge(
            tmp_path, chronology_level=level, tool_output_lines=5, tool_fail_output_lines=20
        )
        chunks = list(
            bridge._render(
                "r1",
                ev("agent.tool_start", tool="bash", args=self.command, tool_call_id="c1"),
            )
        )
        chunks += bridge._render(
            "r1",
            ev(
                "agent.tool_end",
                tool="bash",
                tool_call_id="c1",
                success=False,
                exit_code=1,
                error=self.output,
                output=self.output,
                output_lines=4,
            ),
        )
        chunks += bridge._render("r1", ev("agent.message", text="done"))
        texts = [c.text for c in chunks]
        assert texts
        blob = "\n".join(texts)
        for literal in (self.PAT, self.JWT, self.KEY):
            assert literal not in blob, literal
        assert "***" in blob
        assert all(len(t) <= 2000 for t in texts)
