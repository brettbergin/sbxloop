"""Scheduled area audits: charters versioned in the target repository.

The discovery lane needs a steady supply of *good* charters, and Brett
wants every moving part visible on GitHub. So the charters live in the
repo itself — ``.github/sbxloop/audits/<name>.md`` with a small front-matter block —
where they are reviewed like code, and the daemon opens each one as a
``sbxloop:audit`` issue when it is due. What is due is decided from the
issues that already exist (title search on GitHub, plus the store as a
cache), so a wiped state dir cannot double-file and an audit that is still
open is never re-opened on top of itself.

Pure parsing/scheduling here; the loop does the filing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

AUDIT_DIR = Path(".github") / "sbxloop" / "audits"
AUDIT_TITLE_PREFIX = "audit: "
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
# Preferred metadata form: an HTML comment. Invisible on GitHub and left
# alone by every Markdown formatter (mdformat rewrote a `---` YAML block
# into a thematic break + an H2 the first time the charters shipped, and
# the daemon then saw no metadata at all).
_COMMENT_META = re.compile(r"\A\s*<!--\s*sbxloop(?:-audit)?\s*:?\s*(.*?)\s*-->\s*\n?", re.S)
_EVERY = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([dhm])\s*$", re.I)
_UNIT_S = {"d": 86_400.0, "h": 3_600.0, "m": 60.0}
_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class Charter:
    name: str
    every_s: float
    enabled: bool
    body: str
    rel: str  # path relative to the checkout, for the issue's provenance line

    @property
    def title(self) -> str:
        return f"{AUDIT_TITLE_PREFIX}{self.name}"


def parse_every(text: str) -> float:
    """``7d`` / ``12h`` / ``30m`` → seconds. ``0d`` means every tick (tests,
    or "run once now")."""
    m = _EVERY.match(text)
    if not m:
        raise ValueError(f"unrecognised interval {text!r} (use e.g. 7d, 12h, 30m)")
    return float(m.group(1)) * _UNIT_S[m.group(2).lower()]


def parse_charter(path: Path, rel: str | None = None) -> Charter:
    """One charter file. Metadata keys: ``every`` (required), ``enabled``
    (default true), given as ``<!-- sbxloop: every=7d -->`` on the first line
    (preferred) or a ``---`` front-matter block. The name is the file stem,
    which also keys the schedule and the issue title — keep it stable."""
    name = path.stem
    if not _NAME_OK.match(name):
        raise ValueError(f"charter file name {name!r} must be lowercase [a-z0-9._-]")
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    m = _COMMENT_META.match(text)
    if m:
        # `<!-- sbxloop: every=7d enabled=false -->` (also `key: value`)
        for token in re.split(r"[\s,;]+", m.group(1).strip()):
            if not token:
                continue
            key, sep, value = token.partition("=") if "=" in token else token.partition(":")
            if sep:
                meta[key.strip().lower()] = value.strip().strip("'\"")
    else:
        m = _FRONT_MATTER.match(text)
        if not m:
            raise ValueError(
                f"{path}: missing metadata — start the file with "
                "`<!-- sbxloop: every=7d -->` (or a --- every: 7d --- front-matter block)"
            )
        for line in m.group(1).splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip().lower()] = value.strip().strip("'\"")
    if "every" not in meta:
        raise ValueError(f"{path}: front-matter needs `every: <interval>`")
    body = text[m.end() :].strip()
    if not body:
        raise ValueError(f"{path}: charter body is empty")
    enabled = meta.get("enabled", "true").lower() not in ("false", "no", "0", "off")
    return Charter(
        name=name,
        every_s=parse_every(meta["every"]),
        enabled=enabled,
        body=body,
        rel=rel or path.name,
    )


def load_charters(checkout: Path, audit_dir: Path = AUDIT_DIR) -> tuple[list[Charter], list[str]]:
    """(charters, problems) under ``checkout/audit_dir``; a bad file is a
    problem string, never a crash — one broken charter must not stop the
    others."""
    folder = checkout / audit_dir
    if not folder.is_dir():
        return [], []
    charters: list[Charter] = []
    problems: list[str] = []
    for path in sorted(folder.glob("*.md")):
        try:
            charters.append(parse_charter(path, path.relative_to(checkout).as_posix()))
        except (ValueError, OSError) as exc:
            problems.append(str(exc))
    return charters, problems


def due_charters(
    charters: list[Charter], last_filed: dict[str, float], now: float
) -> list[Charter]:
    """Enabled charters whose interval has elapsed since they were last
    filed (never filed → due)."""
    out = []
    for c in charters:
        if not c.enabled:
            continue
        last = last_filed.get(c.name)
        if last is None or now - last >= c.every_s:
            out.append(c)
    return out


def issue_body(charter: Charter, marker: str) -> str:
    return (
        f"{charter.body}\n\n---\n"
        f"Scheduled audit `{charter.name}` (every {describe_every(charter.every_s)}); "
        f"charter: `{charter.rel}`.\n"
        f"{marker}"
    )


def audit_marker(name: str) -> str:
    return f"<!-- sbxloop-audit {name} -->"


def describe_every(seconds: float) -> str:
    for unit, size in (("d", 86_400.0), ("h", 3_600.0), ("m", 60.0)):
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{seconds:g}s"
