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
* Link previews are the other thing the send seam owes a run's thread.
  Mattermost picks what to embed from the *first autolink* in a post, so a
  chronology full of PR, issue and CI links grows a website card under
  every one of them; ``defuse_unfurls`` writes each bare URL as a markdown
  link to itself, which the server's scan never visits. A post carrying a
  card needs none of this — the server stops at the attachment — but it
  costs nothing to be consistent.
* Discord's *interactions* have analogs here, and the bridge owes a human
  the same three signals it gets there: **something received it** (the ⏳
  ack reaction), **something is still working on it** (a ``user_typing``
  frame, repeated for as long as the concierge thinks — the websocket is
  already open, so it costs no REST call), and **the affordance is spent**
  (a resolved gate or an answered question loses the reactions that were
  seeded on it, the way a Discord view loses its buttons). A reaction that
  the server refuses is the one failure a human cannot see, so it is said
  out loud once rather than swallowed. The ⏳ has one more enemy: the
  asker's *own* webapp, which drops a reaction that lands before its
  create-post reply comes back (see ``RECEIVED_REASSERT_S``), so the bridge
  puts that one mark on twice.

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
import contextlib
import functools
import json
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from sbxloop.chatservices import MATTERMOST_TOKEN_ENV
from sbxloop.config import ChatBackend, Config, MattermostConfig
from sbxloop.daemon.chat import ACK_RECEIVED, ChatBridge, Inbound
from sbxloop.daemon.chat_choices import ChoiceQuestion, render_prose
from sbxloop.daemon.chat_routing import MATTERMOST_MENTION_RE
from sbxloop.daemon.concierge import Concierge
from sbxloop.daemon.discord_format import EmbedSpec, _clip
from sbxloop.daemon.mattermost_format import (
    CHOICE_EMOJI,
    EMOJI_NAMES,
    GATE_EMOJI,
    defuse_unfurls,
    embed_attachment,
    neutralize_mentions,
    thread_permalink,
)
from sbxloop.daemon.store import ChatThread, DaemonStore, MergeGate
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
# ...and what the same two mean about a single post: it is not there, or is
# no longer ours to touch. Either way the caller should put a new one up.
_MISSING_STATUSES = frozenset({403, 404})
#: Mattermost takes at most five files on one post (Discord takes ten); the
#: rest are named by host path, so a workload result never silently loses an
#: artifact to a transport limit.
MAX_POST_FILES = 5
#: A lost websocket is rebuilt after this long, doubling to the cap. The
#: socket is the daemon's only inbound path, so the first retry is quick; the
#: cap keeps a long outage down to one attempt — and one log line — a minute.
RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 60.0
#: How often the bridge re-asserts "…is typing" while a concierge turn runs.
#: A Mattermost client drops the indicator a few seconds after the last
#: frame, so a turn that thinks for a minute has to keep saying it.
TYPING_INTERVAL_S = 3.0
#: How long after the ⏳ lands to put it on a second time.
#:
#: The asker's own web/desktop app races the first one. Their client stores
#: a reaction the moment the websocket delivers it — and the bot reacts
#: within ~50ms of the post — but their create-post HTTP reply lands after
#: that, carrying a post the server serialised BEFORE the reaction existed,
#: and the app's reactions reducer *replaces* what it holds for the post
#: with the reply's (empty) list. Every other viewer already had the post
#: and keeps the mark; the one person it was for watches their message go
#: straight to ✅. Saving an identical reaction again is a 200 and the server
#: broadcasts ``reaction_added`` again, which the client stores after the
#: wipe. 1.5s clears a slow reply without waiting past the answer on a
#: quick concierge turn — and a mark that arrives after ✅ still renders
#: beside it, so late is harmless.
RECEIVED_REASSERT_S = 1.5
#: Question posts remembered for their body and seeded digits. Bounded like
#: the other per-message maps: a long-lived daemon must not grow one entry
#: per question it ever asked.
CHOICE_POST_CAP = 50
#: What a reaction on a question the bridge no longer holds is answered
#: with. Discord and Slack say this privately to whoever clicked; a bot
#: account on Mattermost has no ephemeral post, so it goes in the thread.
EXPIRED_CLICK_NOTE = (
    "That question has expired (or was already answered) — just type your answer "
    "in the channel and I'll pick it up."
)


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
        # Set when the reader stops for any reason, so the bridge can tell a
        # live connection from a dead one (``wait_closed``).
        self._closed: asyncio.Event | None = None
        # Frames the client sends are numbered; the authentication challenge
        # below is 1 and everything after it counts on from there.
        self._seq = 1

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
        # A frame's sequence number is per connection: a reconnect starts
        # over at the authentication challenge's 1.
        self._seq = 1
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
        self._closed = asyncio.Event()
        self._reader = asyncio.create_task(self._listen())
        return str(me.get("id") or ""), str(me.get("username") or "")

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
        if self.ws is not None:
            await self.ws.close()
        if self.session is not None:
            await self.session.close()
        if self._closed is not None:
            # Nothing is listening any more; anyone waiting on the reader
            # must not be parked on it through shutdown.
            self._closed.set()

    async def wait_closed(self) -> None:
        """Return when the websocket reader has stopped — the connection is
        gone and nothing inbound will arrive until it is rebuilt."""
        if self._closed is None:
            return
        await self._closed.wait()

    async def _listen(self) -> None:
        """Read frames until the socket ends, then say so.

        The ``async for`` returns on a clean close, a server restart, a
        proxy's idle timeout or a dropped network — all of which look
        identical from here and all of which mean the same thing: the
        daemon is now deaf. Whether to dial again is the bridge's call, so
        this only reports.
        """
        assert self.ws is not None
        try:
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
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("mattermost.reader_failed", exc_info=True)
        finally:
            if self._closed is not None:
                self._closed.set()

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

    async def delete_reaction(self, user_id: str, post_id: str, emoji_name: str) -> None:
        await self._request("DELETE", f"/users/{user_id}/posts/{post_id}/reactions/{emoji_name}")

    async def send_typing(self, channel_id: str, parent_id: str = "") -> None:
        """What makes "…is typing" appear under the message box.

        There is no REST route for it: the frame rides the websocket the
        bridge already holds, and the server answers nothing. Sent before
        the connection is up it is a no-op rather than an error — the
        indicator is the one thing in a turn that may go missing without
        costing the human an answer.
        """
        if self.ws is None:
            return
        self._seq += 1
        await self.ws.send_json(
            {
                "seq": self._seq,
                "action": "user_typing",
                "data": {"channel_id": channel_id, "parent_id": parent_id},
            }
        )

    async def upload_file(self, channel_id: str, name: str, content: bytes) -> str:
        """Upload one attachment, returning its file id ("" when the server
        accepted the call but named no file)."""
        form = self._aiohttp.FormData()
        form.add_field("channel_id", channel_id)
        form.add_field("files", content, filename=name)
        result = await self._request("POST", "/files", data=form)
        infos = result.get("file_infos") or []
        return str(infos[0].get("id") or "") if infos else ""

    async def get_post(self, post_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("GET", f"/posts/{post_id}")
        return result

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
        # The second ⏳ per post, in flight; cancelled with the client so a
        # shutdown never waits on a beat nobody will see.
        self._reasserts: set[asyncio.Task[None]] = set()
        self._reassert_s = RECEIVED_REASSERT_S
        self._names: dict[str, str] = {}  # user id -> username
        self._team: str = ""  # the control channel's team, for permalinks
        # Gate prompt post id -> run id, so a ✅ reaction finds its gate.
        # In memory; a restart repopulates it lazily from the store.
        self._gate_posts: dict[str, str] = {}
        # Question post id -> (posted body, digits seeded), so an answer can
        # keep the question above its answer line and take the digits back
        # off — the Discord view losing its buttons, in reactions.
        self._choice_posts: dict[str, tuple[str, int]] = {}
        # Question posts already told "that one has expired", so a stale
        # reaction is answered once and not once per person who tries it.
        self._choice_expired: set[str] = set()
        # Emoji names this instance has already refused, so a server whose
        # emoji set lacks one is reported once rather than on every ack.
        self._reaction_refusals: set[str] = set()

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
        """Hold a live websocket for as long as the bridge runs.

        The socket is the *only* way anything reaches the daemon from chat,
        and REST is untouched when it drops. So a bridge that does not
        rebuild it goes on posting every run's chronology while silently
        discarding every steer, command and @mention — healthy from the
        outside, deaf. discord.py reconnects on its own and so does Slack's
        Socket Mode client; this client is ours, so the supervision has to
        be too.

        The *first* connect is not retried. A bad token, a wrong URL or a
        host that is not there is a configuration error, and the base
        bridge already reports it once and carries on degraded — retrying
        it forever would bury the one message that says what to fix. Only a
        connection that was up is rebuilt.
        """
        await self._connect()
        assert self._stop_evt is not None
        while not self._stop_evt.is_set():
            await self._wait_for_disconnect()
            if self._stop_evt.is_set():
                return
            log.warning(
                "mattermost.disconnected",
                channel=self.mattermost.channel_id,
                hint="nothing inbound reaches the daemon until the websocket is back — "
                "steers, commands and @mentions are dropped meanwhile; rebuilding it",
            )
            if not await self._reconnect():
                return

    async def _connect(self) -> None:
        """Dial, learn who we are, and open for business."""
        self._user_id, self._username = await self.client.connect()
        await self._resolve_team()
        log.info(
            "mattermost.connected",
            user=self._user_id,
            username=self._username,
            channel=self.mattermost.channel_id,
        )
        self.mark_ready()

    async def _reconnect(self) -> bool:
        """Rebuild the connection, backing off between attempts, until it is
        up or the bridge is stopping. True when connected.

        It never gives up: an instance that comes back an hour later should
        find the daemon still listening, and there is nothing else to fall
        back to.
        """
        delay = RECONNECT_MIN_S
        attempt = 0
        while await self._pause(delay):
            attempt += 1
            try:
                await self._close_quietly()
                await self._connect()
            except Exception as exc:
                delay = min(delay * 2, RECONNECT_MAX_S)
                log.warning(
                    "mattermost.reconnect_failed",
                    attempt=attempt,
                    error=str(exc),
                    retry_in_s=delay,
                    exc_info=True,
                )
                continue
            log.info("mattermost.reconnected", attempt=attempt)
            return True
        return False

    async def _wait_for_disconnect(self) -> None:
        """Return when the reader has stopped, or the bridge is stopping.

        A client that cannot report a drop parks on the stop event, which is
        all this did before there was anything to supervise.
        """
        assert self._stop_evt is not None
        stop = asyncio.ensure_future(self._stop_evt.wait())
        wait_closed = getattr(self.client, "wait_closed", None)
        if wait_closed is None:
            try:
                await stop
            finally:
                stop.cancel()
            return
        closed = asyncio.ensure_future(wait_closed())
        try:
            await asyncio.wait({stop, closed}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
            closed.cancel()

    async def _pause(self, seconds: float) -> bool:
        """Wait out a backoff; False when the bridge stopped meanwhile, so a
        shutdown never has to sit through one."""
        assert self._stop_evt is not None
        try:
            await asyncio.wait_for(self._stop_evt.wait(), timeout=seconds)
        except TimeoutError:
            return True
        return False

    async def _close_quietly(self) -> None:
        """Let go of what is left of the dead connection before dialing
        again: the client builds a fresh HTTP session per connect, so a
        reconnect that skipped this would leak one per outage."""
        try:
            await self.client.close()
        except Exception:
            log.debug("mattermost.close_before_reconnect_failed", exc_info=True)

    async def _close_client(self) -> None:
        # A second ⏳ still waiting on its beat has nobody left to see it.
        for task in self._reasserts:
            task.cancel()
        self._reasserts.clear()
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
        """Every websocket frame lands here (client thread). A human's
        ``posted`` in the control channel goes on to routing; a
        ``reaction_added`` is this service's click (#932)."""
        event = str(payload.get("event") or "")
        if event == "reaction_added":
            self._schedule(self._route_reaction(payload.get("data") or {}))
            return
        if event != "posted":
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

    # -- reactions as clicks (#932) --------------------------------------------------

    async def _route_reaction(self, data: dict[str, Any]) -> None:
        """A reaction is this bridge's button: Mattermost's interactive
        actions would post to a callback URL, which would cost the daemon
        the dial-out property the bridge is built on, while a reaction
        arrives on the websocket already open. The seeded emoji are the
        affordance; a click lands on the same ``choice`` / ``approve`` paths
        the other bridges' components use."""
        reaction = _decode_json(data.get("reaction"))
        if reaction is None:
            return
        user_id = str(reaction.get("user_id") or "")
        post_id = str(reaction.get("post_id") or "")
        emoji = str(reaction.get("emoji_name") or "")
        # Our own seeded reactions come back as events; they are the
        # affordance, not an answer.
        if not post_id or not user_id or user_id == self._user_id:
            return
        if user_id not in self._names:
            await self._lookup_name(user_id)
        name = self._names.get(user_id, user_id)
        author = f"Mattermost user `{name}`"
        if emoji in CHOICE_EMOJI:
            await self._choice_reacted(post_id, emoji, author, user_id, name)
        elif emoji == GATE_EMOJI:
            await self._gate_reacted(post_id, author)

    async def _choice_reacted(
        self, post_id: str, emoji: str, author: str, user_id: str, name: str
    ) -> None:
        """Anyone may answer, not just the asker; who did is recorded.

        An accepted answer leaves the post looking the way a Discord one
        does: the question still readable, the answer and who gave it under
        it, and the affordance gone — the seeded digits come back off, so
        nobody clicks a question that is already settled.
        """
        outstanding = self._outstanding(post_id)
        if outstanding is None:
            await self._nudge_expired(post_id)
            return
        choices = outstanding.question.choices
        index = CHOICE_EMOJI.index(emoji)
        if index >= len(choices):
            return
        value = choices[index].value
        try:
            accepted = bool(
                self._answer_choice(post_id, value, author, author_id=user_id, author_name=name)
            )
        except Exception:
            log.warning("mattermost.choice_answer_failed", value=value, exc_info=True)
            return
        if not accepted:
            return
        body, seeded = self._choice_posts.pop(post_id, ("", len(choices)))
        self._choice_expired.discard(post_id)
        answered = f"_Answered: **{value}** (by {name})._"
        # ``body`` is the post exactly as it went out — already clipped and
        # already mention-safe — so it is not passed through the guard a
        # second time; the answer line names a handle without an ``@``.
        rewritten = f"{body}\n\n{answered}" if body else answered
        try:
            await self.client.patch_post(
                post_id,
                {"message": _clip(rewritten, self.mattermost.max_message_chars)},
            )
        except Exception:
            log.debug("mattermost.choice_edit_failed", post=post_id, exc_info=True)
        await self._unseed(post_id, CHOICE_EMOJI[:seeded])

    async def _nudge_expired(self, post_id: str) -> None:
        """A reaction on a question the bridge no longer holds: expired, or
        asked before a restart (nothing about a question is persisted).

        Discord and Slack answer that click privately. A Mattermost bot
        account cannot post ephemerally, so the nudge goes in the thread
        under the question — once, and only on a post this bridge actually
        seeded, because a digit on anything else is somebody's ordinary
        emoji and not a misfired click. A question that was *answered* is
        not nudged: its post already says so and its digits are gone.
        """
        if post_id not in self._choice_posts or post_id in self._choice_expired:
            return
        self._choice_expired.add(post_id)
        log.info("mattermost.choice_reaction_expired", post=post_id)
        await self._send(
            MattermostTarget(self.mattermost.channel_id or "", root_id=post_id),
            EXPIRED_CLICK_NOTE,
        )

    def _remember_choice_post(self, post_id: str, body: str, seeded: int) -> None:
        """Hold a question's posted body and how many digits went on it, so
        the answer can rebuild the post. Bounded: a daemon that runs for
        months must not keep one entry per question it ever asked."""
        self._choice_posts[post_id] = (body, seeded)
        while len(self._choice_posts) > CHOICE_POST_CAP:
            oldest = next(iter(self._choice_posts))
            self._choice_posts.pop(oldest, None)
            self._choice_expired.discard(oldest)

    async def _gate_reacted(self, post_id: str, author: str) -> None:
        """The approve reaction: the same call the typed command makes,
        answered in the thread. It never clears the affordance — a failed
        landing re-opens the gate and reacting again works."""
        run_id = self._gate_run_for(post_id)
        if run_id is None:
            return
        loop_ref = self.loop_ref
        if loop_ref is None:
            return
        try:
            reply = await asyncio.get_event_loop().run_in_executor(
                None, functools.partial(loop_ref.approve_merge, run_id, by=author)
            )
        except (KeyError, ValueError) as exc:
            reply = f"failed: {exc.args[0] if exc.args else exc}"
        except Exception:
            log.warning("mattermost.gate_reaction_failed", run=run_id, exc_info=True)
            reply = (
                f"something went wrong — `{self.chat.command_prefix} merge` / `release` still work"
            )
        await self._send(
            MattermostTarget(self.mattermost.channel_id or "", root_id=post_id), str(reply)
        )

    def _gate_run_for(self, post_id: str) -> str | None:
        """Which run's gate this prompt belongs to. The map is filled when
        the prompt is posted; after a restart the prompt survives in the
        store but the map does not, so fall back to the open gates (there
        are few) and their recorded prompt ids."""
        known = self._gate_posts.get(post_id)
        if known is not None:
            return known
        try:
            for gate in self.dstore.open_merge_gates():
                where = self.dstore.gate_prompt(gate.run_id, self.backend)
                if where is not None and where[1] == post_id:
                    self._gate_posts[post_id] = str(gate.run_id)
                    return str(gate.run_id)
        except Exception:
            log.debug("mattermost.gate_lookup_failed", post=post_id, exc_info=True)
        return None

    # -- outbound -------------------------------------------------------------------

    async def _control_channel(self) -> Any:
        return MattermostTarget(self.mattermost.channel_id or "")

    async def _thread_handle(self, thread_id: str) -> Any:
        if thread_id == self.mattermost.channel_id:
            return MattermostTarget(thread_id)
        return MattermostTarget(self.mattermost.channel_id or "", root_id=thread_id)

    async def _fetch_message(self, channel: Any, message_id: str) -> Any:
        """The handle for a post we made, or None when it is not there any
        more.

        This used to be free — ``(channel_id, post_id)`` is everything
        ``patch`` and ``reactions`` need, so the handle could be built
        without asking. But no caller wants a handle: each one is asking
        whether the *message* is still there, and each treats None as "it
        is gone, put a new one up". A fabricated handle answered "still
        there" every time, so a gate prompt somebody deleted was never
        re-posted, and a deleted status message was never replaced — every
        later edit 404ing into a warning instead. One GET per recovery path
        is the honest price.

        A status the server could not answer with is *not* an absence: a
        transient failure propagates rather than being reported as a
        missing post, which would put a duplicate up.
        """
        try:
            post = await self.client.get_post(message_id)
        except MattermostApiError as exc:
            if exc.status not in _MISSING_STATUSES:
                raise
            log.debug("mattermost.post_gone", post=message_id, status=exc.status)
            return None
        if _deleted_at(post):
            return None
        return MattermostMessage(
            str(post.get("channel_id") or getattr(channel, "channel", "") or ""),
            str(post.get("id") or message_id),
            str(post.get("root_id") or "") or None,
        )

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
        allowed-mentions control, so agent prose would otherwise ping), a
        card becomes one coloured attachment, and a result's files are
        uploaded under the attachment cap. ``reply_to`` is accepted for the
        shared signature and ignored — a reply here is a thread post, which
        ``target`` already expresses."""
        notes: list[str] = []
        file_ids: list[str] = []
        if files:
            attach, notes = self._split_files(files)
            if len(attach) > MAX_POST_FILES:
                # Mattermost takes five files on a post; the rest are named
                # with their host path rather than dropped, as on Discord.
                attach, rest = attach[:MAX_POST_FILES], attach[MAX_POST_FILES:]
                notes = [*notes, self._files_note([str(path) for path in rest])]
            file_ids, failed = await self._upload(target.channel, attach)
            notes.extend(failed)
        if notes:
            text = "\n".join(part for part in (text, "\n".join(notes)) if part)
        card = embed if embed is not None and self.mattermost.embeds else None
        body = self._body(text, None if card else embed, mention_users=mention_users)
        if not body and card is None and not file_ids:
            return None
        payload: dict[str, Any] = {"channel_id": target.channel, "message": body}
        if target.root_id:
            payload["root_id"] = target.root_id
        if card is not None:
            payload["props"] = {"attachments": [embed_attachment(card)]}
        if file_ids:
            payload["file_ids"] = file_ids
        return await self._post(target, payload)

    async def _post(self, target: Any, payload: dict[str, Any]) -> Any:
        """Create one post, converting a failure into None the way every
        caller expects. A card the server rejects is retried without it, so
        a run's chronology never goes missing over presentation."""
        try:
            post = await self.client.create_post(payload)
        except Exception as exc:
            if self._report_channel_error(exc):
                return None
            if "props" in payload:
                log.warning(
                    "mattermost.attachment_send_failed",
                    target=target.id,
                    action="retrying text-only",
                    exc_info=True,
                )
                retry = {k: v for k, v in payload.items() if k != "props"}
                if not retry.get("message"):
                    return None
                return await self._post(target, retry)
            log.warning(
                "mattermost.send_failed",
                target=target.id,
                chars=len(str(payload.get("message") or "")),
                exc_info=True,
            )
            return None
        return MattermostMessage(
            str(post.get("channel_id") or target.channel),
            str(post.get("id") or ""),
            target.root_id,
        )

    async def _upload(self, channel: str, paths: Sequence[Path]) -> tuple[list[str], list[str]]:
        """(file ids, notes): every file that uploaded, and one line for each
        that did not — a named file is never silently dropped."""
        ids: list[str] = []
        notes: list[str] = []
        for path in paths:
            try:
                file_id = await self.client.upload_file(channel, path.name, path.read_bytes())
            except Exception:
                log.warning("mattermost.upload_failed", file=path.name, exc_info=True)
                file_id = ""
            if file_id:
                ids.append(file_id)
            else:
                notes.append(
                    f"📎 `{path.name}` — upload failed, kept on the daemon host at `{path}`"
                )
        return ids, notes

    def _body(self, text: str, embed: EmbedSpec | None, *, mention_users: bool = False) -> str:
        """The post text: clipped, mention-safe and preview-free. ``embed``
        reaches here only when cards are off (``[mattermost] embeds =
        false``), in which case it is rendered into the text as its plain
        twin.

        Both guards run after the clip, as the other bridges' do: each adds
        a few characters, and Mattermost's real ceiling is the server's
        (16383 by default), far above the shared ``max_message_chars``.
        """
        limit = self.mattermost.max_message_chars
        parts = [part for part in (text, embed.as_text() if embed is not None else "") if part]
        body = _clip("\n\n".join(parts), limit)
        body = defuse_unfurls(body)
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
        """``posts/{id}/patch`` with the same clipping, mention safety and
        card conversion as a send. Errors propagate: callers log them with
        their own context."""
        card = embed if embed is not None and self.mattermost.embeds else None
        body: dict[str, Any] = {"message": self._body(text, None if card else embed)}
        if card is not None:
            body["props"] = {"attachments": [embed_attachment(card)]}
        await self.client.patch_post(message.post_id, body)

    async def _add_reaction(self, message: Any, emoji: str) -> None:
        name = EMOJI_NAMES.get(emoji)
        if name is None:
            raise ValueError(f"no Mattermost reaction name for {emoji!r}")
        if not self._user_id:
            return
        if await self._save_reaction(message.post_id, emoji, name) and emoji == ACK_RECEIVED:
            # The one mark the asker's own client is known to drop — put
            # it on again once their create-post reply has done its worst.
            task = asyncio.ensure_future(self._reassert(message.post_id, emoji, name))
            self._reasserts.add(task)
            task.add_done_callback(self._reasserts.discard)

    async def _save_reaction(self, post_id: str, emoji: str, name: str) -> bool:
        """One reaction save; True when the server took it. A refused save
        is reported (once per name) rather than raised, anything else
        propagates for the caller to log with its own context."""
        assert self._user_id is not None
        try:
            await self.client.create_reaction(self._user_id, post_id, name)
        except MattermostApiError as exc:
            # Reacting twice is not an error worth raising: the mark the
            # caller wanted is already there.
            if exc.status != 400:
                raise
            self._report_reaction_refused(emoji, name, exc)
            return False
        return True

    async def _reassert(self, post_id: str, emoji: str, name: str) -> None:
        """Save the received mark a second time, after ``RECEIVED_REASSERT_S``.
        Nothing here can be worth surfacing: the first save already landed,
        and this one exists only to outlive the asker's client wiping it."""
        await asyncio.sleep(self._reassert_s)
        if not self._user_id:
            return
        try:
            await self._save_reaction(post_id, emoji, name)
        except Exception:
            log.debug("mattermost.reassert_failed", post=post_id, emoji_name=name, exc_info=True)

    def _report_reaction_refused(self, emoji: str, name: str, exc: MattermostApiError) -> None:
        """A 400 on a reaction, said out loud once per emoji name.

        Two very different things land here. Reacting twice is harmless —
        the mark the caller wanted is already on the post. An instance whose
        emoji set does not carry ``name`` is not: the ack that tells a
        person *your ask was picked up* never appears, and the base bridge
        logs the miss at debug, so the only symptom is a message the bot
        seems not to have noticed. One warning per name per process is cheap
        enough to always pay, and the server's own message is what tells the
        two apart.
        """
        if name in self._reaction_refusals:
            return
        self._reaction_refusals.add(name)
        log.warning(
            "mattermost.reaction_refused",
            emoji=emoji,
            emoji_name=name,
            status=exc.status,
            detail=exc.detail,
            hint="harmless if the reaction was already on the post; if the bridge's ack "
            f"marks never appear, check that `:{name}:` resolves in the control channel — "
            "this instance's emoji set does not carry the name the bridge reacts with",
        )

    async def _create_thread(self, headline: Any, name: str) -> Any:
        # Mattermost threads have no name and no object of their own: the
        # thread *is* the reply stream under the headline post.
        return MattermostTarget(headline.channel, root_id=headline.post_id)

    # -- seeded affordances ----------------------------------------------------------

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
        """A clarifying question seeded with one emoji per choice.

        The message body is the same numbered prose the base seam posts, so
        reacting is an extra way in and typing still answers; a post whose
        seeding fails is still a working question. The digits match the
        prose's numbering, which is what makes the affordance legible
        without a label on it.
        """
        body = render_prose(question)
        if text and text.strip() and text.strip() != question.prompt.strip():
            body = f"{text.strip()}\n\n{body}"
        posted = await self._send(target, body, mention_users=mention_users)
        if posted is not None:
            seeded = CHOICE_EMOJI[: len(question.choices)]
            self._remember_choice_post(
                posted.post_id,
                self._body(body, None, mention_users=mention_users),
                len(seeded),
            )
            await self._seed(posted, seeded)
        return posted

    async def _send_gate(self, target: Any, text: str, gate: MergeGate) -> Any:
        """The approval prompt with the approve reaction seeded on it, on top
        of the base prose — the typed command stays in the body, so a prompt
        whose seeding fails still works by typing."""
        posted = await self._send(target, text, mention_users=True)
        if posted is not None:
            self._gate_posts[posted.post_id] = str(gate.run_id)
            await self._seed(posted, (GATE_EMOJI,))
        return posted

    async def _finalize_gate_message(self, message: Any, text: str) -> None:
        """A resolved gate loses its approve reaction along with the
        rewrite, the way a Discord gate loses its button and a Slack one its
        block: the seeded ✅ *is* this bridge's button, and one left
        standing on a merged gate invites a click that can only fail."""
        await self._edit(message, text)
        post_id = str(getattr(message, "post_id", "") or "")
        self._gate_posts.pop(post_id, None)
        await self._unseed(post_id, (GATE_EMOJI,))

    async def _seed(self, message: Any, emoji: Sequence[str]) -> None:
        """React with each affordance in order. A failure is logged and
        skipped: the prose underneath is the real interface."""
        if not self._user_id:
            return
        for name in emoji:
            try:
                await self.client.create_reaction(self._user_id, message.post_id, name)
            except Exception:
                log.debug("mattermost.seed_failed", post=message.post_id, emoji=name, exc_info=True)

    async def _unseed(self, post_id: str, emoji: Sequence[str]) -> None:
        """Take the seeded affordances back off a post that is no longer
        answerable — Discord clears its view, Slack drops its blocks, and
        this is the same act in reactions.

        It matters more here than it looks: a seeded emoji on a merged gate
        or an answered question is indistinguishable from a live button, so
        the next person clicks it and nothing happens. A removal that fails
        is cosmetic only, so it is logged and skipped.
        """
        if not self._user_id:
            return
        for name in emoji:
            try:
                await self.client.delete_reaction(self._user_id, post_id, name)
            except Exception:
                log.debug("mattermost.unseed_failed", post=post_id, emoji=name, exc_info=True)

    # -- identity -------------------------------------------------------------------

    def _message_id(self, message: Any) -> str:
        return str(getattr(message, "post_id", "") or "")

    def _handle_id(self, target: Any) -> str:
        return str(getattr(target, "id", "") or "")

    def _typing(self, channel: Any) -> Any:
        """Discord's ``channel.typing()``, on Mattermost.

        The ⏳ ack says a message was *received*; this says the daemon is
        *still on it*, which is what a concierge turn that thinks for a
        minute otherwise leaves a person guessing about.
        """
        return _MattermostTyping(self, channel)

    async def _send_typing(self, target: Any) -> None:
        """One ``user_typing`` frame for a target. Never raises: an
        indicator that fails costs nothing but its own absence, and must not
        take the turn it is decorating down with it."""
        send = getattr(self.client, "send_typing", None)
        if send is None:
            return
        try:
            await send(getattr(target, "channel", "") or "", getattr(target, "root_id", "") or "")
        except Exception:
            log.debug("mattermost.typing_failed", exc_info=True)

    def mention_user(self, user_id: str) -> str:
        """``@handle`` — a Mattermost mention is a username, so a stored user
        id is resolved through the name cache. An id whose handle could not
        be learned renders as itself rather than as a broken ping."""
        return f"@{self._names.get(user_id, user_id)}"

    async def _resolve_mentions(self, user_ids: Iterable[str]) -> None:
        """Learn the handles this bridge's mentions are spelled with.

        A Mattermost mention is ``@username``, and the name cache is filled
        by the *inbound* path — so it holds whoever has posted since this
        process started, and nobody else. Every id that reaches here came
        off the store instead: a run watch, a gate's notify list, a review
        ask. After a restart the cache has none of them, and the notice
        that tells somebody their run finished went out carrying a bare
        26-character id — text, not a notification, to the one person who
        asked to be told.

        One users call per id never seen; ``_lookup_name`` caches the id
        itself when the lookup fails, so a deactivated account is not
        looked up again on every notice.
        """
        for user_id in user_ids:
            if user_id and user_id not in self._names and self._owns_user_id(user_id):
                await self._lookup_name(user_id)

    def _owns_user_id(self, user_id: str) -> bool:
        return bool(_MATTERMOST_ID_RE.match(user_id))

    def thread_link(self, thread: ChatThread) -> str:
        if thread.thread_id == thread.channel_id:
            return f"~{thread.channel_id}"
        link = thread_permalink(self.mattermost.url or "", self._team, thread.thread_id)
        return f"[thread]({link})" if link else thread.thread_id


class _MattermostTyping:
    """ "…is typing" for as long as the concierge is thinking.

    Mattermost's indicator is a websocket frame with a short client-side
    life, not a state the server holds, so it has to be repeated; the beat
    runs as a task for the body of the ``async with`` and is cancelled the
    moment the turn resolves. A frame the service refuses ends nothing: the
    beat logs at debug and keeps its rhythm.
    """

    def __init__(self, bridge: MattermostBridge, target: Any) -> None:
        self.bridge = bridge
        self.target = target
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> None:
        self._task = asyncio.ensure_future(self._beat())

    async def __aexit__(self, *exc: object) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _beat(self) -> None:
        while True:
            await self.bridge._send_typing(self.target)
            await asyncio.sleep(TYPING_INTERVAL_S)


def _deleted_at(post: dict[str, Any]) -> bool:
    """Whether a post the server still returned is soft-deleted. A missing
    post is normally a 404, but Mattermost keeps deleted rows and some
    paths hand one back with ``delete_at`` set."""
    try:
        return int(post.get("delete_at") or 0) > 0
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return False


def _decode_json(raw: Any) -> dict[str, Any] | None:
    """A websocket event carries its payload objects as JSON *strings*."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        log.debug("mattermost.bad_payload", exc_info=True)
        return None
    return parsed if isinstance(parsed, dict) else None


#: A ``posted`` event carries its post the same way.
_decode_post = _decode_json


__all__ = [
    "EXPIRED_CLICK_NOTE",
    "MAX_POST_FILES",
    "MattermostApiError",
    "MattermostBridge",
    "MattermostClient",
    "MattermostMessage",
    "MattermostTarget",
]
