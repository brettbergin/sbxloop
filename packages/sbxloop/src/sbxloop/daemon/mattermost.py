"""MattermostBridge: the daemon's human channel on Mattermost.

The service-agnostic bridge — event pump, chronology rendering, steering,
run watches, concierge turns, ``!sbx`` commands — is
:class:`sbxloop.daemon.chat.ChatBridge`; this module is the Mattermost
fifth of it: a websocket connection (the app dials out, so an instance the
operator hosts needs no public URL and no inbound hole — the property
Socket Mode buys on Slack), the REST primitives behind send/edit/react, the
mapping of a ``posted`` event onto :class:`~sbxloop.daemon.chat.Inbound`,
and the permalink spelling of a thread pointer.

How Mattermost's shapes map onto the bridge's:

* A *run thread* is the reply thread under the run's headline post, so the
  persisted ``thread_id`` **is the headline's post id** — there is no
  separate thread object and no thread name, as on Slack. Mattermost does
  not nest: a post carrying ``root_id`` cannot itself be a root, which is
  exactly the one-level shape the chronology wants. With ``thread_per_run
  = false`` the thread id is the channel id and everything posts top-level.
* A message handle is ``(channel_id, post_id)``: all ``posts/{id}/patch``
  and ``reactions`` need, so re-attaching after a restart costs no fetch.
* Only ``posted`` events in the control channel are handled. A post with a
  non-empty ``type`` is a system message (joins, header changes) and is
  dropped, as is anything from a bot — this app included — before routing,
  the same rule as Discord and Slack.
* Replying in Mattermost means posting in a thread, not answering one
  message: a reply carries the thread's ``root_id`` and nothing that says
  *which* post it answers. So ``reply_to_bot`` is always false, as on
  Slack, and the concierge and steering are @mention-only. Treating "in a
  run thread" as "addressed to the bot" would be the tempting shortcut and
  is wrong: steering pauses the agent and can rewrite a running task's
  plan, and people talk to each other in a run's thread.
* A mention is ``@username``, not an id, so this bridge's "bot user id" for
  routing purposes **is the bot's username** — ``route_message`` compares
  ids as text and a backend hands in whatever its mentions are spelled
  with. ``Inbound.author_id`` stays the stable 26-character user id (it is
  what run watches persist), and :meth:`mention_user` resolves that id back
  to a handle through a cache, so attribution reads ``Mattermost user
  `ana``` like the others.

``aiohttp`` is an optional extra (``sbxloop[mattermost]``); the import is
deferred and its absence surfaces as an actionable error. Mattermost's API
is plain REST plus a websocket, so the client below is the handful of calls
the bridge makes rather than a third-party driver — one less dependency to
track, and the same "one shape the bridge and its test fake need" the other
two bridges are built on. The token comes from ``MATTERMOST_BOT_TOKEN`` and
is never logged.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from sbxloop.chatservices import MATTERMOST_TOKEN_ENV
from sbxloop.config import ChatBackend, Config, MattermostConfig
from sbxloop.daemon.chat import ChatBridge, Inbound
from sbxloop.daemon.chat_routing import MATTERMOST_MENTION_RE
from sbxloop.daemon.concierge import Concierge
from sbxloop.daemon.discord_format import EmbedSpec, _clip
from sbxloop.daemon.mattermost_format import (
    EMOJI_NAMES,
    neutralize_mentions,
    thread_permalink,
)
from sbxloop.daemon.store import ChatThread, DaemonStore
from sbxloop.errors import DaemonError
from sbxloop.log import get_logger

log = get_logger(__name__)

INSTALL_HINT = (
    "aiohttp is not installed on this host — install it with "
    "`pip install 'sbxloop[mattermost]'` to enable the daemon's Mattermost bridge"
)
# Handles resolved from the users API (user id -> username); bounded like
# the other bridges' per-author maps so a long-lived daemon does not
# remember everyone who ever posted.
USER_NAME_CAP = 500
# A Mattermost id is 26 lowercase alphanumerics. Used to tell this
# service's user ids from another bridge's when several are running.
_MATTERMOST_ID_RE = re.compile(r"^[a-z0-9]{26}$")
# HTTP statuses that mean the control channel is misconfigured, reported
# once with the fix rather than on every flush.
_CHANNEL_STATUSES = frozenset({403, 404})


@dataclass(frozen=True)
class MattermostTarget:
    """A place to post: the channel, or the thread under ``root_id``."""

    channel: str
    root_id: str | None = None

    @property
    def id(self) -> str:
        return self.root_id or self.channel


@dataclass(frozen=True)
class MattermostMessage:
    """A post we made or were sent — what ``patch`` and ``reactions`` need
    to find it again."""

    channel: str
    post_id: str
    root_id: str | None = None

    @property
    def id(self) -> str:
        return self.post_id


class MattermostApiError(Exception):
    """A non-2xx REST reply, carrying the status the caller branches on."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class MattermostClient:
    """The Mattermost API in the one shape the bridge (and its test fake)
    needs: the REST calls behind send/edit/react, ``connect()`` returning
    the bot's ``(user_id, username)``, ``close()``, and a websocket reader
    that hands each event to the bridge."""

    def __init__(self, bridge: MattermostBridge, url: str, token: str) -> None:
        try:
            import aiohttp
        except ImportError as exc:
            raise DaemonError(INSTALL_HINT) from exc
        self._aiohttp = aiohttp
        self.bridge = bridge
        self.base = url.rstrip("/")
        self.token = token
        self.session: Any = None
        self.ws: Any = None
        self._reader: asyncio.Task[None] | None = None

    # -- REST ----------------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        assert self.session is not None
        url = f"{self.base}/api/v4{path}"
        async with self.session.request(method, url, **kwargs) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise MattermostApiError(resp.status, _error_detail(body))
            return json.loads(body) if body else {}

    async def connect(self) -> tuple[str, str]:
        self.session = self._aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.token}"}
        )
        me = await self._request("GET", "/users/me")
        self.ws = await self.session.ws_connect(f"{self.base}/api/v4/websocket")
        # Mattermost authenticates a websocket by the handshake header or by
        # this challenge; sending it is harmless when the header was already
        # accepted and is the documented path when it was not.
        await self.ws.send_json(
            {"seq": 1, "action": "authentication_challenge", "data": {"token": self.token}}
        )
        self._reader = asyncio.create_task(self._listen())
        return str(me.get("id") or ""), str(me.get("username") or "")

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
        if self.ws is not None:
            await self.ws.close()
        if self.session is not None:
            await self.session.close()

    async def _listen(self) -> None:
        assert self.ws is not None
        async for message in self.ws:
            if message.type is not self._aiohttp.WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(message.data)
            except ValueError:
                log.debug("mattermost.bad_frame", exc_info=True)
                continue
            if isinstance(payload, dict):
                self.bridge._handle_ws_event(payload)

    # -- the calls the bridge makes -------------------------------------------------

    async def create_post(self, body: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("POST", "/posts", json=body)
        return result

    async def patch_post(self, post_id: str, body: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("PUT", f"/posts/{post_id}/patch", json=body)
        return result

    async def create_reaction(self, user_id: str, post_id: str, emoji_name: str) -> None:
        await self._request(
            "POST",
            "/reactions",
            json={"user_id": user_id, "post_id": post_id, "emoji_name": emoji_name},
        )

    async def get_user(self, user_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("GET", f"/users/{user_id}")
        return result

    async def get_channel(self, channel_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("GET", f"/channels/{channel_id}")
        return result

    async def get_team(self, team_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("GET", f"/teams/{team_id}")
        return result


def _error_detail(body: str) -> str:
    """Mattermost's ``message`` field, or the raw body when it is not JSON.

    Never the request: a failed call must not echo a token that rode in a
    header into a log line.
    """
    try:
        parsed = json.loads(body)
    except ValueError:
        return body[:200]
    if isinstance(parsed, dict):
        return str(parsed.get("message") or parsed.get("id") or "")[:200]
    return str(parsed)[:200]


class MattermostBridge(ChatBridge):
    """Runs a websocket client on its own thread; the daemon loop calls the
    ``Frontend`` methods from its threads and never blocks on Mattermost.

    ``client_factory`` builds the client (tests inject a recorder); the
    default imports aiohttp lazily.
    """

    backend: ClassVar[ChatBackend] = "mattermost"
    label: ClassVar[str] = "Mattermost"
    mention_re = MATTERMOST_MENTION_RE

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
        self.mattermost: MattermostConfig = config.mattermost
        self.token = token if token is not None else os.environ.get(MATTERMOST_TOKEN_ENV, "")
        self._user_id: str | None = None
        self._username: str | None = None
        self._names: dict[str, str] = {}  # user id -> username
        self._team: str = ""  # the control channel's team, for permalinks

    # -- transport seams ------------------------------------------------------------

    def _check_credentials(self) -> None:
        if not self.token:
            raise DaemonError(
                f"[mattermost] is configured but {MATTERMOST_TOKEN_ENV} is not set; export it "
                "(or put it in the project .env) — never in sbxloop.toml"
            )

    @staticmethod
    def _default_client(bridge: Any) -> Any:
        return MattermostClient(bridge, bridge.mattermost.url or "", bridge.token)

    async def _run_client(self) -> None:
        self._user_id, self._username = await self.client.connect()
        await self._resolve_team()
        log.info(
            "mattermost.connected",
            user=self._user_id,
            username=self._username,
            channel=self.mattermost.channel_id,
        )
        self.mark_ready()
        assert self._stop_evt is not None
        await self._stop_evt.wait()

    async def _close_client(self) -> None:
        await self.client.close()

    async def _resolve_team(self) -> None:
        """The control channel's team name, for permalinks. A failure leaves
        it empty and thread pointers render as plain ids — a link that 404s
        would be worse than no link."""
        try:
            channel = await self.client.get_channel(self.mattermost.channel_id or "")
            team = await self.client.get_team(str(channel.get("team_id") or ""))
            self._team = str(team.get("name") or "")
        except Exception:
            log.debug("mattermost.team_lookup_failed", exc_info=True)

    def _bot_user_id(self) -> str | None:
        # The username, not the id: mentions are spelled ``@username`` here,
        # and this is what ``route_message`` matches against and strips.
        return self._username

    # -- inbound --------------------------------------------------------------------

    def _handle_ws_event(self, payload: dict[str, Any]) -> None:
        """Every websocket frame lands here (client thread); only a human's
        ``posted`` in the control channel goes on to routing."""
        if str(payload.get("event") or "") != "posted":
            return
        data = payload.get("data") or {}
        post = _decode_post(data.get("post"))
        if post is None:
            return
        if str(post.get("channel_id") or "") != (self.mattermost.channel_id or ""):
            return
        # A non-empty type is a system message (joins, header changes).
        if str(post.get("type") or ""):
            return
        self._schedule(self._route_post(post, data))

    async def _route_post(self, post: dict[str, Any], data: dict[str, Any]) -> None:
        user_id = str(post.get("user_id") or "")
        if user_id and user_id not in self._names and not self._post_is_bot(post, user_id):
            await self._lookup_name(user_id)
        self._handle_message(self._inbound_from(post, data))

    def _post_is_bot(self, post: dict[str, Any], user_id: str) -> bool:
        props = post.get("props") or {}
        from_bot = str(props.get("from_bot") or "").lower() == "true"
        return from_bot or (bool(user_id) and user_id == self._user_id)

    async def _lookup_name(self, user_id: str) -> None:
        """One users call per author; a failure caches the id itself so the
        lookup is not retried on every post."""
        name = user_id
        try:
            profile = await self.client.get_user(user_id)
            name = str(profile.get("username") or user_id)
        except Exception:
            log.debug("mattermost.user_lookup_failed", user=user_id, exc_info=True)
        self._names[user_id] = name
        while len(self._names) > USER_NAME_CAP:
            self._names.pop(next(iter(self._names)))

    def _inbound_from(self, post: dict[str, Any], data: dict[str, Any]) -> Inbound | None:
        channel = str(post.get("channel_id") or "")
        post_id = str(post.get("id") or "")
        if not channel or not post_id:
            return None
        root_id = str(post.get("root_id") or "")
        in_thread = bool(root_id) and root_id != post_id
        user_id = str(post.get("user_id") or "") or None
        text = str(post.get("message") or "")
        return Inbound(
            content=text,
            channel_id=root_id if in_thread else channel,
            message_id=post_id,
            author_id=user_id,
            author_name=self._names.get(user_id) if user_id else None,
            author_is_bot=self._post_is_bot(post, user_id or ""),
            # Usernames, which is what a Mattermost mention carries and what
            # ``_bot_user_id`` hands ``route_message`` to compare them with.
            mentioned_ids=frozenset(MATTERMOST_MENTION_RE.findall(text)),
            reply_to_bot=False,
            channel=MattermostTarget(channel, root_id if in_thread else None),
            raw=MattermostMessage(channel, post_id, root_id if in_thread else None),
            reply_to_id=root_id if in_thread else None,
        )

    def _inbound(self, message: Any) -> Inbound | None:
        if isinstance(message, Inbound):
            return message
        if not isinstance(message, dict):
            return None
        return self._inbound_from(message, {})

    # -- outbound -------------------------------------------------------------------

    async def _control_channel(self) -> Any:
        return MattermostTarget(self.mattermost.channel_id or "")

    async def _thread_handle(self, thread_id: str) -> Any:
        if thread_id == self.mattermost.channel_id:
            return MattermostTarget(thread_id)
        return MattermostTarget(self.mattermost.channel_id or "", root_id=thread_id)

    async def _fetch_message(self, channel: Any, message_id: str) -> Any:
        return MattermostMessage(channel.channel, message_id, getattr(channel, "root_id", None))

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
        """The single send seam: the text is clipped, every ``@name`` in it
        made inert unless mentions were asked for (Mattermost has no
        allowed-mentions control, so agent prose would otherwise ping), and
        a card is appended as text. ``reply_to`` is accepted for the shared
        signature and ignored — a reply here is a thread post, which
        ``target`` already expresses. ``files`` are named by host path;
        uploads are a follow-on (#932)."""
        if files:
            text = "\n".join(part for part in (text, self._files_note(files)) if part)
        body = self._body(text, embed, mention_users=mention_users)
        if not body:
            return None
        payload: dict[str, Any] = {"channel_id": target.channel, "message": body}
        if target.root_id:
            payload["root_id"] = target.root_id
        try:
            post = await self.client.create_post(payload)
        except Exception as exc:
            if self._report_channel_error(exc):
                return None
            log.warning("mattermost.send_failed", target=target.id, chars=len(body), exc_info=True)
            return None
        return MattermostMessage(
            str(post.get("channel_id") or target.channel),
            str(post.get("id") or ""),
            target.root_id,
        )

    def _body(self, text: str, embed: EmbedSpec | None, *, mention_users: bool = False) -> str:
        """The post text: clipped, mention-safe, with a card rendered into
        it. The card becomes text here; :mod:`mattermost_format` turns it
        into a coloured attachment in #932."""
        limit = self.mattermost.max_message_chars
        parts = [part for part in (text, embed.as_text() if embed is not None else "") if part]
        body = _clip("\n\n".join(parts), limit)
        return body if mention_users else neutralize_mentions(body)

    def _report_channel_error(self, exc: Exception) -> bool:
        """True when ``exc`` says the control channel is unreachable — a
        configuration problem, reported once with the fix. The bridge is not
        degraded by it: every later send tries again, so posting resumes the
        moment the bot is added to the channel (what was queued meanwhile is
        lost, not replayed)."""
        if not isinstance(exc, MattermostApiError) or exc.status not in _CHANNEL_STATUSES:
            return False
        if not self._channel_error_logged:
            self._channel_error_logged = True
            log.error(
                "mattermost.channel_unreachable",
                channel=self.mattermost.channel_id,
                status=exc.status,
                hint="check [mattermost] channel_id (channel name → View Info → the ID) and "
                "add the bot account to the channel; posting resumes as soon as the channel "
                "is reachable, but what was sent until then is dropped",
            )
        return True

    async def _edit(self, message: Any, text: str, *, embed: EmbedSpec | None = None) -> None:
        """``posts/{id}/patch`` with the same clipping and mention safety as
        a send. Errors propagate: callers log them with their own context."""
        await self.client.patch_post(message.post_id, {"message": self._body(text, embed)})

    async def _add_reaction(self, message: Any, emoji: str) -> None:
        name = EMOJI_NAMES.get(emoji)
        if name is None:
            raise ValueError(f"no Mattermost reaction name for {emoji!r}")
        if not self._user_id:
            return
        try:
            await self.client.create_reaction(self._user_id, message.post_id, name)
        except MattermostApiError as exc:
            # Reacting twice is not an error worth raising: the mark the
            # caller wanted is already there.
            if exc.status != 400:
                raise

    async def _create_thread(self, headline: Any, name: str) -> Any:
        # Mattermost threads have no name and no object of their own: the
        # thread *is* the reply stream under the headline post.
        return MattermostTarget(headline.channel, root_id=headline.post_id)

    # -- identity -------------------------------------------------------------------

    def _message_id(self, message: Any) -> str:
        return str(getattr(message, "post_id", "") or "")

    def _handle_id(self, target: Any) -> str:
        return str(getattr(target, "id", "") or "")

    def mention_user(self, user_id: str) -> str:
        """``@handle`` — a Mattermost mention is a username, so a stored user
        id is resolved through the cache the inbound path fills. An id we
        never saw post renders as itself rather than as a broken ping."""
        return f"@{self._names.get(user_id, user_id)}"

    def _owns_user_id(self, user_id: str) -> bool:
        return bool(_MATTERMOST_ID_RE.match(user_id))

    def thread_link(self, thread: ChatThread) -> str:
        if thread.thread_id == thread.channel_id:
            return f"~{thread.channel_id}"
        link = thread_permalink(self.mattermost.url or "", self._team, thread.thread_id)
        return f"[thread]({link})" if link else thread.thread_id


def _decode_post(raw: Any) -> dict[str, Any] | None:
    """A ``posted`` event carries its post as a JSON *string*."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        post = json.loads(raw)
    except ValueError:
        log.debug("mattermost.bad_post", exc_info=True)
        return None
    return post if isinstance(post, dict) else None


__all__ = [
    "MattermostApiError",
    "MattermostBridge",
    "MattermostClient",
    "MattermostMessage",
    "MattermostTarget",
]
