"""The configuration as the concierge's ``config_keys`` tool hands it to the
model (#970): sections with counts, or one card per key — the value the
way an operator writes it (never a repr), the layer that set it, what it
accepts, when a change applies, whether chat may change it, and the doc
line — bounded on a card boundary with a tail that says how many were
left out, never a silent cut.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from sbxloop.configedit import Row
from sbxloop.configedit.docs import doc_lines

#: How a top-level scalar (``model``, ``keep_sandboxes``) is grouped.
TOP_LEVEL = "(top level)"
_DOC_CLIP = 110
_INDEX = re.compile(r"\[\d+\]")


def _section_of(key: str) -> str:
    head = key.split(".", 1)[0]
    return TOP_LEVEL if "." not in key and "[" not in key else _INDEX.sub("", head)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def sections(rows: Sequence[Row]) -> str:
    """One line per section: how many keys, how many the operator's file
    sets, and the section's own doc line when the example has one."""
    counts: dict[str, list[int]] = {}
    for row in rows:
        entry = counts.setdefault(_section_of(row.key), [0, 0])
        entry[0] += 1
        entry[1] += int(row.in_file)
    docs = doc_lines()
    lines = [f"{len(rows)} keys in {len(counts)} sections (ask for one by prefix):"]
    for name, (total, in_file) in counts.items():
        line = f"- `{name}` — {total} key(s), {in_file} set in the operator's file"
        doc = docs.get(name)
        if doc:
            line += f": {_clip(doc, _DOC_CLIP)}"
        lines.append(line)
    return "\n".join(lines)


def matches(row: Row, *, prefix: str | None, grep: str | None) -> bool:
    if prefix:
        under = row.key == prefix or row.key.startswith((prefix + ".", prefix + "["))
        if not under:
            return False
    if grep:
        needle = grep.casefold()
        if needle not in row.key.casefold() and needle not in (row.doc or "").casefold():
            return False
    return True


def card(row: Row, *, refusal: Callable[[str], str | None]) -> str:
    """One key, on one line plus its doc line."""
    head = (
        f"`{row.key}` = {row.display} · set by {row.source} · accepts {row.spec.summary}"
        + ("" if row.spec.optional else " (required)")
        + f" · applies {row.applies}"
    )
    why = refusal(row.key)
    if why is not None:
        head += f" · never from chat: {why}"
    return head + (f"\n  {_clip(row.doc, 400)}" if row.doc else "")


def bounded(cards: Sequence[str], max_chars: int) -> str:
    """``cards`` joined, cut on a card boundary when they do not fit, with a
    tail naming how many were left out."""
    if not cards:
        return "no key matches"
    kept: list[str] = []
    used = 0
    for text in cards:
        if kept and used + len(text) + 1 > max_chars - 80:
            break
        kept.append(text)
        used += len(text) + 1
    out = "\n".join(kept)
    left = len(cards) - len(kept)
    if left:
        out += f"\n… {left} more — narrow the prefix or add grep"
    return out


__all__ = ["TOP_LEVEL", "bounded", "card", "matches", "sections"]
