"""Rendering for the console: the chat dialect the bridges speak turned
into what a terminal shows, cards as panels, and the one-line summaries
the status bar and tables use.

The bridges render Discord-flavoured Markdown (``**bold**``, ``<url>``,
``<@id>``, ``<#thread:N>``); Slack re-dialects it at its send seam
(:mod:`sbxloop.daemon.slack_format`), and this module is the console's
twin: :func:`to_commonmark` for Textual's Markdown widget, :func:`to_rich`
for a one-line Rich ``Text``. Both skip code spans, as the Slack converter
does.
"""

from __future__ import annotations

import re
import time
from typing import Any

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from sbxloop.daemon.discord_format import STATE_MARKER, EmbedSpec, code_segments
from sbxloop.daemon.usage import SPEND_NOT_REPORTED
from sbxloop.engine.model import RunRecord

_ANGLE_URL = re.compile(r"<(https?://[^>\s]+)>")
_MENTION = re.compile(r"<@!?([^>\s]+)>")
_THREAD = re.compile(r"<#([^>\s]+)>")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")

#: How a Discord embed colour maps onto a Rich border style.
_COLOR_STYLES: dict[int, str] = {
    0x3498DB: "blue",
    0x2ECC71: "green",
    0xE74C3C: "red",
    0xE67E22: "yellow",
    0x95A5A6: "bright_black",
}


def _outside_code(text: str, convert: Any) -> str:
    """Apply ``convert`` to every segment outside a code span."""
    out: list[str] = []
    for segment, is_code in code_segments(text):
        out.append(segment if is_code else convert(segment))
    return "".join(out)


def to_commonmark(text: str, *, names: dict[str, str] | None = None) -> str:
    """The bridges' dialect as CommonMark: masked links unmasked, user
    mentions as ``@name``, thread pointers as ``thread N``."""

    def convert(segment: str) -> str:
        segment = _ANGLE_URL.sub(r"\1", segment)
        segment = _MENTION.sub(lambda m: "@" + (names or {}).get(m.group(1), m.group(1)), segment)
        return _THREAD.sub(lambda m: f"`{m.group(1)}`", segment)

    return _outside_code(text, convert)


def to_rich(text: str, *, names: dict[str, str] | None = None) -> Text:
    """One line of the bridges' dialect as Rich text: bold, code spans in
    cyan, links underlined, mentions dim."""
    flat = " ".join(to_commonmark(text, names=names).split())
    rich = Text()
    pos = 0
    pattern = re.compile(
        r"(?P<bold>\*\*.+?\*\*)|(?P<code>`[^`]+`)|(?P<link>\[[^\]]+\]\(https?://[^)\s]+\))"
        r"|(?P<url>https?://\S+)|(?P<mention>(?<!\w)@[\w.\-]+)"
    )
    for match in pattern.finditer(flat):
        rich.append(flat[pos : match.start()])
        kind = match.lastgroup
        raw = match.group(0)
        if kind == "bold":
            rich.append(raw[2:-2], style="bold")
        elif kind == "code":
            rich.append(raw[1:-1], style="cyan")
        elif kind == "link":
            link = _LINK.match(raw)
            assert link is not None
            rich.append(link.group(1), style=f"underline link {link.group(2)}")
        elif kind == "url":
            rich.append(raw, style=f"underline link {raw}")
        else:
            rich.append(raw, style="dim")
        pos = match.end()
    rich.append(flat[pos:])
    return rich


def card(spec: EmbedSpec, *, names: dict[str, str] | None = None) -> Panel:
    """A bridge card (the headline, the status card, the finish card) as a
    bordered panel with its fields in two columns."""
    body = Table.grid(padding=(0, 1))
    body.add_column(style="bold", no_wrap=True)
    body.add_column()
    if spec.description:
        body.add_row("", to_rich(spec.description, names=names))
    for name, value, _inline in spec.fields:
        body.add_row(name, to_rich(value, names=names))
    if spec.footer:
        body.add_row("", Text(spec.footer, style="dim"))
    title = to_rich(spec.title or "", names=names) if spec.title else None
    style = _COLOR_STYLES.get(spec.color or 0, "bright_black")
    return Panel(body, title=title, title_align="left", border_style=style, padding=(0, 1))


def clock(ts: float | None, *, seconds: bool = True) -> str:
    """A local wall-clock stamp, ``HH:MM:SS`` (or ``HH:MM``)."""
    if ts is None:
        return "—"
    return time.strftime("%H:%M:%S" if seconds else "%H:%M", time.localtime(ts))


def age(ts: float | None, now: float | None = None) -> str:
    """``3s`` / ``2m`` / ``4h`` / ``3d`` ago, or an em dash."""
    if ts is None:
        return "—"
    delta = max(0.0, (time.time() if now is None else now) - ts)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h {int(seconds % 3600 // 60):02d}m"


def state_label(state: str, reason: str | None = None, *, emoji: bool = True) -> Text:
    """A run or item state with its marker, the reason dimmed after it."""
    marker = STATE_MARKER.get(state, "·") if emoji else ""
    text = Text(f"{marker} {state}".strip())
    if reason:
        text.append(f" — {reason}", style="dim")
    return text


def run_title(record: RunRecord) -> str:
    """What to call a run in a list: its PR title when it has one, else
    the outcome's first line, clipped."""
    title = (record.pr_title or record.outcome or "").strip().splitlines()
    first = title[0] if title else ""
    return first if len(first) <= 70 else first[:69] + "…"


def tokens(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


__all__ = [
    "SPEND_NOT_REPORTED",
    "age",
    "card",
    "clock",
    "duration",
    "run_title",
    "state_label",
    "to_commonmark",
    "to_rich",
    "tokens",
]
