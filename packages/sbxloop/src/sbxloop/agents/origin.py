"""Where work an agent started came from, and how that travels.

An agent that starts a run or files an issue leaves a marker in the body it
writes::

    <!-- sbxloop:origin item=<parent item id> agent=<slug> depth=<n> -->

The marker is the only durable link between a queued issue and the agent
that asked for it: a queued issue is discovered by a poll like any other,
minutes or hours later, in a process that never saw the tool call. Reading
it back at discovery (:mod:`sbxloop.daemon.sources`) is what keeps a chain
of agent-started work countable — without it every generation would start
again at depth zero and the chain cap would never bite.

An inline workload does not need the marker to carry its origin (the
admission sets the item's columns directly), but it carries one anyway so
the ask a person reads says where it came from.

Nothing here talks to a store or a forge: rendering and parsing only, so
both ends of the round trip are one testable pair.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "MAX_CHAIN_DEPTH",
    "WorkOrigin",
    "origin_footer",
    "origin_from_body",
    "origin_marker",
]

#: The most a marker may claim, whatever it says. A forged or corrupted
#: body cannot buy itself a shallower chain by naming a huge number, and a
#: parser that read rubbish clamps rather than raising.
MAX_CHAIN_DEPTH = 64

#: The item id spelling the marker accepts: the typed ids the daemon mints
#: (``gh:issue:12``, ``api:abc``), never free text.
_ITEM = r"[A-Za-z0-9][A-Za-z0-9:._#/-]{0,119}"
_SLUG = r"[a-z0-9][a-z0-9_-]{0,63}"
_NO_PARENT = "none"

_MARKER_RE = re.compile(
    rf"<!--\s*sbxloop:origin\s+item=(?P<item>{_ITEM})\s+agent=(?P<agent>{_SLUG})"
    r"\s+depth=(?P<depth>\d{1,4})\s*-->"
)


@dataclass(frozen=True, slots=True)
class WorkOrigin:
    """One piece of agent-started work: who started it, what it came out of,
    and how many agent-to-agent hops deep it is.

    ``chain_depth`` describes *this* work, not its parent: work a person
    asked for is depth 0, the run an agent starts from it is depth 1.
    """

    agent_slug: str
    parent_item_id: str | None = None
    chain_depth: int = 0

    def marker(self) -> str:
        return origin_marker(self)


def origin_marker(origin: WorkOrigin) -> str:
    """The HTML comment ``origin`` travels in."""
    parent = origin.parent_item_id or _NO_PARENT
    depth = max(0, min(origin.chain_depth, MAX_CHAIN_DEPTH))
    return f"<!-- sbxloop:origin item={parent} agent={origin.agent_slug} depth={depth} -->"


def origin_from_body(body: str | None) -> WorkOrigin | None:
    """The origin a body claims, or None when it claims none.

    The *first* marker wins: a body that quotes an earlier issue cannot
    re-parent the work by appending a second one.
    """
    match = _MARKER_RE.search(body or "")
    if match is None:
        return None
    parent = match.group("item")
    return WorkOrigin(
        agent_slug=match.group("agent"),
        parent_item_id=None if parent == _NO_PARENT else parent,
        chain_depth=min(int(match.group("depth")), MAX_CHAIN_DEPTH),
    )


def origin_footer(agent_slug: str, on_behalf_of: str | None) -> str:
    """The sentence a filed issue or an agent's ask ends with, so a person
    reading it on the forge sees an agent wrote it and for whom."""
    who = f" on behalf of {on_behalf_of}" if on_behalf_of else ""
    return f"Filed by the `{agent_slug}` agent{who} via sbxloop."
