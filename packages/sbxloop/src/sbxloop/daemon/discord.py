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

import os
from typing import Any, ClassVar

from sbxloop.config import ChatBackend, Config, DiscordConfig
from sbxloop.daemon.chat import ChatBridge, Inbound, _ConciergeTurn, _NoTyping, _Pending
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
