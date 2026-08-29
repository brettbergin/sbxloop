"""DiscordBridge: the daemon's human channel — chronology out, steering in.

A gateway bot posts each run's chronology (agent messages with persona
attribution, tool lines, issue/PR links, verdicts) into a thread under a
headline in one control channel, and relays messages that @mention it in
that thread to the running agent as steering — the same ``post_user_message`` /
``chat.reply`` contract the CLI's ``--chat`` uses (engine.py), so no
engine changes are needed. Daemon-level events (queueing, breaker, cap,
recovery) go to the control channel itself. In the control channel,
``!sbx <verb>`` runs an operator command and @mentioning the bot (or
replying to it) talks to the **concierge** — the channel's agent
(``sbxloop.daemon.concierge``). Its ``watch_run`` tool calls back into
``DiscordBridge.on_watch``, which remembers the asker's Discord id and
@mentions them here when the run finishes; watches are persisted in
``daemon_run_watches`` and reloaded at startup, so a daemon restart
mid-run still pings whoever asked. Routing rules live in
``sbxloop.daemon.discord_routing``.

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
import functools
import os
import queue
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sbxloop.config import Config
from sbxloop.daemon.concierge import VIA_CONCIERGE_SUFFIX
from sbxloop.daemon.control import ITEM_COMMANDS, dispatch
from sbxloop.daemon.discord_format import (
    DISCORD_MAX_MESSAGE,
    Chunk,
    EmbedSpec,
    RunStats,
    StatusLine,
    SteerProgress,
    ToolBatcher,
    ToolDigest,
    _clip,
    _one_line,
    code,
    daemon_notice,
    finish_embed,
    finish_text,
    format_for_discord,
    headline_embed,
    headline_text,
    split_markdown,
    status_embed,
    summary_embed,
    summary_text,
)
from sbxloop.daemon.discord_routing import route_message
from sbxloop.daemon.model import TERMINAL_NOTICE_KINDS, DaemonNotice, RunReport, WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.engine import LoopEngine
from sbxloop.errors import DaemonError
from sbxloop.events import Event, EventBus, HostEventTypes
from sbxloop.log import get_logger

if TYPE_CHECKING:
    from sbxloop.daemon.concierge import Concierge, ConciergeReply
    from sbxloop_worker.protocol import HostToolResponse

log = get_logger(__name__)

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
# The normal level's tool digest (one line per burst, #235) is edited at
# most this often; the idle tick lands a deferred edit within COALESCE_WINDOW_S.
DIGEST_EDIT_MIN_S = 3.0
# close() waits this long for the pump to post what is already queued: a
# --once daemon can finish its run before the gateway is even up, and its
# chronology must not end wherever the process happened to exit.
DRAIN_WAIT_S = 20.0
# Requester display-string -> Discord id map (run watches); bounded so a
# long-lived daemon does not accumulate every author it has ever seen.
REQUESTER_ID_CAP = 200
# Run watches (run_id -> watcher ids); bounded for the same reason, and also
# because `_watchers` is only ever cleared on the `run_finished` path — a run
# that never gets there (daemon shutdown mid-run, a crash, a breaker
# teardown) would otherwise leak its entry forever.
WATCHERS_CAP = 200
# The concierge's "🛠 …" tool-note line is edited at most this often.
CONCIERGE_NOTE_EDIT_MIN_S = 1.5

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
# A tool burst ends at a phase/task boundary even when that event renders
# nothing at the normal level — the next phase's calls start a fresh line.
_BURST_BOUNDARY = ("phase.end", "task.start", "task.end", "run.end")


# -- bridge ---------------------------------------------------------------------------


class _Pending:
    """A steering message awaiting its chat.reply. ``status`` is the bridge's
    own "⏳ queued …" note under it, edited in place as the run moves."""

    def __init__(self, run_id: str, thread_id: int, discord_message_id: int) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        self.discord_message_id = discord_message_id
        self.status: Any = None
        self.state = "queued"


class _ConciergeTurn:
    """Per-turn UI state for one concierge answer (discord thread only)."""

    def __init__(self, message: Any) -> None:
        self.message = message
        self.channel = message.channel
        self.note: Any = None  # the "🛠 …" line, posted on the first tool call
        self.calls: list[str] = []
        self.failed = 0
        self.last_edit = 0.0
        self.edit_task: asyncio.Task[None] | None = None

    def render(self) -> str:
        shown = self.calls[-6:]
        more = len(self.calls) - len(shown)
        text = "🛠 concierge: " + " · ".join(shown)
        if more > 0:
            text += f" (+{more} earlier)"
        if self.failed:
            text += f" ⚠ {self.failed} failed"
        return text


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
        concierge: Concierge | None = None,
    ) -> None:
        self.config = config
        self.discord = config.discord
        self.dstore = dstore
        self.loop_ref = loop_ref  # DaemonLoop, for !sbx commands + steering
        # The control channel's agent (None: mentions get a "chat is off" reply).
        self.concierge = concierge
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
        self._drained: asyncio.Event | None = None
        self._drain_wait_s = DRAIN_WAIT_S
        self._unsubscribe: Callable[[], None] | None = None
        self._active_run: str | None = None
        self._active_item: WorkItem | None = None
        self._engine: LoopEngine | None = None
        # run_id -> item for every run whose events may still be queued. The
        # active pair above says "steering is possible"; this says "we can
        # still post the headline" — a short run (--once) can finish before
        # the pump ever sees its first event, and its chronology must not
        # be dropped for lack of an item.
        self._items: dict[str, WorkItem] = {}
        self._pending: dict[str, _Pending] = {}  # message_id -> pending steer
        self._lock = threading.Lock()
        # Run watches (#335). The concierge is transport-agnostic: it hands a
        # requester *display string* to `on_watch`, and the bridge turns it
        # back into a mentionable id through `_requester_ids`. The watches are
        # persisted in `daemon_run_watches` and reloaded here, so a daemon
        # restart mid-run still pings whoever asked; `_requester_ids` stays
        # in memory.
        self._watch_lock = threading.Lock()
        self._watchers: dict[str, list[str]] = {}  # run_id -> discord user ids
        self._requester_ids: dict[str, str] = {}  # author display string -> user id
        self._reload_watches()
        # Per-run rendering state, owned by the pump (discord thread only).
        self._batchers: dict[str, ToolBatcher] = {}
        self._digests: dict[str, ToolDigest] = {}
        self._digest_msg: dict[str, Any] = {}  # run_id -> the current burst's message
        self._digest_last_edit: dict[str, float] = {}
        self._status: dict[str, StatusLine] = {}
        self._status_msg: dict[str, Any] = {}  # run_id -> discord message (cached)
        self._status_task: dict[str, asyncio.Task[None]] = {}
        self._status_last_edit: dict[str, float] = {}
        # Where the agent is, for the note under a queued steer (#236).
        self._progress: dict[str, SteerProgress] = {}
        self._steer_task: dict[str, asyncio.Task[None]] = {}
        self._steer_last_edit: dict[str, float] = {}
        # Facts that enrich the headline card as the run reveals them.
        self._facts: dict[str, dict[str, Any]] = {}
        # Counters behind the end-of-run summary card, the thread's last post.
        self._runstats: dict[str, RunStats] = {}

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
        log.info(
            "discord.bridge_starting",
            channel=self.discord.channel_id,
            chronology_level=self.discord.chronology_level,
            thread_per_run=self.discord.thread_per_run,
            connect_wait_s=connect_wait_s,
        )
        # The asyncio loop exists only once the thread runs; callers may
        # schedule work immediately after start(), so wait for it.
        if not self._loop_up.wait(timeout=10):
            log.warning("discord.loop_not_up", waited_s=10)
        # Give the gateway a moment to connect so a short-lived process
        # (--once) can still post; a slow or failed connect never blocks
        # the daemon — the pump waits for readiness on its own.
        if not self._ready.wait(timeout=connect_wait_s):
            log.warning(
                "discord.not_ready_yet",
                waited_s=connect_wait_s,
                hint="gateway still connecting; the pump posts once it is",
            )

    def close(self, *, drain_wait_s: float = DRAIN_WAIT_S) -> None:
        """Ask the bridge thread to shut down and wait for it. The client is
        closed *inside* its own loop (``_amain``) so aiohttp's connector
        teardown completes before the loop is torn down.

        Everything enqueued before this call is posted first (bounded by
        ``drain_wait_s``): the pump sees the ``__stop__`` sentinel only
        after the events ahead of it, flushes, and signals ``_drained``.
        """
        self._drain_wait_s = drain_wait_s
        self._events.put(("__stop__", None))
        if self._aloop is not None and not self._aloop.is_closed():
            self._aloop.call_soon_threadsafe(self._stop_event_set)
        if self._thread is not None:
            self._thread.join(timeout=20 + drain_wait_s)
            if self._thread.is_alive():
                log.warning(
                    "discord.close_timeout",
                    waited_s=20 + drain_wait_s,
                    unsent=self._events.qsize(),
                )
            else:
                log.info("discord.bridge_closed")

    def _stop_event_set(self) -> None:
        if self._stop_evt is not None:
            self._stop_evt.set()

    def _thread_main(self) -> None:
        self._aloop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._aloop)
        self._stop_evt = asyncio.Event()
        self._drained = asyncio.Event()
        self._loop_up.set()
        try:
            self._aloop.run_until_complete(self._amain())
        except Exception:
            log.error(
                "discord.loop_crashed",
                hint="the asyncio loop died; chronology is off until the daemon restarts",
                exc_info=True,
            )
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
                log.error(
                    "discord.connect_failed",
                    error=str(exc) if exc is not None else "gateway exited",
                    hint="chronology is off until the daemon restarts (token? network?)",
                )
                self._degraded = True
                self._gateway_ready.set()  # unblock the pump so it drains
                await stopper
            elif self._drained is not None:
                # close(): let the pump post what is already queued. A
                # gateway that never connected keeps the pump parked in
                # _wait_ready; the timeout bounds that.
                try:
                    await asyncio.wait_for(self._drained.wait(), timeout=self._drain_wait_s)
                except TimeoutError:
                    log.warning(
                        "discord.drain_timeout",
                        unsent=self._events.qsize(),
                        drain_wait_s=self._drain_wait_s,
                    )
        finally:
            pump.cancel()
            stopper.cancel()
            try:
                await self.client.close()
            except Exception:
                log.debug("discord.client_close_failed", exc_info=True)
            if not gateway.done():
                gateway.cancel()
            await asyncio.gather(pump, stopper, gateway, return_exceptions=True)

    # -- Frontend protocol (called from daemon threads) --------------------------

    def daemon_notice(self, notice: DaemonNotice) -> None:
        self._enqueue("__daemon__", notice)

    def run_started(self, item: WorkItem, run_id: str, engine: LoopEngine, bus: EventBus) -> None:
        with self._lock:
            self._active_run = run_id
            self._active_item = item
            self._items[run_id] = item
            self._engine = engine
            # Non-blocking subscriber: just enqueue; the pump renders + sends.
            self._unsubscribe = bus.subscribe(lambda ev: self._events.put((run_id, ev)))
        if item.requested_by:
            # Whoever asked for the work through the concierge is pinged with
            # the outcome without having to ask for a watch.
            with self._watch_lock:
                watchers = self._watchers.setdefault(run_id, [])
                if item.requested_by not in watchers:
                    watchers.append(item.requested_by)
                self._persist_watch(run_id, item.requested_by)
        self._enqueue(run_id, None)  # sentinel: ensure thread + headline

    def run_finished(self, item: WorkItem, report: RunReport) -> None:
        with self._lock:
            unsubscribe, self._unsubscribe = self._unsubscribe, None
            self._active_run = None
            self._active_item = None
            # Drop the handle with the run: the liveness check below is the
            # gate, but a finished run's engine has no business being reachable.
            self._engine = None
        if unsubscribe is not None:
            unsubscribe()
        state = report.state
        # Which steers went unanswered is decided by the pump in ``_finish``,
        # AFTER it has drained the events queued ahead of this marker: a
        # ``chat.reply`` already on the queue still resolves its steer.
        self._enqueue(report.run_id, ("__finished__", item, state, report))

    def _enqueue(self, run_id: str, payload: Any) -> None:
        self._events.put((run_id, payload))

    # -- steering (called from the discord thread) --------------------------------

    def _handle_message(self, message: Any) -> None:
        """Route an inbound Discord message: command, concierge, steering, or ignore."""
        raw = getattr(getattr(message, "channel", None), "id", None)
        channel_id = int(raw) if raw is not None else None
        author_is_bot = bool(getattr(getattr(message, "author", None), "bot", False))
        # Our own chronology posts arrive here too, so settle the free facts
        # before the one that costs a store lookup.
        route = route_message(
            content=str(getattr(message, "content", "") or ""),
            channel_id=channel_id,
            author_is_bot=author_is_bot,
            mentioned_ids=frozenset(
                int(m.id) for m in getattr(message, "mentions", None) or () if hasattr(m, "id")
            ),
            reply_to_bot=not author_is_bot and self._is_reply_to_bot(message),
            control_channel_id=self.discord.channel_id,
            prefix=self.discord.command_prefix,
            bot_user_id=self._bot_user_id(),
            is_run_thread=not author_is_bot and self._is_run_thread(channel_id),
        )
        if route.kind == "command":
            self._schedule(self._command(message, route.text))
        elif route.kind == "concierge":
            self._schedule(self._concierge_turn(message, route.text))
        elif route.kind == "steer":
            assert channel_id is not None
            self._steer(message, channel_id, route.text)

    def _bot_user_id(self) -> int | None:
        user = getattr(self.client, "user", None)
        uid = getattr(user, "id", None)
        return int(uid) if uid is not None else None

    def _is_run_thread(self, channel_id: int | None) -> bool:
        """Is this channel a thread we opened for a run? The control channel
        answers without touching the store; anything else is one indexed
        point query, the same lookup ``_steer`` already makes for it."""
        if channel_id is None or channel_id == self.discord.channel_id:
            return False
        return self.dstore.run_for_thread(channel_id) is not None

    def _is_reply_to_bot(self, message: Any) -> bool:
        """A reply to one of the bot's own messages counts as talking to it.

        discord.py fills ``reference.resolved`` from the gateway payload, but
        leaves it None when the payload omitted the referenced message and
        substitutes a ``DeletedReferencedMessage`` (no ``.author``) when it was
        deleted. Fall back to the message cache before giving up: this gate
        decides steers now, not just concierge turns.
        """
        bot_id = self._bot_user_id()
        if bot_id is None:
            return False
        reference = getattr(message, "reference", None)
        for referenced in (
            getattr(reference, "resolved", None),
            getattr(reference, "cached_message", None),
        ):
            author_id = getattr(getattr(referenced, "author", None), "id", None)
            if author_id is not None:
                return int(author_id) == bot_id
        return False

    def _steer(self, message: Any, thread_id: int, text: str) -> None:
        """A message in a run's thread: relay it to the running agent."""
        channel = message.channel
        run_id = self.dstore.run_for_thread(thread_id)
        if run_id is None:
            # Routing already confirmed this thread; losing the row between
            # then and now is a race (state reset mid-message), not traffic.
            log.debug("discord.steer_unknown_thread", thread=thread_id)
            return
        with self._lock:
            live = run_id == self._active_run
            engine = self._engine
        if not live or engine is None:
            log.info(
                "discord.steer_rejected",
                run=run_id,
                author=_author_name(message),
                reason="run finished",
            )
            self._schedule(
                self._send(
                    channel, f"run {code(run_id)} has finished; steering is no longer possible."
                )
            )
            return
        mid = engine.post_user_message(text)
        log.info(
            "discord.steer",
            run=run_id,
            author=_author_name(message),
            message=mid,
            chars=len(text),
            text=text[:200],
        )
        with self._lock:
            self._pending[mid] = _Pending(run_id, thread_id, int(getattr(message, "id", 0)))
        self._schedule(self._react(message, "⏳"))
        self._schedule(self._post_steer_status(run_id, mid, channel))

    # -- concierge (the control channel's agent) --------------------------------------

    # -- run watches (#335) -----------------------------------------------------------

    def _remember_requester(self, author: str, author_id: str | None) -> None:
        """Map a concierge requester's display string to their Discord id, so
        `on_watch` can register a mentionable id later in the same turn."""
        if not author_id:
            return
        with self._watch_lock:
            self._requester_ids[author] = author_id
            while len(self._requester_ids) > REQUESTER_ID_CAP:
                self._requester_ids.pop(next(iter(self._requester_ids)))

    def _reload_watches(self) -> None:
        """Repopulate `_watchers` from the store at startup, so a run started
        before a restart still pings whoever asked. A watch whose run
        already reached a terminal ledger state is dropped instead of
        reloaded: `run_finished` for it already fired (or the daemon was
        down when it should have), so reviving the entry would just leave a
        phantom nothing will ever drain again — reconciled against
        `daemon_runs.finished_at` via `finished_run_ids`, and the dropped
        row is cleared with `_evict_watch` so it does not keep coming back
        on every future restart. Never raises: a bad or missing store
        leaves the bridge on its in-memory registry."""
        store = getattr(self, "dstore", None)
        if store is None:
            return
        try:
            watches = store.all_run_watches()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("discord.watch_reload_failed", error=str(exc), exc_info=True)
            return
        if not watches:
            return
        try:
            finished = store.finished_run_ids(list(watches))
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("discord.watch_reconcile_failed", error=str(exc), exc_info=True)
            finished = set()
        for run_id in finished:
            ids = watches.pop(run_id, [])
            log.warning("discord.watch_orphaned", run=run_id, watchers=len(ids))
            self._evict_watch(run_id)
        if not watches:
            return
        with self._watch_lock:
            for run_id, ids in watches.items():
                self._watchers[run_id] = list(dict.fromkeys(ids))
            while len(self._watchers) > WATCHERS_CAP:
                evicted = next(iter(self._watchers))
                self._watchers.pop(evicted)
                self._evict_watch(evicted)
        log.info("discord.watches_reloaded", runs=len(self._watchers))

    def on_watch(self, run_id: str, requester: str) -> str | None:
        """Register interest in a run's outcome; the callback handed to the
        Concierge (the transport seam — the concierge never imports discord).
        Returns None on success, or a short note to append to the concierge's
        confirmation when the requester has no mentionable id. Never raises:
        a bridge problem must not fail the concierge turn.

        A concierge-driven call arrives tagged with ``VIA_CONCIERGE_SUFFIX``
        (``_tool_handler`` builds ``by = f"{author} (via concierge)"``), but
        `_remember_requester` stores the id under the bare, untagged author
        string. Strip the tag back off before the lookup, or every real
        watch fails to find an id and silently registers nothing.

        The in-memory append and the store persist happen under the same
        `_watch_lock` `_take_watchers` uses to drain both registries — a
        TOCTOU otherwise: with the persist outside the lock, a concurrent
        drain for this run could pop the (still empty) in-memory entry and
        delete zero store rows *before* this call's INSERT lands, orphaning
        the row forever (nothing else ever deletes a row for a run that has
        already finished)."""
        key = requester.removesuffix(VIA_CONCIERGE_SUFFIX)
        try:
            with self._watch_lock:
                user_id = self._requester_ids.get(key)
                if user_id is None:
                    return (
                        "(I don't have a mentionable id for you, so nothing was "
                        "registered — ask again from the control channel)"
                    )
                watchers = self._watchers.setdefault(run_id, [])
                if user_id not in watchers:
                    watchers.append(user_id)
                self._persist_watch(run_id, user_id)
                while len(self._watchers) > WATCHERS_CAP:
                    evicted = next(iter(self._watchers))
                    self._watchers.pop(evicted)
                    self._evict_watch(evicted)
            log.info("discord.watch_registered", run=run_id, by=requester)
            return None
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("discord.watch_register_failed", run=run_id, error=str(exc), exc_info=True)
            return None

    def _persist_watch(self, run_id: str, user_id: str) -> None:
        """Must be called with `_watch_lock` held (see `on_watch`), so the
        memory mutation and the store write are one atomic step from
        `_take_watchers`' point of view."""
        store = getattr(self, "dstore", None)
        if store is None:
            return
        try:
            store.add_run_watch(run_id, user_id, time.time())
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("discord.watch_persist_failed", run=run_id, error=str(exc), exc_info=True)

    def _evict_watch(self, run_id: str) -> None:
        """Drop a run's persisted row when its in-memory entry is evicted
        (cap trim in `on_watch`/`_reload_watches`) or reconciled away
        (`_reload_watches` dropping an already-finished run). Without this
        the store row outlives the eviction it is supposed to mirror and
        `daemon_run_watches` grows without bound for runs that never reach
        `_post_watch_notice`."""
        store = getattr(self, "dstore", None)
        if store is None:
            return
        try:
            store.clear_run_watch(run_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("discord.watch_evict_failed", run=run_id, error=str(exc), exc_info=True)

    def _take_watchers(self, run_id: str) -> list[str]:
        """Drain both registries for a run under `_watch_lock` — the same
        lock `on_watch` holds across its own memory-append-plus-persist —
        so the two operations cannot interleave. Unioned and de-duplicated,
        order preserved. Both are cleared, so a second finish for the same
        run pings nobody."""
        with self._watch_lock:
            watchers = list(self._watchers.pop(run_id, []))
            store = getattr(self, "dstore", None)
            if store is not None:
                try:
                    watchers.extend(store.take_run_watchers(run_id))
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning(
                        "discord.watch_take_failed", run=run_id, error=str(exc), exc_info=True
                    )
        return list(dict.fromkeys(watchers))

    def _watch_notice(
        self, run_id: str, watchers: list[str], state: str, report: RunReport, thread: Any
    ) -> str:
        mentions = " ".join(f"<@{uid}>" for uid in watchers)
        lines = [f"{mentions} run `{run_id}` finished: **{state}**"]
        if report.task_summary:
            lines.append(f"tasks: {_one_line(report.task_summary, 200)}")
        if report.pr:
            marker = "🎉 merged" if state == "merged" else "🔀"
            lines.append(f"{marker} PR #{report.pr[0]} <{report.pr[1]}>")
        if report.reason and state != "merged":
            lines.append(f"⚠ {_one_line(report.reason, 200)}")
        if thread is not None:
            lines.append(f"chronology: <#{thread.thread_id}>")
        return _clip("\n".join(lines), self.discord.max_message_chars)

    async def _post_watch_notice(self, run_id: str, state: str, report: RunReport) -> None:
        """Ping everyone watching this run, once. `_watchers` is keyed by run
        id only: a watch asked for by work item is resolved to that item's
        newest run concierge-side, before `on_watch` is called, so there is no
        item-id key to pop here. The drain covers the in-memory entry *and*
        the persisted rows (a watch registered before a daemon restart), and
        is final — a second finish for the same run pings nobody. A run that
        never reaches this path at all (shutdown mid-run, a crash, a breaker
        teardown) leaves its entry in `_watchers` *and* its persisted row —
        `WATCHERS_CAP` bounds both together (`_evict_watch` clears the row
        whenever the in-memory entry is evicted), instead of leaking either
        forever, evicting the oldest registrations first. A watch whose run
        already finished while the daemon was down is a separate case,
        reconciled at `_reload_watches` rather than here."""
        watchers = self._take_watchers(run_id)
        if not watchers:
            return
        try:
            thread = self.dstore.discord_thread(run_id)
            await self._send_channel(
                self._watch_notice(run_id, watchers, state, report, thread),
                mentions=True,
            )
        except Exception as exc:
            log.warning("discord.watch_notice_failed", run=run_id, error=str(exc), exc_info=True)

    async def _concierge_turn(self, message: Any, text: str) -> None:
        channel = message.channel
        prefix = self.discord.command_prefix
        if self.concierge is None:
            await self._send(
                channel,
                f"chat is off for this daemon — use `{prefix} status` and friends "
                "(`[concierge] enabled` turns the agent on).",
                reply_to=message,
            )
            return
        await self._react(message, "⏳")
        turn = _ConciergeTurn(message)
        behind = self.concierge.pending
        if behind > 0:
            await self._send(channel, f"⏳ queued behind {behind} other question(s)…")

        def on_tool(name: str, args: dict[str, Any], response: HostToolResponse) -> None:
            self._schedule(self._concierge_tool_note(turn, name, args, response))

        author = _author_name(message)
        self._remember_requester(author, _author_id(message))
        log.info("discord.concierge_turn", by=author, chars=len(text))
        try:
            async with _typing(channel):
                future = self.concierge.submit_turn(text, author=author, on_tool=on_tool)
                reply = await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A closed concierge (shutdown race) or a lost future: say so, once.
            log.warning("discord.concierge_turn_failed", by=author, error=str(exc), exc_info=True)
            await self._react(message, "⚠")
            await self._send(channel, f"⚠ concierge: {_one_line(str(exc), 300)}", reply_to=message)
            return
        await self._finish_concierge_note(turn)
        await self._post_concierge_reply(message, reply)

    async def _post_concierge_reply(self, message: Any, reply: ConciergeReply) -> None:
        channel = message.channel
        if not reply.ok:
            await self._react(message, "⚠")
            await self._send(
                channel, f"⚠ concierge: {reply.error or 'no answer'}", reply_to=message
            )
            return
        text = reply.text or "(the concierge had nothing to say)"
        first = True
        for chunk in split_markdown(text, self.discord.max_message_chars):
            await self._send(channel, chunk, reply_to=message if first else None)
            first = False
        await self._react(message, "✅")

    async def _concierge_tool_note(
        self, turn: _ConciergeTurn, name: str, args: dict[str, Any], response: HostToolResponse
    ) -> None:
        """One edited line per turn listing the tools the concierge used —
        the audit trail for actions taken without a confirmation step."""
        if self.discord.chronology_level == "quiet":
            return
        turn.calls.append(_tool_call_summary(name, args))
        if not response.ok:
            turn.failed += 1
        if turn.note is None:
            turn.note = await self._send(turn.channel, turn.render(), suppress_embeds=True)
            turn.last_edit = time.monotonic()
            return
        self._schedule_note_edit(turn)

    def _schedule_note_edit(self, turn: _ConciergeTurn) -> None:
        if turn.edit_task is not None and not turn.edit_task.done():
            return
        turn.edit_task = asyncio.ensure_future(self._note_edit_later(turn))

    async def _note_edit_later(self, turn: _ConciergeTurn) -> None:
        wait = CONCIERGE_NOTE_EDIT_MIN_S - (time.monotonic() - turn.last_edit)
        if wait > 0:
            await asyncio.sleep(wait)
        await self._edit_concierge_note(turn)

    async def _edit_concierge_note(self, turn: _ConciergeTurn) -> None:
        if turn.note is None:
            return
        try:
            await turn.note.edit(content=_clip(turn.render(), self.discord.max_message_chars))
            turn.last_edit = time.monotonic()
        except Exception:
            log.warning("discord.concierge_note_edit_failed", exc_info=True)

    async def _finish_concierge_note(self, turn: _ConciergeTurn) -> None:
        if turn.edit_task is not None and not turn.edit_task.done():
            turn.edit_task.cancel()
        await self._edit_concierge_note(turn)

    async def _command(self, message: Any, cmd: str) -> None:
        loop = self.loop_ref
        channel = message.channel
        if loop is None:
            await self._send(channel, "daemon loop not attached")
            return
        prefix = self.discord.command_prefix
        # Same dispatcher as `sbxloop daemon ctl` (#232): Discord only adds
        # the author attribution and the rendering — the status embed, and
        # the steering hint on the usage line.
        word = (cmd.split() or [""])[0].lower()
        by = _author_name(message)
        if word in ITEM_COMMANDS:
            # Abandoning a queued GitHub item reports through the ops
            # sandbox — seconds, not milliseconds — so keep it off the
            # gateway's event loop.
            reply = await asyncio.get_event_loop().run_in_executor(
                None, functools.partial(dispatch, loop, cmd, prefix=prefix, by=by, via="discord")
            )
        else:
            reply = dispatch(loop, cmd, prefix=prefix, by=by, via="discord")
        if reply.status is not None:
            await self._send(channel, reply.text, embed=status_embed(reply.status))
        elif not reply.known:
            hint = " — or @mention me in a run's thread to steer that run"
            if self.concierge is not None:
                hint += ", or here to ask in plain language"
            await self._send(channel, f"{reply.text}{hint}.")
        else:
            await self._send(channel, reply.text, suppress_embeds=True)

    # -- pump: queue -> discord (discord thread) -------------------------------------

    async def _pump(self) -> None:
        await self._wait_ready()
        if self._degraded:
            # Consume forever so the queue stays bounded; nothing to send to.
            while True:
                try:
                    run_id, _ = await asyncio.get_event_loop().run_in_executor(
                        None, self._events.get, True, COALESCE_WINDOW_S
                    )
                except queue.Empty:
                    continue
                if run_id == "__stop__" and self._drained is not None:
                    self._drained.set()
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
                    # A digest edit deferred by the rate limit lands now.
                    buffer = await self._digest_tick(buffer_run, buffer)
                continue
            try:
                if run_id == "__stop__":
                    if buffer_run:
                        buffer = await self._flush_all(buffer_run, buffer)
                    if self._drained is not None:
                        self._drained.set()
                    continue
                if run_id == "__daemon__":
                    notice = (
                        payload
                        if isinstance(payload, DaemonNotice)
                        else DaemonNotice("daemon.started", str(payload))
                    )
                    await self._daemon_notice(notice)
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
                if event.type == HostEventTypes.CHAT_MESSAGE:
                    await self._steer_picked_up(event)
                if event.type == HostEventTypes.CHAT_REPLY:
                    await self._resolve_reply(event)
                if buffer_run not in (None, run_id) and buffer_run:
                    buffer = await self._flush_all(buffer_run, buffer)
                    buffer = await self._digest_tick(buffer_run, buffer, close=True)
                buffer_run = run_id
                chunks = self._render(run_id, event)
                self._observe_facts(run_id, event)
                self._runstats.setdefault(run_id, RunStats()).observe(event)
                if event.type == "agent.tool_start":
                    buffer = await self._digest_tick(run_id, buffer)
                elif event.type == "agent.tool_end":
                    if chunks:  # a failure detail: the line it belongs to goes first
                        buffer = await self._digest_tick(run_id, buffer, force=True)
                elif chunks or event.type in _BURST_BOUNDARY:
                    buffer = await self._digest_tick(run_id, buffer, close=True)
                self._observe_progress(run_id, event)
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
                log.warning(
                    "discord.pump_failed",
                    run=run_id,
                    queued=self._events.qsize(),
                    exc_info=True,
                )

    def _render(self, run_id: str, event: Event) -> list[Chunk]:
        """Chunks for one event, routing tool events through the run's
        batcher (verbose/quiet) or digest (normal) and lifecycle events
        through its status line."""
        level = self.discord.chronology_level
        if self.discord.status_line and event.type in _STATUS_EVENTS:
            status = self._status.setdefault(run_id, StatusLine())
            status.observe(event)
            if status.dirty:
                self._schedule_status_edit(run_id)
        d = event.data
        if level == "normal":
            # Field (#235): streaming every tool line buried the agent
            # messages and verdicts a human reads the thread for. The
            # digest folds a burst into one line the pump edits in place;
            # only failures keep their own detail block.
            digest = self._digest(run_id)
            if event.type == "agent.tool_start":
                digest.add_start(
                    str(d.get("tool") or "?"),
                    str(d.get("args") or ""),
                    d.get("tool_call_id"),
                )
                return []
            if event.type == "agent.tool_end":
                detail = digest.add_end(
                    str(d.get("tool") or "?"),
                    d.get("tool_call_id"),
                    success=d.get("success"),
                    exit_code=d.get("exit_code"),
                    detail=str(d.get("error") or d.get("output") or ""),
                    output_lines=d.get("output_lines"),
                )
                return [detail] if detail else []
            return format_for_discord(event, level=level, max_chars=self.discord.max_message_chars)
        batcher = self._batchers.get(run_id)
        if batcher is None:
            batcher = self._batchers[run_id] = ToolBatcher(
                max_lines=self.discord.tool_batch_lines,
                quiet=(level == "quiet"),
                output_lines=self.discord.tool_output_lines,
                fail_output_lines=self.discord.tool_fail_output_lines,
            )
        if event.type == "agent.tool_start":
            # No line yet: the call renders once, on completion.
            batcher.add_start(
                str(d.get("tool") or "?"), str(d.get("args") or ""), d.get("tool_call_id")
            )
            return []
        if event.type == "agent.tool_end":
            detail = batcher.add_end(
                str(d.get("tool") or "?"),
                d.get("tool_call_id"),
                success=d.get("success"),
                exit_code=d.get("exit_code"),
                detail=str(d.get("error") or d.get("output") or ""),
                args=str(d.get("args") or ""),
                duration_ms=d.get("duration_ms"),
                output_lines=d.get("output_lines"),
            )
            if detail is None and not batcher.full:
                return []
            batch = batcher.flush()
            if batch and detail:
                return [batch, detail]
            if batch:
                return [batch]
            return [detail] if detail else []
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
        elif event.type == HostEventTypes.RUN_DELIVER and d.get("pr") and d.get("url"):
            facts["pr"] = (int(d["pr"]), str(d["url"]))
            changed = True
        if changed:
            self._schedule(self._refresh_headline(run_id))

    def _observe_progress(self, run_id: str, event: Event) -> None:
        """Keep the "where is the agent" tracker current; re-render the note
        under any queued steer when it moved (coalesced like the status line)."""
        progress = self._progress.get(run_id)
        if progress is None:
            progress = self._progress[run_id] = SteerProgress(
                cap=self.config.budgets.max_tool_calls_per_phase
            )
        progress.observe(event)
        if not progress.dirty:
            return
        with self._lock:
            waiting = any(
                p.run_id == run_id and p.state == "queued" for p in self._pending.values()
            )
        if waiting:
            self._schedule_steer_edit(run_id)

    async def _flush_all(self, run_id: str, buffer: list[Chunk]) -> list[Chunk]:
        """Send the tool batch (if any) then everything buffered; returns the
        new (empty) buffer."""
        batcher = self._batchers.get(run_id)
        pending = batcher.flush() if batcher else None
        chunks = ([pending] if pending else []) + buffer
        if chunks:
            await self._flush(run_id, chunks)
        return []

    # -- tool digest (normal level) -----------------------------------------------------

    def _digest(self, run_id: str) -> ToolDigest:
        digest = self._digests.get(run_id)
        if digest is None:
            digest = self._digests[run_id] = ToolDigest(
                cancel_hint=f"{self.discord.command_prefix} cancel",
                # The normal level exists to *not* stream successes: the
                # digest line already reports them, so successes get no
                # excerpt here regardless of tool_output_lines (which is a
                # verbose-level knob). Failures keep the configured budget.
                output_lines=0,
                fail_output_lines=self.discord.tool_fail_output_lines,
            )
        return digest

    async def _digest_tick(
        self, run_id: str, buffer: list[Chunk], *, force: bool = False, close: bool = False
    ) -> list[Chunk]:
        """Bring the burst's summary message up to date: first sighting
        sends it (after flushing what came before, so the thread stays in
        order), later sightings edit it at most once per DIGEST_EDIT_MIN_S
        unless ``force``d. ``close`` ends the burst — final edit, then the
        next tool call starts a fresh message. Returns the (possibly
        flushed) buffer."""
        digest = self._digests.get(run_id)
        if digest is None or not len(digest):
            return buffer
        msg = self._digest_msg.get(run_id)
        now = asyncio.get_event_loop().time()
        due = force or close or now - self._digest_last_edit.get(run_id, 0) >= DIGEST_EDIT_MIN_S
        if digest.dirty and (msg is None or due):
            text = _clip(digest.render(), self.discord.max_message_chars)
            try:
                if msg is None:
                    buffer = await self._flush_all(run_id, buffer)
                    thread = await self._ensure_thread(run_id)
                    if thread is not None:
                        msg = await self._send(thread, text)
                        if msg is not None:
                            self._digest_msg[run_id] = msg
                else:
                    await msg.edit(content=text)
                self._digest_last_edit[run_id] = now
            except Exception:
                log.warning("discord.digest_edit_failed", run=run_id, exc_info=True)
        if close:
            self._digests[run_id] = ToolDigest(cancel_hint=digest.cancel_hint)
            self._digest_msg.pop(run_id, None)
            self._digest_last_edit.pop(run_id, None)
        return buffer

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

    async def _daemon_notice(self, notice: DaemonNotice) -> None:
        """Route a notice: a run-scoped one goes into that run's thread when
        the bridge opened one; a terminal one is mirrored to the control
        channel with a pointer to the thread, so a human not reading the
        thread still sees how the run ended. Everything else is a
        control-channel line."""
        thread_id: int | None = None
        if notice.run_id and self.discord.thread_per_run:
            known = self.dstore.discord_thread(notice.run_id)
            if known is not None:
                thread_id = known.thread_id
        text = daemon_notice(notice)
        if thread_id is not None:
            try:
                thread = await self._thread_channel(thread_id)
                if thread is not None:
                    await self._send(thread, text)
            except Exception:
                log.warning("discord.notice_thread_failed", run=notice.run_id, exc_info=True)
            if notice.kind not in TERMINAL_NOTICE_KINDS:
                return
        await self._send_channel(daemon_notice(notice, thread_id=thread_id))

    async def _thread_channel(self, thread_id: int) -> Any:
        """The thread object for a persisted thread id, or None."""
        client = self.client
        thread = client.get_channel(thread_id) if client is not None else None
        if thread is None and client is not None:
            thread = await client.fetch_channel(thread_id)
        return thread

    async def _finish(self, payload: tuple[Any, ...]) -> None:
        _, item, state, report = payload
        run_id = report.run_id
        with self._lock:
            unanswered = [p for p in self._pending.values() if p.run_id == run_id]
            self._pending = {m: p for m, p in self._pending.items() if p.run_id != run_id}
        thread = await self._ensure_thread(run_id)
        # Close the tool batch: anything still in flight can no longer get an
        # end event, so this is the one flush that renders `… running` lines.
        batcher = self._batchers.get(run_id)
        leftovers = batcher.flush(final=True) if batcher else None
        if leftovers is not None:
            await self._flush(run_id, [leftovers])
        await self._digest_tick(run_id, [], close=True)
        # Final status-line edit, then the report card.
        status = self._status.get(run_id)
        if status is not None:
            status.finish(state)
            await self._edit_status(run_id, force=True)
        text = finish_text(state, report)
        if report.pr:
            marker = "🎉 merged" if state == "merged" else "🔀"
            text += f"\n{marker} PR #{report.pr[0]} <{report.pr[1]}>"
        if report.reason and state != "merged":
            text += f"\n⚠ {_one_line(report.reason, 300)}"
        repo = self.config.github.repo
        if unanswered:
            text += (
                f"\n⚠ {len(unanswered)} steering message(s) were not answered before the run ended"
            )
            for pending in unanswered:
                await self._edit_steer_status(pending, "unanswered")
        if thread is not None:
            await self._send(
                thread,
                text,
                embed=finish_embed(item, report, state, len(unanswered), repo=repo),
            )
            # The thread's LAST post: the summary card — the run's numbers,
            # what went well, what needed work — so a human opening the
            # thread later reads the outcome bottom-up without scrolling.
            stats = self._runstats.get(run_id)
            await self._send(
                thread,
                summary_text(stats, state),
                embed=summary_embed(stats, report, state, len(unanswered)),
            )
        facts = self._facts.setdefault(run_id, {})
        if report.pr:
            facts["pr"] = report.pr
        facts["summary"] = report.task_summary
        await self._refresh_headline(run_id, item=item, state=state)
        await self._post_watch_notice(run_id, state, report)
        # Per-run render state is no longer needed.
        self._batchers.pop(run_id, None)
        self._digests.pop(run_id, None)
        self._digest_msg.pop(run_id, None)
        self._digest_last_edit.pop(run_id, None)
        self._status.pop(run_id, None)
        self._status_msg.pop(run_id, None)
        self._status_last_edit.pop(run_id, None)
        self._facts.pop(run_id, None)
        self._runstats.pop(run_id, None)
        self._items.pop(run_id, None)
        self._progress.pop(run_id, None)
        self._steer_last_edit.pop(run_id, None)
        for task in (self._status_task.pop(run_id, None), self._steer_task.pop(run_id, None)):
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
            log.warning("discord.status_edit_failed", run=run_id, exc_info=True)

    # -- steer status note ------------------------------------------------------------

    async def _post_steer_status(self, run_id: str, mid: str, thread: Any) -> None:
        """The "⏳ steer queued — agent is mid-execute on t2 (12/40 tool calls);
        answered at the next checkpoint" note, posted right under the steer
        so the wait is visible, then edited in place as the run moves."""
        with self._lock:
            pending = self._pending.get(mid)
        if pending is None:
            return
        progress = self._progress.setdefault(
            run_id, SteerProgress(cap=self.config.budgets.max_tool_calls_per_phase)
        )
        msg = await self._send(thread, progress.render(state=pending.state))
        if msg is None:
            return
        pending.status = msg
        self._steer_last_edit[run_id] = asyncio.get_event_loop().time()
        if pending.state != "queued":
            # The reply landed while we were posting; catch the note up.
            await self._edit_steer_status(pending, pending.state)

    def _schedule_steer_edit(self, run_id: str) -> None:
        pending = self._steer_task.get(run_id)
        if pending is not None and not pending.done():
            return
        self._steer_task[run_id] = asyncio.ensure_future(self._steer_edit_later(run_id))

    async def _steer_edit_later(self, run_id: str) -> None:
        now = asyncio.get_event_loop().time()
        last = self._steer_last_edit.get(run_id)
        if last is not None and now - last < STATUS_EDIT_MIN_S:
            await asyncio.sleep(STATUS_EDIT_MIN_S - (now - last))
        with self._lock:
            waiting = [
                p for p in self._pending.values() if p.run_id == run_id and p.state == "queued"
            ]
        for pending in waiting:
            await self._edit_steer_status(pending, "queued")
        self._steer_last_edit[run_id] = asyncio.get_event_loop().time()

    async def _edit_steer_status(self, pending: _Pending, state: str) -> None:
        pending.state = state
        if pending.status is None:
            return  # not posted yet; _post_steer_status catches up
        progress = self._progress.get(pending.run_id) or SteerProgress()
        try:
            await pending.status.edit(
                content=_clip(progress.render(state=state), self.discord.max_message_chars)
            )
        except Exception:
            log.warning(
                "discord.steer_status_edit_failed", run=pending.run_id, state=state, exc_info=True
            )

    async def _steer_picked_up(self, event: Event) -> None:
        mid = str(event.data.get("message_id") or "")
        with self._lock:
            pending = self._pending.get(mid)
        if pending is not None:
            await self._edit_steer_status(pending, "answering")

    async def _resolve_reply(self, event: Event) -> None:
        mid = str(event.data.get("message_id") or "")
        with self._lock:
            pending = self._pending.pop(mid, None)
        if pending is None:
            return
        await self._edit_steer_status(pending, "failed" if event.data.get("error") else "answered")
        try:
            channel = self.client.get_channel(pending.thread_id) or await self.client.fetch_channel(
                pending.thread_id
            )
            msg = await channel.fetch_message(pending.discord_message_id)
            await msg.add_reaction("✅")
        except Exception:
            log.warning(
                "discord.steer_react_failed",
                run=pending.run_id,
                message=mid,
                hint="the human never sees the ✅ that their steer landed",
                exc_info=True,
            )

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
            log.warning("discord.work_dropped", reason="scheduled before the asyncio loop was up")

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
                log.error(
                    "discord.channel_unreachable",
                    channel=cid,
                    error=str(exc),
                    hint="check [discord] channel_id (right-click the channel → Copy Channel "
                    "ID) and that the bot is invited with View Channel; chronology is off "
                    "until the daemon restarts",
                )
            return None

    async def _send(
        self,
        target: Any,
        text: str = "",
        *,
        embed: EmbedSpec | None = None,
        suppress_embeds: bool = False,
        reply_to: Any = None,
        mention_users: bool = False,
    ) -> Any:
        """The single send seam: content is clipped, mentions are always
        disabled (agent prose can contain @everyone), embeds are converted
        here and dropped — text-only retry — if Discord rejects them.
        ``reply_to`` threads the message under a human's message
        (``send(reference=…)``); if Discord rejects the reference the text
        is sent plainly instead."""
        content = _clip(text, self.discord.max_message_chars) if text else None
        kwargs: dict[str, Any] = {}
        if reply_to is not None:
            kwargs["reference"] = reply_to
            kwargs["mention_author"] = False
        mentions = _allowed_mentions_users() if mention_users else _allowed_mentions_none()
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
            target_id = getattr(target, "id", None)
            if "reference" in kwargs:
                # A deleted/unknown message reference fails the send outright;
                # answer in the channel instead of not at all.
                kwargs.pop("reference")
                kwargs.pop("mention_author", None)
                try:
                    return await target.send(content, **kwargs)
                except Exception:
                    log.warning("discord.reply_send_failed", target=target_id, exc_info=True)
                    return None
            if "embed" in kwargs and embed is not None:
                log.warning(
                    "discord.embed_send_failed",
                    target=target_id,
                    action="retrying text-only",
                    exc_info=True,
                )
                kwargs.pop("embed")
                fallback = content or _clip(embed.as_text(), self.discord.max_message_chars)
                try:
                    return await target.send(fallback, **kwargs)
                except Exception:
                    log.warning(
                        "discord.send_failed",
                        target=target_id,
                        chars=len(fallback),
                        text_only_retry=True,
                        exc_info=True,
                    )
                    return None
            log.warning(
                "discord.send_failed",
                target=target_id,
                chars=len(content or ""),
                exc_info=True,
            )
            return None

    async def _send_channel(
        self, text: str, *, embed: EmbedSpec | None = None, mentions: bool = False
    ) -> None:
        channel = await self._channel()
        if channel is not None:
            await self._send(channel, text, embed=embed, mention_users=mentions)

    async def _react(self, message: Any, emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except Exception:
            log.debug("discord.react_failed", emoji=emoji, exc_info=True)

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
                log.warning("discord.thread_lost", run=run_id, thread=thread_id, exc_info=True)
                return None
        with self._lock:
            item = self._items.get(run_id)
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
            log.info(
                "discord.thread_created",
                run=run_id,
                item=item.item_id,
                thread=int(thread.id),
                headline=int(headline.id),
            )
            return thread
        except Exception:
            log.warning("discord.thread_create_failed", run=run_id, exc_info=True)
            return None

    async def _refresh_headline(
        self, run_id: str, *, item: WorkItem | None = None, state: str | None = None
    ) -> None:
        """Re-render the headline (text + card) with everything known so far."""
        if item is None:
            with self._lock:
                item = self._items.get(run_id)
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
                pr=facts.get("pr"),
                requested_by=item.requested_by,
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
            log.warning("discord.headline_edit_failed", run=run_id, exc_info=True)

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
            log.info(
                "discord.connected",
                user=str(client.user),
                guilds=len(getattr(client, "guilds", ()) or ()),
                channel=bridge.discord.channel_id,
            )
            bridge.mark_ready()

        async def on_disconnect() -> None:
            log.warning("discord.disconnected", hint="discord.py reconnects on its own")

        async def on_resumed() -> None:
            log.info("discord.resumed")

        async def on_message(message: Any) -> None:
            bridge._handle_message(message)

        # discord.py's `@client.event` registers by function name; calling it
        # directly is the same registration without an untyped decorator.
        client.event(on_ready)
        client.event(on_disconnect)
        client.event(on_resumed)
        client.event(on_message)
        return client


class _NoTyping:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


def _typing(channel: Any) -> Any:
    """``channel.typing()`` when the client offers it, else a no-op context."""
    typing = getattr(channel, "typing", None)
    if callable(typing):
        try:
            return typing()
        except Exception:
            return _NoTyping()
    return _NoTyping()


def _tool_call_summary(name: str, args: dict[str, Any]) -> str:
    """``sbx_control(status)`` / ``run_detail(r1abc)`` — the one-argument
    shape covers most concierge tools; anything else is key=value."""
    if not args:
        return f"{name}()"
    if len(args) == 1:
        (value,) = args.values()
        return f"{name}({_one_line(str(value), 40)})"
    inner = ", ".join(f"{k}={_one_line(str(v), 24)}" for k, v in args.items())
    return f"{name}({_one_line(inner, 60)})"


def _author_id(message: Any) -> str | None:
    """The author's numeric Discord id, which is what an ``<@id>`` mention
    needs; `_author_name` stays the display/attribution form (backticked, so
    it never becomes a GitHub mention). None when the message has no author
    id (a webhook, or a fake message in tests)."""
    author = getattr(message, "author", None)
    ident = getattr(author, "id", None)
    return str(ident) if ident is not None else None


def _author_name(message: Any) -> str:
    """Who sent a control-channel command, for attribution on the source
    (GitHub comment) and in the finish card. Username over display name (it
    is stable), in backticks so a GitHub comment never @-mentions whoever
    happens to own that handle on GitHub."""
    author = getattr(message, "author", None)
    name = getattr(author, "name", None) or getattr(author, "display_name", None)
    return f"Discord user `{name}`" if name else "a Discord operator"


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


def _allowed_mentions_users() -> Any:
    """Mentions of explicitly named users only (never @everyone/@here or
    roles) — what a run-watch ping needs to actually reach the requester."""
    try:
        import discord as discordpy
    except ImportError:
        return None
    return discordpy.AllowedMentions(everyone=False, roles=False, users=True)


def _allowed_mentions_none() -> Any:
    try:
        import discord as discordpy
    except ImportError:
        return None
    return discordpy.AllowedMentions.none()
