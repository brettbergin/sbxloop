"""Typed forge resource identifiers: ``gh:issue:<n>`` and ``gh:pr:<n>``.

This module owns the whole grammar of forge work-item ids. Nothing else in
the codebase should slice ``gh:`` strings by hand.

The prefix names the forge — ``gh:`` GitHub, ``gl:`` GitLab, ``gt:`` Gitea
(:data:`FORGE_PREFIXES`) — so one daemon can tend repositories on more than
one and the ids never collide; ``gh`` is the default everywhere a forge is
not named. Ids may be repo-qualified — ``gh:<owner>/<name>:<kind>:<n>`` —
so one daemon can tend several repositories without item ids colliding.

Rendering is strict — every id this module produces carries its kind. Parsing
is lenient — the legacy bare form ``gh:<n>`` is accepted and normalised to
``gh:issue:<n>`` so checkpoints, watches and human-typed operator commands
written before the migration keep resolving.

Chat asks are work items too (#760): ``chat:<message>`` names the message
that asked, on the surface the concierge answered — an opaque key with no
grammar beyond its prefix, since the transport mints it. So are the ticks
of a schedule (#761): ``sched:<name>:<due>`` names the schedule and the
minute it was due, in UTC. Both are *local* ids — nothing on GitHub stands
behind them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, get_args

GhKind = Literal["issue", "pr"]

# The forge each id prefix names. ``gh`` is the historical and default one.
FORGE_PREFIXES: dict[str, str] = {"gh": "gh:", "gl": "gl:", "gt": "gt:"}
GH_PREFIX = FORGE_PREFIXES["gh"]
DEFAULT_FORGE = "gh"
CHAT_PREFIX = "chat:"
SCHED_PREFIX = "sched:"

_KINDS: tuple[GhKind, ...] = get_args(GhKind)
_NUMBER_RE = re.compile(r"^[0-9]+$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def is_repo_slug(value: str) -> bool:
    """True when ``value`` looks like an ``owner/name`` repository slug."""
    return bool(_REPO_RE.fullmatch(value))


@dataclass(frozen=True, slots=True)
class GhId:
    """A parsed forge resource id."""

    kind: GhKind
    number: int
    # The originating repository (``owner/name``), when the id carries one.
    # ``None`` means an id minted before multi-repo support, or one whose
    # repository is implied by the daemon's sole configured repo.
    repo: str | None = None
    # Which forge holds the resource — a key of :data:`FORGE_PREFIXES`.
    forge: str = DEFAULT_FORGE

    def __str__(self) -> str:
        return self.item_id

    @property
    def item_id(self) -> str:
        """The canonical string form, repo-qualified when a repo is known."""
        return format_gh_id(self.kind, self.number, repo=self.repo, forge=self.forge)


def format_gh_id(
    kind: GhKind, number: int, repo: str | None = None, *, forge: str = DEFAULT_FORGE
) -> str:
    """Render the canonical id for a forge resource.

    With ``repo`` the id is repo-qualified (``gh:<owner>/<name>:<kind>:<n>``);
    without it the historical typed form ``gh:<kind>:<n>`` is produced.
    ``forge`` picks the prefix (:data:`FORGE_PREFIXES`).
    """
    if kind not in _KINDS:
        raise ValueError(f"unknown GitHub id kind: {kind!r}")
    if number < 1:
        raise ValueError(f"GitHub id number must be positive, got {number!r}")
    prefix = FORGE_PREFIXES.get(forge)
    if prefix is None:
        raise ValueError(f"unknown forge: {forge!r}")
    if repo is None:
        return f"{prefix}{kind}:{number}"
    if not is_repo_slug(repo):
        raise ValueError(f"malformed repository slug: {repo!r}")
    return f"{prefix}{repo}:{kind}:{number}"


def issue_item_id(number: int, repo: str | None = None, *, forge: str = DEFAULT_FORGE) -> str:
    """The work item id for an issue."""
    return format_gh_id("issue", number, repo=repo, forge=forge)


def pr_item_id(number: int, repo: str | None = None, *, forge: str = DEFAULT_FORGE) -> str:
    """The id for a pull request referenced as a work-item resource."""
    return format_gh_id("pr", number, repo=repo, forge=forge)


def chat_item_id(key: str) -> str:
    """The work item id for a chat ask, keyed by the message that made it."""
    key = key.strip()
    if not key or any(ch.isspace() for ch in key):
        raise ValueError(f"malformed chat item key: {key!r}")
    return f"{CHAT_PREFIX}{key}"


def is_chat_id(value: str) -> bool:
    """True when ``value`` is a chat ask's item id."""
    return value.startswith(CHAT_PREFIX) and len(value) > len(CHAT_PREFIX)


def schedule_item_id(name: str, due: str) -> str:
    """The work item id for one tick of a schedule: its name and the
    minute it was due (``2026-09-05T07:00Z``)."""
    if not name or ":" in name or any(ch.isspace() for ch in name):
        raise ValueError(f"malformed schedule name: {name!r}")
    if not due or any(ch.isspace() for ch in due):
        raise ValueError(f"malformed schedule due: {due!r}")
    return f"{SCHED_PREFIX}{name}:{due}"


def is_schedule_id(value: str) -> bool:
    """True when ``value`` is a schedule tick's item id."""
    return value.startswith(SCHED_PREFIX) and len(value) > len(SCHED_PREFIX)


def parse_schedule_id(value: str) -> tuple[str, str]:
    """``(name, due)`` from a schedule tick's id; ``ValueError`` otherwise."""
    if not is_schedule_id(value):
        raise ValueError(f"not a schedule id: {value!r}")
    name, sep, due = value[len(SCHED_PREFIX) :].partition(":")
    if not sep or not name or not due:
        raise ValueError(f"malformed schedule id: {value!r}")
    return name, due


def is_local_id(value: str) -> bool:
    """True for an id with nothing on GitHub behind it — a chat ask or a
    schedule tick: no issue to read, comment on or label."""
    return is_chat_id(value) or is_schedule_id(value)


def is_gh_id(value: str) -> bool:
    """True when ``value`` is a well-formed forge id (typed or legacy)."""
    return try_parse_gh_id(value) is not None


def _forge_of(value: str) -> str | None:
    """The forge whose prefix ``value`` carries, or None."""
    for forge, prefix in FORGE_PREFIXES.items():
        if value.startswith(prefix):
            return forge
    return None


def has_gh_prefix(value: str) -> bool:
    """True when ``value`` claims to be a forge id, well-formed or not."""
    return _forge_of(value) is not None


def parse_gh_id(value: str) -> GhId:
    """Parse a typed or legacy forge id, raising ``ValueError`` if malformed."""
    forge = _forge_of(value)
    if forge is None:
        raise ValueError(f"not a GitHub id: {value!r}")
    rest = value[len(FORGE_PREFIXES[forge]) :]
    repo: str | None = None
    head, sep, tail = rest.partition(":")
    if sep and "/" in head:
        # Repo-qualified: gh:<owner>/<name>:<kind>:<n>
        if not is_repo_slug(head):
            raise ValueError(f"malformed repository slug in {value!r}")
        if ":" not in tail:
            raise ValueError(f"malformed repo-qualified GitHub id: {value!r}")
        repo = head
        rest = tail
    elif "/" in rest:
        raise ValueError(f"malformed repo-qualified GitHub id: {value!r}")
    if ":" in rest:
        kind_text, _, number_text = rest.partition(":")
        if kind_text not in _KINDS:
            raise ValueError(f"unknown GitHub id kind in {value!r}")
        kind: GhKind = "issue" if kind_text == "issue" else "pr"
    else:
        # Legacy bare form: gh:<n> always meant an issue.
        kind = "issue"
        number_text = rest
    if not _NUMBER_RE.fullmatch(number_text):
        raise ValueError(f"malformed GitHub id number in {value!r}")
    number = int(number_text)
    if number < 1:
        raise ValueError(f"GitHub id number must be positive: {value!r}")
    return GhId(kind=kind, number=number, repo=repo, forge=forge)


def try_parse_gh_id(value: str) -> GhId | None:
    """Parse a GitHub id, returning ``None`` instead of raising."""
    try:
        return parse_gh_id(value)
    except ValueError:
        return None


def normalize_item_id(value: str) -> str:
    """Canonicalise a work item id.

    GitHub ids are returned in typed form; a repo-qualified id keeps its
    repository. Ids from other sources (e.g. ``inbox:foo.md``) and
    unparseable values are returned unchanged.
    """
    parsed = try_parse_gh_id(value)
    if parsed is None:
        return value
    return parsed.item_id


__all__ = [
    "CHAT_PREFIX",
    "DEFAULT_FORGE",
    "FORGE_PREFIXES",
    "GH_PREFIX",
    "SCHED_PREFIX",
    "GhId",
    "GhKind",
    "chat_item_id",
    "format_gh_id",
    "has_gh_prefix",
    "is_chat_id",
    "is_gh_id",
    "is_local_id",
    "is_repo_slug",
    "is_schedule_id",
    "issue_item_id",
    "normalize_item_id",
    "parse_gh_id",
    "parse_schedule_id",
    "pr_item_id",
    "schedule_item_id",
    "try_parse_gh_id",
]
