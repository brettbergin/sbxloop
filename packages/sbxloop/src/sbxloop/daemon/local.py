"""LocalBridge: the daemon's human channel for the operator console.

The service-agnostic bridge — event pump, chronology rendering, steering,
run watches, concierge turns, clarifying questions, the merge gate and
``!sbx`` commands — is :class:`sbxloop.daemon.chat.ChatBridge`; this
module is its transport for ``sbxloop tui``. There is no service to dial:
the transport is a **mailbox in the daemon's own ``state.db``**
(``daemon_local_messages``). Every message the bridge would have posted to
Discord becomes a row, an edit rewrites the row, a reaction decorates it,
and what an operator types in the console arrives as a row the bridge
claims — the same file-drop shape as the ctl queue, for the same reasons
(no socket, no new dependency, inspectable, survives a detached console).

How the console's shapes map onto the bridge's:

* The *control channel* is ``"control"``; a *run thread* is
  ``"thread:<headline row id>"``. Both are plain ids the console renders as
  screens, so ``_create_thread`` costs no row.
* A message handle is the row id (text form for the bridge, so the same
  id addresses an inbound row for a reaction and an outbound row for an
  edit).
* "Addressing the bot" is the literal ``@sbx`` token in the text, or a
  reply to one of the bot's rows — the two gestures
  :func:`~sbxloop.daemon.chat_routing.route_message` already understands,
  so the console inserts the token and nothing about routing changes.
* Clicking a choice or the approve button is an inbound row of kind
  ``choice`` / ``approve`` replying to the question or prompt row — the
  twins of the Discord view handlers — and lands on the same bridge paths.
* Rows pending when the bridge starts, and rows stamped before it did, are
  refused with a note rather than executed, exactly as the ctl queue
  refuses a stale request: a ``cancel`` typed while the daemon was down
  must not fire at boot.
* Every store call runs off the bridge's asyncio loop (one executor thread,
  so sends and their edits stay ordered), as Discord's I/O is awaited: a
  contended SQLite write must not freeze the pump and inbound dispatch.

The bridge is always on: there is no credential, no channel to configure,
and :func:`sbxloop.daemon.fanout.build_frontend` runs it beside whatever
``[chat] backend`` names. ``[tui]`` carries its few knobs.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, ClassVar

from sbxloop.config import TUI_CONTROL_CHANNEL, BridgeBackend, Config, TuiConfig
from sbxloop.daemon.chat import ChatBridge, Inbound
from sbxloop.daemon.chat_choices import ChoiceQuestion, render_prose
from sbxloop.daemon.concierge import Concierge
from sbxloop.daemon.discord_format import EmbedSpec, _clip, embed_to_json
from sbxloop.daemon.store import LOCAL_STARTED_KEY, ChatThread, DaemonStore, LocalMessage, MergeGate
from sbxloop.gc import DAY_S
from sbxloop.log import get_logger

log = get_logger(__name__)

#: What ``_bot_user_id`` answers and what the mention token names.
LOCAL_BOT_ID = "sbx"
#: A run thread's channel id: this prefix plus the headline row's id.
LOCAL_THREAD_PREFIX = "thread:"
#: ``@sbx`` in text, as a standalone token; group 1 is the mentioned id,
#: the shape :func:`~sbxloop.daemon.chat_routing.strip_mentions` expects.
LOCAL_MENTION_RE = re.compile(r"(?<![\w@.])@(sbx)\b")
#: How often the bridge looks for inbound rows (the ctl server polls at 0.5s).
LOCAL_POLL_S = 0.25
#: How often ``daemon_state`` learns the bridge is alive.
LOCAL_HEARTBEAT_S = 5.0
#: Inbound rows claimed per poll.
LOCAL_INBOUND_BATCH = 50
#: The note left under a row that predates this bridge's start.
STALE_INBOUND_NOTE = (
    "ignored: this was sent before the daemon started, so it was not executed — "
    "send it again if you still want it"
)
#: The note under a click on a question the bridge no longer holds.
EXPIRED_CLICK_NOTE = "that question has expired or was already answered — type your answer"
#: Ids other backends spell: a Discord snowflake, a Slack member id. The
#: console's operator ids are login names, which look like neither.
_FOREIGN_ID_RE = re.compile(r"^(\d{15,}|[UW][A-Z0-9]{8,})$")


@dataclass(frozen=True)
class LocalTarget:
    """A place to post: the control channel or a run thread."""

    id: str


@dataclass(frozen=True)
class LocalRef:
    """A row we posted or were sent — what an edit or a reaction addresses."""

    id: int
    channel_id: str


class LocalBridge(ChatBridge):
    backend: ClassVar[BridgeBackend] = "local"
    label: ClassVar[str] = "TUI"
    mention_re: ClassVar[re.Pattern[str]] = LOCAL_MENTION_RE

    def __init__(
        self,
        config: Config,
        dstore: DaemonStore,
        *,
        loop_ref: Any = None,
        client_factory: Any = None,
        concierge: Concierge | None = None,
        clock: Any = time.time,
    ) -> None:
        self.clock = clock
        # The stale cutoff: stamped once, at construction — the daemon
        # process is up from here, whatever a gateway connect costs later.
        self._started_at: float = clock()
        super().__init__(
            config, dstore, loop_ref=loop_ref, client_factory=client_factory, concierge=concierge
        )
        assert isinstance(self.chat, TuiConfig)
        self.tui: TuiConfig = self.chat
        # One thread for every store call from the asyncio loop: off the
        # loop, and in order.
        self._io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sbxloop-local-io")

    # -- lifecycle ---------------------------------------------------------------

    def _check_credentials(self) -> None:
        return None

    @staticmethod
    def _default_client(bridge: Any) -> Any:
        return None

    async def _io_call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.get_event_loop().run_in_executor(
            self._io, functools.partial(fn, *args, **kwargs)
        )

    async def _run_client(self) -> None:
        """The poller: claim inbound rows, keep the heartbeat, prune."""
        assert self._stop_evt is not None
        try:
            await self._io_call(
                self.dstore.set_value, LOCAL_STARTED_KEY, repr(float(self._started_at))
            )
            # Pending at boot means typed while no daemon was reading: refused,
            # whatever the row's own stamp says (the ctl queue's boot sweep).
            pending = await self._io_call(self.dstore.take_local_inbound, self.clock())
            for row in pending:
                self._refuse_stale(row)
        except Exception:
            self.log.warning("local.start_bookkeeping_failed", exc_info=True)
        self.mark_ready()
        last_beat = 0.0
        last_prune = 0.0
        while not self._stop_evt.is_set():
            try:
                rows = await self._io_call(self.dstore.take_local_inbound, self.clock())
            except Exception:
                self.log.warning("local.poll_failed", exc_info=True)
                rows = []
            for row in rows:
                try:
                    self._dispatch_inbound(row)
                except Exception:
                    self.log.warning("local.inbound_failed", id=row.id, exc_info=True)
            now = self.clock()
            if now - last_beat >= LOCAL_HEARTBEAT_S:
                with contextlib.suppress(Exception):
                    await self._io_call(self.dstore.set_local_heartbeat, now)
                last_beat = now
            if now - last_prune >= DAY_S:
                await self._io_call(self._prune, now)
                last_prune = now
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_evt.wait(), timeout=LOCAL_POLL_S)

    def _prune(self, now: float) -> None:
        days = float(self.tui.retention_days)
        if days <= 0:
            return
        try:
            dropped = self.dstore.prune_local_messages(now - days * DAY_S)
        except Exception:
            self.log.warning("local.prune_failed", exc_info=True)
            return
        if dropped:
            self.log.info("local.pruned", rows=dropped, retention_days=days)

    async def _close_client(self) -> None:
        self._io.shutdown(wait=False)

    def _bot_user_id(self) -> str | None:
        return LOCAL_BOT_ID

    def _owns_user_id(self, user_id: str) -> bool:
        """A login name is the console's; a snowflake or a Slack id is not."""
        return not _FOREIGN_ID_RE.match(user_id)

    # -- inbound -----------------------------------------------------------------

    def _refuse_stale(self, row: LocalMessage) -> None:
        self.log.info("local.inbound_stale", id=row.id, kind=row.kind)
        self._post_sync(row.channel_id, STALE_INBOUND_NOTE, reply_to_id=row.id)

    def _dispatch_inbound(self, row: LocalMessage) -> None:
        """One claimed row: a typed message takes the ordinary route; a
        click on a choice or the approve button takes the side door its
        Discord twin does."""
        if row.created_at < self._started_at:
            self._refuse_stale(row)
            return
        author = f"{self.label} user `{row.author_name}`"
        if row.kind == "choice":
            if row.reply_to_id is not None and self._answer_choice(
                str(row.reply_to_id),
                row.text,
                author,
                author_id=row.author_id,
                author_name=row.author_name,
            ):
                self._record_choice_answer(row.reply_to_id, row.text, author)
            else:
                self._post_sync(row.channel_id, EXPIRED_CLICK_NOTE, reply_to_id=row.id)
            return
        if row.kind == "approve":
            self._schedule(self._approve_click(row, author))
            return
        self._handle_message(row)

    def _record_choice_answer(self, question_id: int, value: str, by: str) -> None:
        """Rewrite the question row the way the Discord view disables its
        buttons: the answer and who gave it, in the body and in the spec."""
        question = self.dstore.local_message(question_id)
        if question is None:
            return
        spec: dict[str, Any] = {}
        if question.choices_json:
            with contextlib.suppress(ValueError):
                spec = dict(json.loads(question.choices_json))
        spec["answered"] = {"value": value, "by": by, "at": self.clock()}
        try:
            self.dstore.local_edit(
                question_id,
                f"{question.text}\n\n_Answered: **{value}** (by {by})._",
                now=self.clock(),
                choices_json=json.dumps(spec),
            )
        except Exception:
            self.log.debug("local.choice_record_failed", id=question_id, exc_info=True)

    async def _approve_click(self, row: LocalMessage, author: str) -> None:
        """The approve button: one call into ``DaemonLoop.approve_merge``
        off the bridge loop, answered under the click."""
        prompt = (
            await self._io_call(self.dstore.local_message, row.reply_to_id)
            if row.reply_to_id
            else None
        )
        run_id = prompt.gate_run_id if prompt is not None else None
        if run_id is None:
            reply = "that merge prompt is no longer open — `!sbx merge <item>` still works"
        elif self.loop_ref is None:
            reply = "daemon loop not attached"
        else:
            try:
                reply = await asyncio.get_event_loop().run_in_executor(
                    None, functools.partial(self.loop_ref.approve_merge, run_id, by=author)
                )
            except (KeyError, ValueError) as exc:
                reply = f"merge failed: {exc.args[0] if exc.args else exc}"
            except Exception:
                self.log.warning("local.gate_click_failed", run=run_id, exc_info=True)
                reply = "something went wrong approving the merge — `!sbx merge` still works"
        await self._send(
            LocalTarget(row.channel_id), str(reply), reply_to=LocalRef(row.id, row.channel_id)
        )

    def _inbound(self, message: Any) -> Inbound | None:
        row: LocalMessage = message
        if row.direction != "in":
            return None
        return Inbound(
            content=row.text,
            channel_id=row.channel_id,
            message_id=str(row.id),
            author_id=row.author_id,
            author_name=row.author_name,
            author_is_bot=False,
            mentioned_ids=frozenset(LOCAL_MENTION_RE.findall(row.text)),
            reply_to_bot=row.reply_to_direction == "out",
            channel=LocalTarget(row.channel_id),
            raw=LocalRef(row.id, row.channel_id),
            reply_to_id=None if row.reply_to_id is None else str(row.reply_to_id),
        )

    # -- handles -----------------------------------------------------------------

    async def _control_channel(self) -> Any:
        return LocalTarget(TUI_CONTROL_CHANNEL)

    async def _thread_handle(self, thread_id: str) -> Any:
        return LocalTarget(thread_id)

    async def _fetch_message(self, channel: Any, message_id: str) -> Any:
        try:
            row_id = int(message_id)
        except (TypeError, ValueError):
            return None
        row = await self._io_call(self.dstore.local_message, row_id)
        return None if row is None else LocalRef(row.id, row.channel_id)

    # -- output ------------------------------------------------------------------

    def _post_sync(self, channel_id: str, text: str, *, reply_to_id: int | None = None) -> None:
        try:
            self.dstore.local_post(channel_id, text, now=self.clock(), reply_to_id=reply_to_id)
        except Exception:
            self.log.warning("local.post_failed", channel=channel_id, exc_info=True)

    async def _send(
        self,
        target: Any,
        text: str = "",
        *,
        embed: EmbedSpec | None = None,
        reply_to: Any = None,
        mention_users: bool = False,
    ) -> Any:
        return await self._io_call(
            self._post, target, text, embed=embed, reply_to=reply_to, mention_users=mention_users
        )

    def _post(
        self,
        target: Any,
        text: str,
        *,
        kind: str = "message",
        embed: EmbedSpec | None = None,
        reply_to: Any = None,
        mention_users: bool = False,
        choices_json: str | None = None,
        gate_run_id: str | None = None,
    ) -> LocalRef | None:
        limit = self.tui.max_message_chars
        body = _clip(text, limit) if text else ""
        embed_json: str | None = None
        if embed is not None and self.tui.embeds:
            embed_json = embed_to_json(embed.clamped())
        elif embed is not None and not body:
            body = _clip(embed.as_text(), limit)
        if not body and embed_json is None:
            return None
        try:
            row_id = self.dstore.local_post(
                target.id,
                body,
                now=self.clock(),
                kind=kind,
                embed_json=embed_json,
                choices_json=choices_json,
                gate_run_id=gate_run_id,
                reply_to_id=getattr(reply_to, "id", None),
                mention_users=mention_users,
            )
        except Exception:
            self.log.warning("local.send_failed", target=target.id, chars=len(body), exc_info=True)
            return None
        return LocalRef(row_id, target.id)

    async def _send_choices(
        self,
        target: Any,
        text: str,
        question: ChoiceQuestion,
        *,
        reply_to: Any = None,
        pending_key: str | None = None,
        mention_users: bool = False,
    ) -> Any:
        """The numbered prose every backend posts, plus the choices as data
        so the console renders one button per answer. A click comes back as
        a ``choice`` row replying to this one; typing works regardless."""
        body = render_prose(question)
        if text and text.strip() and text.strip() != question.prompt.strip():
            body = f"{text.strip()}\n\n{body}"
        spec = {
            "prompt": question.prompt,
            "choices": [{"value": c.value, "label": c.label} for c in question.choices],
            "allow_free_text": question.allow_free_text,
            "expires_at": self.clock() + self._question_ttl_s,
            "answered": None,
        }
        return await self._io_call(
            self._post,
            target,
            body,
            kind="choices",
            reply_to=reply_to,
            mention_users=mention_users,
            choices_json=json.dumps(spec),
        )

    async def _edit(self, message: Any, text: str, *, embed: EmbedSpec | None = None) -> None:
        embed_json = (
            embed_to_json(embed.clamped()) if embed is not None and self.tui.embeds else None
        )
        ok = await self._io_call(
            self.dstore.local_edit,
            message.id,
            _clip(text, self.tui.max_message_chars),
            now=self.clock(),
            embed_json=embed_json,
        )
        if not ok:
            raise LookupError(f"no such message {message.id}")

    async def _add_reaction(self, message: Any, emoji: str) -> None:
        if not await self._io_call(self.dstore.local_react, message.id, emoji, now=self.clock()):
            raise LookupError(f"no such message {message.id}")

    async def _create_thread(self, headline: Any, name: str) -> Any:
        # The headline anchors the thread: no row, no name to store.
        return LocalTarget(f"{LOCAL_THREAD_PREFIX}{headline.id}")

    def _message_id(self, message: Any) -> str:
        return str(getattr(message, "id", "") or "")

    def _handle_id(self, target: Any) -> str:
        return str(getattr(target, "id", "") or "")

    def thread_link(self, thread: ChatThread) -> str:
        return f"<#{thread.thread_id}>"

    def mention_user(self, user_id: str) -> str:
        return f"@{user_id}"

    # -- the merge gate ----------------------------------------------------------

    async def _send_gate(self, target: Any, text: str, gate: MergeGate) -> Any:
        """The prose prompt, marked with the run whose gate it approves so
        the console offers the button while the gate stands."""
        return await self._io_call(
            self._post, target, text, kind="gate", mention_users=True, gate_run_id=gate.run_id
        )

    async def _finalize_gate_message(self, message: Any, text: str) -> None:
        await self._edit(message, text)
        await self._io_call(self.dstore.local_clear_gate, message.id, now=self.clock())


__all__ = [
    "EXPIRED_CLICK_NOTE",
    "LOCAL_BOT_ID",
    "LOCAL_HEARTBEAT_S",
    "LOCAL_MENTION_RE",
    "LOCAL_POLL_S",
    "LOCAL_THREAD_PREFIX",
    "STALE_INBOUND_NOTE",
    "LocalBridge",
    "LocalRef",
    "LocalTarget",
]
