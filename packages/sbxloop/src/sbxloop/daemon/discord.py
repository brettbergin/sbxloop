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

from sbxloop.config import Config
from sbxloop.daemon.discord_format import (
    DISCORD_MAX_MESSAGE,
    Chunk,
    EmbedSpec,
    StatusLine,
    ToolBatcher,
    _clip,
    _one_line,
    code,
    daemon_notice,
    finish_embed,
    finish_text,
    format_for_discord,
    headline_embed,
    headline_text,
    queue_lines,
    status_embed,
)
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
# Consecutive lines are coalesced into one message to stay well under
# Discord's rate limits on a chatty agent; the status message is edited at
# most once per STATUS_EDIT_MIN_S.
COALESCE_MAX_LINES = 10
COALESCE_WINDOW_S = 2.0
STATUS_EDIT_MIN_S = 2.0

# Re-exported for callers/tests that import the formatting names from here.
__all__ = [
    "DISCORD_MAX_MESSAGE",
    "DiscordBridge",
    "_clip",
    "format_for_discord",
    "headline_text",
]

_TOOL_EVENTS = ("agent.tool_start", "agent.tool_end")
_STATUS_EVENTS = ("task.start", "task.state", "task.end", "phase.end", "run.end", "run.state")


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
        self._gateway_ready = threading.Event()
        self._degraded = False
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
        # Per-run rendering state, owned by the pump (discord thread only).
        self._batchers: dict[str, ToolBatcher] = {}
        self._status: dict[str, StatusLine] = {}
        self._status_msg: dict[str, Any] = {}  # run_id -> discord message (cached)
        self._status_task: dict[str, asyncio.Task[None]] = {}
        self._status_last_edit: dict[str, float] = {}
        # Facts that enrich the headline card as the run reveals them.
        self._facts: dict[str, dict[str, Any]] = {}
        # item_id -> run_id for the last run of an item (notice -> thread pointer).
        self._item_runs: dict[str, str] = {}

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
            if gateway in done and not self._stop_evt.is_set():
                # Login/gateway failure (bad token, network): the daemon
                # keeps running without Discord. Say why once, then stay
                # alive in DRAINING mode until close() — the loop keeps
                # enqueuing events via the bus subscription, and something
                # must consume them or the queue grows for the rest of the
                # process (review). The pump drops events while degraded.
                exc = gateway.exception()
                logger.warning(
                    "discord bridge could not connect (%s); chronology is off until the "
                    "daemon restarts",
                    exc if exc is not None else "gateway exited",
                )
                self._degraded = True
                self._gateway_ready.set()  # unblock the pump so it drains
                await stopper
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
            self._item_runs[item.item_id] = run_id
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
        if channel_id == self.discord.channel_id:
            if text.startswith(self.discord.command_prefix):
                self._schedule(
                    self._command(message, text[len(self.discord.command_prefix) :].strip())
                )
            elif text:
                # A plain message in the control channel is almost always
                # someone trying to steer (field: "hello there" in the
                # channel while a run was live). Point them at the thread.
                self._schedule(self._hint_where_to_steer(message))
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
                self._send(
                    channel, f"run {code(run_id)} has finished; steering is no longer possible."
                )
            )
            return
        mid = engine.post_user_message(text)
        with self._lock:
            self._pending[mid] = _Pending(thread_id, int(getattr(message, "id", 0)))
        self._schedule(self._react(message, "⏳"))

    async def _hint_where_to_steer(self, message: Any) -> None:
        with self._lock:
            run_id = self._active_run
            item = self._active_item
        if run_id is not None and item is not None:
            known = self.dstore.discord_thread(run_id)
            where = f"<#{known.thread_id}>" if known else f"the thread for {code(run_id)}"
            text = (
                f"To steer the running agent, type inside its thread: {where} "
                f"({code(run_id)} — {_one_line(item.title, 80)}). Daemon commands start with "
                f"`{self.discord.command_prefix}` (try `{self.discord.command_prefix} status`)."
            )
        else:
            text = (
                "Nothing is running right now. Steering happens inside a run's thread once "
                f"one starts; daemon commands start with `{self.discord.command_prefix}` "
                f"(try `{self.discord.command_prefix} status`)."
            )
        await self._send(message.channel, text)

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
            await self._send(channel, "\n".join(lines), embed=status_embed(s))
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
            await self._send(channel, queue_lines(loop.dstore.queued()), suppress_embeds=True)
        else:
            await self._send(
                channel,
                f"commands: `{self.discord.command_prefix} status|pause|resume|cancel|queue` — "
                "or type in a run's thread to steer that run.",
            )

    # -- pump: queue -> discord (discord thread) -------------------------------------

    async def _pump(self) -> None:
        await self._wait_ready()
        if self._degraded:
            # Consume forever so the queue stays bounded; nothing to send to.
            while True:
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, self._events.get, True, COALESCE_WINDOW_S
                    )
                except queue.Empty:
                    continue
        buffer: list[Chunk] = []
        buffer_run: str | None = None
        last_flush = asyncio.get_event_loop().time()
        while True:
            try:
                run_id, payload = await asyncio.get_event_loop().run_in_executor(
                    None, self._events.get, True, COALESCE_WINDOW_S
                )
            except queue.Empty:
                if buffer_run:
                    buffer = await self._flush_all(buffer_run, buffer)
                continue
            try:
                if run_id == "__daemon__":
                    await self._daemon_notice(str(payload))
                    continue
                if payload is None:
                    await self._ensure_thread(run_id)
                    continue
                if isinstance(payload, tuple) and payload and payload[0] == "__finished__":
                    if buffer_run:
                        buffer = await self._flush_all(buffer_run, buffer)
                    await self._finish(payload)
                    continue
                event: Event = payload
                if event.type == HostEventTypes.CHAT_REPLY:
                    await self._resolve_reply(event)
                if buffer_run not in (None, run_id) and buffer_run:
                    buffer = await self._flush_all(buffer_run, buffer)
                buffer_run = run_id
                chunks = self._render(run_id, event)
                self._observe_facts(run_id, event)
                if not chunks:
                    continue
                buffer.extend(chunks)
                now = asyncio.get_event_loop().time()
                if (
                    any(c.flush for c in chunks)
                    or sum(1 for c in buffer if c.kind == "line") >= COALESCE_MAX_LINES
                    or now - last_flush >= COALESCE_WINDOW_S
                ):
                    buffer = await self._flush_all(run_id, buffer)
                    last_flush = now
            except Exception:
                logger.warning("discord pump: send failed", exc_info=True)

    def _render(self, run_id: str, event: Event) -> list[Chunk]:
        """Chunks for one event, routing tool events through the run's
        batcher and lifecycle events through its status line."""
        level = self.discord.chronology_level
        batcher = self._batchers.get(run_id)
        if batcher is None:
            batcher = self._batchers[run_id] = ToolBatcher(
                max_lines=self.discord.tool_batch_lines, quiet=(level == "quiet")
            )
        if self.discord.status_line and event.type in _STATUS_EVENTS:
            status = self._status.setdefault(run_id, StatusLine())
            status.observe(event)
            if status.dirty:
                self._schedule_status_edit(run_id)
        d = event.data
        if event.type == "agent.tool_start":
            batcher.add_start(
                str(d.get("tool") or "?"), str(d.get("args") or ""), d.get("tool_call_id")
            )
            if batcher.full:
                batch = batcher.flush()
                return [batch] if batch else []
            return []
        if event.type == "agent.tool_end":
            detail = batcher.add_end(
                str(d.get("tool") or "?"),
                d.get("tool_call_id"),
                success=d.get("success"),
                exit_code=d.get("exit_code"),
                detail=str(d.get("error") or d.get("output") or ""),
            )
            if detail is None:
                return []
            batch = batcher.flush()
            return [batch, detail] if batch else [detail]
        chunks = format_for_discord(event, level=level, max_chars=self.discord.max_message_chars)
        if not chunks:
            return []
        # A non-tool line closes the current tool batch first so the
        # chronology stays in order.
        batch = batcher.flush()
        return [batch, *chunks] if batch else chunks

    def _observe_facts(self, run_id: str, event: Event) -> None:
        """Remember what enriches the headline card; edit it when something new lands."""
        d = event.data
        facts = self._facts.setdefault(run_id, {})
        changed = False
        if event.type == "sandbox.workspace_clone" and d.get("branch"):
            facts["branch"] = str(d["branch"])
            changed = True
        elif event.type == HostEventTypes.RUN_REPORT and d.get("issue"):
            facts["tracking"] = (int(d["issue"]), str(d.get("url") or ""))
            changed = True
        elif event.type == HostEventTypes.RUN_DELIVER and d.get("pr") and d.get("url"):
            facts["pr"] = (int(d["pr"]), str(d["url"]))
            changed = True
        if changed:
            self._schedule(self._refresh_headline(run_id))

    async def _flush_all(self, run_id: str, buffer: list[Chunk]) -> list[Chunk]:
        """Send the tool batch (if any) then everything buffered; returns the
        new (empty) buffer."""
        batcher = self._batchers.get(run_id)
        pending = batcher.flush() if batcher else None
        chunks = ([pending] if pending else []) + buffer
        if chunks:
            await self._flush(run_id, chunks)
        return []

    async def _flush(self, run_id: str, chunks: list[Chunk]) -> None:
        thread = await self._ensure_thread(run_id)
        if thread is None:
            return
        group: list[str] = []
        size = 0
        limit = self.discord.max_message_chars

        async def send_group() -> None:
            nonlocal group, size
            if group:
                await self._send(thread, "\n".join(group))
                group, size = [], 0

        for chunk in chunks:
            if chunk.kind == "line":
                if size + len(chunk.text) + 1 > limit and group:
                    await send_group()
                group.append(chunk.text)
                size += len(chunk.text) + 1
                continue
            await send_group()
            await self._send(
                thread, chunk.text, embed=chunk.embed, suppress_embeds=chunk.suppress_embeds
            )
        await send_group()

    async def _daemon_notice(self, text: str) -> None:
        # Point the notice at the run's thread when it names an item we ran.
        thread_id: int | None = None
        for item_id, run_id in list(self._item_runs.items()):
            if item_id in text:
                known = self.dstore.discord_thread(run_id)
                if known is not None and self.discord.thread_per_run:
                    thread_id = known.thread_id
                break
        await self._send_channel(daemon_notice(text, thread_id=thread_id))

    async def _finish(self, payload: tuple[Any, ...]) -> None:
        _, item, state, report, unanswered = payload
        run_id = report.run_id
        thread = await self._ensure_thread(run_id)
        # Final status-line edit, then the report card.
        status = self._status.get(run_id)
        if status is not None:
            status.finish(state)
            await self._edit_status(run_id, force=True)
        text = finish_text(state, report)
        if report.tracking_issue:
            text += f"\n📋 tracking issue #{report.tracking_issue[0]} <{report.tracking_issue[1]}>"
        if report.delivery:
            text += f"\n🔀 PR #{report.delivery[0]} <{report.delivery[1]}>"
        if report.delivery_error:
            text += f"\n⚠ delivery failed: {_one_line(report.delivery_error, 300)}"
        if unanswered:
            text += (
                f"\n⚠ {len(unanswered)} steering message(s) were not answered before the run ended"
            )
        if thread is not None:
            await self._send(thread, text, embed=finish_embed(item, report, state, len(unanswered)))
        facts = self._facts.setdefault(run_id, {})
        if report.tracking_issue:
            facts["tracking"] = report.tracking_issue
        if report.delivery:
            facts["pr"] = report.delivery
        facts["summary"] = report.task_summary
        await self._refresh_headline(run_id, item=item, state=state)
        # Per-run render state is no longer needed.
        self._batchers.pop(run_id, None)
        self._status.pop(run_id, None)
        self._status_msg.pop(run_id, None)
        self._status_last_edit.pop(run_id, None)
        self._facts.pop(run_id, None)
        task = self._status_task.pop(run_id, None)
        if task is not None:
            task.cancel()

    # -- status line ------------------------------------------------------------------

    def _schedule_status_edit(self, run_id: str) -> None:
        """Coalesce edits: at most one per STATUS_EDIT_MIN_S per run, always
        applying the latest text."""
        pending = self._status_task.get(run_id)
        if pending is not None and not pending.done():
            return  # an edit is already pending; it will render the latest state
        self._status_task[run_id] = asyncio.ensure_future(self._status_edit_later(run_id))

    async def _status_edit_later(self, run_id: str) -> None:
        now = asyncio.get_event_loop().time()
        last = self._status_last_edit.get(run_id)
        if last is not None and now - last < STATUS_EDIT_MIN_S:
            await asyncio.sleep(STATUS_EDIT_MIN_S - (now - last))
        await self._edit_status(run_id)

    async def _edit_status(self, run_id: str, *, force: bool = False) -> None:
        status = self._status.get(run_id)
        if status is None or (not status.dirty and not force):
            return
        text = _clip(status.render(), self.discord.max_message_chars)
        try:
            msg = self._status_msg.get(run_id)
            if msg is None:
                thread = await self._ensure_thread(run_id)
                if thread is None:
                    return
                known = self.dstore.discord_thread(run_id)
                if known is not None and known.status_id:
                    try:
                        msg = await thread.fetch_message(known.status_id)
                    except Exception:
                        msg = None
                if msg is None:
                    msg = await self._send(thread, text)
                    if msg is None:
                        return
                    self.dstore.set_discord_status_id(run_id, int(getattr(msg, "id", 0)) or None)
                    self._status_msg[run_id] = msg
                    self._status_last_edit[run_id] = asyncio.get_event_loop().time()
                    return
                self._status_msg[run_id] = msg
            await msg.edit(content=text)
            self._status_last_edit[run_id] = asyncio.get_event_loop().time()
        except Exception:
            logger.debug("discord: status edit failed", exc_info=True)

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
        """Block the pump until the gateway is up.

        Not ``client.wait_until_ready()``: discord.py creates the event that
        waits on *inside* ``client.start()`` → ``login()``, so a coroutine
        that awaits it before the client has started can park forever
        (field failure: bridge connected, pump never posted). ``on_ready``
        sets our own event; the fake client in tests does the same.
        """
        while not self._gateway_ready.is_set():
            await asyncio.sleep(0.1)
        self._ready.set()

    def mark_ready(self) -> None:
        """Called from the client's on_ready handler (any thread)."""
        self._gateway_ready.set()

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

    async def _send(
        self,
        target: Any,
        text: str = "",
        *,
        embed: EmbedSpec | None = None,
        suppress_embeds: bool = False,
    ) -> Any:
        """The single send seam: content is clipped, mentions are always
        disabled (agent prose can contain @everyone), embeds are converted
        here and dropped — text-only retry — if Discord rejects them."""
        content = _clip(text, self.discord.max_message_chars) if text else None
        kwargs: dict[str, Any] = {}
        mentions = _allowed_mentions_none()
        if mentions is not None:
            kwargs["allowed_mentions"] = mentions
        if suppress_embeds:
            kwargs["suppress_embeds"] = True
        if embed is not None:
            converted = _to_embed(embed) if self.discord.embeds else None
            if converted is not None:
                kwargs["embed"] = converted
            elif not content:
                content = _clip(embed.as_text(), self.discord.max_message_chars)
        if content is None and "embed" not in kwargs:
            return None
        try:
            return await target.send(content, **kwargs)
        except Exception:
            if "embed" in kwargs and embed is not None:
                logger.debug("discord: embed send failed; retrying text-only", exc_info=True)
                kwargs.pop("embed")
                fallback = content or _clip(embed.as_text(), self.discord.max_message_chars)
                try:
                    return await target.send(fallback, **kwargs)
                except Exception:
                    logger.warning("discord: text-only retry failed too", exc_info=True)
                    return None
            logger.warning("discord: send failed", exc_info=True)
            return None

    async def _send_channel(self, text: str, *, embed: EmbedSpec | None = None) -> None:
        channel = await self._channel()
        if channel is not None:
            await self._send(channel, text, embed=embed)

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
            thread_id = known.thread_id
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
            headline = await self._send(
                channel, headline_text(item, run_id), embed=headline_embed(item, run_id)
            )
            if headline is None:
                return None
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

    async def _refresh_headline(
        self, run_id: str, *, item: WorkItem | None = None, state: str | None = None
    ) -> None:
        """Re-render the headline (text + card) with everything known so far."""
        if item is None:
            with self._lock:
                item = self._active_item if self._active_run == run_id else None
        if item is None:
            return
        facts = self._facts.get(run_id, {})
        await self._edit_headline(
            run_id,
            headline_text(item, run_id, state),
            embed=headline_embed(
                item,
                run_id,
                state,
                branch=facts.get("branch"),
                tracking=facts.get("tracking"),
                pr=facts.get("pr"),
                summary=facts.get("summary"),
            ),
        )

    async def _edit_headline(
        self, run_id: str, text: str, *, embed: EmbedSpec | None = None
    ) -> None:
        known = self.dstore.discord_thread(run_id)
        if known is None or known.headline_id is None:
            return
        try:
            channel = await self._channel()
            if channel is None:
                return
            msg = await channel.fetch_message(known.headline_id)
            kwargs: dict[str, Any] = {"content": _clip(text, self.discord.max_message_chars)}
            if embed is not None and self.discord.embeds:
                converted = _to_embed(embed)
                if converted is not None:
                    kwargs["embed"] = converted
            await msg.edit(**kwargs)
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
            bridge.mark_ready()

        async def on_message(message: Any) -> None:
            bridge._handle_message(message)

        # discord.py's `@client.event` registers by function name; calling it
        # directly is the same registration without an untyped decorator.
        client.event(on_ready)
        client.event(on_message)
        return client


# -- discord.py adapters (the only place the optional extra is touched) ------------------


def _to_embed(spec: EmbedSpec) -> Any:
    """``discord.Embed`` for a spec, or None when discord.py is unavailable
    (tests, or a host without the extra — the caller falls back to text)."""
    try:
        import discord as discordpy
    except ImportError:
        return None
    spec = spec.clamped()
    embed = discordpy.Embed(
        title=spec.title, description=spec.description, url=spec.url, colour=spec.color
    )
    for name, value, inline in spec.fields:
        embed.add_field(name=name, value=value, inline=inline)
    if spec.footer:
        embed.set_footer(text=spec.footer)
    return embed


def _allowed_mentions_none() -> Any:
    try:
        import discord as discordpy
    except ImportError:
        return None
    return discordpy.AllowedMentions.none()
