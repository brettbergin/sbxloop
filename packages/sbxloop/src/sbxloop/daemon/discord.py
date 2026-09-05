"""DiscordBridge: the daemon's human channel on Discord.

The service-agnostic bridge — event pump, chronology rendering, steering,
run watches, concierge turns, ``!sbx`` commands — is
:class:`sbxloop.daemon.chat.ChatBridge`; this module is the Discord fifth
of it: a discord.py gateway client, the send/edit/react/thread primitives,
the mapping of a discord.py ``Message`` onto :class:`~sbxloop.daemon.chat.Inbound`,
and the ``<#thread>`` spelling of a thread pointer.

``discord.py`` is an optional extra (``sbxloop[discord]``); the import is
deferred and its absence surfaces as an actionable error, the same pattern
as the ``[copilot]`` extra behind ``sbxloop list-models``. The bot token
comes from ``DISCORD_BOT_TOKEN``.
"""

from __future__ import annotations

import asyncio
import functools
import os
from typing import Any, ClassVar

from sbxloop.config import ChatBackend, Config, DiscordConfig
from sbxloop.daemon.chat import (
    CHOICE_QUESTION_TTL_S,
    ChatBridge,
    Inbound,
    _ConciergeTurn,
    _NoTyping,
    _Pending,
)
from sbxloop.daemon.chat_choices import ChoiceQuestion, render_prose
from sbxloop.daemon.chat_routing import DISCORD_MENTION_RE
from sbxloop.daemon.concierge import Concierge
from sbxloop.daemon.discord_format import (
    DISCORD_MAX_MESSAGE,
    EmbedSpec,
    _clip,
    format_for_discord,
    headline_text,
    no_unfurl,
)
from sbxloop.daemon.store import ChatThread, DaemonStore
from sbxloop.errors import DaemonError
from sbxloop.log import get_logger

log = get_logger(__name__)

TOKEN_ENV = "DISCORD_BOT_TOKEN"  # nosec B105 - env var name, not a secret
INSTALL_HINT = (
    "discord.py is not installed on this host — install it with "
    "`pip install 'sbxloop[discord]'` to enable the daemon's Discord bridge"
)

# Re-exported for callers/tests that import the formatting names from here.
__all__ = [
    "DISCORD_MAX_MESSAGE",
    "DiscordBridge",
    "_ConciergeTurn",
    "_Pending",
    "_clip",
    "format_for_discord",
    "headline_text",
]


class DiscordBridge(ChatBridge):
    """Runs a discord.py client on its own thread; the daemon loop calls
    the ``Frontend`` methods from its threads and never blocks on Discord.

    ``client_factory`` builds the client (tests inject a recorder); the
    default imports discord.py lazily.
    """

    backend: ClassVar[ChatBackend] = "discord"
    label: ClassVar[str] = "Discord"
    mention_re = DISCORD_MENTION_RE

    def __init__(
        self,
        config: Config,
        dstore: DaemonStore,
        *,
        loop_ref: Any = None,
        client_factory: Any = None,
        token: str | None = None,
        concierge: Concierge | None = None,
    ) -> None:
        super().__init__(
            config,
            dstore,
            loop_ref=loop_ref,
            client_factory=client_factory,
            concierge=concierge,
        )
        self.discord: DiscordConfig = config.discord
        self.token = token if token is not None else os.environ.get(TOKEN_ENV, "")

    # -- transport seams ------------------------------------------------------------

    def _check_credentials(self) -> None:
        if not self.token:
            raise DaemonError(
                f"[discord] is configured but {TOKEN_ENV} is not set; export it (or put it "
                "in the project .env) — never in sbxloop.toml"
            )

    async def _run_client(self) -> None:
        await self.client.start(self.token)

    async def _close_client(self) -> None:
        await self.client.close()

    def _bot_user_id(self) -> str | None:
        user = getattr(self.client, "user", None)
        uid = getattr(user, "id", None)
        return str(int(uid)) if uid is not None else None

    def _inbound(self, message: Any) -> Inbound | None:
        raw = getattr(getattr(message, "channel", None), "id", None)
        channel_id = str(int(raw)) if raw is not None else None
        author = getattr(message, "author", None)
        author_is_bot = bool(getattr(author, "bot", False))
        mid = getattr(message, "id", None)
        return Inbound(
            content=str(getattr(message, "content", "") or ""),
            channel_id=channel_id,
            message_id=str(int(mid)) if mid else "",
            author_id=_author_id(message),
            author_name=getattr(author, "name", None) or getattr(author, "display_name", None),
            author_is_bot=author_is_bot,
            mentioned_ids=frozenset(
                str(int(m.id)) for m in getattr(message, "mentions", None) or () if hasattr(m, "id")
            ),
            reply_to_bot=not author_is_bot and self._is_reply_to_bot(message),
            channel=getattr(message, "channel", None),
            raw=message,
            reply_to_id=_reference_id(message),
        )

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
                return str(int(author_id)) == bot_id
        return False

    async def _control_channel(self) -> Any:
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

    async def _thread_handle(self, thread_id: str) -> Any:
        """The thread object for a persisted thread id, or None."""
        client = self.client
        tid = int(thread_id)
        thread = client.get_channel(tid) if client is not None else None
        if thread is None and client is not None:
            thread = await client.fetch_channel(tid)
        return thread

    async def _fetch_message(self, channel: Any, message_id: str) -> Any:
        return await channel.fetch_message(int(message_id))

    async def _send(
        self,
        target: Any,
        text: str = "",
        *,
        embed: EmbedSpec | None = None,
        reply_to: Any = None,
        mention_users: bool = False,
    ) -> Any:
        """The single send seam: content is clipped, mentions are always
        disabled (agent prose can contain @everyone), embeds are converted
        here and dropped — text-only retry — if Discord rejects them.
        ``reply_to`` threads the message under a human's message
        (``send(reference=…)``); if Discord rejects the reference the text
        is sent plainly instead.

        Link previews never survive this seam: unless the message carries
        one of our own embeds, ``suppress_embeds=True`` is always set so
        Discord renders no auto-generated unfurl. When one of our embeds
        *is* attached the flag would hide it too, so the text body is run
        through ``no_unfurl`` instead."""
        kwargs: dict[str, Any] = {}
        if reply_to is not None:
            kwargs["reference"] = reply_to
            kwargs["mention_author"] = False
        mentions = _allowed_mentions_users() if mention_users else _allowed_mentions_none()
        if mentions is not None:
            kwargs["allowed_mentions"] = mentions
        converted = _to_embed(embed) if (embed is not None and self.discord.embeds) else None
        if converted is not None:
            kwargs["embed"] = converted
            text = no_unfurl(text) if text else text
        else:
            kwargs["suppress_embeds"] = True
        content = _clip(text, self.discord.max_message_chars) if text else None
        if converted is None and embed is not None and not content:
            content = _clip(no_unfurl(embed.as_text()), self.discord.max_message_chars)
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
                kwargs["suppress_embeds"] = True
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
        """Post a clarifying question with one clickable button per choice.

        The message body is always the same numbered prose the base seam
        posts, so every failure mode here — discord.py without component
        support, a send Discord rejects because of the view, a view that
        times out, a click that arrives after the bridge deadline — leaves
        a question that is still answerable by typing. Nothing about the
        typed path changes: the buttons are an extra way in, not the only
        one.
        """
        body = render_prose(question)
        if text and text.strip() and text.strip() != question.prompt.strip():
            body = f"{text.strip()}\n\n{body}"
        view = _build_choice_view(
            self, question, timeout=self._question_ttl_s, pending_key=pending_key
        )
        if view is None:
            # No component support on this host: prose, exactly as before.
            return await self._send(target, body, reply_to=reply_to, mention_users=mention_users)

        kwargs: dict[str, Any] = {"view": view, "suppress_embeds": True}
        if reply_to is not None:
            kwargs["reference"] = reply_to
            kwargs["mention_author"] = False
        mentions = _allowed_mentions_users() if mention_users else _allowed_mentions_none()
        if mentions is not None:
            kwargs["allowed_mentions"] = mentions
        content = _clip(body, self.discord.max_message_chars)
        posted = None
        failure: BaseException | None = None
        try:
            posted = await target.send(content, **kwargs)
        except Exception as exc:
            failure = exc
            if "reference" in kwargs:
                kwargs.pop("reference")
                kwargs.pop("mention_author", None)
                try:
                    posted = await target.send(content, **kwargs)
                    failure = None
                except Exception as retry_exc:
                    failure = retry_exc
                    posted = None
        if posted is None:
            log.warning(
                "discord.choices_view_send_failed",
                target=getattr(target, "id", None),
                choices=len(question.choices),
                action="falling back to free-text prose",
                error=str(failure) if failure is not None else None,
                # The traceback must be taken from the caught exception: by
                # here `sys.exc_info()` is cleared, so `exc_info=True` would
                # log "NoneType: None" instead of why the send failed.
                exc_info=failure,
            )
            return await self._send(target, body, reply_to=reply_to, mention_users=mention_users)
        bind = getattr(view, "bind", None)
        if bind is not None:
            try:
                bind(posted)
            except Exception:  # pragma: no cover - defensive
                log.debug("discord.choices_bind_failed", exc_info=True)
        return posted

    async def _send_gate(self, target: Any, text: str, gate: Any) -> Any:
        """The approval prompt with a persistent ✅ button on top of the
        base prose — the typed command stays in the body, so every failure
        mode (no component support, a rejected send, a dead view) leaves a
        prompt that still works by typing."""
        view = _build_gate_view(self, gate)
        if view is None:
            return await super()._send_gate(target, text, gate)
        kwargs: dict[str, Any] = {"view": view, "suppress_embeds": True}
        mentions = _allowed_mentions_users()
        if mentions is not None:
            kwargs["allowed_mentions"] = mentions
        content = _clip(text, self.discord.max_message_chars)
        try:
            return await target.send(content, **kwargs)
        except Exception:
            log.warning(
                "discord.gate_view_send_failed",
                run=gate.run_id,
                action="falling back to prose",
                exc_info=True,
            )
            return await super()._send_gate(target, text, gate)

    async def _finalize_gate_message(self, message: Any, text: str) -> None:
        """A resolved gate clears its button along with the rewrite."""
        try:
            await message.edit(content=_clip(text, self.discord.max_message_chars), view=None)
        except TypeError:
            await super()._finalize_gate_message(message, text)

    def _register_gate_views(self, client: Any) -> None:
        """Re-arm the approve buttons for standing gates on (re)connect.

        Persistent views (``timeout=None`` + a stable ``custom_id``) only
        fire while the running client knows them — precisely the property
        #570's choice views lack, and why those buttons die on restart.
        ``approving`` gates are armed too: their click loses the CAS
        politely now and works again the moment boot reconciliation puts
        the gate back up."""
        add_view = getattr(client, "add_view", None)
        if add_view is None:
            return
        try:
            gates = self.dstore.open_merge_gates()
        except Exception:
            log.warning("discord.gate_views_unavailable", exc_info=True)
            return
        armed = getattr(self, "_armed_gate_views", None)
        if armed is None:
            armed = set()
            self._armed_gate_views = armed
        for gate in gates:
            if gate.custom_id in armed:
                continue
            view = _build_gate_view(self, gate)
            if view is None:
                return
            try:
                where = self.dstore.gate_prompt(gate.run_id, self.backend)
                if where is not None and where[1].isdigit():
                    add_view(view, message_id=int(where[1]))
                else:
                    add_view(view)
                armed.add(gate.custom_id)
            except Exception:
                log.warning("discord.gate_view_rearm_failed", run=gate.run_id, exc_info=True)

    async def _edit(self, message: Any, text: str, *, embed: EmbedSpec | None = None) -> None:
        """Edit a message we posted, re-asserting unfurl suppression.

        discord.py drops the ``SUPPRESS_EMBEDS`` flag unless the edit
        re-asserts it (``suppress=True``), so a link preview would come
        back on the first edit of a live status/digest/note message. When
        the message carries one of our own embeds the flag would hide it,
        so the body is angle-bracketed via ``no_unfurl`` instead. Errors
        propagate: callers already log them with their own context."""
        converted = _to_embed(embed) if (embed is not None and self.discord.embeds) else None
        kwargs: dict[str, Any] = {}
        if converted is not None:
            kwargs["embed"] = converted
            body = no_unfurl(text)
        else:
            kwargs["suppress"] = True
            body = text
        kwargs["content"] = _clip(body, self.discord.max_message_chars)
        try:
            await message.edit(**kwargs)
        except TypeError:
            # Message object without a ``suppress`` keyword: fall back to
            # angle-bracketing so the preview still cannot appear.
            kwargs.pop("suppress", None)
            kwargs["content"] = _clip(no_unfurl(text), self.discord.max_message_chars)
            await message.edit(**kwargs)

    async def _add_reaction(self, message: Any, emoji: str) -> None:
        await message.add_reaction(emoji)

    async def _create_thread(self, headline: Any, name: str) -> Any:
        return await headline.create_thread(name=name)

    def _message_id(self, message: Any) -> str:
        mid = getattr(message, "id", None)
        return str(int(mid)) if mid else ""

    def _handle_id(self, target: Any) -> str:
        tid = getattr(target, "id", None)
        return str(int(tid)) if tid else ""

    def thread_link(self, thread: ChatThread) -> str:
        return f"<#{thread.thread_id}>"

    def _typing(self, channel: Any) -> Any:
        return _typing_context(channel)

    # -- default client -----------------------------------------------------------------

    @staticmethod
    def _default_client(bridge: Any) -> Any:
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
            # Arm the merge-gate approve buttons before anything can click.
            bridge._register_gate_views(client)
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


def _typing_context(channel: Any) -> Any:
    """``channel.typing()`` when the client offers it, else a no-op context."""
    typing = getattr(channel, "typing", None)
    if callable(typing):
        try:
            return typing()
        except Exception:
            return _NoTyping()
    return _NoTyping()


def _reference_id(message: Any) -> str | None:
    """The id of the message this one is a reply to, or None.

    discord.py exposes it on ``message.reference.message_id`` even when the
    referenced message itself was not resolved, which is precisely the case
    that matters: an answer to a clarifying question must be matched to the
    question it replies to, not to whichever question was posted last.
    """
    reference = getattr(message, "reference", None)
    mid = getattr(reference, "message_id", None)
    if mid is None:
        resolved = getattr(reference, "resolved", None) or getattr(
            reference, "cached_message", None
        )
        mid = getattr(resolved, "id", None)
    if mid is None:
        return None
    try:
        return str(int(mid))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(mid)


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


# -- clarifying-question components (#564) ----------------------------------------------


EXPIRED_CLICK_NOTE = (
    "That question has expired (or was already answered) — just type your answer "
    "in the channel and I'll pick it up."
)
TIMED_OUT_NOTE = "_These buttons expired — you can still answer by typing._"


class _ChoiceHandler:
    """The transport-free half of a choice view.

    Holds everything the button callbacks need so the discord.py View is a
    thin shell: whether or not the extra is installed, the click and timeout
    behaviour is the same object and is directly testable.
    """

    def __init__(
        self,
        bridge: Any,
        question: ChoiceQuestion,
        body: str = "",
        *,
        pending_key: str | None = None,
    ) -> None:
        self.bridge = bridge
        self.question = question
        self.body = body
        # The key the bridge registered the question under before the send
        # went out. A click that beats ``bind`` still resolves through it.
        self.pending_key = pending_key
        self.message: Any = None
        self.answered = False

    def bind(self, message: Any) -> None:
        self.message = message
        if not self.body:
            self.body = str(getattr(message, "content", "") or "")

    def _message_id(self) -> str:
        if self.message is None:
            return self.pending_key or ""
        try:
            resolved = str(self.bridge._message_id(self.message))
        except Exception:  # pragma: no cover - defensive
            resolved = ""
        return resolved or (self.pending_key or "")

    async def on_click(self, interaction: Any, value: str) -> None:
        """A user clicked a choice. Anyone may click — the asker is not the
        only person who can unblock the bot — but who clicked is recorded.
        A click on a question the bridge no longer holds (expired, or
        already answered) is answered ephemerally with the typed route
        rather than raising or leaving the clicker with a dead button."""
        author = _interaction_author(interaction)
        user = getattr(interaction, "user", None)
        author_name = getattr(user, "name", None) or getattr(user, "display_name", None)
        uid = getattr(user, "id", None)
        message_id = self._message_id()
        accepted = False
        if message_id:
            try:
                accepted = bool(
                    self.bridge._answer_choice(
                        message_id,
                        value,
                        author,
                        author_id=str(uid) if uid is not None else None,
                        author_name=str(author_name) if author_name else None,
                    )
                )
            except Exception:
                log.warning("discord.choice_answer_failed", value=value, exc_info=True)
                accepted = False
        if accepted:
            self.answered = True
            await _ack_interaction(interaction, f"Got it — **{value}**.")
            await self._disable(f"{self.body}\n\n_Answered: **{value}** (by {author})._")
            return
        log.info("discord.choice_click_expired", value=value, by=author)
        await _ack_interaction(interaction, EXPIRED_CLICK_NOTE)

    async def on_timeout(self) -> None:
        """The view's deadline matches the bridge's: when it passes, grey
        the buttons out and say plainly that typing still works."""
        if self.answered:
            return
        await self._disable(f"{self.body}\n\n{TIMED_OUT_NOTE}")

    async def _disable(self, text: str) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(content=_clip(text, DISCORD_MAX_MESSAGE), view=None)
        except TypeError:
            try:
                await self.message.edit(content=_clip(text, DISCORD_MAX_MESSAGE))
            except Exception:
                log.debug("discord.choices_disable_failed", exc_info=True)
        except Exception:
            log.debug("discord.choices_disable_failed", exc_info=True)


async def _ack_interaction(interaction: Any, note: str) -> None:
    """Acknowledge a component interaction; never raise. Discord requires a
    response within three seconds or the click shows as failed, but a failed
    acknowledgement must not lose the answer we already recorded."""
    response = getattr(interaction, "response", None)
    if response is None:
        return
    attempts: tuple[tuple[str, Any], ...] = (
        ("send_message", lambda: response.send_message(note, ephemeral=True)),
        ("defer", lambda: response.defer()),
    )
    for name, attempt in attempts:
        try:
            await attempt()
            return
        except Exception:
            # An ephemeral note is nicer, but a bare defer still clears the
            # click; either failing only costs the acknowledgement, never the
            # answer, so try the next one rather than raising into discord.py.
            log.debug("discord.interaction_ack_attempt_failed", how=name, exc_info=True)
    log.debug("discord.interaction_ack_failed")


def _interaction_author(interaction: Any) -> str:
    user = getattr(interaction, "user", None)
    name = getattr(user, "name", None) or getattr(user, "display_name", None)
    return f"Discord user `{name}`" if name else "a Discord user"


GATE_CUSTOM_ID_PREFIX = "sbxgate:"


class _GateHandler:
    """The transport-free half of a merge-gate approve button.

    A click is one call into ``DaemonLoop.approve_merge`` — store CAS plus
    a spawned gh-ops landing thread — run off the gateway's event loop and
    answered ephemerally with whatever the loop said (an approval, a lost
    CAS, a refusal). The click never disables the view: a failed landing
    re-opens the gate and the same button works again; resolution clears
    it through ``_finalize_gate_message``."""

    def __init__(self, bridge: Any, gate: Any) -> None:
        self.bridge = bridge
        self.gate = gate

    async def on_click(self, interaction: Any) -> None:
        author = _interaction_author(interaction)
        loop_ref = getattr(self.bridge, "loop_ref", None)
        if loop_ref is None:
            await _ack_interaction(interaction, "daemon loop not attached")
            return
        try:
            reply = await asyncio.get_event_loop().run_in_executor(
                None, functools.partial(loop_ref.approve_merge, self.gate.run_id, by=author)
            )
        except (KeyError, ValueError) as exc:
            reply = f"merge failed: {exc.args[0] if exc.args else exc}"
        except Exception:
            log.warning("discord.gate_click_failed", run=self.gate.run_id, exc_info=True)
            reply = "something went wrong approving the merge — `!sbx merge` still works"
        await _ack_interaction(interaction, str(reply))


def _build_gate_view(bridge: Any, gate: Any) -> Any:
    """A persistent one-button approve view, or None when this host's
    discord.py has no component support (the caller posts prose instead).

    Persistent means ``timeout=None`` and a stable ``custom_id`` (the gate
    row's token), the two requirements ``Client.add_view`` re-arming has —
    a gate's button survives restarts and never expires, unlike #570's
    choice buttons."""
    try:
        import discord as discordpy

        view_cls = discordpy.ui.View
        button_cls = discordpy.ui.Button
        style = discordpy.ButtonStyle.success
    except (ImportError, AttributeError):
        return None

    handler = _GateHandler(bridge, gate)

    class _GateView(view_cls):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__(timeout=None)
            self.handler = handler

    try:
        view = _GateView()
        button = button_cls(
            label="Approve merge",
            style=style,
            custom_id=f"{GATE_CUSTOM_ID_PREFIX}{gate.custom_id}"[:100],
        )
        button.callback = handler.on_click
        view.add_item(button)
    except Exception:
        log.warning("discord.gate_view_unavailable", exc_info=True)
        return None
    return view


def _build_choice_view(
    bridge: Any,
    question: ChoiceQuestion,
    *,
    timeout: float = CHOICE_QUESTION_TTL_S,
    pending_key: str | None = None,
) -> Any:
    """A discord.py View of one button per choice, or None when this host's
    discord.py has no component support (the caller posts prose instead)."""
    try:
        import discord as discordpy

        view_cls = discordpy.ui.View
        button_cls = discordpy.ui.Button
        style = discordpy.ButtonStyle.secondary
    except (ImportError, AttributeError):
        return None

    handler = _ChoiceHandler(bridge, question, pending_key=pending_key)

    class _ChoiceView(view_cls):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__(timeout=timeout)
            self.handler = handler

        def bind(self, message: Any) -> None:
            handler.bind(message)

        async def on_timeout(self) -> None:
            await handler.on_timeout()

    try:
        view = _ChoiceView()
        for choice in question.choices:
            button = button_cls(label=_clip(choice.label, 80), style=style, row=0)

            def _callback(interaction: Any, value: str = choice.value) -> Any:
                return handler.on_click(interaction, value)

            button.callback = _callback
            view.add_item(button)
    except Exception:
        log.warning("discord.choice_view_unavailable", exc_info=True)
        return None
    return view


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

        return discordpy.AllowedMentions(everyone=False, roles=False, users=True)
    except (ImportError, AttributeError):
        # No discord.py, or one without AllowedMentions: the send goes out
        # with the transport's defaults rather than failing.
        return None


def _allowed_mentions_none() -> Any:
    try:
        import discord as discordpy

        return discordpy.AllowedMentions.none()
    except (ImportError, AttributeError):
        return None
