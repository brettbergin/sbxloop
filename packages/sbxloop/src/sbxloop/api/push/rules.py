"""Which chronology events are news, and for whom.

The same rules a chat client applies before showing a notice, decided here
with what the daemon knows first-hand — who wrote a message, who asked for
a turn, who may decide a gate — instead of what a client can infer:

- **mention**: a person's message (never the recipient's own) that
  addresses the recipient by ``@username``, word-bounded and
  case-insensitively, in a channel the recipient can open.
- **work**: work delivered, or a reply finished, for a turn the recipient
  asked for; a job's ``work`` attention to the channel's members.
- **failure**: the same when it ended failed, blocked, cancelled or
  abandoned, or the reply failed; a job's ``failure`` attention.
- **gate**: a job's ``action_required`` attention, or a merge gate
  opening, to the people who may approve it and can see where it is.

Historical events (a job imported from before the daemon knew it) are
never news. :func:`allowed` then narrows by a device's own preferences.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from sbxloop.api.collaboration import CollaborationStore, Member, _message
from sbxloop.api.publicids import run_public_id
from sbxloop.daemon.controls.principal import ROLE_CAPABILITIES
from sbxloop.db.collaboration_models import (
    ChannelMemberRow,
    ChannelRow,
    MessageRow,
    TurnRow,
)
from sbxloop.db.daemon_models import WorkItemRow

MESSAGE_CREATED = "collaboration.message.created"
WORK_DELIVERED = "collaboration.work.delivered"
TURN_COMPLETED = "collaboration.turn.completed"
TURN_FAILED = "collaboration.turn.failed"
ATTENTION = "collaboration.external_work.attention"
GATE_OPENED = "gate.opened"
#: Every event type a notice can come from.
TYPES: frozenset[str] = frozenset(
    {MESSAGE_CREATED, WORK_DELIVERED, TURN_COMPLETED, TURN_FAILED, ATTENTION, GATE_OPENED}
)

#: A work state that means the work did not get done.
FAILED_STATES = frozenset({"failed", "blocked", "cancelled", "abandoned"})
#: The device preference each kind answers to.
KIND_PREF: dict[str, str] = {
    "mention": "mentions",
    "gate": "gates",
    "work": "work",
    "failure": "failures",
}
#: How a job's attention kind is pushed.
ATTENTION_KINDS: dict[str, str] = {
    "work": "work",
    "failure": "failure",
    "action_required": "gate",
}
#: A notification body is cut to this many characters, ellipsis included.
BODY_LIMIT = 140
#: A username longer than this is not one.
HANDLE_LIMIT = 200


@dataclass(frozen=True, slots=True)
class Event:
    """One chronology row, as the rules read it."""

    seq: int
    type: str
    channel_id: str | None
    run_id: str | None
    item_id: str | None
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Notice:
    """News for one person."""

    user_id: str
    kind: str
    channel_id: str | None
    turn_id: str | None
    title: str
    body: str
    #: Two notices with the same key for one person are one notice (a gate
    #: announced both as it opens and through its conversation).
    dedupe: str | None = None


def mentions(text: str, username: str) -> bool:
    """Whether ``text`` addresses ``username`` as ``@username``."""
    handle = username.strip()
    if not handle or len(handle) > HANDLE_LIMIT:
        return False
    pattern = rf"(^|[^\w@.])@{re.escape(handle)}(?![\w-])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def excerpt(text: str) -> str:
    """``text`` on one line, short enough for a notification body."""
    flat = " ".join(text.split())
    return flat if len(flat) <= BODY_LIMIT else flat[: BODY_LIMIT - 1] + "…"


def allowed(prefs: Mapping[str, Any], kind: str, channel_id: str | None) -> bool:
    """Whether a device's preferences let a notice of ``kind`` through."""
    if kind == "test":
        return True
    if not prefs.get(KIND_PREF.get(kind, kind), True):
        return False
    per_channel = prefs.get("per_channel") or {}
    mode = per_channel.get(channel_id, "all") if channel_id else "all"
    if mode == "none":
        return False
    if mode == "mentions":
        return kind == "mention"
    return True


def _can_decide(member: Member) -> bool:
    return "gates:approve" in ROLE_CAPABILITIES[member.role]


class NoticeRules:
    """Turns chronology events into notices. ``agent_name`` names an agent
    by slug (``None``: the default assistant) the way the chat does."""

    def __init__(self, agent_name: Callable[[str | None], str]) -> None:
        self.agent_name = agent_name

    def notices(self, session: Any, event: Event) -> list[Notice]:
        if event.type == MESSAGE_CREATED:
            return self._mention(session, event)
        if event.type == WORK_DELIVERED:
            return self._delivered(session, event)
        if event.type in (TURN_COMPLETED, TURN_FAILED):
            return self._turn(session, event)
        if event.type == ATTENTION:
            return self._attention(session, event)
        if event.type == GATE_OPENED:
            return self._gate(session, event)
        return []

    # -- who ---------------------------------------------------------------------

    @staticmethod
    def _members(session: Any) -> list[Member]:
        return [m for m in CollaborationStore._join(session) if m.user.active]

    @staticmethod
    def _channel(session: Any, channel_id: str | None) -> ChannelRow | None:
        if not channel_id:
            return None
        row = session.get(ChannelRow, channel_id)
        if row is None or row.deleted_at is not None or row.state != "active":
            return None
        return row  # type: ignore[no-any-return]

    @staticmethod
    def _joined(session: Any, channel_id: str) -> set[str]:
        return set(
            session.scalars(
                select(ChannelMemberRow.user_id).where(ChannelMemberRow.channel_id == channel_id)
            )
        )

    def _viewers(self, session: Any, channel: ChannelRow) -> list[Member]:
        """The active members who can open ``channel``."""
        joined = self._joined(session, str(channel.id))
        return [
            member
            for member in self._members(session)
            if member.workspace_id == channel.workspace_id
            and (channel.visibility == "workspace" or member.user.id in joined)
        ]

    def _asker(self, session: Any, turn_id: str | None) -> str | None:
        """The person who asked for a turn: its author, or for a turn
        recorded before authors were, the author of the message that opened
        it. An agent's follow-up turn was asked by nobody."""
        if not turn_id:
            return None
        turn = session.get(TurnRow, turn_id)
        if turn is None:
            return None
        if turn.author_kind is not None:
            return str(turn.author_id) if turn.author_kind == "human" and turn.author_id else None
        opening = session.get(MessageRow, turn.input_message_id)
        if opening is None:
            return None
        author = _message(session, opening).author
        return author.id if author.kind == "human" else None

    def _visible_asker(self, session: Any, event: Event, turn_id: str | None) -> str | None:
        channel = self._channel(session, event.channel_id)
        asker = self._asker(session, turn_id)
        if channel is None or asker is None:
            return None
        if all(member.user.id != asker for member in self._viewers(session, channel)):
            return None
        return asker

    def _turn_agent(self, session: Any, turn_id: str | None) -> str:
        turn = session.get(TurnRow, turn_id) if turn_id else None
        targets = json.loads(turn.targets_json or "[]") if turn is not None else []
        slug = next((str(t) for t in targets if isinstance(t, str) and t), None)
        return self.agent_name(slug)

    # -- what --------------------------------------------------------------------

    def _mention(self, session: Any, event: Event) -> list[Notice]:
        if event.data.get("author_kind") != "human":
            return []
        channel = self._channel(session, event.channel_id)
        row = session.get(MessageRow, str(event.data.get("message_id") or ""))
        if channel is None or row is None:
            return []
        message = _message(session, row)
        author = message.author
        if author.kind != "human":
            return []
        name = author.display_name or "Someone"
        body = excerpt(message.content) or "They mentioned you in the chat."
        return [
            Notice(
                user_id=member.user.id,
                kind="mention",
                channel_id=message.channel_id,
                turn_id=message.turn_id,
                title=f"{name} mentioned you",
                body=body,
            )
            for member in self._viewers(session, channel)
            if member.user.id != author.id and mentions(message.content, member.user.username)
        ]

    def _delivered(self, session: Any, event: Event) -> list[Notice]:
        turn_id = str(event.data.get("turn_id") or "") or None
        asker = self._visible_asker(session, event, turn_id)
        row = session.get(MessageRow, str(event.data.get("message_id") or ""))
        if asker is None or row is None:
            return []
        work = json.loads(row.work_json) if row.work_json else {}
        work = work if isinstance(work, dict) else {}
        state = str(work.get("state") or "")
        label = str(work.get("title") or "") or "your work"
        slug = work.get("agent_slug") or row.agent_slug
        agent = self.agent_name(str(slug) if slug else None)
        if state in FAILED_STATES:
            kind, title = "failure", f"{agent} could not finish {label}"
            body = f"The run ended {state}. The details are in the chat."
        else:
            kind, title = "work", f"{agent} delivered {label}"
            body = (
                "Changes merged. The result is in the chat."
                if state == "merged"
                else "The result is in the chat."
            )
        return [Notice(asker, kind, event.channel_id, turn_id, title, body)]

    def _turn(self, session: Any, event: Event) -> list[Notice]:
        turn_id = str(event.data.get("turn_id") or "") or None
        asker = self._visible_asker(session, event, turn_id)
        if asker is None:
            return []
        agent = self._turn_agent(session, turn_id)
        if event.type == TURN_FAILED:
            error = str(event.data.get("error") or "").strip()
            return [
                Notice(
                    asker,
                    "failure",
                    event.channel_id,
                    turn_id,
                    f"{agent} could not reply",
                    excerpt(error) if error else f"{agent} could not finish that reply.",
                )
            ]
        return [
            Notice(
                asker,
                "work",
                event.channel_id,
                turn_id,
                f"{agent} replied",
                "A new reply is waiting in the chat.",
            )
        ]

    def _attention(self, session: Any, event: Event) -> list[Notice]:
        data = event.data
        kind = ATTENTION_KINDS.get(str(data.get("kind") or ""))
        title = data.get("title")
        if data.get("historical") is not False or kind is None:
            return []
        if not isinstance(title, str) or not title:
            return []
        channel = self._channel(session, event.channel_id)
        if channel is None:
            return []
        viewers = self._viewers(session, channel)
        if kind == "gate":
            recipients = [m.user.id for m in viewers if _can_decide(m)]
        else:
            # The people in the conversation, not everyone who could open it.
            joined = self._joined(session, str(channel.id)) | {str(channel.user_id)}
            recipients = [m.user.id for m in viewers if m.user.id in joined]
        body = data.get("body")
        run_id = data.get("run_id")
        dedupe = f"gate:{run_id}" if kind == "gate" and isinstance(run_id, str) and run_id else None
        return [
            Notice(
                user_id,
                kind,
                str(channel.id),
                None,
                title,
                excerpt(body)
                if isinstance(body, str) and body
                else "The details are in the conversation.",
                dedupe,
            )
            for user_id in recipients
        ]

    def _gate(self, session: Any, event: Event) -> list[Notice]:
        channel = self._channel(session, event.channel_id)
        if event.channel_id and channel is None:
            return []
        members = self._viewers(session, channel) if channel is not None else self._members(session)
        item = session.get(WorkItemRow, event.item_id) if event.item_id else None
        what = str(item.title) if item is not None and item.title else ""
        body = (
            f"{excerpt(what)} is waiting for your decision."
            if what
            else "Work is waiting for your decision."
        )
        dedupe = f"gate:{run_public_id(event.run_id)}" if event.run_id else None
        return [
            Notice(
                member.user.id,
                "gate",
                event.channel_id,
                None,
                "Decision needed",
                body,
                dedupe,
            )
            for member in members
            if _can_decide(member)
        ]


__all__ = [
    "TYPES",
    "Event",
    "Notice",
    "NoticeRules",
    "allowed",
    "excerpt",
    "mentions",
]
