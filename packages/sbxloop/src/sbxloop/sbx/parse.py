"""Tolerant parsers for sbx CLI text output.

The sbx binary is proprietary and its output format is not a stable API, so
parsing is column-header-driven and deliberately forgiving: unknown columns
are ignored and malformed lines are skipped rather than raising. Formats are
pinned by fixture tests captured from sbx v0.35.x and v0.38.x (which renamed
the ``NAME`` column to ``SANDBOX`` and added a frequently-empty ``PORTS``
column); ``sbxloop doctor`` warns on version mismatch.
"""

from __future__ import annotations

import re
from bisect import bisect_right

from sbxloop.sbx.models import SandboxInfo

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


_CELL_SPLIT = re.compile(r"\s{2,}")

# A cell is a run of words separated by single spaces; runs of two or more
# spaces separate cells.
_CELL_ITER = re.compile(r"\S+(?: \S+)*")


def parse_columns(text: str) -> list[dict[str, str]]:
    """Parse a left-aligned column table using the header row for names.

    Cells are assigned to columns by their start offset against the header
    positions rather than by index, so a row with an empty middle cell
    (sbx 0.38's ``PORTS`` column) doesn't shift its later cells into the
    wrong columns. A cell that overflows its column width still lands on
    the nearest header at or left of where it starts; if that column is
    already taken the cell spills into the next free one. Missing cells
    stay empty strings.
    """
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    headers = [(match.group().lower(), match.start()) for match in _CELL_ITER.finditer(lines[0])]
    if not headers:
        return []
    starts = [start for _, start in headers]

    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        row = {name: "" for name, _ in headers}
        for match in _CELL_ITER.finditer(line):
            i = max(bisect_right(starts, match.start()) - 1, 0)
            while i < len(headers) and row[headers[i][0]]:
                i += 1
            if i < len(headers):
                row[headers[i][0]] = match.group()
        rows.append(row)
    return rows


def parse_ls(text: str) -> list[SandboxInfo]:
    """Parse ``sbx ls`` output into SandboxInfo rows (nameless rows skipped).

    The name column is ``NAME`` on sbx 0.35.x and ``SANDBOX`` since 0.38;
    both are accepted.
    """
    infos: list[SandboxInfo] = []
    for row in parse_columns(text):
        name = row.get("name") or row.get("sandbox") or ""
        if not name:
            continue
        infos.append(
            SandboxInfo(
                name=name,
                agent=row.get("agent") or None,
                status=row.get("status") or None,
                workspace=row.get("workspace") or None,
            )
        )
    return infos


def parse_version(text: str) -> str | None:
    """Extract a semver from ``sbx version`` output, if present."""
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None
