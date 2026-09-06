"""A proportional bar drawn with background colour rather than glyphs.

Block characters (``▇▓▒``) quantise to whole cells and read as texture;
a run of spaces on a coloured background is a solid bar at any width, and
segments meet without a seam. The widget fills whatever width it is given,
so a band re-proportions itself on resize with no arithmetic at the call
site.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from rich.text import Text
from textual.widgets import Static


class Segment(NamedTuple):
    """One part of a band: what it is, how much, and its colour."""

    label: str
    value: float
    colour: str


#: Enough distinct hues for the phases a run has, in the order phases are
#: usually reported. Chosen to stay apart on both light and dark terminals.
PALETTE: tuple[str, ...] = (
    "#4C8DF6",
    "#8B5CF6",
    "#14B8A6",
    "#F59E0B",
    "#EC4899",
    "#06B6D4",
    "#84CC16",
    "#64748B",
)

#: The bar colours with a fixed meaning, kept here so no screen writes a
#: bare hex — `scripts/check_self_references.py` reads `#NNNNNN` in console
#: text as an issue reference, and it is right to.
OK_COLOUR = "#22C55E"
BAD_COLOUR = "#EF4444"
IDLE_COLOUR = "#6B7280"
WAIT_COLOUR = "#F59E0B"
# Note the letter: an all-digit hex reads as an issue reference to
# `scripts/check_self_references.py`, and adding an allowlist entry for a
# colour would blunt a gate that is otherwise right.
PARKED_COLOUR = "#3A4151"
#: The unfilled remainder of a ranked row's track.
TRACK_COLOUR = "#1C2128"
REST_COLOUR = "#30363D"


class Band(Static):
    """A single-row bar split into proportional coloured segments."""

    DEFAULT_CSS = "Band { height: 1; }"

    def __init__(self, segments: Sequence[Segment] = (), **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.segments: tuple[Segment, ...] = tuple(segments)

    def show(self, segments: Sequence[Segment]) -> None:
        self.segments = tuple(segments)
        self.refresh()

    def render(self) -> Text:
        return paint(self.segments, self.size.width)


def widths(segments: Sequence[Segment], width: int) -> list[int]:
    """How many cells each segment gets in a row ``width`` wide.

    The last segment takes the remainder rather than its own rounding, so
    the parts always sum to exactly ``width``: a band never leaves a gap at
    the end or overruns its row."""
    total = sum(max(s.value, 0.0) for s in segments)
    if not segments or total <= 0 or width <= 0:
        return []
    out: list[int] = []
    used = 0
    for index, segment in enumerate(segments):
        if index == len(segments) - 1:
            cells = width - used
        else:
            cells = min(round(width * max(segment.value, 0.0) / total), width - used)
        used += cells
        out.append(cells)
    return out


def paint(segments: Sequence[Segment], width: int) -> Text:
    """A band's row: the segments' colours, or an empty track."""
    parts = widths(segments, width)
    if not parts:
        return Text(" " * max(width, 0), style=f"on {REST_COLOUR}")
    text = Text()
    for segment, cells in zip(segments, parts, strict=True):
        if cells > 0:
            text.append(" " * cells, style=f"on {segment.colour}")
    return text


def legend(segments: Sequence[Segment]) -> Text:
    """The key under a band: a swatch, a name and a share."""
    total = sum(max(s.value, 0.0) for s in segments)
    text = Text()
    if total <= 0:
        return text
    for segment in segments:
        share = round(100 * max(segment.value, 0.0) / total)
        if not share:
            continue
        text.append("■ ", style=segment.colour)
        text.append(f"{segment.label} {share}%  ", style="dim")
    return text


__all__ = [
    "BAD_COLOUR",
    "IDLE_COLOUR",
    "OK_COLOUR",
    "PALETTE",
    "PARKED_COLOUR",
    "REST_COLOUR",
    "TRACK_COLOUR",
    "WAIT_COLOUR",
    "Band",
    "Segment",
    "legend",
    "paint",
    "widths",
]
