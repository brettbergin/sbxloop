"""Where an inbound Discord message goes — as a pure function.

The bridge grew its routing inline (command / canned hint / thread steer);
with the concierge there are four destinations and the mention rules are
easy to get subtly wrong (``<@id>`` vs ``<@!id>``, a reply to the bot, a
``!sbx`` command that happens to mention the bot). :func:`route_message`
takes plain facts the bridge extracts from the discord.py message and
returns a :class:`Route`, so every rule is unit-testable without a client:

- messages from bots (including our own) are ignored;
- in the control channel: text starting with the command prefix is a
  ``command`` (mention or not); a message that @mentions the bot or
  replies to one of its messages is for the ``concierge`` (with the
  mention token stripped); anything else is ignored — people talk among
  themselves without the bot answering;
- in any other channel (a run thread) the message is a ``steer``; the
  bridge decides whether that thread's run is live.
"""

from __future__ import annotations

import re
from typing import Literal, NamedTuple

RouteKind = Literal["command", "concierge", "steer", "ignore"]

_MENTION_RE = re.compile(r"<@!?(\d+)>")


class Route(NamedTuple):
    kind: RouteKind
    text: str


def strip_mentions(content: str, user_id: int | None) -> str:
    """Remove ``<@id>`` / ``<@!id>`` tokens for ``user_id`` (all of them, when
    ``user_id`` is None) and collapse the whitespace they leave behind."""

    def keep(match: re.Match[str]) -> str:
        if user_id is None or int(match.group(1)) == user_id:
            return " "
        return match.group(0)

    return " ".join(_MENTION_RE.sub(keep, content).split())


def route_message(
    *,
    content: str,
    channel_id: int | None,
    author_is_bot: bool,
    mentioned_ids: frozenset[int] | set[int],
    reply_to_bot: bool,
    control_channel_id: int | None,
    prefix: str,
    bot_user_id: int | None,
) -> Route:
    if author_is_bot:
        return Route("ignore", "")
    text = (content or "").strip()
    if channel_id is not None and channel_id == control_channel_id:
        stripped = strip_mentions(text, bot_user_id)
        if stripped.startswith(prefix):
            return Route("command", stripped[len(prefix) :].strip())
        mentioned = bot_user_id is not None and bot_user_id in mentioned_ids
        if (mentioned or reply_to_bot) and stripped:
            return Route("concierge", stripped)
        return Route("ignore", "")
    if channel_id is None:
        return Route("ignore", "")
    return Route("steer", text)
