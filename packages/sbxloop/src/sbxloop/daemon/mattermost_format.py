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

The cards (``EmbedSpec``) become one *message attachment* — Mattermost
implements the Slack-legacy attachment schema, so the colour bar that makes
a ✅/❌ verdict readable at a glance is available here too.
"""

from __future__ import annotations

import re
from typing import Any

from sbxloop.daemon.discord_format import EmbedSpec, _code_segments, _cut

# Mattermost's own limits for an attachment's parts. A post's own ceiling is
# server-configurable (16383 by default), far above the 2000 the shared
# ``max_message_chars`` allows, so the renderer never approaches it.
ATTACHMENT_TEXT_MAX = 16383
FIELD_VALUE_MAX = 2000
FALLBACK_MAX = 1000

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

#: The emoji a clarifying question is seeded with, one per choice, in order:
#: reacting with one answers it. Mattermost's interactive buttons would post
#: to a callback URL, which would cost the bridge the dial-out property it is
#: built on; a reaction arrives on the websocket already open. The choice
#: model caps a question at five options, which is why the list stops there.
CHOICE_EMOJI: tuple[str, ...] = ("one", "two", "three", "four", "five")
#: The approve button's twin: reacting with this on a gate prompt approves.
GATE_EMOJI = "white_check_mark"

# A markdown link or image. The server's embed scan never looks inside one,
# so these are already inert and are copied through untouched.
_MD_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\([^)\s]*\)")
# A URL the server would treat as an *autolink*: bare, or in angle brackets —
# CommonMark makes ``<url>`` an autolink too, which is why Discord's
# angle-bracket trick is the wrong one to copy here.
_AUTOLINK_RE = re.compile(r"<(https?://[^>\s]+)>|(?<!\w)(https?://[^\s<>\[\]()]+)")
# Punctuation a sentence leaves on the end of a URL, which is not part of it.
_URL_TAIL = ".,;:!?"

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


def defuse_unfurls(text: str) -> str:
    """Keep every link clickable and stop it growing a preview card.

    Mattermost decides what to embed under a post from the *first autolink*
    in its message — that is the whole rule, and it is what fills a run's
    thread with website cards for every PR, issue and CI link the
    chronology carries. There is no per-post flag to turn it off
    (``EnableLinkPreviews`` is server-wide, and no post prop overrides it),
    but there does not need to be: the server's scan only visits autolink
    nodes, so a markdown link — ``[label](url)`` — is never a candidate.
    Rewriting each bare URL as a link to itself leaves the same text, the
    same click and no card.

    Two traps this avoids. ``<url>`` is an autolink in CommonMark, so
    Discord's angle-bracket suppression would still unfurl here. And a URL
    already inside a markdown link must be left exactly as it is — putting
    a link inside a link corrupts both.

    Code spans and fenced blocks are skipped: nothing in them is a link to
    Mattermost either, and rewriting one would corrupt what the agent is
    quoting.
    """
    out: list[str] = []
    for segment, is_code in _code_segments(str(text)):
        out.append(segment if is_code else _defuse_segment(segment))
    return "".join(out)


def _defuse_segment(segment: str) -> str:
    """One non-code stretch: rewrite the autolinks around the markdown links
    already in it, which are copied through verbatim."""
    parts: list[str] = []
    last = 0
    for match in _MD_LINK_RE.finditer(segment):
        parts.append(_AUTOLINK_RE.sub(_as_markdown_link, segment[last : match.start()]))
        parts.append(match.group(0))
        last = match.end()
    parts.append(_AUTOLINK_RE.sub(_as_markdown_link, segment[last:]))
    return "".join(parts)


def _as_markdown_link(match: re.Match[str]) -> str:
    url = match.group(1) or match.group(2) or ""
    tail = ""
    while url and url[-1] in _URL_TAIL:
        url, tail = url[:-1], url[-1] + tail
    if not url:
        return match.group(0)
    return f"[{url}]({url}){tail}"


def embed_attachment(spec: EmbedSpec) -> dict[str, Any]:
    """One Mattermost message attachment for a card.

    The Slack-legacy schema Mattermost implements: a colour bar, an
    optionally linked title, the description as text, the card's fields as
    attachment fields (``short`` honouring the spec's own inline flag) and
    the footer. ``fallback`` is what a notification shows.

    Mention-safe like the post text: an attachment's text pings exactly as a
    post's does, so every part a card carries goes through the guard.
    """
    spec = spec.clamped()
    attachment: dict[str, Any] = {
        "fallback": _cut(neutralize_mentions(spec.as_text()), FALLBACK_MAX)
    }
    if spec.title:
        attachment["title"] = neutralize_mentions(spec.title)
        if spec.url:
            attachment["title_link"] = spec.url
    if spec.description:
        attachment["text"] = _cut(neutralize_mentions(spec.description), ATTACHMENT_TEXT_MAX)
    if spec.fields:
        attachment["fields"] = [
            {
                "title": neutralize_mentions(name),
                "value": _cut(neutralize_mentions(value), FIELD_VALUE_MAX),
                "short": bool(inline),
            }
            for name, value, inline in spec.fields
        ]
    if spec.footer:
        attachment["footer"] = neutralize_mentions(spec.footer)
    if spec.color is not None:
        attachment["color"] = f"#{spec.color:06X}"
    return attachment


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
    "CHOICE_EMOJI",
    "EMOJI_NAMES",
    "GATE_EMOJI",
    "ZERO_WIDTH_SPACE",
    "defuse_unfurls",
    "embed_attachment",
    "neutralize_mentions",
    "thread_permalink",
]
