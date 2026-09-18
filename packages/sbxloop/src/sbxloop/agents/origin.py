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

The marker is the daemon's, never the agent's. The agent writes the body;
the daemon strips every marker out of it (:func:`strip_origin_markers`)
and appends its own, and the reader takes the last one, so nothing an
agent types can name a different agent or claim a shallower chain.

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
    "strip_origin_markers",
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


def strip_origin_markers(text: str) -> str:
    """``text`` with every marker in it removed.

    Everything an agent writes goes through this before the daemon appends
    its own marker: the body is agent-controlled text, and a marker the
    agent wrote would otherwise be read back at discovery as if the daemon
    had written it -- naming another agent and resetting the chain depth,
    which would make both the per-agent cap and ``max_chain_depth``
    unenforceable.

    Removal repeats until nothing matches, because one marker can be
    written *inside* another: taking the inner one out would leave a valid
    outer one behind. Every pass shortens the text, so this terminates.
    """
    while True:
        stripped = _MARKER_RE.sub("", text)
        if stripped == text:
            return text
        text = stripped


def origin_from_body(body: str | None) -> WorkOrigin | None:
    """The origin a body claims, or None when it claims none.

    The *last* marker wins. The daemon appends its marker after whatever
    the agent wrote, so the last one is always the one the daemon itself
    put there -- the reading that cannot be steered from a body, even if a
    forged marker survived :func:`strip_origin_markers`.
    """
    matches = _MARKER_RE.findall(body or "")
    if not matches:
        return None
    item, agent, depth = matches[-1]
    return WorkOrigin(
        agent_slug=agent,
        parent_item_id=None if item == _NO_PARENT else item,
        chain_depth=min(int(depth), MAX_CHAIN_DEPTH),
    )


def origin_footer(agent_slug: str, on_behalf_of: str | None) -> str:
    """The sentence a filed issue or an agent's ask ends with, so a person
    reading it on the forge sees an agent wrote it and for whom."""
    who = f" on behalf of {on_behalf_of}" if on_behalf_of else ""
    return f"Filed by the `{agent_slug}` agent{who} via sbxloop."
