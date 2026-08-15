"""DiscordBridge: the daemon's human channel — chronology out, steering in.

A gateway bot posts each run's chronology (agent messages with persona
attribution, tool lines, issue/PR links, verdicts) into a thread under a
headline in one control channel, and relays messages typed in that thread
to the running agent as steering — the same ``post_user_message`` /
``chat.reply`` contract the CLI's ``--chat`` uses (engine.py), so no
engine changes are needed. Daemon-level events (queueing, breaker, cap,
recovery) go to the control channel itself.

Two rules make this safe to bolt onto the engine:

* The bus subscriber is a non-blocking ``queue.put`` — subscribers run
  inline on the engine thread under the bus lock, so all Discord I/O
  happens on the bridge's own thread and asyncio loop, never in the
  subscriber.
* Discord is observability, not a dependency of work: every send is
  guarded, an outage logs and retries, and the daemon keeps running.

``discord.py`` is an optional extra (``sbxloop[discord]``); the import is
deferred and its absence surfaces as an actionable error, the same pattern
as the ``[copilot]`` extra behind ``sbxloop list-models``. The bot token
comes from ``DISCORD_BOT_TOKEN``. Anyone who can post in the channel can
steer — that is the operator's boundary to set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
from collections.abc import Callable
from typing import Any

from sbxloop.cli.tui import _LIFECYCLE_PREFIXES, _TRANSCRIPT_SKIP
from sbxloop.config import Config
from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.engine import LoopEngine
from sbxloop.errors import DaemonError
from sbxloop.events import Event, EventBus, HostEventTypes

logger = logging.getLogger(__name__)

TOKEN_ENV = "DISCORD_BOT_TOKEN"  # nosec B105 - env var name, not a secret
INSTALL_HINT = (
    "discord.py is not installed on this host — install it with "
    "`pip install 'sbxloop[discord]'` to enable the daemon's Discord bridge"
)
# Consecutive tool lines are coalesced into one message to stay well under
# Discord's rate limits on a chatty agent.
COALESCE_MAX_LINES = 10
COALESCE_WINDOW_S = 2.0


# -- formatting (pure) ---------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _one_line(text: str, limit: int = 160) -> str:
    return _clip(" ".join(str(text).split()), limit)


def format_for_discord(event: Event, *, level: str = "normal", max_chars: int = 1900) -> str | None:
    """The Discord line(s) for one run event, or None to drop it.

    Mirrors ``render_event`` (cli/tui.py) with Discord Markdown: agent
    messages carry persona/model attribution, URL-bearing lifecycle events
    become link lines, deltas/heartbeats/usage/resources are dropped.
    """
    if event.type in _TRANSCRIPT_SKIP:
        return None
    data = event.data
    t = event.type
    if t == "agent.message":
        content = str(data.get("content") or "").strip()
        if not content:
            return None
        who = str(data.get("agent") or "agent")
        model = data.get("model")
        header = f"**{who}**" + (f" · `{model}`" if model else "")
        return f"{header}\n{_clip(content, max_chars - len(header) - 1)}"
    if t == "agent.tool_start":
        if level == "quiet":
            return None
        return f"⚙ `{data.get('tool')}` {_one_line(data.get('args') or '')}".rstrip()
    if t == "agent.tool_end":
        if data.get("success") is False:
            tail = str(data.get("error") or data.get("output") or "").strip().splitlines()[-3:]
            body = "\n".join(_one_line(line) for line in tail)
            return f"✗ `{data.get('tool')}` failed" + (f"\n```\n{body}\n```" if body else "")
        return None
    if t == HostEventTypes.RUN_REPORT:
        return f"📋 tracking issue #{data.get('issue')} {data.get('url', '')}".rstrip()
    if t == HostEventTypes.RUN_DELIVER:
        if data.get("created"):
            return f"📦 created repository {data.get('repo')}"
        if data.get("error"):
            return f"⚠ delivery failed: {_one_line(data['error'], 300)}"
        if data.get("url"):
            return f"🔀 PR #{data.get('pr')} {data['url']}"
        return None
    if t == "sandbox.workspace_clone":
        return f"🌿 working on branch `{data.get('branch')}` (clone of `{data.get('source')}`)"
    if t == HostEventTypes.CHAT_MESSAGE:
        return (
            "💬 received — answering at the next checkpoint "
            "(may take a few minutes during a long step)"
        )
    if t == HostEventTypes.CHAT_ACTION:
        return f"↪ applied `{data.get('action')}`: {_one_line(data.get('guidance') or '', 300)}"
    if t == HostEventTypes.CHAT_REPLY:
        if data.get("error"):
            return f"⚠ steering failed: {_one_line(data['error'], 300)}"
        return f"🧭 **steering:** {_clip(str(data.get('reply') or ''), max_chars - 20)}"
    if t == "phase.end" and data.get("message"):
        return f"· {_one_line(data['message'], 300)}"
    if t == "task.state":
        if level == "verbose":
            return f"· task {data.get('task_id')} → {data.get('state')}"
        return None
    if t in ("task.end", "run.end", "run.state"):
        bits = [t]
        for key in ("task_id", "title", "state"):
            if data.get(key):
                bits.append(str(data[key]))
        return "· " + " ".join(bits)
    if t.startswith(_LIFECYCLE_PREFIXES):
        if level != "verbose":
            return None
        return f"· {t} {_one_line(' '.join(f'{k}={v}' for k, v in data.items()), 200)}"
    return None


def headline_text(item: WorkItem, run_id: str, state: str | None = None) -> str:
    origin = (
        f"issue #{item.source_key}" if item.source == "github" else f"inbox `{item.source_key}`"
    )
    if item.url:
        origin += f" ({item.url})"
    marker = {"completed": "✅", "failed": "❌", "delivery_failed": "⚠", None: "▶"}.get(state, "▶")
    return f"{marker} run `{run_id}` — **{_one_line(item.title, 120)}** · {origin}"


# -- bridge ---------------------------------------------------------------------------


class _Pending:
    """A steering message awaiting its chat.reply."""

    def __init__(self, thread_id: int, discord_message_id: int) -> None:
        self.thread_id = thread_id
        self.discord_message_id = discord_message_id


class DiscordBridge:
    """Runs a discord.py client on its own thread; the daemon loop calls
    the ``Frontend`` methods from its threads and never blocks on Discord.

    ``client_factory`` builds the client (tests inject a recorder); the
    default imports discord.py lazily.
    """

    def __init__(
        self,
        config: Config,
        dstore: DaemonStore,
        *,
        loop_ref: Any = None,
        client_factory: Callable[[DiscordBridge], Any] | None = None,
        token: str | None = None,
    ) -> None:
        self.config = config
        self.discord = config.discord
        self.dstore = dstore
        self.loop_ref = loop_ref  # DaemonLoop, for !sbx commands + steering
        self.token = token if token is not None else os.environ.get(TOKEN_ENV, "")
        self._client_factory = client_factory or self._default_client
        self.client: Any = None
        self._aloop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._loop_up = threading.Event()
        self._stop_evt: asyncio.Event | None = None
        # Channel access failures are reported once, not on every flush.
        self._channel_error_logged = False
        # run_id -> per-run state; only the in-flight run has an unsubscribe
        # (run_id, payload): payload is an Event, None (ensure-thread sentinel),
        # a daemon-event string, or a ("__finished__", ...) tuple.
        self._events: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        self._unsubscribe: Callable[[], None] | None = None
        self._active_run: str | None = None
        self._active_item: WorkItem | None = None
        self._pending: dict[str, _Pending] = {}  # message_id -> pending steer
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------------------

    def start(self, *, connect_wait_s: float = 15.0) -> None:
        if not self.token:
            raise DaemonError(
                f"[discord] is configured but {TOKEN_ENV} is not set; export it (or put it "
                "in the project .env) — never in sbxloop.toml"
            )
        self.client = self._client_factory(self)
        self._thread = threading.Thread(
            target=self._thread_main, name="sbxloop-discord", daemon=True
        )
        self._thread.start()
        # The asyncio loop exists only once the thread runs; callers may
        # schedule work immediately after start(), so wait for it.
        self._loop_up.wait(timeout=10)
        # Give the gateway a moment to connect so a short-lived process
        # (--once) can still post; a slow or failed connect never blocks
        # the daemon — the pump waits for readiness on its own.
        self._ready.wait(timeout=connect_wait_s)

    def close(self) -> None:
        """Ask the bridge thread to shut down and wait for it. The client is
        closed *inside* its own loop (``_amain``) so aiohttp's connector
        teardown completes before the loop is torn down."""
        if self._aloop is not None and not self._aloop.is_closed():
            self._aloop.call_soon_threadsafe(self._stop_event_set)
        if self._thread is not None:
            self._thread.join(timeout=20)

    def _stop_event_set(self) -> None:
        if self._stop_evt is not None:
            self._stop_evt.set()

    def _thread_main(self) -> None:
        self._aloop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._aloop)
        self._stop_evt = asyncio.Event()
        self._loop_up.set()
        try:
            self._aloop.run_until_complete(self._amain())
        except Exception:
            logger.warning("discord bridge stopped", exc_info=True)
        finally:
            # Let cancelled tasks (aiohttp connector, gateway) finish
            # unwinding before the loop goes away — otherwise asyncio logs
            # "Task was destroyed but it is pending" for each of them.
            pending = [t for t in asyncio.all_tasks(self._aloop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                self._aloop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._aloop.run_until_complete(self._aloop.shutdown_asyncgens())
            self._aloop.close()

    async def _amain(self) -> None:
        assert self._stop_evt is not None
        pump = asyncio.ensure_future(self._pump())
        gateway = asyncio.ensure_future(self.client.start(self.token))
        stopper = asyncio.ensure_future(self._stop_evt.wait())
        try:
            done, _ = await asyncio.wait({gateway, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if gateway in done and (exc := gateway.exception()) is not None:
                # Login/gateway failure: the daemon keeps running without
                # Discord; say why once, at warning level, without a dump.
                logger.warning("discord bridge could not connect: %s", exc)
        finally:
            pump.cancel()
            stopper.cancel()
            try:
                await self.client.close()
            except Exception:
                logger.debug("discord client close failed", exc_info=True)
            if not gateway.done():
                gateway.cancel()
            await asyncio.gather(pump, stopper, gateway, return_exceptions=True)

    # -- Frontend protocol (called from daemon threads) --------------------------

    def daemon_event(self, text: str) -> None:
        self._enqueue("__daemon__", text)

    def run_started(self, item: WorkItem, run_id: str, engine: LoopEngine, bus: EventBus) -> None:
        with self._lock:
            self._active_run = run_id
            self._active_item = item
            self._engine = engine
            # Non-blocking subscriber: just enqueue; the pump renders + sends.
            self._unsubscribe = bus.subscribe(lambda ev: self._events.put((run_id, ev)))
        self._enqueue(run_id, None)  # sentinel: ensure thread + headline

    def run_finished(self, item: WorkItem, report: RunReport) -> None:
        with self._lock:
            unsubscribe, self._unsubscribe = self._unsubscribe, None
            self._active_run = None
            self._active_item = None
            unanswered = list(self._pending.values())
            self._pending.clear()
        if unsubscribe is not None:
            unsubscribe()
        state = (
            "delivery_failed"
            if report.delivery_error and report.state == "completed"
            else report.state
        )
        self._enqueue(report.run_id, ("__finished__", item, state, report, unanswered))

    def _enqueue(self, run_id: str, payload: Any) -> None:
        self._events.put((run_id, payload))

    # -- steering (called from the discord thread) --------------------------------

    def _handle_message(self, message: Any) -> None:
        """Route an inbound Discord message: command, steering, or ignore."""
        if getattr(getattr(message, "author", None), "bot", False):
            return
        text = str(getattr(message, "content", "") or "").strip()
        channel = getattr(message, "channel", None)
        channel_id = getattr(channel, "id", None)
        if channel_id == self.discord.channel_id and text.startswith(self.discord.command_prefix):
            self._schedule(self._command(message, text[len(self.discord.command_prefix) :].strip()))
            return
        if channel_id is None:
            return
        thread_id = int(channel_id)
        run_id = self.dstore.run_for_thread(thread_id)
        if run_id is None:
            return
        with self._lock:
            live = run_id == self._active_run
            engine = getattr(self, "_engine", None)
        if not live or engine is None:
            self._schedule(
                self._send(channel, f"run `{run_id}` has finished; steering is no longer possible.")
            )
            return
        mid = engine.post_user_message(text)
        with self._lock:
            self._pending[mid] = _Pending(thread_id, int(getattr(message, "id", 0)))
        self._schedule(self._react(message, "⏳"))

    async def _command(self, message: Any, cmd: str) -> None:
        loop = self.loop_ref
        channel = message.channel
        if loop is None:
            await self._send(channel, "daemon loop not attached")
            return
        word = (cmd.split() or [""])[0].lower()
        if word == "status":
            s = loop.status()
            cur = s["current"]
            lines = [
                f"**current:** {cur['run_id']} — {cur['title']}" if cur else "**current:** idle",
                f"**queued:** {s['queued']} · **runs today:** "
                f"{s['runs_today']}/{s['max_runs_per_day']}",
                f"**breaker:** {'open' if s['breaker_open'] else 'closed'} · "
                f"**paused:** {s['paused']}",
            ]
            await self._send(channel, "\n".join(lines))
        elif word == "pause":
            loop.pause()
            await self._send(channel, "paused — the current run finishes; nothing new is claimed.")
        elif word in ("resume", "unpause"):
            loop.unpause()
            await self._send(channel, "resumed.")
        elif word == "cancel":
            ok = loop.cancel_current()
            await self._send(
                channel,
                "cancel requested — honored at the next task boundary; the run stays resumable."
                if ok
                else "nothing is running.",
            )
        elif word == "queue":
            items = loop.dstore.queued()
            if not items:
                await self._send(channel, "queue is empty.")
            else:
                await self._send(
                    channel,
                    "\n".join(
                        f"• `{i.item_id}` {_one_line(i.title, 80)}" + (f" {i.url}" if i.url else "")
                        for i in items[:15]
                    ),
                )
        else:
            await self._send(
                channel,
                f"commands: `{self.discord.command_prefix} status|pause|resume|cancel|queue` — "
                "or type in a run's thread to steer that run.",
            )

    # -- pump: queue -> discord (discord thread) -------------------------------------

    async def _pump(self) -> None:
        await self._wait_ready()
        buffer: list[str] = []
        buffer_run: str | None = None
        last_flush = asyncio.get_event_loop().time()
        while True:
            try:
                run_id, payload = await asyncio.get_event_loop().run_in_executor(
                    None, self._events.get, True, COALESCE_WINDOW_S
                )
            except queue.Empty:
                if buffer and buffer_run:
                    await self._flush(buffer_run, buffer)
                    buffer = []
                continue
            try:
                if run_id == "__daemon__":
                    await self._send_channel(str(payload))
                    continue
                if payload is None:
                    await self._ensure_thread(run_id)
                    continue
                if isinstance(payload, tuple) and payload and payload[0] == "__finished__":
                    if buffer and buffer_run:
                        await self._flush(buffer_run, buffer)
                        buffer = []
                    await self._finish(payload)
                    continue
                event: Event = payload
                text = format_for_discord(
                    event,
                    level=self.discord.chronology_level,
                    max_chars=self.discord.max_message_chars,
                )
                if event.type == HostEventTypes.CHAT_REPLY:
                    await self._resolve_reply(event)
                if text is None:
                    continue
                if buffer_run not in (None, run_id):
                    await self._flush(buffer_run, buffer)
                    buffer = []
                buffer_run = run_id
                buffer.append(text)
                now = asyncio.get_event_loop().time()
                if (
                    len(buffer) >= COALESCE_MAX_LINES
                    or now - last_flush >= COALESCE_WINDOW_S
                    or event.type
                    in ("agent.message", HostEventTypes.CHAT_REPLY, HostEventTypes.RUN_DELIVER)
                ):
                    await self._flush(run_id, buffer)
                    buffer = []
                    last_flush = now
            except Exception:
                logger.warning("discord pump: send failed", exc_info=True)

    async def _flush(self, run_id: str, lines: list[str]) -> None:
        thread = await self._ensure_thread(run_id)
        if thread is None:
            return
        chunk: list[str] = []
        size = 0
        for line in lines:
            if size + len(line) + 1 > self.discord.max_message_chars and chunk:
                await self._send(thread, "\n".join(chunk))
                chunk, size = [], 0
            chunk.append(line)
            size += len(line) + 1
        if chunk:
            await self._send(thread, "\n".join(chunk))

    async def _finish(self, payload: tuple[Any, ...]) -> None:
        _, item, state, report, unanswered = payload
        run_id = report.run_id
        thread = await self._ensure_thread(run_id)
        lines = [f"**finished: {state}** — {report.task_summary}"]
        if report.tracking_issue:
            lines.append(
                f"📋 tracking issue #{report.tracking_issue[0]} {report.tracking_issue[1]}"
            )
        if report.delivery:
            lines.append(f"🔀 PR #{report.delivery[0]} {report.delivery[1]}")
        if report.delivery_error:
            lines.append(f"⚠ delivery failed: {_one_line(report.delivery_error, 300)}")
        if unanswered:
            lines.append(
                f"⚠ {len(unanswered)} steering message(s) were not answered before the run ended"
            )
        if thread is not None:
            await self._send(thread, "\n".join(lines))
        await self._edit_headline(run_id, headline_text(item, run_id, state))

    async def _resolve_reply(self, event: Event) -> None:
        mid = str(event.data.get("message_id") or "")
        with self._lock:
            pending = self._pending.pop(mid, None)
        if pending is None:
            return
        try:
            channel = self.client.get_channel(pending.thread_id) or await self.client.fetch_channel(
                pending.thread_id
            )
            msg = await channel.fetch_message(pending.discord_message_id)
            await msg.add_reaction("✅")
        except Exception:
            logger.debug("discord: could not react to steering message", exc_info=True)

    # -- discord primitives -----------------------------------------------------------

    async def _wait_ready(self) -> None:
        wait = getattr(self.client, "wait_until_ready", None)
        if wait is not None:
            await wait()
        self._ready.set()

    def _schedule(self, coro: Any) -> None:
        if self._aloop is not None:
            asyncio.run_coroutine_threadsafe(coro, self._aloop)
        else:
            coro.close()
            logger.debug("discord: dropped work scheduled before the loop was up")

    async def _channel(self) -> Any:
        """The control channel, or None when the bot cannot reach it.

        Discord's 404 (wrong id) and 403 (bot not invited / no View Channel)
        are configuration problems, not transient faults: report the cause
        once with the fix, then stay quiet — the daemon keeps working and
        the transcript is not buried under a stack trace per flush.
        """
        cid = self.discord.channel_id
        channel = self.client.get_channel(cid)
        if channel is not None:
            return channel
        try:
            return await self.client.fetch_channel(cid)
        except Exception as exc:
            if not self._channel_error_logged:
                self._channel_error_logged = True
                logger.warning(
                    "discord: cannot access channel %s (%s) — check [discord] channel_id "
                    "(right-click the channel → Copy Channel ID) and that the bot is "
                    "invited to the server with View Channel on it; chronology is off "
                    "until the daemon restarts",
                    cid,
                    exc,
                )
            return None

    async def _send(self, target: Any, text: str) -> Any:
        try:
            return await target.send(_clip(text, self.discord.max_message_chars))
        except Exception:
            logger.warning("discord: send failed", exc_info=True)
            return None

    async def _send_channel(self, text: str) -> None:
        channel = await self._channel()
        if channel is not None:
            await self._send(channel, text)

    async def _react(self, message: Any, emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except Exception:
            logger.debug("discord: react failed", exc_info=True)

    async def _ensure_thread(self, run_id: str) -> Any:
        """The run's thread, creating headline + thread on first sight;
        re-attaches to a persisted thread after a daemon restart."""
        known = self.dstore.discord_thread(run_id)
        if known is not None:
            _, thread_id, _ = known
            try:
                return self.client.get_channel(thread_id) or await self.client.fetch_channel(
                    thread_id
                )
            except Exception:
                logger.warning("discord: lost thread %s for %s", thread_id, run_id, exc_info=True)
                return None
        with self._lock:
            item = self._active_item if self._active_run == run_id else None
        if item is None:
            return None
        try:
            channel = await self._channel()
            if channel is None:
                return None
            headline = await channel.send(headline_text(item, run_id))
            if self.discord.thread_per_run:
                thread = await headline.create_thread(name=_clip(f"{run_id} · {item.title}", 90))
            else:
                thread = channel
            self.dstore.record_discord_thread(
                run_id, int(self.discord.channel_id or 0), int(thread.id), int(headline.id)
            )
            return thread
        except Exception:
            logger.warning("discord: could not create thread for %s", run_id, exc_info=True)
            return None

    async def _edit_headline(self, run_id: str, text: str) -> None:
        known = self.dstore.discord_thread(run_id)
        if known is None or known[2] is None:
            return
        try:
            channel = await self._channel()
            if channel is None:
                return
            msg = await channel.fetch_message(known[2])
            await msg.edit(content=text)
        except Exception:
            logger.debug("discord: headline edit failed", exc_info=True)

    # -- default client -----------------------------------------------------------------

    @staticmethod
    def _default_client(bridge: DiscordBridge) -> Any:
        try:
            import discord as discordpy
        except ImportError as exc:
            raise DaemonError(INSTALL_HINT) from exc
        intents = discordpy.Intents.default()
        intents.message_content = True
        client: Any = discordpy.Client(intents=intents)

        async def on_ready() -> None:
            logger.info("discord bridge connected as %s", client.user)

        async def on_message(message: Any) -> None:
            bridge._handle_message(message)

        # discord.py's `@client.event` registers by function name; calling it
        # directly is the same registration without an untyped decorator.
        client.event(on_ready)
        client.event(on_message)
        return client
