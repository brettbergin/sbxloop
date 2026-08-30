"""Slack mrkdwn for the chat bridge — no slack_sdk import.

Everything a bridge says is shaped once, in Discord's Markdown dialect, by
``discord_format`` (the pure, unit-tested layer). Slack speaks *mrkdwn*,
which differs in exactly the places a renderer trips over: ``*bold*`` not
``**bold**``, ``~strike~`` not ``~~strike~~``, ``<url|label>`` not
``[label](url)``, no language tag after a code fence (it would print), and
``&``, ``<``, ``>`` must be entity-escaped everywhere — *except* inside the
control sequences Slack itself defines (``<@U…>`` user mentions, ``<#C…>``
channel links, ``<url>`` / ``<url|label>`` links). :func:`to_mrkdwn` is
that re-dialecting, applied at the Slack bridge's send/edit seam and
nowhere else, so nothing upstream knows there is a second service.

The cards (``EmbedSpec``) become one coloured *attachment* carrying Block
Kit sections: the colour bar is the only place Slack shows a colour, and
it is what makes a ✅/❌ verdict readable at a glance in a busy channel.
"""

from __future__ import annotations

import re
from typing import Any

from sbxloop.daemon.discord_format import EmbedSpec, _code_segments, _cut

# Slack's own limits: a section block's text and each field's text.
SECTION_TEXT_MAX = 3000
FIELD_TEXT_MAX = 2000
FIELDS_PER_SECTION = 10
# Unicode emoji the bridge reacts with -> Slack's reaction *names*
# (``reactions.add`` takes a name, never a glyph).
EMOJI_NAMES: dict[str, str] = {
    "⏳": "hourglass_flowing_sand",
    "✅": "white_check_mark",
    "⚠": "warning",
    "❌": "x",
    "🎉": "tada",
    "👀": "eyes",
}

_MASKED_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_ANGLE_LINK_RE = re.compile(r"<(https?://[^\s<>|]+)(\|[^>\n]+)?>")
_BARE_URL_RE = re.compile(r"(?<![<(\[])https?://[^\s<>()\[\]]+")
_USER_MENTION_RE = re.compile(r"<@[A-Z0-9]+(?:\|[^>\n]*)?>")
_CHANNEL_REF_RE = re.compile(r"<#[A-Z0-9]+(?:\|[^>\n]*)?>")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_FENCE_LANG_RE = re.compile(r"\A(\s*```)[\w+#.-]+[ \t]*(?=\n)")
_STASH_RE = re.compile("\x00(\\d+)\x00")


def escape(text: str) -> str:
    """Slack's three control characters as entities. Idempotent on text that
    is already escaped only if it carried no bare ``&``; callers apply it
    exactly once, to text that has not been through it before."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_mrkdwn(text: str, *, mentions: bool = False) -> str:
    """Discord-flavoured Markdown → Slack mrkdwn.

    Code spans and fenced blocks keep their bodies verbatim (entity-escaped
    — Slack requires that even inside code — and with the fence's language
    tag dropped, since Slack would print it); prose is re-dialected.
    ``<@U…>`` user mentions in the text survive only with ``mentions=True``
    (the run-watch ping); otherwise they are escaped and render as literal
    text, the Slack equivalent of Discord's ``AllowedMentions.none()``, so
    agent prose can never ping anyone.
    """
    out: list[str] = []
    for segment, is_code in _code_segments(str(text)):
        out.append(_code_to_mrkdwn(segment) if is_code else _prose_to_mrkdwn(segment, mentions))
    return "".join(out)


def _code_to_mrkdwn(segment: str) -> str:
    if segment.startswith("```"):
        segment = _FENCE_LANG_RE.sub(r"\1", segment, count=1)
    return escape(segment)


def _prose_to_mrkdwn(text: str, mentions: bool) -> str:
    stash: list[str] = []

    def keep(rendered: str) -> str:
        stash.append(rendered)
        return f"\x00{len(stash) - 1}\x00"

    text = _MASKED_LINK_RE.sub(lambda m: keep(f"<{m.group(2)}|{escape(m.group(1))}>"), text)
    text = _ANGLE_LINK_RE.sub(lambda m: keep(m.group(0)), text)
    text = _CHANNEL_REF_RE.sub(lambda m: keep(m.group(0)), text)
    if mentions:
        text = _USER_MENTION_RE.sub(lambda m: keep(m.group(0)), text)
    text = _BARE_URL_RE.sub(lambda m: keep(f"<{m.group(0)}>"), text)
    text = escape(text)
    text = _HEADING_RE.sub(r"*\1*", text)
    text = _BOLD_RE.sub(r"*\1*", text)
    text = _STRIKE_RE.sub(r"~\1~", text)
    return _STASH_RE.sub(lambda m: stash[int(m.group(1))], text)


def embed_attachment(spec: EmbedSpec) -> dict[str, Any]:
    """One Slack attachment for a card: the colour bar plus Block Kit
    sections for the title/description, the fields (ten per section, as
    Slack allows) and the footer as a context line. ``fallback`` is the
    text notifications and text-only clients show."""
    spec = spec.clamped()
    blocks: list[dict[str, Any]] = []
    head: list[str] = []
    if spec.title:
        title = escape(spec.title)
        head.append(f"*<{spec.url}|{title}>*" if spec.url else f"*{title}*")
    if spec.description:
        head.append(to_mrkdwn(spec.description))
    if head:
        blocks.append(_section("\n".join(head)))
    fields = [f"*{escape(name)}*\n{to_mrkdwn(value)}" for name, value, _ in spec.fields]
    for start in range(0, len(fields), FIELDS_PER_SECTION):
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": _cut(f, FIELD_TEXT_MAX)}
                    for f in fields[start : start + FIELDS_PER_SECTION]
                ],
            }
        )
    if spec.footer:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": _cut(to_mrkdwn(spec.footer), SECTION_TEXT_MAX)}
                ],
            }
        )
    attachment: dict[str, Any] = {"blocks": blocks, "fallback": _cut(spec.as_text(), 1000)}
    if spec.color is not None:
        attachment["color"] = f"#{spec.color:06X}"
    return attachment


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _cut(text, SECTION_TEXT_MAX)}}


def thread_permalink(channel_id: str, ts: str) -> str:
    """The workspace-agnostic deep link to a message (and so to the thread
    under it): ``slack.com/archives`` redirects into whichever workspace
    the reader is signed into."""
    return f"https://slack.com/archives/{channel_id}/p{ts.replace('.', '')}"


__all__ = [
    "EMOJI_NAMES",
    "embed_attachment",
    "escape",
    "thread_permalink",
    "to_mrkdwn",
]
