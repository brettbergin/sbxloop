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

import asyncio
import functools
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from sbxloop.chatservices import SLACK_APP_TOKEN_ENV, SLACK_BOT_TOKEN_ENV
from sbxloop.config import ChatBackend, Config, SlackConfig
from sbxloop.daemon.chat import ChatBridge, Inbound
from sbxloop.daemon.chat_choices import ChoiceQuestion, render_prose
from sbxloop.daemon.chat_routing import SLACK_MENTION_RE
from sbxloop.daemon.concierge import Concierge
from sbxloop.daemon.discord_format import EmbedSpec, _clip
from sbxloop.daemon.slack_format import (
    EMOJI_NAMES,
    embed_attachment,
    escape,
    thread_permalink,
    to_mrkdwn,
)
from sbxloop.daemon.store import ChatThread, DaemonStore
from sbxloop.errors import DaemonError
from sbxloop.log import get_logger

log = get_logger(__name__)

#: Re-exported from the service descriptor, which is what doctor and
#: ``build_bridge`` read; the spellings live in one place.
BOT_TOKEN_ENV = SLACK_BOT_TOKEN_ENV
APP_TOKEN_ENV = SLACK_APP_TOKEN_ENV
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
_SLACK_USER_RE = re.compile(r"^[UW][A-Z0-9]+$")


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
        dispatch_envelope(self.bridge, str(req.type or ""), req.payload or {})


def dispatch_envelope(bridge: Any, kind: str, payload: dict[str, Any]) -> None:
    """Route one Socket Mode envelope: an Events API event to the message
    handler, a block-kit click (``interactive`` / ``block_actions``, #571)
    to the interaction handler; anything else is dropped."""
    if kind == "events_api":
        bridge._handle_event(payload.get("event") or {})
    elif kind == "interactive" and payload.get("type") == "block_actions":
        bridge._handle_interaction(payload)


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
            reply_to_id=thread_ts if in_thread else None,
            parent_channel_id=channel if in_thread else None,
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
        files: Sequence[str] = (),
    ) -> Any:
        """The single send seam: the text is clipped and re-dialected to
        mrkdwn (user mentions escaped unless asked for), link unfurls are
        off, a card becomes one coloured attachment and is dropped —
        text-only retry — if Slack rejects it. ``reply_to`` is accepted for
        the shared signature and ignored (see the module docstring).
        ``files`` are named by host path (#799): Slack uploads are a
        follow-on (#763)."""
        if files:
            text = "\n".join(part for part in (text, self._files_note(files)) if part)
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

    # -- block-kit buttons (#571) ------------------------------------------------

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
        """A clarifying question with one block-kit button per choice.

        The message text is the same numbered prose the base seam posts,
        so a click is an extra way in and typing still answers; a post
        Slack refuses with the blocks falls back to that prose. The
        buttons carry the choice value, and the actions block carries the
        provisional key the question was registered under, so a click
        that lands before the post's ``ts`` is known still resolves.
        """
        body = render_prose(question)
        if text and text.strip() and text.strip() != question.prompt.strip():
            body = f"{text.strip()}\n\n{body}"
        limit = self.slack.max_message_chars
        mrkdwn = to_mrkdwn(_clip(body, limit), mentions=mention_users)
        kwargs: dict[str, Any] = {
            "channel": target.channel,
            "text": mrkdwn,
            "blocks": [
                _section(mrkdwn),
                _actions_block(
                    [
                        (f"{CHOICE_ACTION_PREFIX}{index}", choice.label, choice.value, None)
                        for index, choice in enumerate(question.choices)
                    ],
                    block_id=pending_key or CHOICE_BLOCK_ID,
                ),
            ],
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if target.thread_ts:
            kwargs["thread_ts"] = target.thread_ts
        try:
            resp = await self.client.web.chat_postMessage(**kwargs)
        except Exception:
            if self._report_channel_error_quietly():
                return None
            log.warning("slack.choices_send_failed", target=target.id, exc_info=True)
            return await super()._send_choices(
                target, text, question, reply_to=reply_to, mention_users=mention_users
            )
        return SlackMessage(
            str(resp.get("channel") or target.channel), str(resp["ts"]), target.thread_ts
        )

    async def _send_gate(self, target: Any, text: str, gate: Any) -> Any:
        """The approval prompt with one persistent button on top of the base
        prose — the typed command stays in the body, so a post Slack
        refuses with the block leaves a prompt that works by typing."""
        held = getattr(gate, "kind", "merge") == "publish"
        limit = self.slack.max_message_chars
        mrkdwn = to_mrkdwn(_clip(text, limit), mentions=True)
        kwargs: dict[str, Any] = {
            "channel": target.channel,
            "text": mrkdwn,
            "blocks": [
                _section(mrkdwn),
                _actions_block(
                    [
                        (
                            GATE_ACTION_ID,
                            "Release result" if held else "Approve merge",
                            str(gate.run_id),
                            "primary",
                        )
                    ],
                    block_id=f"{GATE_BLOCK_PREFIX}{gate.run_id}",
                ),
            ],
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if target.thread_ts:
            kwargs["thread_ts"] = target.thread_ts
        try:
            resp = await self.client.web.chat_postMessage(**kwargs)
        except Exception:
            if self._report_channel_error_quietly():
                return None
            log.warning("slack.gate_send_failed", run=gate.run_id, exc_info=True)
            return await super()._send_gate(target, text, gate)
        return SlackMessage(
            str(resp.get("channel") or target.channel), str(resp["ts"]), target.thread_ts
        )

    async def _finalize_gate_message(self, message: Any, text: str) -> None:
        """Rewrite the prompt once the gate is resolved and drop its button."""
        await self.client.web.chat_update(
            channel=message.channel,
            ts=message.ts,
            text=to_mrkdwn(_clip(text, self.slack.max_message_chars)),
            blocks=[],
        )

    def _report_channel_error_quietly(self) -> bool:
        """Whether the last send failure was the unreachable-channel case
        (already reported once); False when it was something else."""
        return self._channel_error_logged

    def _handle_interaction(self, payload: dict[str, Any]) -> None:
        """A block-kit click (client thread): schedule it on the loop."""
        self._schedule(self._route_interaction(payload))

    async def _route_interaction(self, payload: dict[str, Any]) -> None:
        user = payload.get("user") or {}
        user_id = str(user.get("id") or "")
        name = str(user.get("username") or user.get("name") or "").strip()
        if not name and user_id:
            if user_id not in self._names:
                await self._lookup_name(user_id)
            name = self._names.get(user_id, user_id)
        author = f"Slack user `{name}`" if name else "a Slack user"
        container = payload.get("container") or {}
        message = payload.get("message") or {}
        channel = str(container.get("channel_id") or (payload.get("channel") or {}).get("id") or "")
        ts = str(container.get("message_ts") or message.get("ts") or "")
        thread_ts = container.get("thread_ts") or message.get("thread_ts")
        for action in payload.get("actions") or []:
            if not isinstance(action, dict):
                continue
            action_id = str(action.get("action_id") or "")
            value = str(action.get("value") or "")
            block_id = str(action.get("block_id") or "")
            if action_id.startswith(CHOICE_ACTION_PREFIX):
                await self._choice_clicked(
                    channel, ts, thread_ts, block_id, value, author, user_id, name
                )
            elif action_id == GATE_ACTION_ID:
                await self._gate_clicked(channel, ts, thread_ts, value, author)

    async def _choice_clicked(
        self,
        channel: str,
        ts: str,
        thread_ts: Any,
        block_id: str,
        value: str,
        author: str,
        user_id: str,
        name: str,
    ) -> None:
        """Anyone may click; who clicked is recorded. The question is found
        by the post's ``ts``, else by the provisional key in the block —
        a click that beat the rekey — and a click on a question the bridge
        no longer holds gets the typed route, ephemerally, not a dead
        button."""
        accepted = False
        for key in (ts, block_id):
            if not key or key == CHOICE_BLOCK_ID:
                continue
            try:
                accepted = bool(
                    self._answer_choice(
                        key, value, author, author_id=user_id or None, author_name=name or None
                    )
                )
            except Exception:
                log.warning("slack.choice_answer_failed", value=value, exc_info=True)
                accepted = False
            if accepted:
                break
        if not accepted:
            log.info("slack.choice_click_expired", value=value, by=author)
            await self._ephemeral(channel, user_id, thread_ts, EXPIRED_CLICK_NOTE)
            return
        try:
            await self.client.web.chat_update(
                channel=channel,
                ts=ts,
                text=f"_Answered: *{escape(value)}* (by {escape(author)})._",
                blocks=[],
            )
        except Exception:
            log.debug("slack.choice_edit_failed", ts=ts, exc_info=True)

    async def _gate_clicked(
        self, channel: str, ts: str, thread_ts: Any, run_id: str, author: str
    ) -> None:
        """The approve / release button: the same call the typed command
        makes, answered in the thread. The click never disables the button:
        a failed landing re-opens the gate and the same button works
        again; resolution clears it through ``_finalize_gate_message``."""
        loop_ref = self.loop_ref
        if loop_ref is None:
            await self._ephemeral(channel, "", thread_ts, "daemon loop not attached")
            return
        try:
            reply = await asyncio.get_event_loop().run_in_executor(
                None, functools.partial(loop_ref.approve_merge, run_id, by=author)
            )
        except (KeyError, ValueError) as exc:
            reply = f"failed: {exc.args[0] if exc.args else exc}"
        except Exception:
            log.warning("slack.gate_click_failed", run=run_id, exc_info=True)
            reply = (
                f"something went wrong — `{self.chat.command_prefix} merge` / `release` still work"
            )
        await self._send(SlackTarget(channel, thread_ts=thread_ts or ts), str(reply))

    async def _ephemeral(self, channel: str, user_id: str, thread_ts: Any, text: str) -> None:
        """A note only the clicker sees, when the workspace allows it."""
        post = getattr(self.client.web, "chat_postEphemeral", None)
        if post is None or not user_id:
            return
        kwargs: dict[str, Any] = {"channel": channel, "user": user_id, "text": to_mrkdwn(text)}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        try:
            await post(**kwargs)
        except Exception:
            log.debug("slack.ephemeral_failed", exc_info=True)

    def _message_id(self, message: Any) -> str:
        return str(getattr(message, "ts", "") or "")

    def _handle_id(self, target: Any) -> str:
        return str(getattr(target, "id", "") or "")

    def _owns_user_id(self, user_id: str) -> bool:
        # A Slack member id: U… (or W… for an enterprise grid user).
        return bool(_SLACK_USER_RE.match(user_id))

    def thread_link(self, thread: ChatThread) -> str:
        if thread.thread_id == thread.channel_id:
            return f"<#{thread.channel_id}>"
        return f"<{thread_permalink(thread.channel_id, thread.thread_id)}|thread>"


#: Block-kit ids (#571): one action per choice, the provisional question
#: key on the actions block; one persistent action for a gate's button.
CHOICE_ACTION_PREFIX = "sbx-choice:"
CHOICE_BLOCK_ID = "sbx-choices"
GATE_ACTION_ID = "sbx-gate"
GATE_BLOCK_PREFIX = "sbx-gate:"
EXPIRED_CLICK_NOTE = (
    "That question has expired (or was already answered) — just type your answer "
    "in the channel and I'll pick it up."
)


def _section(mrkdwn: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": mrkdwn}}


def _actions_block(
    buttons: list[tuple[str, str, str, str | None]], *, block_id: str
) -> dict[str, Any]:
    """An actions block of ``(action_id, label, value, style)`` buttons —
    at most five, Slack's cap, which is also the choice model's."""
    elements = []
    for action_id, label, value, style in buttons[:5]:
        button: dict[str, Any] = {
            "type": "button",
            "action_id": action_id,
            "text": {"type": "plain_text", "text": label[:75] or value[:75] or "?"},
            "value": value[:2000],
        }
        if style:
            button["style"] = style
        elements.append(button)
    return {"type": "actions", "block_id": block_id[:255], "elements": elements}


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
