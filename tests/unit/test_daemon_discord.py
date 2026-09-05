"""Discord bridge: pure formatter, steering/command routing with a fake
client, non-blocking bus subscription. No network, discord.py not required."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.daemon.discord import DiscordBridge, _Pending, format_for_discord, headline_text
from sbxloop.daemon.discord_format import Chunk
from sbxloop.daemon.model import DaemonNotice, RunReport, WorkItem
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
        assert chunks and chunks[0].text.startswith("**planner** · `unknown · claude-sonnet-5`\n")
        assert all(len(c.text) <= 1900 for c in chunks)
        for t in ("agent.message_delta", "worker.heartbeat", "agent.usage", "sandbox.resources"):
            assert format_for_discord(ev(t, x=1)) == []

    def test_headline_states(self) -> None:
        item = WorkItem(item_id="gh:issue:4", source_key="4", title="Fix login", url="https://x/4")
        assert headline_text(item, "r1").startswith(
            "▶ run `r1` — **Fix login** · `gh:issue:4` · [issue #4](https://x/4)"
        )
        legacy = WorkItem(item_id="gh:4", source_key="4", title="Fix login", url="https://x/4")
        assert "`gh:issue:4`" in headline_text(legacy, "r1")
        assert "gh:4 " not in headline_text(legacy, "r1")
        assert headline_text(item, "r1", "completed").startswith("✅")
        assert headline_text(item, "r1", "blocked").startswith("🚧")


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

    async def edit(
        self,
        *,
        content: str | None = None,
        embed: Any = None,
        suppress: bool | None = None,
        **extra: Any,
    ) -> None:
        if content is not None:
            self.content = content
        self.embed = embed
        self.suppress = suppress
        self.edit_kwargs = getattr(self, "edit_kwargs", [])
        self.edit_kwargs.append({"content": content, "embed": embed, "suppress": suppress, **extra})
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
        self.holds: set[str] = set()
        self.claiming: str | None = None
        self.cancelled = 0
        self.cancel_calls: list[tuple[str | None, bool]] = []
        self.retried: list[tuple[str, str | None]] = []
        self.hold_calls: list[tuple[str, str | None, str | None]] = []
        self.granted: list[tuple[str, int, str | None]] = []
        self.repos: list[dict[str, Any]] = []
        self.resumed_repos: list[tuple[str, str | None]] = []

    @property
    def paused(self) -> bool:
        return bool(self.holds)

    @paused.setter
    def paused(self, value: bool) -> None:
        # Tests that predate named holds flip the flag directly: that is the
        # operator's hold.
        if value:
            self.holds.add("operator")
        else:
            self.holds.clear()

    def status(self) -> dict[str, Any]:
        return {
            "current": None,
            "queued": 2,
            "runs_today": 1,
            "max_runs_per_day": 12,
            "run_cap_timezone": "UTC",
            "breaker_open": False,
            "paused": self.paused,
            "holds": sorted(self.holds),
            "claiming": self.claiming,
            "stopping": False,
            "repos": list(self.repos),
        }

    def pause(self, hold: str = "operator", *, by: str | None = None) -> list[str]:
        self.hold_calls.append(("pause", hold, by))
        self.holds.add(hold)
        return sorted(self.holds)

    def unpause(self, hold: str | None = "operator", *, by: str | None = None) -> list[str]:
        self.hold_calls.append(("unpause", hold, by))
        if hold is None:
            self.holds.clear()
        else:
            self.holds.discard(hold)
        return sorted(self.holds)

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

    def resume_repo(self, repo: str, by: str | None = None) -> dict[str, Any]:
        self.resumed_repos.append((repo, by))
        if repo == "o/zzz":
            raise KeyError(f"unknown repository {repo!r}")
        return {"repo": repo, "state": "ok"}

    def grant_rounds(self, run_id: str, rounds: int, by: str | None = None) -> WorkItem:
        self.granted.append((run_id, rounds, by))
        if run_id == "r_unknown":
            raise ValueError(f"unknown run {run_id}")
        return WorkItem(item_id="gh:issue:9", source_key="9", title="Nine", run_id=run_id)


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

    def submit_turn(
        self, text: str, *, author: str, author_id: str | None = None, on_tool: Any = None
    ) -> Any:
        import concurrent.futures

        from sbxloop_worker.protocol import HostToolResponse

        self.turns.append((text, author))
        self.author_ids = [*getattr(self, "author_ids", []), author_id]
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
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
                    pr=(3, "https://x/pull/3"),
                ),
            )
            assert wait_for(
                lambda: any(s.startswith("**finished: completed**") for s in thread.sent)
            )
            # the PR rides on the finish text
            finish_text = next(s for s in thread.sent if s.startswith("**finished"))
            assert "\n🔀 PR #3 <https://x/pull/3>" in finish_text
            headline = control.messages[bridge.dstore.discord_thread("r1").headline_id]  # type: ignore[union-attr]
            assert wait_for(lambda: headline.content.startswith("✅"))
            # the finished card is an embed with the PR field
            finish_kwargs = [
                k
                for t, k in zip(thread.sent, thread.sent_kwargs, strict=True)
                if t.startswith("**finished")
            ]
            assert finish_kwargs and finish_kwargs[0].get("embed") is not None
            assert [f.name for f in finish_kwargs[0]["embed"].fields] == ["PR"]
        finally:
            bridge.close()

    def test_bus_subscriber_never_blocks(self, tmp_path: Path) -> None:
        bridge, _client, _ = make_bridge(tmp_path)
        # do NOT start the bridge: nothing drains the queue; emits must still return instantly
        item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
            WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A"), 1.0
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
            reply = ask("!sbx abandon gh:issue:404")
            assert "abandon failed:" in reply and "gh:issue:404" in reply
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
        floop.dstore.upsert_new(WorkItem(item_id="gh:issue:8", source_key="8", title="Eight"), 1.0)
        floop.dstore.mark_running("gh:issue:8", "r1", 1.0)
        floop.dstore.mark_cancelled("gh:issue:8", "cancelled by op", 2.0)
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
            for cmd in ("!sbx retry gh:issue:8", "!sbx retry"):
                n = len(control.sent)
                bridge._handle_message(FakeMessage(cmd, control))
                assert wait_for(lambda n=n: len(control.sent) > n), cmd
            assert floop.retried == [("gh:issue:8", "Discord user `brett`")]
            joined = "\n".join(control.sent)
            assert "settles as cancelled" in joined and "run again fresh" in joined
            assert "`gh:issue:8` re-queued" in joined and "usage: retry" in joined
            item = floop.dstore.get("gh:issue:8")
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
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
            bus = EventBus()
            bridge.run_started(item, "r1", FakeEngine(), bus)  # type: ignore[arg-type]
            for _ in range(200):
                bus.emit("agent.tool_start", "r1", tool="bash", args="ls")
            assert wait_for(lambda: bridge._events.qsize() < 50, timeout=10)
            assert bridge._thread.is_alive()
        finally:
            bridge.close()
        assert bridge._thread is not None and not bridge._thread.is_alive()

    def test_daemon_notices_go_to_control_channel(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            bridge.daemon_notice(
                DaemonNotice("breaker.opened", "circuit breaker opened", level="warning")
            )
            control = client.channels[42]
            assert wait_for(lambda: any("circuit breaker opened" in s for s in control.sent))
            # the level rides as a marker; a notice with no run stays in the channel
            assert any(s.startswith("⚠ circuit breaker opened") for s in control.sent)
            # URLs are masked so notices do not unfurl; an item we ran points at its thread
            item = WorkItem(item_id="gh:issue:8", source_key="8", title="T")
            bridge.run_started(item, "r1", FakeEngine(), EventBus())  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            tid = bridge.dstore.discord_thread("r1").thread_id  # type: ignore[union-attr]
            bridge.daemon_notice(
                DaemonNotice(
                    "run.done",
                    "✅ gh:issue:8 done (1/1 tasks done) · PR https://x/pull/9",
                    item_id="gh:issue:8",
                    run_id="r1",
                    url="https://x/pull/9",
                )
            )
            assert wait_for(
                lambda: any("<https://x/pull/9>" in s and f"<#{tid}>" in s for s in control.sent)
            )
            # ...and the same line landed in the run's thread, without the pointer
            thread = client.channels[tid]
            assert wait_for(lambda: any("<https://x/pull/9>" in s for s in thread.sent))
            assert not any(f"<#{tid}>" in s for s in thread.sent)
        finally:
            bridge.close()

    def test_non_terminal_run_notices_stay_in_the_thread(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="gh:issue:8", source_key="8", title="T")
            bridge.run_started(item, "r1", FakeEngine(), EventBus())  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            tid = bridge.dstore.discord_thread("r1").thread_id  # type: ignore[union-attr]
            bridge.daemon_notice(
                DaemonNotice(
                    "run.resuming", "resuming r1 (attempt 2)", item_id="gh:issue:8", run_id="r1"
                )
            )
            thread = client.channels[tid]
            assert wait_for(lambda: any("resuming r1" in s for s in thread.sent))
            assert not any("resuming r1" in s for s in client.channels[42].sent)
            # a run the bridge never opened a thread for falls back to the channel
            bridge.daemon_notice(
                DaemonNotice(
                    "run.resuming", "resuming r9 (attempt 2)", item_id="gh:issue:9", run_id="r9"
                )
            )
            assert wait_for(lambda: any("resuming r9" in s for s in client.channels[42].sent))
        finally:
            bridge.close()

    def test_tool_calls_are_batched_into_one_block_when_verbose(self, tmp_path: Path) -> None:
        # verbose keeps the stream-everything behaviour normal had before #235
        bridge, client, _ = make_bridge(tmp_path, chronology_level="verbose")
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
            item = WorkItem(item_id="gh:issue:4", source_key="4", title="Fix", url="https://x/4")
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
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
        item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
        bridge.daemon_notice(DaemonNotice("item.queued", "queued something", item_id="gh:issue:1"))
        t0 = time.time()
        bridge.close(drain_wait_s=0.5)
        assert time.time() - t0 < 5
        assert bridge._thread is not None and not bridge._thread.is_alive()

    def test_run_summary_is_the_threads_last_post(self, tmp_path: Path) -> None:
        """The very last post in a run thread is the end-of-run summary card:
        the run's numbers, what went well, what needed work."""
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
            bus = EventBus()
            bridge.run_started(item, "r1", FakeEngine(), bus)  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
            bus.emit("agent.usage", "r1", input_tokens=1200, output_tokens=80)
            bus.emit(
                "phase.end",
                "r1",
                task_id="t1",
                phase="verify",
                status="failed",
                message="verify command failed: `lint` (exit 1)",
            )
            bus.emit("task.state", "r1", task_id="t1", state="done", revisions=1)
            bridge.run_finished(
                item,
                RunReport("r1", "completed", "1/1 tasks done", pr=(3, "https://x/pull/3")),
            )
            assert wait_for(lambda: any(s.startswith("📊 **run summary**") for s in thread.sent))
            # After the finish card, nothing else lands in the thread.
            assert thread.sent[-1].startswith("📊 **run summary**")
            assert "1 turn(s)" in thread.sent[-1] and "1,200 in / 80 out tokens" in thread.sent[-1]
            summary_kwargs = thread.sent_kwargs[-1]
            card = summary_kwargs["embed"]
            names = [f.name for f in card.fields]
            assert names == ["Stats", "Went well", "Needed work"]
            values = {f.name: f.value for f in card.fields}
            assert "delivered PR [#3](https://x/pull/3)" in values["Went well"]
            assert (
                "`t1` verify: **failed** — verify command failed: `lint` (exit 1)"
                in values["Needed work"]
            )
        finally:
            bridge.close()

    def test_steer_gets_a_live_wait_note_edited_in_place(self, tmp_path: Path) -> None:
        """#236: the ⏳ reaction says "received"; the note under the steer
        says where the agent is (phase, task, tool calls vs the #228
        ceiling) and is edited as that moves, then resolved with the reply."""
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
            assert "mid-**build** on `t2` · Wire CLI (3/60 tool calls so far)" in note.content
            assert note.content.endswith("answered at the next checkpoint")
            # more tool calls -> the SAME note is edited, not a new one
            for _ in range(2):
                bus.emit("agent.tool_start", "r1", tool="bash", args="ls")
            assert wait_for(lambda: "5/60 tool calls" in note.content, timeout=8)
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
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
        item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
            assert bridge.dstore.run_watchers("r1") == ["1"]
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
        store.set_run_state("r1", "building")
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
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
                    pr=(3, "https://x/pull/3"),
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
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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

    def test_a_restart_reloads_every_watch(self, tmp_path: Path) -> None:
        # watches are persisted in daemon_state: a freshly constructed bridge
        # over the same state dir repopulates `_watchers` at startup.
        bridge, _, _ = make_bridge(tmp_path)
        bridge._remember_requester("Discord user `brett`", "1")
        assert bridge.on_watch("r1", "Discord user `brett`") is None
        assert bridge._watchers == {"r1": ["1"]}
        assert bridge.dstore.run_watchers("r1") == ["1"]

        restarted, _, _ = make_bridge(tmp_path)
        assert restarted._watchers == {"r1": ["1"]}
        # the requester-id map is still memory-only, so a re-registration
        # from a bridge that has not seen the author yet says so
        note = restarted.on_watch("r2", "Discord user `brett`")
        assert note is not None and "mentionable id" in note

    def test_reloaded_watch_is_pinged_once_after_a_restart(self, tmp_path: Path) -> None:
        first, _, _ = make_bridge(tmp_path)
        first._remember_requester("Discord user `brett`", "1")
        assert first.on_watch("r1", "Discord user `brett`") is None

        bridge, client, _ = make_bridge(tmp_path)
        assert bridge._watchers == {"r1": ["1"]}
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
            bridge.run_started(item, "r1", FakeEngine(), EventBus())  # type: ignore[arg-type]
            control = client.channels[42]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            bridge.run_finished(item, RunReport("r1", "completed", "1/1 tasks done"))
            assert wait_for(lambda: any(s.startswith("<@1> run `r1`") for s in control.sent))
            assert bridge._watchers == {}
            assert wait_for(lambda: bridge.dstore.run_watchers("r1") == [])
            # a second finish pings nobody: the persisted rows are gone too
            bridge.run_finished(item, RunReport("r1", "completed", "1/1 tasks done"))
            assert not wait_for(
                lambda: sum(1 for s in control.sent if s.startswith("<@1>")) > 1, timeout=1.0
            )
        finally:
            bridge.close()

    def test_persist_and_drain_are_atomic_under_the_same_lock(self, tmp_path: Path) -> None:
        """Regression for the TOCTOU: before the fix, `_persist_watch` ran
        after `_watch_lock` was released, so a `_take_watchers` drain could
        interleave between the in-memory append and the store INSERT — the
        drain would pop an empty in-memory entry and delete zero store
        rows, and the INSERT would land afterward, orphaning the row
        forever (nothing else ever deletes a row for a run that already
        finished). Now the persist happens inside `on_watch`'s
        `_watch_lock`, the same lock `_take_watchers` holds across its own
        pop-plus-drain, so a concurrent drain cannot even start until a
        registration in flight has fully committed to both registries."""
        bridge, _, _ = make_bridge(tmp_path)
        bridge._remember_requester("Discord user `brett`", "1")

        entered_persist = threading.Event()
        release_persist = threading.Event()
        real_add = bridge.dstore.add_run_watch

        def slow_add_run_watch(
            run_id: str, watcher_id: str, now: float, *, backend: str = "discord"
        ) -> None:
            entered_persist.set()
            assert release_persist.wait(timeout=5.0)
            real_add(run_id, watcher_id, now, backend=backend)

        bridge.dstore.add_run_watch = slow_add_run_watch  # type: ignore[method-assign]

        result: dict[str, object] = {}

        def register() -> None:
            result["note"] = bridge.on_watch("r1", "Discord user `brett`")

        registrar = threading.Thread(target=register)
        registrar.start()
        assert entered_persist.wait(timeout=5.0)

        drained: list[str] = []
        drain_done = threading.Event()

        def drain() -> None:
            drained.extend(bridge._take_watchers("r1"))
            drain_done.set()

        drainer = threading.Thread(target=drain)
        drainer.start()
        # The registration is mid-flight, still holding `_watch_lock` —
        # a concurrent drain must not be able to complete (or even see a
        # partial state) until it releases.
        assert not drain_done.wait(timeout=0.3), (
            "drain proceeded while a registration was in flight"
        )

        release_persist.set()
        registrar.join(timeout=5.0)
        drainer.join(timeout=5.0)
        assert not registrar.is_alive()
        assert not drainer.is_alive()

        assert result["note"] is None
        # The drain necessarily runs after the registration fully commits,
        # so it sees a consistent state: the watcher, and no orphaned row.
        assert drained == ["1"]
        assert bridge.dstore.run_watchers("r1") == []

    def test_watchers_cap_eviction_clears_the_persisted_row_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: the `WATCHERS_CAP` trim evicted the in-memory entry
        but left its row in `daemon_run_watches`, so the persisted registry
        grew without bound for runs evicted before they ever finish (and
        left `DaemonStore.clear_run_watch` dead code, exercised only by a
        store-level unit test). The eviction path now calls it."""
        import sbxloop.daemon.chat as chat_mod

        monkeypatch.setattr(chat_mod, "WATCHERS_CAP", 1)
        bridge, _, _ = make_bridge(tmp_path)
        bridge._remember_requester("brett", "1")
        bridge._remember_requester("dana", "2")
        assert bridge.on_watch("r1", "brett") is None
        assert bridge.dstore.run_watchers("r1") == ["1"]

        # registering a second run trips the cap and evicts r1's entry.
        assert bridge.on_watch("r2", "dana") is None
        assert "r1" not in bridge._watchers
        assert bridge.dstore.run_watchers("r1") == []
        assert bridge.dstore.run_watchers("r2") == ["2"]

    def test_reload_drops_a_watch_whose_run_already_finished(self, tmp_path: Path) -> None:
        """A watch for a run that reached a terminal ledger state while the
        daemon was down is dropped at reload instead of revived: the
        `run_finished` event that would drain it has already fired, so
        reviving the entry would leave it waiting for an event that will
        never happen again. Reconciled via `DaemonStore.finished_run_ids`."""
        bridge, _, _ = make_bridge(tmp_path)
        bridge._remember_requester("Discord user `brett`", "1")
        assert bridge.on_watch("r1", "Discord user `brett`") is None
        bridge.dstore.upsert_new(
            WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A"), 1.0
        )
        bridge.dstore.mark_running("inbox:a.md", "r1", 1.0)
        bridge.dstore.finish_ledger("r1", "completed", 2.0)

        restarted, _, _ = make_bridge(tmp_path)
        assert restarted._watchers == {}
        assert restarted.dstore.run_watchers("r1") == []

    def test_watches_work_without_a_store_wired(self, tmp_path: Path) -> None:
        bridge, _, _ = make_bridge(tmp_path)
        bridge.dstore = None  # type: ignore[assignment]
        bridge._remember_requester("Discord user `brett`", "1")
        assert bridge.on_watch("r1", "Discord user `brett`") is None
        assert bridge._watchers == {"r1": ["1"]}
        assert bridge._take_watchers("r1") == ["1"]
        assert bridge._take_watchers("r1") == []

    def test_post_failure_is_not_fatal(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
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


class TestSendSuppressesUnfurls:
    """The send seam defaults to Discord's SUPPRESS_EMBEDS flag so agent
    prose never sprouts link previews; messages carrying one of our own
    embed cards angle-bracket their URLs instead (the flag would hide the
    card too)."""

    def _send(self, bridge: DiscordBridge, channel: Any, *args: Any, **kwargs: Any) -> Any:
        return asyncio.run(bridge._send(channel, *args, **kwargs))

    def test_plain_send_sets_suppress_embeds(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        self._send(bridge, channel, "see https://example.com/x")
        assert channel.sent == ["see https://example.com/x"]
        assert channel.sent_kwargs[0]["suppress_embeds"] is True
        assert channel.sent_kwargs[0]["allowed_mentions"] == "none"

    def test_send_with_our_embed_masks_urls_instead_of_flagging(self, tmp_path: Path) -> None:
        from sbxloop.daemon.discord_format import EmbedSpec

        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        self._send(bridge, channel, "run https://x/run/1", embed=EmbedSpec(title="t"))
        assert "suppress_embeds" not in channel.sent_kwargs[0]
        assert channel.sent == ["run <https://x/run/1>"]
        assert channel.sent_kwargs[0].get("embed") is not None

    def test_embed_rejection_retry_is_text_only_and_suppressed(self, tmp_path: Path) -> None:
        from sbxloop.daemon.discord_format import EmbedSpec

        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        real_send = channel.send

        async def send(text: str | None = None, **kwargs: Any) -> Any:
            if "embed" in kwargs:
                raise RuntimeError("embeds rejected")
            return await real_send(text, **kwargs)

        channel.send = send  # type: ignore[assignment]
        self._send(bridge, channel, "body", embed=EmbedSpec(title="t"))
        assert channel.sent == ["body"]
        assert channel.sent_kwargs[0]["suppress_embeds"] is True
        assert "embed" not in channel.sent_kwargs[0]

    def test_content_is_clipped_on_every_path(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path, max_message_chars=200)
        channel = client.channels[42]
        self._send(bridge, channel, "x" * 500)
        assert len(channel.sent[0]) <= 200
        assert channel.sent_kwargs[0]["suppress_embeds"] is True


def _no_unfurl_possible(text: str, kwargs: dict[str, Any]) -> bool:
    """A sent message can render no auto preview: either the message flag
    is set, or every bare URL in the body is angle-bracketed."""
    if kwargs.get("suppress_embeds") is True:
        return True
    return not re.search(r"(?<![<(\[])https?://", text or "")


class TestEditsKeepSuppression:
    """discord.py drops SUPPRESS_EMBEDS unless an edit re-asserts it, so
    every in-place edit surface (live status, concierge note, steering
    note, tool digest, headline) goes through ``_edit``."""

    def _edit(self, bridge: DiscordBridge, *args: Any, **kwargs: Any) -> Any:
        return asyncio.run(bridge._edit(*args, **kwargs))

    def test_edit_sets_suppress_and_clips(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path, max_message_chars=200)
        channel = client.channels[42]
        msg = asyncio.run(channel.send("old"))
        self._edit(bridge, msg, "see https://example.com/x" + "y" * 500)
        assert msg.suppress is True
        assert msg.content.startswith("see https://example.com/x")
        assert len(msg.content) <= 200

    def test_edit_with_our_embed_masks_urls_instead(self, tmp_path: Path) -> None:
        from sbxloop.daemon.discord_format import EmbedSpec

        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        msg = asyncio.run(channel.send("old"))
        self._edit(bridge, msg, "run https://x/run/1", embed=EmbedSpec(title="t"))
        assert msg.suppress is None
        assert msg.content == "run <https://x/run/1>"

    def test_edit_falls_back_to_masking_without_suppress_kwarg(self, tmp_path: Path) -> None:
        bridge, _client, _ = make_bridge(tmp_path)
        seen: dict[str, Any] = {}

        class OldMessage:
            async def edit(self, *, content: str | None = None, **kwargs: Any) -> None:
                if "suppress" in kwargs:
                    raise TypeError("unexpected keyword 'suppress'")
                seen["content"] = content

        self._edit(bridge, OldMessage(), "run https://x/run/1")
        assert seen["content"] == "run <https://x/run/1>"

    def test_status_message_create_and_edit_are_suppressed(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
            bus = EventBus()
            bridge.run_started(item, "r1", FakeEngine(), bus)  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
            bus.emit("task.start", "r1", task_id="t1", title="First https://x/t/1")
            assert wait_for(lambda: bridge.dstore.discord_thread("r1").status_id is not None)  # type: ignore[union-attr]
            status_id = bridge.dstore.discord_thread("r1").status_id  # type: ignore[union-attr]
            idx = thread.sent.index(thread.messages[status_id].content)
            assert thread.sent_kwargs[idx]["suppress_embeds"] is True
            assert thread.sent_kwargs[idx].get("embed") is None
            bus.emit("task.end", "r1", task_id="t1", state="done")
            assert wait_for(lambda: getattr(thread.messages[status_id], "edits", 0) > 0)
            assert thread.messages[status_id].suppress is True
        finally:
            bridge.close()

    def test_concierge_note_edit_is_suppressed(self, tmp_path: Path) -> None:
        from sbxloop.daemon.discord import _ConciergeTurn

        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        turn = _ConciergeTurn(FakeMessage("hi", channel))
        turn.calls = ["`gh issue list` https://x/i/1"]
        turn.note = asyncio.run(channel.send(turn.render()))
        turn.calls.append("`gh pr list`")
        asyncio.run(bridge._edit_concierge_note(turn))
        assert turn.note.suppress is True
        assert "gh pr list" in turn.note.content

    def test_steer_status_edit_is_suppressed(self, tmp_path: Path) -> None:
        from sbxloop.daemon.discord import _Pending

        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        pending = _Pending("r1", 42, 778)
        pending.status = asyncio.run(channel.send("⏳ queued"))
        asyncio.run(bridge._edit_steer_status(pending, "delivered"))
        assert pending.status.suppress is True

    def test_chronology_chunk_send_is_suppressed(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.start()
        try:
            item = WorkItem(item_id="inbox:a.md", source_key="a.md", title="Do A")
            bus = EventBus()
            bridge.run_started(item, "r1", FakeEngine(), bus)  # type: ignore[arg-type]
            control = client.channels[42]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            thread = client.channels[bridge.dstore.discord_thread("r1").thread_id]  # type: ignore[union-attr]
            assert _no_unfurl_possible(control.sent[0], control.sent_kwargs[0])
            bus.emit("run.deliver", "r1", repo="o/r", pr=3, url="https://x/pull/3")
            assert wait_for(lambda: any("https://x/pull/3" in s for s in thread.sent))
            bridge.run_finished(
                item,
                RunReport("r1", "completed", "1/1 tasks done", pr=(3, "https://x/pull/3")),
            )
            assert wait_for(
                lambda: any(s.startswith("**finished: completed**") for s in thread.sent)
            )
            assert all(
                _no_unfurl_possible(t, k)
                for t, k in zip(thread.sent, thread.sent_kwargs, strict=True)
            )
        finally:
            bridge.close()


class TestEmbedsArePreserved:
    """Regression guard for #519: suppressing Discord's *link previews* must
    not take the bridge's own embed cards with it. With ``[discord] embeds``
    at its default (true) a real embed object still reaches ``target.send``
    for the headline, the finish verdict, the run summary and the live
    status message; with ``embeds = false`` the plain-text twins are used
    and the message is unfurl-suppressed instead."""

    def _run(self, bridge: DiscordBridge, client: Any, item: WorkItem, report: RunReport) -> Any:
        bridge.start()
        try:
            bus = EventBus()
            bridge.run_started(item, "r1", FakeEngine(), bus)  # type: ignore[arg-type]
            assert wait_for(lambda: bridge.dstore.discord_thread("r1") is not None)
            known = bridge.dstore.discord_thread("r1")
            assert known is not None
            thread = client.channels[known.thread_id]
            bus.emit("task.start", "r1", task_id="t1", title="First")
            assert wait_for(lambda: bridge.dstore.discord_thread("r1").status_id is not None)  # type: ignore[union-attr]
            bridge.run_finished(item, report)
            assert wait_for(
                lambda: any(s.startswith("**finished: completed**") for s in thread.sent)
            )
            return thread
        finally:
            bridge.close()

    @staticmethod
    def _item() -> WorkItem:
        return WorkItem(item_id="gh:issue:4", source_key="4", title="Fix login", url="https://x/4")

    @staticmethod
    def _report() -> RunReport:
        return RunReport("r1", "completed", "1/1 tasks done", pr=(3, "https://x/pull/3"))

    def test_embeds_default_to_true(self) -> None:
        assert Config().discord.embeds is True

    def test_format_module_still_exports_the_embed_path(self) -> None:
        from sbxloop.daemon import discord_format as fmt

        assert fmt.EmbedSpec is not None
        assert hasattr(fmt.EmbedSpec(title="t"), "clamped")
        for name in (
            "EMBED_TITLE_MAX",
            "EMBED_DESCRIPTION_MAX",
            "EMBED_FIELD_NAME_MAX",
            "EMBED_FIELD_VALUE_MAX",
            "EMBED_FIELDS_MAX",
            "EMBED_TOTAL_MAX",
            "EMBED_FOOTER_MAX",
            "COLOR_RUNNING",
            "COLOR_OK",
            "COLOR_FAIL",
            "COLOR_WARN",
            "COLOR_DIM",
        ):
            assert isinstance(getattr(fmt, name), int), name
        for renderer in ("headline_embed", "finish_embed", "summary_embed", "status_embed"):
            assert callable(getattr(fmt, renderer)), renderer
        assert "embed" in Chunk.__dataclass_fields__

    def test_headline_finish_summary_still_send_embeds(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        item, report = self._item(), self._report()
        thread = self._run(bridge, client, item, report)
        control = client.channels[42]
        # headline card on the control channel
        assert isinstance(control.sent_kwargs[0].get("embed"), FakeEmbed)
        assert control.sent[0].startswith("▶ run `r1`")
        titles = [
            k["embed"].spec.title
            for k in thread.sent_kwargs
            if isinstance(k.get("embed"), FakeEmbed)
        ]
        assert len(titles) >= 2  # finish verdict + run summary
        blob = "\n".join(
            (k["embed"].spec.as_text() if isinstance(k.get("embed"), FakeEmbed) else "")
            for k in thread.sent_kwargs
        )
        assert "https://x/pull/3" in blob

    def test_live_status_message_stays_text_and_unfurl_free(self, tmp_path: Path) -> None:
        """The live status line has no embed twin and never had one — it is
        plain text, edited in place, and must stay unfurl-suppressed."""
        bridge, client, _ = make_bridge(tmp_path)
        thread = self._run(bridge, client, self._item(), self._report())
        known = bridge.dstore.discord_thread("r1")
        assert known is not None and known.status_id
        status = thread.messages[known.status_id]
        assert status.embed is None
        assert getattr(status, "edit_kwargs", [{}])[-1].get("suppress") is True

    def test_ctl_status_reply_still_carries_an_embed(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        asyncio.run(bridge._command(FakeMessage("!status", channel), "status"))
        assert isinstance(channel.sent_kwargs[-1].get("embed"), FakeEmbed)

    def test_embeds_false_uses_text_twins_and_suppresses_unfurls(self, tmp_path: Path) -> None:
        from sbxloop.daemon.discord_format import finish_text, headline_text, summary_text

        bridge, client, _ = make_bridge(tmp_path, embeds=False)
        item, report = self._item(), self._report()
        thread = self._run(bridge, client, item, report)
        control = client.channels[42]
        assert control.sent[0].startswith(headline_text(item, "r1").split("\n")[0])
        assert control.sent_kwargs[0].get("embed") is None
        assert control.sent_kwargs[0]["suppress_embeds"] is True
        assert not any(isinstance(k.get("embed"), FakeEmbed) for k in thread.sent_kwargs)
        assert all(k.get("suppress_embeds") is True for k in thread.sent_kwargs)
        assert any(
            s.startswith(finish_text("completed", report).split("\n")[0]) for s in thread.sent
        )
        assert any(
            s.startswith(summary_text(None, "completed").split("\n")[0][:20]) for s in thread.sent
        )
        known = bridge.dstore.discord_thread("r1")
        assert known is not None and known.status_id
        status = thread.messages[known.status_id]
        assert status.embed is None
        assert getattr(status, "edit_kwargs", [{}])[-1].get("suppress") is True


# -- clarifying questions with clickable choices (#564) ---------------------------------


class StubButton:
    def __init__(self, *, label: str, style: Any = None, row: int | None = None) -> None:
        self.label = label
        self.style = style
        self.row = row
        self.callback: Any = None
        self.disabled = False


class StubView:
    """Stands in for discord.ui.View so the component path is exercised
    without the optional extra (CI syncs without it)."""

    def __init__(self, *, timeout: float | None = None) -> None:
        self.timeout = timeout
        self.children: list[StubButton] = []

    def add_item(self, item: StubButton) -> None:
        self.children.append(item)


def stub_discord_module() -> Any:
    import types

    mod = types.ModuleType("discord")
    ui = types.ModuleType("discord.ui")
    ui.View = StubView
    ui.Button = StubButton
    mod.ui = ui
    mod.ButtonStyle = type("ButtonStyle", (), {"secondary": "secondary"})
    return mod


@pytest.fixture
def stub_discord(monkeypatch: pytest.MonkeyPatch) -> Any:
    import sys

    mod = stub_discord_module()
    monkeypatch.setitem(sys.modules, "discord", mod)
    monkeypatch.setitem(sys.modules, "discord.ui", mod.ui)
    return mod


class StubResponse:
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[tuple[str, bool]] = []
        self.deferred = 0
        self.fail = fail

    async def send_message(self, content: str, *, ephemeral: bool = False) -> None:
        if self.fail:
            raise RuntimeError("interaction already acknowledged")
        self.messages.append((content, ephemeral))

    async def defer(self) -> None:
        self.deferred += 1


class StubInteraction:
    def __init__(self, name: str = "clicker", *, fail: bool = False) -> None:
        self.user = FakeUser(9, name)
        self.response = StubResponse(fail=fail)


CHOICE_Q = None


def _question() -> Any:
    from sbxloop.daemon.chat_choices import Choice, ChoiceQuestion

    return ChoiceQuestion(
        prompt="What do you want changed?",
        choices=(
            Choice(value="the wording", label="The wording"),
            Choice(value="the layout", label="The layout"),
        ),
    )


class TestChoiceComponents:
    def test_buttons_are_attached_for_an_enumerable_question(
        self, tmp_path: Path, stub_discord: Any
    ) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        question = _question()
        posted = asyncio.run(bridge._send_choices(channel, "I need one more thing.", question))
        assert posted is not None
        view = channel.sent_kwargs[-1]["view"]
        assert [b.label for b in view.children] == ["The wording", "The layout"]
        assert all(b.row == 0 for b in view.children)
        assert view.timeout == bridge._question_ttl_s
        # the prose is still in the body, so typing works either way
        assert "The wording" in channel.sent[-1]
        assert channel.sent_kwargs[-1]["suppress_embeds"] is True
        assert channel.sent_kwargs[-1]["allowed_mentions"] == "none"

    def test_a_click_answers_through_answer_choice_with_no_typing(
        self, tmp_path: Path, stub_discord: Any
    ) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        question = _question()
        posted = asyncio.run(bridge._send_choices(channel, "", question))
        answered: list[tuple[str, str, str | None]] = []
        bridge._answer_choice = lambda mid, value, author=None, **kw: (  # type: ignore[method-assign]
            answered.append((mid, value, author)) or True
        )
        view = channel.sent_kwargs[-1]["view"]
        interaction = StubInteraction("brett")
        asyncio.run(view.children[1].callback(interaction))
        assert answered == [(str(posted.id), "the layout", "Discord user `brett`")]
        assert interaction.response.messages and "the layout" in interaction.response.messages[0][0]
        # the message is edited to record the answer and drop the buttons
        assert posted.edit_kwargs[-1]["view"] is None
        assert "the layout" in posted.edit_kwargs[-1]["content"]

    def test_inbound_carries_the_replied_to_message_id(self, tmp_path: Path) -> None:
        # #570 review: matching a typed answer to its question needs the
        # reference id, not recency.
        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        target = FakeMessage("QUESTION", channel, bot=True, mid=888)
        reply = FakeMessage("1", channel, mid=889, reply_to=target)
        # the gateway often gives only message_id, with nothing resolved
        reply.reference = type("Ref", (), {"resolved": None, "message_id": 888})()
        inbound = bridge._inbound(reply)
        assert inbound is not None and inbound.reply_to_id == "888"
        plain = bridge._inbound(FakeMessage("hi", channel, mid=890))
        assert plain is not None and plain.reply_to_id is None

    def test_a_click_reports_the_clicker_identity(self, tmp_path: Path, stub_discord: Any) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        asyncio.run(bridge._send_choices(channel, "", _question()))
        seen: list[dict[str, Any]] = []
        bridge._answer_choice = lambda mid, value, author=None, **kw: (  # type: ignore[method-assign]
            seen.append({"author": author, **kw}) or True
        )
        view = channel.sent_kwargs[-1]["view"]
        asyncio.run(view.children[0].callback(StubInteraction("dana")))
        assert seen and seen[0]["author_name"] == "dana"
        assert seen[0]["author_id"] is not None

    def test_a_failed_view_send_logs_the_real_traceback(
        self, tmp_path: Path, stub_discord: Any, caplog: Any
    ) -> None:
        # #570 review: the warning ran outside the `except`, so `exc_info=True`
        # logged "NoneType: None" instead of why the send failed.
        import logging

        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        real_send = channel.send

        async def send(text: str | None = None, **kwargs: Any) -> Any:
            if "view" in kwargs:
                raise RuntimeError("components not supported here")
            return await real_send(text, **kwargs)

        channel.send = send  # type: ignore[method-assign]
        with caplog.at_level(logging.WARNING, logger="sbxloop.daemon.discord"):
            asyncio.run(bridge._send_choices(channel, "", _question()))
        record = next(r for r in caplog.records if "choices_view_send_failed" in r.getMessage())
        # structlog carries the caught exception through as the exc_info
        # value; `exc_info=True` here would have rendered "NoneType: None"
        # because sys.exc_info() is already cleared at this point.
        payload = record.msg if isinstance(record.msg, dict) else {}
        carried = payload.get("exc_info", record.exc_info)
        if isinstance(carried, tuple):
            carried = carried[1]
        assert isinstance(carried, RuntimeError)
        assert "components not supported here" in str(carried)

    def test_a_view_rejecting_send_falls_back_to_prose(
        self, tmp_path: Path, stub_discord: Any
    ) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        real_send = channel.send

        async def send(text: str | None = None, **kwargs: Any) -> Any:
            if "view" in kwargs:
                raise RuntimeError("components not supported here")
            return await real_send(text, **kwargs)

        channel.send = send  # type: ignore[method-assign]
        posted = asyncio.run(bridge._send_choices(channel, "", _question()))
        assert posted is not None
        assert "view" not in channel.sent_kwargs[-1]
        assert "1. The wording" in channel.sent[-1]
        assert "answer in your own words" in channel.sent[-1]

    def test_components_unavailable_posts_prose(self, tmp_path: Path) -> None:
        # no stub_discord fixture: discord.py has no ui.View on this host
        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        posted = asyncio.run(bridge._send_choices(channel, "", _question()))
        assert posted is not None
        assert "view" not in channel.sent_kwargs[-1]
        assert "1. The wording" in channel.sent[-1]

    def test_a_click_after_expiry_is_answered_not_raised(
        self, tmp_path: Path, stub_discord: Any
    ) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        asyncio.run(bridge._send_choices(channel, "", _question()))
        view = channel.sent_kwargs[-1]["view"]
        # nothing was ever registered, so the bridge does not know this id
        interaction = StubInteraction()
        asyncio.run(view.children[0].callback(interaction))
        note, ephemeral = interaction.response.messages[0]
        assert ephemeral and "type your answer" in note

    def test_a_failing_acknowledgement_defers_instead_of_raising(
        self, tmp_path: Path, stub_discord: Any
    ) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        asyncio.run(bridge._send_choices(channel, "", _question()))
        view = channel.sent_kwargs[-1]["view"]
        interaction = StubInteraction(fail=True)
        asyncio.run(view.children[0].callback(interaction))
        assert interaction.response.deferred == 1

    def test_timeout_disables_the_buttons_and_says_typing_works(
        self, tmp_path: Path, stub_discord: Any
    ) -> None:
        from sbxloop.daemon.discord import TIMED_OUT_NOTE

        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        posted = asyncio.run(bridge._send_choices(channel, "", _question()))
        view = channel.sent_kwargs[-1]["view"]
        asyncio.run(view.on_timeout())
        assert posted.edit_kwargs[-1]["view"] is None
        assert TIMED_OUT_NOTE in posted.edit_kwargs[-1]["content"]

    def test_answered_question_ignores_a_later_timeout(
        self, tmp_path: Path, stub_discord: Any
    ) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        posted = asyncio.run(bridge._send_choices(channel, "", _question()))
        bridge._answer_choice = lambda mid, value, author=None, **kw: True  # type: ignore[method-assign]
        view = channel.sent_kwargs[-1]["view"]
        asyncio.run(view.children[0].callback(StubInteraction()))
        edits = len(posted.edit_kwargs)
        asyncio.run(view.on_timeout())
        assert len(posted.edit_kwargs) == edits

    def test_a_mentioning_choice_post_survives_a_discord_without_allowed_mentions(
        self, tmp_path: Path, stub_discord: Any
    ) -> None:
        """A discord.py exposing components but no AllowedMentions must not
        take the whole post down: the send goes out with defaults."""
        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        posted = asyncio.run(bridge._send_choices(channel, "", _question(), mention_users=True))
        assert posted is not None
        assert "allowed_mentions" not in channel.sent_kwargs[-1]

    def test_a_click_before_bind_resolves_through_the_pending_key(
        self, tmp_path: Path, stub_discord: Any
    ) -> None:
        from sbxloop.daemon.discord import _build_choice_view

        bridge, _client, _ = make_bridge(tmp_path)
        seen: list[str] = []
        bridge._answer_choice = lambda mid, value, author=None, **kw: (  # type: ignore[method-assign]
            seen.append(mid) or True
        )
        view = _build_choice_view(bridge, _question(), pending_key="pending:abc")
        # bind() has not run: the view only knows the provisional key
        asyncio.run(view.children[0].callback(StubInteraction()))
        assert seen == ["pending:abc"]

    def test_the_pending_key_is_threaded_into_the_view(
        self, tmp_path: Path, stub_discord: Any
    ) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        channel = client.channels[42]
        posted = asyncio.run(
            bridge._send_choices(channel, "", _question(), pending_key="pending:xyz")
        )
        view = channel.sent_kwargs[-1]["view"]
        assert view.handler.pending_key == "pending:xyz"
        # once bound, the real message id wins
        assert view.handler._message_id() == str(bridge._message_id(posted))

    def test_a_click_while_the_send_is_in_flight_is_answered(
        self, tmp_path: Path, stub_discord: Any
    ) -> None:
        """#573: the whole path — the click fires from inside `send`, before
        `_post_choice_question` can learn the posted message id."""
        from sbxloop.daemon.chat_choices import Choice, ChoiceQuestion
        from sbxloop.daemon.concierge import ConciergeReply

        question = ChoiceQuestion(
            prompt="What do you want changed?",
            choices=(Choice(value="the wording", label="The wording"),),
        )
        concierge = FakeConcierge(
            [ConciergeReply("I need one more thing.", question=question), ConciergeReply("done")]
        )
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge)
        bridge.start()
        try:
            channel = client.channels[42]
            real_send = channel.send
            interaction = StubInteraction("dana")

            async def send(text: str | None = None, **kwargs: Any) -> Any:
                view = kwargs.get("view")
                if view is not None:
                    # the click lands before this send has returned, so no
                    # message id exists anywhere yet
                    await view.children[0].callback(interaction)
                return await real_send(text, **kwargs)

            channel.send = send  # type: ignore[method-assign]
            msg = FakeMessage(
                f"<@{BOT_USER.id}> please file a thing", channel, mid=701, mentions=[BOT_USER]
            )
            channel.messages[701] = msg
            bridge._handle_message(msg)
            assert wait_for(lambda: len(concierge.turns) == 2)
            assert concierge.turns[1][0] == "the wording"
            note = interaction.response.messages[0][0]
            assert "Got it" in note
            assert "type your answer" not in note
            # nothing left outstanding, provisional or otherwise
            assert wait_for(lambda: bridge._questions == {})
        finally:
            bridge.close()


# -- the merge-gate approve button (PR: sbx/gate-button) --------------------------------


def make_gate(
    run_id: str = "r77",
    *,
    notify: tuple[str, ...] = ("1",),
    custom: str = "tok77",
    prompt_channel: str | None = None,
    prompt_message: str | None = None,
    state: str = "open",
) -> Any:
    from sbxloop.daemon.store import MergeGate

    return MergeGate(
        run_id=run_id,
        item_id="gh:issue:7",
        repo="o/r",
        pr_number=9,
        pr_url="https://x/pull/9",
        branch=None,
        notify_ids=notify,
        custom_id=custom,
        state=state,
        prompt_channel_id=prompt_channel,
        prompt_message_id=prompt_message,
        created_at=1.0,
        resolved_at=None,
        resolved_by=None,
        detail=None,
    )


class TestGatePrompt:
    """The base-bridge prompt through the Discord transport (prose path —
    CI runs without the discord.py extra, so the view falls back)."""

    def test_prompt_posts_in_thread_with_mention_and_both_commands(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.client = client
        item = WorkItem(item_id="gh:issue:7", source_key="7", title="Seven")
        with bridge._lock:
            bridge._items["r77"] = item
        bridge.dstore.create_merge_gate(
            "r77", "gh:issue:7", "o/r", 9, "https://x/pull/9", None, ["1"], "tok77", 1.0
        )
        asyncio.run(bridge._post_gate_prompt(make_gate()))
        thread = client.channels[421]  # created under the headline in control(42)
        prompt = thread.sent[-1]
        assert "ready to merge" in prompt
        assert "<@1>" in prompt
        assert "!sbx merge gh:issue:7" in prompt
        assert "abandon gh:issue:7" in prompt
        stored = bridge.dstore.gate_prompt("r77", "discord")
        assert stored is not None, "the prompt id is persisted for restarts"
        assert stored[0] == "421" and stored[1]

    def test_resolution_edits_the_prompt_in_place(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.client = client
        control = client.channels[42]
        msg = asyncio.run(control.send("⏸ prompt"))
        bridge.dstore.create_merge_gate(
            "r77", "gh:issue:7", "o/r", 9, "https://x/pull/9", None, ["1"], "tok77", 1.0
        )
        bridge.dstore.set_gate_prompt("r77", "42", str(msg.id), backend="discord")
        gate = bridge.dstore.merge_gate_for("r77")
        assert gate is not None
        asyncio.run(bridge._update_gate_prompt(gate, "merged", "brett", "abc123def456"))
        assert "approved by brett" in msg.content and "merged" in msg.content

    def test_failed_approval_pings_a_fresh_line_and_keeps_the_prompt(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        bridge.client = client
        control = client.channels[42]
        msg = asyncio.run(control.send("⏸ prompt"))
        thread = FakeChannel(client, 421, name="run thread")
        client.channels[421] = thread
        bridge.dstore.record_chat_thread("r77", "42", "421", None, backend="discord")
        bridge.dstore.create_merge_gate(
            "r77", "gh:issue:7", "o/r", 9, "https://x/pull/9", None, ["1"], "tok77", 1.0
        )
        bridge.dstore.set_gate_prompt("r77", "42", str(msg.id), backend="discord")
        gate = bridge.dstore.merge_gate_for("r77")
        assert gate is not None
        asyncio.run(bridge._update_gate_prompt(gate, "failed", "brett", "CI went red"))
        assert msg.content == "⏸ prompt", "the prompt is left standing"
        line = thread.sent[-1]
        assert "failed" in line and "CI went red" in line and "<@1>" in line


class TestGateButtonHandler:
    """The transport-free click half — directly testable without discord.py."""

    def _interaction(self, name: str = "alice") -> Any:
        class Resp:
            def __init__(self) -> None:
                self.sent: list[tuple[str, bool]] = []

            async def send_message(self, note: str, ephemeral: bool = False) -> None:
                self.sent.append((note, ephemeral))

        class Interaction:
            user = FakeUser(5, name)
            response = Resp()

        return Interaction()

    def test_click_approves_with_attribution(self, tmp_path: Path) -> None:
        from sbxloop.daemon.discord import _GateHandler

        bridge, _, floop = make_bridge(tmp_path)
        calls: list[tuple[str, str | None]] = []

        def approve_merge(target: str, by: str | None = None) -> str:
            calls.append((target, by))
            return "✅ approved — completing the landing"

        floop.approve_merge = approve_merge  # type: ignore[attr-defined]
        interaction = self._interaction()
        asyncio.run(_GateHandler(bridge, make_gate()).on_click(interaction))
        assert calls == [("r77", "Discord user `alice`")]
        ((note, ephemeral),) = interaction.response.sent
        assert "approved" in note and ephemeral

    def test_a_refusal_answers_ephemerally(self, tmp_path: Path) -> None:
        from sbxloop.daemon.discord import _GateHandler

        bridge, _, floop = make_bridge(tmp_path)

        def approve_merge(target: str, by: str | None = None) -> str:
            raise ValueError("no merge gate for 'r77'")

        floop.approve_merge = approve_merge  # type: ignore[attr-defined]
        interaction = self._interaction()
        asyncio.run(_GateHandler(bridge, make_gate()).on_click(interaction))
        ((note, _),) = interaction.response.sent
        assert "merge failed" in note and "no merge gate" in note


class TestGateViewRearm:
    def test_ready_rearms_open_and_approving_gates_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.daemon import discord as bridge_module

        bridge, client, _ = make_bridge(tmp_path)
        built: list[str] = []
        monkeypatch.setattr(
            bridge_module,
            "_build_gate_view",
            lambda b, g: (built.append(g.run_id), object())[1],
        )
        armed: list[Any] = []
        client.add_view = lambda view, message_id=None: armed.append(message_id)  # type: ignore[attr-defined]
        bridge.dstore.create_merge_gate("r1", "gh:issue:1", "o/r", 9, "u", None, [], "t1", 1.0)
        bridge.dstore.set_gate_prompt("r1", "42", "555", backend="discord")
        bridge.dstore.create_merge_gate("r2", "gh:issue:2", "o/r", 9, "u", None, [], "t2", 1.0)
        assert bridge.dstore.claim_merge_gate("r2")  # approving: armed anyway
        bridge._register_gate_views(client)
        assert sorted(built) == ["r1", "r2"]
        assert sorted(armed, key=str) == [555, None] or sorted(armed, key=repr) == [None, 555]
        # A reconnect must not double-register the same custom_ids.
        bridge._register_gate_views(client)
        assert len(built) == 2

    def test_a_resolved_gate_is_not_armed(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        armed: list[Any] = []
        client.add_view = lambda view, message_id=None: armed.append(message_id)  # type: ignore[attr-defined]
        bridge.dstore.create_merge_gate("r1", "gh:issue:1", "o/r", 9, "u", None, [], "t1", 1.0)
        bridge.dstore.resolve_merge_gate("r1", "merged", "b", 2.0)
        bridge._register_gate_views(client)
        assert armed == []

    def test_no_component_support_arms_nothing_and_never_raises(self, tmp_path: Path) -> None:
        """CI has no discord.py: _build_gate_view answers None and re-arming
        walks away quietly — the prose prompt still works by typing."""
        bridge, client, _ = make_bridge(tmp_path)
        armed: list[Any] = []
        client.add_view = lambda view, message_id=None: armed.append(message_id)  # type: ignore[attr-defined]
        bridge.dstore.create_merge_gate("r1", "gh:issue:1", "o/r", 9, "u", None, [], "t1", 1.0)
        bridge._register_gate_views(client)
        assert armed == []
