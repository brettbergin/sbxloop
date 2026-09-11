"""Mattermost rendering for the chat bridge — no HTTP client import.

Everything a bridge says is shaped once, in Discord's Markdown dialect, by
``discord_format`` (the pure, unit-tested layer). Mattermost speaks that
same dialect closely enough that there is no re-dialecting to do: ``**bold**``,
``~~strike~~``, ``[label](url)`` and fenced blocks with a language tag all
render. Slack needed :mod:`sbxloop.daemon.slack_format` for exactly the
places it differs; here the only transformation is about *pings*.

That one is not cosmetic. Mattermost has no allowed-mentions control — the
Discord bridge posts with ``AllowedMentions.none()`` and Slack escapes
``<@U…>``, but a Mattermost post containing ``@ana`` pings ana, and agent
prose quoting a commit author or a code comment would do it. So prose is
passed through :func:`neutralize_mentions`, which parks a zero-width space
after the ``@`` of anything that would resolve: invisible to a reader,
inert to the mention parser, and applied at the send/edit seam only, so
nothing upstream knows there is a third service.

Cards (``EmbedSpec``) are rendered into the post text for now; the coloured
*message attachment* Mattermost supports is #932's half of this module.
"""

from __future__ import annotations

import re

from sbxloop.daemon.discord_format import _code_segments

#: Unicode emoji the bridge reacts with -> the emoji *names* the reactions
#: API takes (never a glyph). The standard shortcode set, which is why these
#: are spelled the same as Slack's; kept here rather than imported so
#: neither service's renderer depends on the other's.
EMOJI_NAMES: dict[str, str] = {
    "⏳": "hourglass_flowing_sand",
    "✅": "white_check_mark",
    "⚠": "warning",
    "❌": "x",
    "🎉": "tada",
    "👀": "eyes",
}

# A zero-width space: invisible in a rendered post, and enough to stop the
# mention parser matching the name that follows.
ZERO_WIDTH_SPACE = "​"
# ``@name`` as Mattermost resolves it: usernames are lowercase alphanumerics
# with dots, dashes and underscores. The special @channel / @here / @all
# broadcasts match the same shape and must be neutralized too — those are
# the ones that would wake a whole channel.
_MENTION_RE = re.compile(r"@([a-z0-9][a-z0-9._-]*)", re.IGNORECASE)


def neutralize_mentions(text: str) -> str:
    """Make every ``@name`` in prose inert, leaving it readable.

    Code spans and fenced blocks are left alone: Mattermost does not
    resolve a mention inside them, and rewriting a code sample would
    corrupt what the agent is quoting.
    """
    out: list[str] = []
    for segment, is_code in _code_segments(str(text)):
        out.append(segment if is_code else _MENTION_RE.sub(_park, segment))
    return "".join(out)


def _park(match: re.Match[str]) -> str:
    return f"@{ZERO_WIDTH_SPACE}{match.group(1)}"


def thread_permalink(base_url: str, team: str, post_id: str) -> str:
    """The deep link to a post (and so to the thread under it).

    Without a team name — the bridge could not resolve one — there is no
    permalink to build, and the caller renders the post id instead of a
    link that would 404.
    """
    if not team:
        return ""
    return f"{base_url.rstrip('/')}/{team}/pl/{post_id}"


__all__ = [
    "EMOJI_NAMES",
    "ZERO_WIDTH_SPACE",
    "neutralize_mentions",
    "thread_permalink",
]
