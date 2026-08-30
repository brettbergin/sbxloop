"""Where an inbound chat message goes — as a pure function, for every backend.

The Discord bridge grew its routing inline (command / canned hint / thread
steer); with the concierge there are four destinations and the mention
rules are easy to get subtly wrong (``<@id>`` vs ``<@!id>``, a reply to
the bot, a ``!sbx`` command that happens to mention the bot). Slack adds a
second dialect of the same facts (``<@U…>`` / ``<@U…|name>``).
:func:`route_message` takes plain facts a bridge extracts from its own
message object and returns a :class:`Route`, so every rule is
unit-testable without a client and identical on both services:

- messages from bots (including our own) are ignored;
- the bot listens on exactly two surfaces — the control channel and a run
  thread it opened itself (``is_run_thread``). Anywhere else — a DM, an
  unrelated channel — is ignored outright;
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

Ids are compared as text so a backend may hand in whatever it has —
Discord's integer snowflakes, Slack's ``U…``/``C…`` strings.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Literal, NamedTuple

RouteKind = Literal["command", "concierge", "steer", "ignore"]

#: Discord: ``<@123>`` and the nickname form ``<@!123>``.
DISCORD_MENTION_RE = re.compile(r"<@!?(\d+)>")
#: Slack: ``<@U0123>`` and the labelled form ``<@U0123|name>``.
SLACK_MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")


class Route(NamedTuple):
    kind: RouteKind
    text: str


def strip_mentions(
    content: str,
    user_id: int | str | None,
    *,
    mention_re: re.Pattern[str] = DISCORD_MENTION_RE,
) -> str:
    """Remove the mention tokens for ``user_id`` (all of them, when
    ``user_id`` is None) and collapse the whitespace they leave behind."""
    wanted = None if user_id is None else str(user_id)

    def keep(match: re.Match[str]) -> str:
        if wanted is None or match.group(1) == wanted:
            return " "
        return match.group(0)

    return " ".join(mention_re.sub(keep, content).split())


def route_message(
    *,
    content: str,
    channel_id: int | str | None,
    author_is_bot: bool,
    mentioned_ids: Collection[int | str],
    reply_to_bot: bool,
    control_channel_id: int | str | None,
    prefix: str,
    bot_user_id: int | str | None,
    is_run_thread: bool,
    mention_re: re.Pattern[str] = DISCORD_MENTION_RE,
) -> Route:
    if author_is_bot or channel_id is None:
        return Route("ignore", "")
    in_control = control_channel_id is not None and str(channel_id) == str(control_channel_id)
    if not in_control and not is_run_thread:
        return Route("ignore", "")
    stripped = strip_mentions((content or "").strip(), bot_user_id, mention_re=mention_re)
    if stripped.startswith(prefix):
        return Route("command", stripped[len(prefix) :].strip())
    mentioned = bot_user_id is not None and str(bot_user_id) in {str(m) for m in mentioned_ids}
    if not ((mentioned or reply_to_bot) and stripped):
        return Route("ignore", "")
    return Route("concierge" if in_control else "steer", stripped)
