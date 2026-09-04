"""What the repository says about itself, for the prompts (#688).

A repository's own instruction files — ``AGENTS.md``, ``CLAUDE.md``, a
``.cursorrules``, Copilot's ``copilot-instructions.md``, ``CONTRIBUTING``
and ``CODEOWNERS`` — carry the conventions a run is judged by: "run
``make lint`` before committing", "never touch ``generated/``", "PRs
need a changelog entry". None of them reached the planner or the
reviewer, which run as their own sessions with their own prompts, and
the builder saw ``AGENTS.md`` only where its CLI happened to read it by
convention (the Claude Agent SDK loads no filesystem settings at all
unless told to). So the loop reads them itself and hands every phase
the same block, capped, under one heading.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CONVENTION_FILES",
    "RepoContext",
    "read_repo_context",
    "repo_conventions",
]

# In the order they are rendered: agent instructions first (the most
# specific to automated work), then the human contributor guide, then
# ownership. A file that is a symlink to, or a copy of, an earlier one
# is rendered once under both names.
CONVENTION_FILES: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    ".cursorrules",
    ".github/copilot-instructions.md",
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
    "CODEOWNERS",
    ".github/CODEOWNERS",
    "docs/CODEOWNERS",
)

HEADING = (
    "## Repository conventions (from the repository itself — follow them over the defaults below)"
)


@dataclass(frozen=True)
class RepoContext:
    """The rendered conventions block and where it came from."""

    conventions: str  # each file under a "### <path>" heading; "" when none
    files: tuple[str, ...]  # the paths rendered, in order
    clipped: bool  # the block was cut at the budget and says so


def _read(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _heading(names: list[str]) -> str:
    first, *others = names
    return f"{first} (also {', '.join(others)})" if others else first


def read_repo_context(workspace: Path | None, *, max_chars: int) -> RepoContext:
    """Read every convention file the workspace has, in ``CONVENTION_FILES``
    order, into one block of at most ``max_chars`` characters.

    Files that resolve to the same inode or carry the same text (an
    ``AGENTS.md`` symlinked as ``CLAUDE.md`` is the common case) are
    rendered once, the heading naming every path. A ``max_chars`` of 0
    reads nothing. Past the budget the block is cut and ends with an
    explicit ``(clipped at N chars)`` note, so a prompt never carries a
    silently truncated rule.
    """
    if workspace is None or max_chars <= 0:
        return RepoContext("", (), False)
    sections: list[tuple[list[str], str]] = []
    seen: dict[tuple[object, str], int] = {}
    for name in CONVENTION_FILES:
        path = workspace / name
        text = _read(path)
        if text is None or not text.strip():
            continue
        try:
            identity: object = path.resolve()
        except OSError:
            identity = name
        for key in ((identity, ""), ("text", text)):
            if key in seen:
                sections[seen[key]][0].append(name)
                break
        else:
            seen[(identity, "")] = seen[("text", text)] = len(sections)
            sections.append(([name], text.strip()))
    if not sections:
        return RepoContext("", (), False)
    rendered = "\n\n".join(f"### {_heading(names)}\n\n{text}" for names, text in sections)
    files = tuple(name for names, _ in sections for name in names)
    if len(rendered) <= max_chars:
        return RepoContext(rendered, files, False)
    return RepoContext(
        rendered[:max_chars].rstrip() + f"\n\n(clipped at {max_chars} chars)", files, True
    )


def repo_conventions(workspace: Path | None, *, max_chars: int) -> str:
    """The prompt section: the heading over the block, or "" when the
    repository declares nothing — so a template never carries an empty
    heading that teaches the model there was something to follow."""
    context = read_repo_context(workspace, max_chars=max_chars)
    if not context.conventions:
        return ""
    return f"{HEADING}\n\n{context.conventions}"
