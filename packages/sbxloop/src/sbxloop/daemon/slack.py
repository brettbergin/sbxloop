"""SlackBridge: the daemon's human channel on Slack.

The service-agnostic bridge — event pump, chronology rendering, steering,
run watches, concierge turns, ``!sbx`` commands — is
:class:`sbxloop.daemon.chat.ChatBridge`; this module is the Slack fifth of
it: a Socket Mode connection (no public URL, no request signing — the app
dials out, which is what a daemon on a home server needs), the Web API
primitives behind send/edit/react, the mapping of an Events API ``message``
onto :class:`~sbxloop.daemon.chat.Inbound`, and the permalink spelling of
a thread pointer.

How Slack's shapes map onto the bridge's:

* A *run thread* is the reply thread under the run's headline message, so
  the persisted ``thread_id`` **is the headline's ``ts``** — there is no
  separate thread object and no thread name. With ``thread_per_run =
  false`` the thread id is the channel id and everything posts top-level.
* A message handle is ``(channel, ts)``: that is all ``chat.update`` and
  ``reactions.add`` need, so re-attaching after a restart costs no fetch.
* Only ``message`` events from the control channel are handled;
  ``app_mention`` duplicates them and is ignored, edits/joins (any other
  ``subtype``) are ignored, and anything from a bot — this app included —
  is dropped before routing, the same rule as Discord.
* Slack has no "reply to a message" outside threads, so ``reply_to_bot``
  is always false: the concierge and steering are @mention-only here.
  Replies to a concierge question stay top-level in the channel (a Slack
  thread under the question would be neither surface the bot listens on).
* A mention is ``<@U…>``; user handles come from ``users.info`` (cached),
  so attribution reads ``Slack user `ana``` like Discord's.

``slack_sdk`` is an optional extra (``sbxloop[slack]``); the import is
deferred and its absence surfaces as an actionable error. The two tokens
come from ``SLACK_BOT_TOKEN`` (``xoxb-…``) and ``SLACK_APP_TOKEN``
(``xapp-…``) and are never logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, ClassVar

from sbxloop.config import ChatBackend, Config, SlackConfig
from sbxloop.daemon.chat import ChatBridge, Inbound
from sbxloop.daemon.chat_routing import SLACK_MENTION_RE
from sbxloop.daemon.concierge import Concierge
from sbxloop.daemon.discord_format import EmbedSpec, _clip
from sbxloop.daemon.slack_format import (
    EMOJI_NAMES,
    embed_attachment,
    thread_permalink,
    to_mrkdwn,
)
from sbxloop.daemon.store import ChatThread, DaemonStore
from sbxloop.errors import DaemonError
from sbxloop.log import get_logger

log = get_logger(__name__)

BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"  # nosec B105 - env var name, not a secret
APP_TOKEN_ENV = "SLACK_APP_TOKEN"  # nosec B105 - env var name, not a secret
INSTALL_HINT = (
    "slack_sdk is not installed on this host — install it with "
    "`pip install 'sbxloop[slack]'` to enable the daemon's Slack bridge"
)
# users.info results (user id -> handle); bounded like the bridge's other
# per-author maps so a long-lived daemon does not remember everyone.
USER_NAME_CAP = 500
# Event subtypes that still carry a human's message. Everything else
# (message_changed, message_deleted, channel_join, bot_message, …) is not
# something to route.
_ROUTABLE_SUBTYPES = frozenset({"", "thread_broadcast", "file_share"})
# Web API errors that mean the control channel is misconfigured, reported
# once with the fix rather than on every flush.
_CHANNEL_ERRORS = frozenset({"channel_not_found", "not_in_channel", "is_archived"})


@dataclass(frozen=True)
class SlackTarget:
    """A place to post: the channel, or the thread under ``thread_ts``."""

    channel: str
    thread_ts: str | None = None

    @property
    def id(self) -> str:
        return self.thread_ts or self.channel


@dataclass(frozen=True)
class SlackMessage:
    """A message we posted or were sent — what ``chat.update`` and
    ``reactions.add`` need to find it again."""

    channel: str
    ts: str
    thread_ts: str | None = None

    @property
    def id(self) -> str:
        return self.ts


class SlackClient:
    """slack_sdk in the one shape the bridge (and its test fake) needs: a
    ``web`` client, ``connect()`` returning the bot's user id, ``close()``,
    and the Socket Mode listener that acks every envelope and hands Events
    API events to the bridge."""

    def __init__(self, bridge: SlackBridge, bot_token: str, app_token: str) -> None:
        try:
            from slack_sdk.socket_mode.aiohttp import SocketModeClient
            from slack_sdk.web.async_client import AsyncWebClient
        except ImportError as exc:
            raise DaemonError(INSTALL_HINT) from exc
        self.bridge = bridge
        self.web: Any = AsyncWebClient(token=bot_token)
        self.socket: Any = SocketModeClient(app_token=app_token, web_client=self.web)
        self.socket.socket_mode_request_listeners.append(self._on_request)

    async def connect(self) -> str:
        auth = await self.web.auth_test()
        user_id = str(auth["user_id"])
        await self.socket.connect()
        return user_id

    async def close(self) -> None:
        await self.socket.close()

    async def _on_request(self, client: Any, req: Any) -> None:
        from slack_sdk.socket_mode.response import SocketModeResponse

        # Ack first: Slack retries an envelope it does not hear back on
        # within 3 s, and a retried message would be routed twice.
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return
        event = (req.payload or {}).get("event") or {}
        self.bridge._handle_event(event)


class SlackBridge(ChatBridge):
    """Runs a Socket Mode client on its own thread; the daemon loop calls
    the ``Frontend`` methods from its threads and never blocks on Slack.

    ``client_factory`` builds the client (tests inject a recorder); the
    default imports slack_sdk lazily.
    """

    backend: ClassVar[ChatBackend] = "slack"
    label: ClassVar[str] = "Slack"
    mention_re = SLACK_MENTION_RE

    def __init__(
        self,
        config: Config,
        dstore: DaemonStore,
        *,
        loop_ref: Any = None,
        client_factory: Any = None,
        bot_token: str | None = None,
        app_token: str | None = None,
        concierge: Concierge | None = None,
    ) -> None:
        super().__init__(
            config,
            dstore,
            loop_ref=loop_ref,
            client_factory=client_factory,
            concierge=concierge,
        )
        self.slack: SlackConfig = config.slack
        self.bot_token = bot_token if bot_token is not None else os.environ.get(BOT_TOKEN_ENV, "")
        self.app_token = app_token if app_token is not None else os.environ.get(APP_TOKEN_ENV, "")
        self._user_id: str | None = None
        self._names: dict[str, str] = {}  # user id -> handle, from users.info

    # -- transport seams ------------------------------------------------------------

    def _check_credentials(self) -> None:
        missing = [
            env
            for env, token in ((BOT_TOKEN_ENV, self.bot_token), (APP_TOKEN_ENV, self.app_token))
            if not token
        ]
        if missing:
            verb = "is" if len(missing) == 1 else "are"
            raise DaemonError(
                f"[slack] is configured but {' and '.join(missing)} {verb} not set; export "
                "them (or put them in the project .env) — never in sbxloop.toml"
            )

    @staticmethod
    def _default_client(bridge: Any) -> Any:
        return SlackClient(bridge, bridge.bot_token, bridge.app_token)

    async def _run_client(self) -> None:
        self._user_id = await self.client.connect()
        log.info("slack.connected", user=self._user_id, channel=self.slack.channel_id)
        self.mark_ready()
        assert self._stop_evt is not None
        await self._stop_evt.wait()

    async def _close_client(self) -> None:
        await self.client.close()

    def _bot_user_id(self) -> str | None:
        return self._user_id

    def _handle_event(self, event: dict[str, Any]) -> None:
        """Every Events API event lands here (client thread); only a human's
        ``message`` in the control channel goes on to routing, after the
        author's handle is known."""
        if event.get("type") != "message":
            return
        if str(event.get("subtype") or "") not in _ROUTABLE_SUBTYPES:
            return
        if str(event.get("channel") or "") != (self.slack.channel_id or ""):
            return
        self._schedule(self._route_event(event))

    async def _route_event(self, event: dict[str, Any]) -> None:
        user = str(event.get("user") or "")
        if user and user not in self._names and not self._event_is_bot(event):
            await self._lookup_name(user)
        self._handle_message(event)

    def _event_is_bot(self, event: dict[str, Any]) -> bool:
        user = event.get("user")
        return bool(event.get("bot_id")) or (user is not None and str(user) == self._user_id)

    async def _lookup_name(self, user_id: str) -> None:
        """``users.info`` once per author; a failure caches the id itself so
        the lookup is not retried on every message."""
        name = user_id
        try:
            resp = await self.client.web.users_info(user=user_id)
            profile = resp.get("user") or {}
            name = str(profile.get("name") or profile.get("real_name") or user_id)
        except Exception:
            log.debug("slack.users_info_failed", user=user_id, exc_info=True)
        self._names[user_id] = name
        while len(self._names) > USER_NAME_CAP:
            self._names.pop(next(iter(self._names)))

    def _inbound(self, event: Any) -> Inbound | None:
        if not isinstance(event, dict):
            return None
        channel = str(event.get("channel") or "")
        ts = str(event.get("ts") or "")
        if not channel or not ts:
            return None
        thread_ts = str(event.get("thread_ts") or "")
        in_thread = bool(thread_ts) and thread_ts != ts
        user = str(event.get("user") or "") or None
        text = str(event.get("text") or "")
        return Inbound(
            content=text,
            channel_id=thread_ts if in_thread else channel,
            message_id=ts,
            author_id=user,
            author_name=self._names.get(user) if user else None,
            author_is_bot=self._event_is_bot(event),
            mentioned_ids=frozenset(SLACK_MENTION_RE.findall(text)),
            reply_to_bot=False,
            channel=SlackTarget(channel, thread_ts if in_thread else None),
            raw=SlackMessage(channel, ts, thread_ts if in_thread else None),
        )

    async def _control_channel(self) -> Any:
        return SlackTarget(self.slack.channel_id or "")

    async def _thread_handle(self, thread_id: str) -> Any:
        if thread_id == self.slack.channel_id:
            return SlackTarget(thread_id)
        return SlackTarget(self.slack.channel_id or "", thread_ts=thread_id)

    async def _fetch_message(self, channel: Any, message_id: str) -> Any:
        return SlackMessage(channel.channel, message_id, getattr(channel, "thread_ts", None))

    async def _send(
        self,
        target: Any,
        text: str = "",
        *,
        embed: EmbedSpec | None = None,
        reply_to: Any = None,
        mention_users: bool = False,
    ) -> Any:
        """The single send seam: the text is clipped and re-dialected to
        mrkdwn (user mentions escaped unless asked for), link unfurls are
        off, a card becomes one coloured attachment and is dropped —
        text-only retry — if Slack rejects it. ``reply_to`` is accepted for
        the shared signature and ignored (see the module docstring)."""
        limit = self.slack.max_message_chars
        body = to_mrkdwn(_clip(text, limit), mentions=mention_users) if text else ""
        kwargs: dict[str, Any] = {
            "channel": target.channel,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if target.thread_ts:
            kwargs["thread_ts"] = target.thread_ts
        if embed is not None and self.slack.embeds:
            kwargs["attachments"] = [embed_attachment(embed)]
        elif embed is not None and not body:
            body = to_mrkdwn(_clip(embed.as_text(), limit))
        if not body and "attachments" not in kwargs:
            return None
        try:
            resp = await self.client.web.chat_postMessage(text=body, **kwargs)
        except Exception as exc:
            if self._report_channel_error(exc):
                return None
            if "attachments" in kwargs and embed is not None:
                log.warning(
                    "slack.attachment_send_failed",
                    target=target.id,
                    action="retrying text-only",
                    exc_info=True,
                )
                kwargs.pop("attachments")
                fallback = body or to_mrkdwn(_clip(embed.as_text(), limit))
                try:
                    resp = await self.client.web.chat_postMessage(text=fallback, **kwargs)
                except Exception:
                    log.warning(
                        "slack.send_failed",
                        target=target.id,
                        chars=len(fallback),
                        text_only_retry=True,
                        exc_info=True,
                    )
                    return None
            else:
                log.warning("slack.send_failed", target=target.id, chars=len(body), exc_info=True)
                return None
        return SlackMessage(
            str(resp.get("channel") or target.channel), str(resp["ts"]), target.thread_ts
        )

    def _report_channel_error(self, exc: Exception) -> bool:
        """True when ``exc`` says the control channel is unreachable — a
        configuration problem, reported once with the fix. The bridge is
        not degraded by it: every later send tries again, so posting
        resumes the moment the app is invited (what was queued meanwhile
        is lost, not replayed)."""
        error = _api_error(exc)
        if error not in _CHANNEL_ERRORS:
            return False
        if not self._channel_error_logged:
            self._channel_error_logged = True
            log.error(
                "slack.channel_unreachable",
                channel=self.slack.channel_id,
                error=error,
                hint="check [slack] channel_id (channel details → copy the ID) and invite "
                "the app to the channel (/invite @app); posting resumes as soon as the "
                "channel is reachable, but what was sent until then is dropped",
            )
        return True

    async def _edit(self, message: Any, text: str, *, embed: EmbedSpec | None = None) -> None:
        """``chat.update`` with the same text and card conversion as a send,
        and the same unfurl flags: an edit that introduces a link would
        otherwise grow a preview the original post never had. Errors
        propagate: callers log them with their own context."""
        kwargs: dict[str, Any] = {
            "channel": message.channel,
            "ts": message.ts,
            "text": to_mrkdwn(_clip(text, self.slack.max_message_chars)),
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if embed is not None and self.slack.embeds:
            kwargs["attachments"] = [embed_attachment(embed)]
        await self.client.web.chat_update(**kwargs)

    async def _add_reaction(self, message: Any, emoji: str) -> None:
        name = EMOJI_NAMES.get(emoji)
        if name is None:
            raise ValueError(f"no Slack reaction name for {emoji!r}")
        try:
            await self.client.web.reactions_add(
                channel=message.channel, timestamp=message.ts, name=name
            )
        except Exception as exc:
            if _api_error(exc) != "already_reacted":
                raise

    async def _create_thread(self, headline: Any, name: str) -> Any:
        # Slack threads have no name and no object of their own: the thread
        # *is* the reply stream under the headline.
        return SlackTarget(headline.channel, thread_ts=headline.ts)

    def _message_id(self, message: Any) -> str:
        return str(getattr(message, "ts", "") or "")

    def _handle_id(self, target: Any) -> str:
        return str(getattr(target, "id", "") or "")

    def thread_link(self, thread: ChatThread) -> str:
        if thread.thread_id == thread.channel_id:
            return f"<#{thread.channel_id}>"
        return f"<{thread_permalink(thread.channel_id, thread.thread_id)}|thread>"


def _api_error(exc: Exception) -> str:
    """The ``error`` field of a ``SlackApiError`` response, or ``""``."""
    response = getattr(exc, "response", None)
    if response is None:
        return ""
    data = getattr(response, "data", response)
    try:
        return str(data.get("error") or "")
    except AttributeError:
        return ""


__all__ = [
    "APP_TOKEN_ENV",
    "BOT_TOKEN_ENV",
    "SlackBridge",
    "SlackClient",
    "SlackMessage",
    "SlackTarget",
]
