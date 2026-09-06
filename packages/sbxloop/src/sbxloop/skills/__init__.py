"""The skill tree: procedures the agent pulls when it needs them.

A procedure the agent needs *sometimes* has had two homes, both bad. Inlined
into a phase prompt it is paid for on every turn of every run, whether or
not the turn is about it — and the prompts are already the loop's largest
recurring cost (``AGENTS.md`` goal 3). Left out entirely, the agent
rediscovers it by trial, which costs a revision.

A skill is the third option: one file, listed in the prompt by a single
line, fetched in full only when the agent decides it needs it. The
catalogue costs a handful of tokens per run; a body costs nothing until it
is asked for.

Each skill is ``<name>/SKILL.md`` with YAML frontmatter (``name``,
``description``, ``roles``) above a Markdown body, which is deliberately the
shape Claude Code's own skills use: the same tree can be handed to a backend
that loads skills natively without a second source of truth (the host tool
in ``engine.skilltools`` is the door every backend gets).

Skills are sbxloop's, not the target repository's. What the repository says
about itself reaches the agent through ``engine.repocontext`` instead, and
outranks anything here. Bodies obey the same rules as prompts: domain
neutral, no issue numbers, no paths into this codebase
(``scripts/check_self_references.py`` gates them alongside
``engine/prompts``).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from importlib import resources

import yaml

from sbxloop.engine.harness import Role

__all__ = [
    "Skill",
    "load_skills",
    "skill_body",
    "skills_for",
]

#: The frontmatter delimiter, as every SKILL.md opens and closes it.
_FENCE = "---"


@dataclass(frozen=True)
class Skill:
    """One skill: what the catalogue says about it, and what it says."""

    name: str
    description: str
    roles: frozenset[str]
    body: str


def _parse(text: str, source: str) -> tuple[dict[str, object], str]:
    """Split ``SKILL.md`` into its frontmatter mapping and its body.

    A file without frontmatter is a programming error rather than a
    silently description-less skill: the catalogue line is the only part
    the model sees for free, so a skill with no description is a skill
    nothing will ever load.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        raise ValueError(f"{source}: must open with a '{_FENCE}' frontmatter fence")
    try:
        end = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == _FENCE)
    except StopIteration:
        raise ValueError(f"{source}: frontmatter is never closed") from None
    meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    if not isinstance(meta, dict):
        raise ValueError(f"{source}: frontmatter must be a mapping")
    return meta, "\n".join(lines[end + 1 :]).strip() + "\n"


def _roles(raw: object, source: str) -> frozenset[str]:
    """``roles`` as a set. A comma-separated scalar and a YAML list are both
    accepted: the scalar keeps the frontmatter readable at a glance, and a
    backend that loads these natively ignores the key either way."""
    if isinstance(raw, str):
        names = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, list):
        names = [str(part).strip() for part in raw]
    else:
        raise ValueError(f"{source}: roles must be a list or a comma-separated string")
    roles = frozenset(name for name in names if name)
    if not roles:
        raise ValueError(f"{source}: names no roles, so nothing would ever load it")
    return roles


@cache
def load_skills() -> tuple[Skill, ...]:
    """Every shipped skill, in name order.

    Cached: the tree is package data and cannot change under a running
    daemon, and the catalogue is rendered on every phase of every run.
    """
    found: list[Skill] = []
    root = resources.files(__name__)
    for entry in root.iterdir():
        skill_file = entry / "SKILL.md"
        if not entry.is_dir() or not skill_file.is_file():
            continue
        source = f"{entry.name}/SKILL.md"
        meta, body = _parse(skill_file.read_text(encoding="utf-8"), source)
        name = str(meta.get("name") or entry.name)
        if name != entry.name:
            raise ValueError(f"{source}: frontmatter name {name!r} != directory {entry.name!r}")
        description = str(meta.get("description") or "").strip()
        if not description:
            raise ValueError(f"{source}: needs a one-line description for the catalogue")
        found.append(
            Skill(
                name=name,
                description=description,
                roles=_roles(meta.get("roles"), source),
                body=body,
            )
        )
    return tuple(sorted(found, key=lambda skill: skill.name))


def skills_for(role: Role) -> tuple[Skill, ...]:
    """The skills a session running as ``role`` may load. A role sees only
    what applies to it: a critic offered the delivery procedure would be
    reading about work it is forbidden to do."""
    return tuple(skill for skill in load_skills() if role in skill.roles)


def skill_body(name: str) -> str | None:
    """The full text of skill ``name``, or None when there is no such skill."""
    for skill in load_skills():
        if skill.name == name:
            return skill.body
    return None
