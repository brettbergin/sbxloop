"""Tolerant parsers for sbx CLI text output.

The sbx binary is proprietary and its output format is not a stable API, so
parsing is column-header-driven and deliberately forgiving: unknown columns
are ignored and malformed lines are skipped rather than raising. Formats are
pinned by fixture tests captured from sbx v0.35.x; ``sdxloop doctor`` warns
on version mismatch.
"""

from __future__ import annotations

import re

from sdxloop.sbx.models import SandboxInfo

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


_CELL_SPLIT = re.compile(r"\s{2,}")


def parse_columns(text: str) -> list[dict[str, str]]:
    """Parse a left-aligned column table using the header row for names.

    Cells are split on runs of two or more spaces, which survives cells that
    overflow their column width (unlike offset slicing). Rows with fewer
    cells than headers get empty strings; extra cells are dropped.
    """
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    headers = [h.lower() for h in _CELL_SPLIT.split(lines[0].strip())]
    if not headers:
        return []

    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        cells = _CELL_SPLIT.split(line.strip())
        row = {
            header: cells[i].strip() if i < len(cells) else "" for i, header in enumerate(headers)
        }
        rows.append(row)
    return rows


def parse_ls(text: str) -> list[SandboxInfo]:
    """Parse ``sbx ls`` output into SandboxInfo rows (nameless rows skipped)."""
    infos: list[SandboxInfo] = []
    for row in parse_columns(text):
        name = row.get("name", "")
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
