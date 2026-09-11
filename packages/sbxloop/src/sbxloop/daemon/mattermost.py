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
import functools
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from sbxloop.chatservices import MATTERMOST_TOKEN_ENV
from sbxloop.config import ChatBackend, Config, MattermostConfig
from sbxloop.daemon.chat import ChatBridge, Inbound
from sbxloop.daemon.chat_choices import ChoiceQuestion, render_prose
from sbxloop.daemon.chat_routing import MATTERMOST_MENTION_RE
from sbxloop.daemon.concierge import Concierge
from sbxloop.daemon.discord_format import EmbedSpec, _clip
from sbxloop.daemon.mattermost_format import (
    CHOICE_EMOJI,
    EMOJI_NAMES,
    GATE_EMOJI,
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

    async def upload_file(self, channel_id: str, name: str, content: bytes) -> str:
        """Upload one attachment, returning its file id ("" when the server
        accepted the call but named no file)."""
        form = self._aiohttp.FormData()
        form.add_field("channel_id", channel_id)
        form.add_field("files", content, filename=name)
        result = await self._request("POST", "/files", data=form)
        infos = result.get("file_infos") or []
        return str(infos[0].get("id") or "") if infos else ""

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
        # Gate prompt post id -> run id, so a ✅ reaction finds its gate.
        # In memory; a restart repopulates it lazily from the store.
        self._gate_posts: dict[str, str] = {}

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
        """Anyone may answer, not just the asker; who did is recorded. A
        reaction on a question the bridge no longer holds is left alone —
        the typed route still works and a stale note would be noise."""
        outstanding = self._outstanding(post_id)
        if outstanding is None:
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
        try:
            await self.client.patch_post(
                post_id,
                {"message": f"_Answered: **{value}** (by {name})._"},
            )
        except Exception:
            log.debug("mattermost.choice_edit_failed", post=post_id, exc_info=True)

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
        allowed-mentions control, so agent prose would otherwise ping), a
        card becomes one coloured attachment, and a result's files are
        uploaded under the attachment cap. ``reply_to`` is accepted for the
        shared signature and ignored — a reply here is a thread post, which
        ``target`` already expresses."""
        notes: list[str] = []
        file_ids: list[str] = []
        if files:
            attach, notes = self._split_files(files)
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
        """The post text: clipped and mention-safe. ``embed`` reaches here
        only when cards are off (``[mattermost] embeds = false``), in which
        case it is rendered into the text as its plain twin."""
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
            await self._seed(posted, CHOICE_EMOJI[: len(question.choices)])
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
    "MattermostApiError",
    "MattermostBridge",
    "MattermostClient",
    "MattermostMessage",
    "MattermostTarget",
]
