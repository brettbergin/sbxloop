"""The console's side of the local chat bridge.

Reading is a cursor per channel over the mailbox — new rows after the last
id, plus rows the console already holds whose ``updated_at`` moved since
the last pull (an edit, a reaction, a claim, a resolved gate), so a status
line or a tool digest repaints in place the way it does on Discord. Writing
is the three inbound kinds the bridge takes: a typed message, a choice
click, an approve click.

The routing rules are the bridge's (:mod:`sbxloop.daemon.chat_routing`):
``!sbx …`` is a command; a message addressed to the bot — the literal
``@sbx`` token in the text, or a reply to one of its rows — is a concierge
turn in the control channel and a steer in a run thread; plain text is left
alone. :func:`compose_outbound` is where the console's "address the bot"
gesture becomes that token, and :func:`is_addressed` asks
:func:`~sbxloop.daemon.chat_routing.route_message` itself, so the rules
live in one place.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sbxloop.config import TUI_CONTROL_CHANNEL
from sbxloop.daemon.chat_choices import Choice, ChoiceQuestion, match_free_text
from sbxloop.daemon.chat_routing import route_message
from sbxloop.daemon.local import LOCAL_BOT_ID, LOCAL_MENTION_RE
from sbxloop.daemon.mailbox import MailboxClient
from sbxloop.daemon.store import LocalMessage

#: The token the console inserts to address the bot.
MENTION = f"@{LOCAL_BOT_ID}"


def compose_outbound(text: str, *, addressed: bool, prefix: str = "!sbx") -> str:
    """The text to write for what the operator typed.

    A command (``!sbx …``) goes as typed. With the address gesture on, the
    mention token is guaranteed once at the front — unless the operator
    already wrote it anywhere in the text. Without it the text goes as
    typed: unaddressed, and left alone by the bridge like a person talking
    to a colleague on Discord."""
    body = text.strip()
    if not body:
        return ""
    if body.startswith(prefix):
        return body
    if addressed and not LOCAL_MENTION_RE.search(body):
        return f"{MENTION} {body}"
    return body


def is_addressed(text: str, *, prefix: str = "!sbx") -> bool:
    """Whether the bridge would act on ``text`` as typed — the routing
    function's own answer, on the control channel's terms."""
    route = route_message(
        content=text,
        channel_id=TUI_CONTROL_CHANNEL,
        author_is_bot=False,
        mentioned_ids=frozenset(LOCAL_MENTION_RE.findall(text)),
        reply_to_bot=False,
        control_channel_id=TUI_CONTROL_CHANNEL,
        prefix=prefix,
        bot_user_id=LOCAL_BOT_ID,
        is_run_thread=False,
        mention_re=LOCAL_MENTION_RE,
    )
    return route.kind != "ignore"


@dataclass(frozen=True)
class ChoiceSpec:
    """What a ``choices`` row offers, as the bridge's own question model
    plus the row's deadline and answer."""

    question: ChoiceQuestion
    expires_at: float | None
    answered: dict[str, Any] | None

    def open(self, now: float) -> bool:
        if self.answered is not None:
            return False
        return self.expires_at is None or now < self.expires_at

    def value_for(self, index: int) -> str | None:
        """The 1-based choice's value — the typed ``"2"`` route's answer."""
        return match_free_text(self.question, str(index))


def choice_spec(row: LocalMessage) -> ChoiceSpec | None:
    """The question a ``choices`` row carries, parsed from the JSON the
    local bridge wrote from a :class:`ChoiceQuestion`."""
    if row.kind != "choices" or not row.choices_json:
        return None
    try:
        data = json.loads(row.choices_json)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    choices = tuple(
        Choice(
            value=str(c.get("value", "")),
            label=str(c.get("label") or c.get("value", "")),
            description=str(c["description"]) if c.get("description") else None,
        )
        for c in data.get("choices") or ()
        if isinstance(c, dict) and c.get("value")
    )
    if not choices:
        return None
    expires = data.get("expires_at")
    answered = data.get("answered")
    return ChoiceSpec(
        question=ChoiceQuestion(
            prompt=str(data.get("prompt") or ""),
            choices=choices,
            allow_free_text=bool(data.get("allow_free_text", True)),
        ),
        expires_at=float(expires) if isinstance(expires, int | float) else None,
        answered=dict(answered) if isinstance(answered, dict) else None,
    )


@dataclass
class ChannelTail:
    """A cursor over one channel: what is new, what changed. Pulls are
    serialised — two workers racing the cursor would report the same rows
    twice."""

    channel_id: str
    after_id: int = 0
    since: float = 0.0
    #: How many of the newest rows the first pull shows; older ones are
    #: history the daemon keeps for `retention_days`, not a screenful.
    window: int = 200
    seen: dict[int, float] = field(default_factory=dict)
    skipped: int = 0
    _primed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def pull(
        self, mailbox: MailboxClient, *, now: float
    ) -> tuple[list[LocalMessage], list[LocalMessage]]:
        """``(new, changed)``: rows after the cursor, and rows the console
        holds whose ``updated_at`` moved."""
        with self._lock:
            if not self._primed:
                self._primed = True
                latest = mailbox.latest_ids().get(self.channel_id, 0)
                if latest > self.window:
                    self.after_id = latest - self.window
                    self.skipped = self.after_id
            new = mailbox.messages(self.channel_id, after_id=self.after_id)
            changed: list[LocalMessage] = []
            if self.after_id:
                for row in mailbox.changed_since(
                    self.channel_id, after_id=self.after_id, since=self.since
                ):
                    if row.updated_at > self.seen.get(row.id, 0.0):
                        changed.append(row)
            return new, changed

    def commit(self, new: list[LocalMessage], changed: list[LocalMessage], *, now: float) -> None:
        """Move the cursor past rendered rows — called once they are on
        screen, so a superseded pull never advances it."""
        with self._lock:
            for row in new:
                self.after_id = max(self.after_id, row.id)
                self.seen[row.id] = row.updated_at
            for row in changed:
                self.seen[row.id] = row.updated_at
            self.since = now - 1.0


class ChatSession:
    """One console's identity and its writes to the mailbox. Read-only is
    enforced here, once: every write answers ``None``."""

    def __init__(
        self,
        mailbox: MailboxClient,
        *,
        read_only: bool = False,
        prefix: str = "!sbx",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.mailbox = mailbox
        self.read_only = read_only
        self.prefix = prefix
        self.clock = clock
        # The console's writes not yet claimed by the daemon, shown pending.
        self.pending: set[int] = set()

    @property
    def operator(self) -> str:
        return self.mailbox.operator_name

    def send(
        self,
        channel_id: str,
        text: str,
        *,
        addressed: bool,
        reply_to_id: int | None = None,
    ) -> int | None:
        """Write what the operator typed; None when nothing was sent."""
        if self.read_only:
            return None
        # A reply to a bot row is addressed by the bridge's own rule
        # (reply_to_bot), so the text is left as typed.
        body = compose_outbound(text, addressed=addressed, prefix=self.prefix)
        if not body:
            return None
        row_id = self.mailbox.post(channel_id, body, now=self.clock(), reply_to_id=reply_to_id)
        self.pending.add(row_id)
        return row_id

    def click_choice(self, question_id: int, value: str) -> int | None:
        if self.read_only:
            return None
        row_id = self.mailbox.click_choice(question_id, value, now=self.clock())
        self.pending.add(row_id)
        return row_id

    def approve(self, prompt_id: int) -> int | None:
        if self.read_only:
            return None
        row_id = self.mailbox.approve(prompt_id, now=self.clock())
        self.pending.add(row_id)
        return row_id

    def taken(self) -> set[int]:
        """The pending rows the daemon has claimed since last asked."""
        if not self.pending:
            return set()
        done = self.mailbox.taken(sorted(self.pending))
        self.pending -= done
        return done


__all__ = [
    "MENTION",
    "ChannelTail",
    "ChatSession",
    "ChoiceSpec",
    "choice_spec",
    "compose_outbound",
    "is_addressed",
]
