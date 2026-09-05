"""Typed GitHub resource identifiers: ``gh:issue:<n>`` and ``gh:pr:<n>``.

This module owns the whole grammar of GitHub work-item ids. Nothing else in
the codebase should slice ``gh:`` strings by hand.

Ids may be repo-qualified — ``gh:<owner>/<name>:<kind>:<n>`` — so one daemon
can tend several repositories without item ids colliding.

Rendering is strict — every id this module produces carries its kind. Parsing
is lenient — the legacy bare form ``gh:<n>`` is accepted and normalised to
``gh:issue:<n>`` so checkpoints, watches and human-typed operator commands
written before the migration keep resolving.

Chat asks are work items too (#760): ``chat:<message>`` names the message
that asked, on the surface the concierge answered — an opaque key with no
grammar beyond its prefix, since the transport mints it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, get_args

GhKind = Literal["issue", "pr"]

GH_PREFIX = "gh:"
CHAT_PREFIX = "chat:"

_KINDS: tuple[GhKind, ...] = get_args(GhKind)
_NUMBER_RE = re.compile(r"^[0-9]+$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def is_repo_slug(value: str) -> bool:
    """True when ``value`` looks like an ``owner/name`` repository slug."""
    return bool(_REPO_RE.fullmatch(value))


@dataclass(frozen=True, slots=True)
class GhId:
    """A parsed GitHub resource id."""

    kind: GhKind
    number: int
    # The originating repository (``owner/name``), when the id carries one.
    # ``None`` means an id minted before multi-repo support, or one whose
    # repository is implied by the daemon's sole configured repo.
    repo: str | None = None

    def __str__(self) -> str:
        return self.item_id

    @property
    def item_id(self) -> str:
        """The canonical string form, repo-qualified when a repo is known."""
        return format_gh_id(self.kind, self.number, repo=self.repo)


def format_gh_id(kind: GhKind, number: int, repo: str | None = None) -> str:
    """Render the canonical id for a GitHub resource.

    With ``repo`` the id is repo-qualified (``gh:<owner>/<name>:<kind>:<n>``);
    without it the historical typed form ``gh:<kind>:<n>`` is produced.
    """
    if kind not in _KINDS:
        raise ValueError(f"unknown GitHub id kind: {kind!r}")
    if number < 1:
        raise ValueError(f"GitHub id number must be positive, got {number!r}")
    if repo is None:
        return f"{GH_PREFIX}{kind}:{number}"
    if not is_repo_slug(repo):
        raise ValueError(f"malformed repository slug: {repo!r}")
    return f"{GH_PREFIX}{repo}:{kind}:{number}"


def issue_item_id(number: int, repo: str | None = None) -> str:
    """The work item id for a GitHub issue."""
    return format_gh_id("issue", number, repo=repo)


def pr_item_id(number: int, repo: str | None = None) -> str:
    """The id for a GitHub pull request referenced as a work-item resource."""
    return format_gh_id("pr", number, repo=repo)


def chat_item_id(key: str) -> str:
    """The work item id for a chat ask, keyed by the message that made it."""
    key = key.strip()
    if not key or any(ch.isspace() for ch in key):
        raise ValueError(f"malformed chat item key: {key!r}")
    return f"{CHAT_PREFIX}{key}"


def is_chat_id(value: str) -> bool:
    """True when ``value`` is a chat ask's item id."""
    return value.startswith(CHAT_PREFIX) and len(value) > len(CHAT_PREFIX)


def is_gh_id(value: str) -> bool:
    """True when ``value`` is a well-formed GitHub id (typed or legacy)."""
    return try_parse_gh_id(value) is not None


def has_gh_prefix(value: str) -> bool:
    """True when ``value`` claims to be a GitHub id, well-formed or not."""
    return value.startswith(GH_PREFIX)


def parse_gh_id(value: str) -> GhId:
    """Parse a typed or legacy GitHub id, raising ``ValueError`` if malformed."""
    if not value.startswith(GH_PREFIX):
        raise ValueError(f"not a GitHub id: {value!r}")
    rest = value[len(GH_PREFIX) :]
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
    return GhId(kind=kind, number=number, repo=repo)


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
    "GH_PREFIX",
    "GhId",
    "GhKind",
    "chat_item_id",
    "format_gh_id",
    "has_gh_prefix",
    "is_chat_id",
    "is_gh_id",
    "is_repo_slug",
    "issue_item_id",
    "normalize_item_id",
    "parse_gh_id",
    "pr_item_id",
    "try_parse_gh_id",
]
