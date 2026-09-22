"""Reading the agents an agent's own reply addresses.

A reply is chat markdown, so an `@slug` inside a fenced or inline code span
is a command line, and one inside a block quote is something *someone else*
said being repeated. Neither addresses anybody, and acting on either is how
a channel ends up with an agent answering a quotation of itself. Only prose
mentions count.

:class:`MentionRouter` turns the ones that do into follow-up turns: it keeps
the slugs that name an active agent, drops the author's own, the source
message's author and any agent still to answer in the same turn, asks
:class:`~sbxloop.api.guardrails.Guardrails` about each survivor, and queues
the allowed ones. ``handoff_agent`` — an agent addressing a peer *within*
its own turn — is a different mechanism and is untouched by this one.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from sbxloop.log import get_logger

log = get_logger(__name__)

#: `@slug` in prose. The lookbehind keeps `x@y` and `@@y` out.
MENTION = re.compile(r"(?<![\w@])@([a-z0-9][a-z0-9_-]{0,63})\b", re.IGNORECASE)
#: A fenced block: ``` or ~~~ to the matching fence or the end of the text.
FENCE = re.compile(r"(?ms)^[ \t]*(?P<fence>`{3,}|~{3,}).*?(?:^[ \t]*(?P=fence)[ \t]*$|\Z)")
#: An inline code span.
CODE_SPAN = re.compile(r"`[^`\n]*`")
#: A block quote line.
QUOTE = re.compile(r"(?m)^[ \t]{0,3}>.*$")
#: How many mentions one reply may address, so a list of every agent in the
#: workspace cannot fan one reply out into a swarm.
MAX_MENTIONS = 4


def addressed_slugs(text: str) -> tuple[str, ...]:
    """The slugs ``text`` addresses in prose, in the order they appear.

    Code fences, inline code spans and block quotes are blanked out first,
    so what they contain is never an address.
    """
    prose = FENCE.sub(_blank, text)
    prose = CODE_SPAN.sub(_blank, prose)
    prose = QUOTE.sub(_blank, prose)
    return tuple(dict.fromkeys(match.group(1).casefold() for match in MENTION.finditer(prose)))


def _blank(match: re.Match[str]) -> str:
    """Keep the offsets, lose the content."""
    return re.sub(r"[^\n]", " ", match.group(0))


class MentionRouter:
    """Queues a follow-up turn for each agent an agent's reply addresses."""

    def __init__(
        self,
        *,
        resolve: Callable[[str], str | None],
        participants: Callable[[str], Iterable[str]],
        join: Callable[[str, str], object],
        admit: Callable[..., object],
        queue: Callable[..., object],
    ) -> None:
        self.resolve = resolve
        self.participants = participants
        self.join = join
        self.admit = admit
        self.queue = queue

    def route(
        self,
        text: str,
        *,
        channel_id: str,
        source_message_id: str,
        author_slug: str,
        reply_to_author: str | None,
        depth: int,
        trigger: str = "mention",
        skip: Iterable[str] = (),
    ) -> tuple[str, ...]:
        """Queue what ``text`` addresses; the slugs actually queued.

        ``skip`` names agents that must not get a follow-up: those still to
        answer in the turn that produced ``text`` see it there already.
        """
        passed = {slug.casefold() for slug in skip}
        passed.add(author_slug.casefold())
        if reply_to_author:
            passed.add(reply_to_author.casefold())
        queued: list[str] = []
        for slug in addressed_slugs(text)[:MAX_MENTIONS]:
            if slug in passed:
                continue
            resolved = self.resolve(slug)
            if resolved is None or resolved in passed:
                continue
            passed.add(resolved)
            if resolved not in set(self.participants(channel_id)):
                self.join(channel_id, resolved)
            admission = self.admit(channel_id, target_slug=resolved, depth=depth, trigger=trigger)
            if not getattr(admission, "ok", False):
                continue
            try:
                self.queue(
                    channel_id=channel_id,
                    source_message_id=source_message_id,
                    author_slug=author_slug,
                    target_slug=resolved,
                    depth=depth,
                    trigger=trigger,
                )
            except Exception:
                log.warning(
                    "collaboration.followup_queue_failed",
                    channel=channel_id,
                    agent=resolved,
                    exc_info=True,
                )
                continue
            queued.append(resolved)
        return tuple(queued)


__all__ = ["MAX_MENTIONS", "MentionRouter", "addressed_slugs"]
