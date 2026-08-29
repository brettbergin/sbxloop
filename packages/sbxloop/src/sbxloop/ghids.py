"""Typed GitHub resource identifiers: ``gh:issue:<n>`` and ``gh:pr:<n>``.

This module owns the whole grammar of GitHub work-item ids. Nothing else in
the codebase should slice ``gh:`` strings by hand.

Rendering is strict — every id this module produces carries its kind. Parsing
is lenient — the legacy bare form ``gh:<n>`` is accepted and normalised to
``gh:issue:<n>`` so checkpoints, watches and human-typed operator commands
written before the migration keep resolving.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, get_args

GhKind = Literal["issue", "pr"]

GH_PREFIX = "gh:"

_KINDS: tuple[GhKind, ...] = get_args(GhKind)
_NUMBER_RE = re.compile(r"^[0-9]+$")


@dataclass(frozen=True, slots=True)
class GhId:
    """A parsed GitHub resource id."""

    kind: GhKind
    number: int

    def __str__(self) -> str:
        return format_gh_id(self.kind, self.number)

    @property
    def item_id(self) -> str:
        """The canonical typed string form."""
        return format_gh_id(self.kind, self.number)


def format_gh_id(kind: GhKind, number: int) -> str:
    """Render the canonical typed id for a GitHub resource."""
    if kind not in _KINDS:
        raise ValueError(f"unknown GitHub id kind: {kind!r}")
    if number < 1:
        raise ValueError(f"GitHub id number must be positive, got {number!r}")
    return f"{GH_PREFIX}{kind}:{number}"


def issue_item_id(number: int) -> str:
    """The work item id for a GitHub issue."""
    return format_gh_id("issue", number)


def pr_item_id(number: int) -> str:
    """The id for a GitHub pull request referenced as a work-item resource."""
    return format_gh_id("pr", number)


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
    return GhId(kind=kind, number=number)


def try_parse_gh_id(value: str) -> GhId | None:
    """Parse a GitHub id, returning ``None`` instead of raising."""
    try:
        return parse_gh_id(value)
    except ValueError:
        return None


def normalize_item_id(value: str) -> str:
    """Canonicalise a work item id.

    GitHub ids are returned in typed form; ids from other sources (e.g.
    ``inbox:foo.md``) and unparseable values are returned unchanged.
    """
    parsed = try_parse_gh_id(value)
    if parsed is None:
        return value
    return parsed.item_id


__all__ = [
    "GH_PREFIX",
    "GhId",
    "GhKind",
    "format_gh_id",
    "has_gh_prefix",
    "is_gh_id",
    "issue_item_id",
    "normalize_item_id",
    "parse_gh_id",
    "pr_item_id",
    "try_parse_gh_id",
]
