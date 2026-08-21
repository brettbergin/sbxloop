"""Where an inbound Discord message goes — as a pure function.

The bridge grew its routing inline (command / canned hint / thread steer);
with the concierge there are four destinations and the mention rules are
easy to get subtly wrong (``<@id>`` vs ``<@!id>``, a reply to the bot, a
``!sbx`` command that happens to mention the bot). :func:`route_message`
takes plain facts the bridge extracts from the discord.py message and
returns a :class:`Route`, so every rule is unit-testable without a client:

- messages from bots (including our own) are ignored;
- the bot listens on exactly two surfaces — the control channel and a run
  thread it opened itself (``is_run_thread``). Anywhere else — a DM, an
  unrelated guild channel — is ignored outright;
- on either surface, text starting with the command prefix is a
  ``command`` (mention or not), and a message that @mentions the bot or
  replies to one of its messages is *addressed* to it: in the control
  channel that goes to the ``concierge``, in a run thread it is a
  ``steer``. The mention token is stripped either way;
- anything else is ignored — people talk among themselves, in the control
  channel *and* in a run thread, without the bot answering.

That last rule is why the thread branch is not a catch-all: steering
pauses the agent and can rewrite the running task's plan, so it takes the
same deliberate "@mention the bot" as talking to the concierge does.
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
    is_run_thread: bool,
) -> Route:
    if author_is_bot or channel_id is None:
        return Route("ignore", "")
    in_control = channel_id == control_channel_id
    if not in_control and not is_run_thread:
        return Route("ignore", "")
    stripped = strip_mentions((content or "").strip(), bot_user_id)
    if stripped.startswith(prefix):
        return Route("command", stripped[len(prefix) :].strip())
    mentioned = bot_user_id is not None and bot_user_id in mentioned_ids
    if not ((mentioned or reply_to_bot) and stripped):
        return Route("ignore", "")
    return Route("concierge" if in_control else "steer", stripped)
