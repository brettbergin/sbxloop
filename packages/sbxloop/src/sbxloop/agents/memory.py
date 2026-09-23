"""What an agent keeps beyond one conversation.

A memory belongs to one agent and is scoped by the channel it was learned
in: a memory with no source channel is global to that agent, and one with a
source channel is shown only in that channel, unless the platform says the
channel is visible to the whole workspace (:class:`ChannelVisibility`;
:class:`WorkspaceChannelVisibility` reads the channel's ``visibility``). A
person reviews and edits an agent's memories through the API; the agent
itself remembers, recalls and forgets through the tools in
:mod:`sbxloop.agents.tools`, and its memory block is added to its prompts.

:class:`MemoryService` is the one interface. Rows live in the daemon's store
(``agent_memories``); forgetting is a soft delete, updates are checked
against the row's ``revision``, and an agent past its configured cap loses
its oldest unpinned memory. Listing, updating and forgetting all take the
caller's ``readable`` channel access, so a memory a person may not read is
not found for them whichever of the three they ask for. Every change writes
an ``agent.memory.*`` event naming the memory, never its text.
"""

from __future__ import annotations

import builtins
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, get_args

from sqlalchemy import insert, or_, select

from sbxloop.db.api_models import ApiEventRow
from sbxloop.db.collaboration_models import AgentMemoryRow, ChannelMemberRow, ChannelRow
from sbxloop.errors import SbxloopError
from sbxloop.ids import _token

if TYPE_CHECKING:
    from sbxloop.config import MemoryConfig
    from sbxloop.daemon.store import DaemonStore

__all__ = [
    "MEMORY_KINDS",
    "PROMPT_HEADING",
    "AgentMemoryError",
    "ChannelVisibility",
    "Memory",
    "MemoryKind",
    "MemoryRevisionConflict",
    "MemoryService",
    "NoWorkspaceVisibility",
    "WorkspaceChannelVisibility",
]

MemoryKind = Literal["fact", "preference", "procedure"]
MEMORY_KINDS: frozenset[str] = frozenset(get_args(MemoryKind))
#: The first line of a non-empty prompt block.
PROMPT_HEADING = "## What you remember\n"
_WORD = re.compile(r"\w+")
_AUTHOR = re.compile(r"^(agent|user):\S+$")


class AgentMemoryError(SbxloopError):
    """A memory operation refused; ``code`` is the API's problem code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MemoryRevisionConflict(AgentMemoryError):
    """The memory changed since the revision the caller acted on."""

    def __init__(self, memory_id: str, expected: int, actual: int) -> None:
        super().__init__(
            "revision_conflict",
            f"memory {memory_id} is at revision {actual}, not {expected}",
        )
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class Memory:
    id: str
    agent_slug: str
    kind: str
    content: str
    source_channel_id: str | None
    source_run_id: str | None
    source_message_id: str | None
    author: str
    pinned: bool
    created_at: float
    updated_at: float
    last_used_at: float | None
    revision: int


class ChannelVisibility(Protocol):
    def workspace_visible(self, channel_id: str) -> bool:
        """Whether what was learned in ``channel_id`` may be shown in any
        other channel of the workspace."""
        ...


class NoWorkspaceVisibility:
    """Every channel is private: a memory stays in the channel it came from."""

    def workspace_visible(self, channel_id: str) -> bool:
        return False


class WorkspaceChannelVisibility:
    """The platform's answer: a channel whose ``visibility`` is
    ``workspace`` shares what was learned in it; any other channel, or one
    that does not exist, keeps it."""

    def __init__(self, store: DaemonStore) -> None:
        self.store = store

    def workspace_visible(self, channel_id: str) -> bool:
        with self.store.read() as session:
            row = session.get(ChannelRow, channel_id)
            return row is not None and row.visibility == "workspace"

    def readable_by(self, user_id: str) -> Callable[[str], bool]:
        """Whether ``user_id`` may read a channel: a workspace channel, one
        they created, or one they are a member of. The channels are read
        once, when this is called."""
        with self.store.read() as session:
            readable = set(
                session.scalars(
                    select(ChannelRow.id).where(
                        or_(
                            ChannelRow.visibility == "workspace",
                            ChannelRow.user_id == user_id,
                            ChannelRow.id.in_(
                                select(ChannelMemberRow.channel_id).where(
                                    ChannelMemberRow.user_id == user_id
                                )
                            ),
                        )
                    )
                )
            )
        return readable.__contains__


def _memory(row: AgentMemoryRow) -> Memory:
    return Memory(
        id=str(row.id),
        agent_slug=str(row.agent_slug),
        kind=str(row.kind),
        content=str(row.content),
        source_channel_id=row.source_channel_id,
        source_run_id=row.source_run_id,
        source_message_id=row.source_message_id,
        author=str(row.author),
        pinned=bool(row.pinned),
        created_at=float(row.created_at or 0.0),
        updated_at=float(row.updated_at or 0.0),
        last_used_at=None if row.last_used_at is None else float(row.last_used_at),
        revision=int(row.revision),
    )


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.casefold()))


def _recent_first(memory: Memory) -> tuple[float, float, str]:
    return (-memory.updated_at, -memory.created_at, memory.id)


def _event(session: Any, type_: str, now: float, memory: Memory, **extra: Any) -> None:
    """The change, with who made it and where the memory came from; the
    text stays out of the chronology. A memory learned in a channel is
    that channel's event, so only people who can open it see the change."""
    data = {
        "memory_id": memory.id,
        "agent_slug": memory.agent_slug,
        "kind": memory.kind,
        "pinned": memory.pinned,
        "revision": memory.revision,
        "source_channel_id": memory.source_channel_id,
        **extra,
    }
    session.execute(
        insert(ApiEventRow).values(
            recorded_at=now,
            occurred_at=now,
            type=type_,
            run_id=None,
            item_id=None,
            operation_id=None,
            actor_json=None,
            source_seq=None,
            channel_id=memory.source_channel_id,
            data_json=json.dumps(data, default=str),
        )
    )


def _live(session: Any, agent: str) -> list[Memory]:
    rows = session.scalars(
        select(AgentMemoryRow).where(
            AgentMemoryRow.agent_slug == agent,
            AgentMemoryRow.deleted_at.is_(None),
        )
    )
    return [_memory(row) for row in rows]


def _row(
    session: Any,
    memory_id: str,
    agent: str | None,
    readable: Callable[[str], bool] | None = None,
) -> AgentMemoryRow:
    """The live memory ``memory_id`` names, once it is ``agent``'s and its
    source channel is one ``readable`` admits. Every miss is the same
    ``memory_not_found``, so a memory the caller may not read answers what
    an unknown id answers and is never confirmed to exist."""
    row = session.get(AgentMemoryRow, memory_id)
    if (
        not isinstance(row, AgentMemoryRow)
        or row.deleted_at is not None
        or (agent is not None and row.agent_slug != agent)
        or (
            readable is not None
            and row.source_channel_id is not None
            and not readable(str(row.source_channel_id))
        )
    ):
        raise AgentMemoryError("memory_not_found", "memory not found")
    return row


def _check_author(author: str) -> None:
    if not _AUTHOR.match(author):
        raise AgentMemoryError(
            "invalid_memory", "a memory's author is 'agent:<slug>' or 'user:<id>'"
        )


class MemoryService:
    def __init__(
        self,
        store: DaemonStore,
        visibility: ChannelVisibility,
        cfg: MemoryConfig,
        clock: Callable[[], float],
    ) -> None:
        self.store = store
        self.visibility = visibility
        self.cfg = cfg
        self.clock = clock

    def _clean(self, content: str) -> str:
        clean = content.strip()[: self.cfg.max_item_chars].strip()
        if not clean:
            raise AgentMemoryError("invalid_memory", "a memory needs some text")
        return clean

    def _visible(
        self, memory: Memory, channel_id: str | None, shared: dict[str, bool] | None = None
    ) -> bool:
        source = memory.source_channel_id
        if source is None or source == channel_id:
            return True
        if shared is None:
            return self.visibility.workspace_visible(source)
        if source not in shared:
            shared[source] = self.visibility.workspace_visible(source)
        return shared[source]

    def remember(
        self,
        agent: str,
        content: str,
        *,
        kind: MemoryKind = "fact",
        channel_id: str | None,
        run_id: str | None = None,
        message_id: str | None = None,
        author: str,
        pinned: bool = False,
    ) -> Memory:
        """Keep ``content`` for ``agent``, learned in ``channel_id`` (None:
        global). Past the cap the agent's oldest unpinned memory goes; a
        store holding only pinned memories refuses instead."""
        if not self.cfg.enabled:
            raise AgentMemoryError("memory_disabled", "agent memory is turned off")
        if kind not in MEMORY_KINDS:
            raise AgentMemoryError(
                "invalid_memory", f"kind must be one of: {', '.join(sorted(MEMORY_KINDS))}"
            )
        _check_author(author)
        clean = self._clean(content)
        now = self.clock()
        memory_id = "mem_" + _token(16)
        with self.store.transaction() as session:
            live = _live(session, agent)
            excess = len(live) + 1 - self.cfg.max_items_per_agent
            evict: list[Memory] = []
            if excess > 0:
                unpinned = sorted(
                    (memory for memory in live if not memory.pinned),
                    key=lambda memory: (memory.created_at, memory.id),
                )
                if len(unpinned) < excess:
                    raise AgentMemoryError(
                        "memory_full",
                        f"{agent} keeps {len(live)} pinned memories already; "
                        "unpin or forget one first",
                    )
                evict = unpinned[:excess]
            for old in evict:
                _row(session, old.id, agent).deleted_at = now
                _event(session, "agent.memory.deleted", now, old, author=author, reason="evicted")
            session.execute(
                insert(AgentMemoryRow).values(
                    id=memory_id,
                    agent_slug=agent,
                    kind=kind,
                    content=clean,
                    source_channel_id=channel_id,
                    source_run_id=run_id,
                    source_message_id=message_id,
                    author=author,
                    pinned=1 if pinned else 0,
                    created_at=now,
                    updated_at=now,
                    revision=1,
                )
            )
            created = _memory(_row(session, memory_id, agent))
            _event(session, "agent.memory.created", now, created, author=author)
            return created

    def recall(
        self, agent: str, *, query: str, channel_id: str | None, limit: int = 10
    ) -> builtins.list[Memory]:
        """``agent``'s memories visible in ``channel_id`` that share a word
        with ``query`` (all of them for a blank query): pinned first, then
        the most words in common, then the most recent. What is returned is
        marked as used."""
        if not self.cfg.enabled or limit <= 0:
            return []
        terms = _words(query)
        now = self.clock()
        # The platform is asked about each source channel before the write
        # transaction opens (it reads the same store); a source that appears
        # in between stays private for this call.
        with self.store.read() as session:
            sources = {memory.source_channel_id for memory in _live(session, agent)}
        shared = {
            source: self.visibility.workspace_visible(source)
            for source in sources
            if source is not None and source != channel_id
        }
        with self.store.transaction() as session:
            scored: list[tuple[int, Memory]] = []
            for memory in _live(session, agent):
                source = memory.source_channel_id
                if not (source is None or source == channel_id or shared.get(source, False)):
                    continue
                score = len(terms & _words(memory.content))
                if terms and not score:
                    continue
                scored.append((score, memory))
            scored.sort(key=lambda pair: (not pair[1].pinned, -pair[0], *_recent_first(pair[1])))
            chosen = [memory for _, memory in scored[:limit]]
            for memory in chosen:
                _row(session, memory.id, agent).last_used_at = now
            return chosen

    def forget(
        self,
        agent: str,
        memory_id: str,
        *,
        author: str,
        readable: Callable[[str], bool] | None = None,
    ) -> None:
        """Soft-delete one of ``agent``'s memories. ``readable``, when given,
        is the caller's channel access: a memory from a channel it cannot
        read is not found, exactly as an unknown id is not found."""
        _check_author(author)
        now = self.clock()
        with self.store.transaction() as session:
            row = _row(session, memory_id, agent, readable)
            row.deleted_at = now
            _event(session, "agent.memory.deleted", now, _memory(row), author=author)

    def update(
        self,
        memory_id: str,
        *,
        content: str | None,
        pinned: bool | None,
        expected_revision: int,
        author: str,
        agent: str | None = None,
        readable: Callable[[str], bool] | None = None,
    ) -> Memory:
        """Change a memory's text or pin at ``expected_revision``; ``agent``,
        when given, must be the memory's own. ``readable``, when given, is
        the caller's channel access: a memory from a channel it cannot read
        is not found, so neither the change nor the text is theirs to take."""
        _check_author(author)
        clean = None if content is None else self._clean(content)
        now = self.clock()
        with self.store.transaction() as session:
            row = _row(session, memory_id, agent, readable)
            if int(row.revision) != expected_revision:
                raise MemoryRevisionConflict(memory_id, expected_revision, int(row.revision))
            if clean is not None:
                row.content = clean
            if pinned is not None:
                row.pinned = 1 if pinned else 0
            row.updated_at = now
            row.revision = int(row.revision) + 1
            session.flush()
            updated = _memory(row)
            _event(session, "agent.memory.updated", now, updated, author=author)
            return updated

    def prompt_block(
        self, agent: str, *, channel_id: str | None, budget_chars: int | None = None
    ) -> str:
        """The memories ``agent`` may see in ``channel_id`` as a prompt
        section of at most ``budget_chars`` (default: the configured
        budget): pinned first, then the most recent. Empty when nothing is
        visible or nothing fits, so a prompt without memories is unchanged."""
        if not self.cfg.enabled:
            return ""
        budget = self.cfg.prompt_budget_chars if budget_chars is None else budget_chars
        memories = sorted(
            self.list(agent, channel_id=channel_id, include_private=False),
            key=lambda memory: (not memory.pinned, *_recent_first(memory)),
        )
        lines: list[str] = []
        used = len(PROMPT_HEADING)
        for memory in memories:
            line = f"- ({memory.kind}) {' '.join(memory.content.split())}\n"
            if used + len(line) > budget:
                break
            lines.append(line)
            used += len(line)
        return PROMPT_HEADING + "".join(lines) if lines else ""

    def list(
        self,
        agent: str,
        *,
        channel_id: str | None,
        include_private: bool,
        query: str | None = None,
        readable: Callable[[str], bool] | None = None,
    ) -> builtins.list[Memory]:
        """``agent``'s live memories, most recent first: those visible in
        ``channel_id``, or all of them with ``include_private``. ``query``
        keeps those sharing a word with it. ``readable``, when given, is the
        reader's channel access: a memory from a channel it cannot read is
        left out either way. Listing is not a use."""
        terms = _words(query or "")
        with self.store.read() as session:
            memories = _live(session, agent)
        shared: dict[str, bool] = {}

        def shown(memory: Memory) -> bool:
            source = memory.source_channel_id
            if readable is not None and source is not None and not readable(source):
                return False
            return include_private or self._visible(memory, channel_id, shared)

        return sorted(
            (
                memory
                for memory in memories
                if shown(memory) and (not terms or terms & _words(memory.content))
            ),
            key=_recent_first,
        )
