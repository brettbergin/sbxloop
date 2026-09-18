"""What a run says in the channel that asked for it.

A run is not a conversation, so it has no turn to reply on. This is the
narrow contract it speaks through instead: an agent, a kind of post, some
text, and the identity of the work it is about. The platform implements
:class:`ChannelPoster` over the collaboration store; the engine side builds
:class:`ChannelPost` objects and never learns how a channel is stored.

Every post names a ``dedupe_key``. A run that is replayed, resumed or
observed twice posts the same key again, and the same message comes back
rather than a second copy of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, get_args, runtime_checkable

#: What a post is: the plan a run settled on, progress through it, a
#: reviewer's verdict, what it delivered, a reply to someone who asked, or
#: a notice that it is blocked or waiting on a person.
PostKind = Literal["plan", "progress", "review", "delivery", "reply", "notice"]

#: Every kind this build knows. ``PostKind`` is not enforced at runtime, so
#: a post naming anything else is refused before it is stored, and a stored
#: kind outside this set (a later build's) reads back as no kind at all.
POST_KINDS: frozenset[str] = frozenset(get_args(PostKind))

#: The posts a channel hears even while it is silenced: a run that has
#: finished or stopped says so, because nobody is coming to look.
TERMINAL_POST_KINDS: frozenset[str] = frozenset({"delivery", "notice"})


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """A file a run delivered, by catalog identity; its bytes are served by
    ``GET /v1/artifacts/{id}``. ``run_id`` is the run's own id, not a
    public one: the platform renders it."""

    id: str
    run_id: str
    relpath: str
    media_type: str
    size: int


@dataclass(frozen=True, slots=True)
class ChannelPost:
    """One thing a run has to say, addressed to a channel."""

    channel_id: str
    author_agent: str
    kind: PostKind
    text: str
    run_id: str
    item_id: str
    #: Stable across replays of the same moment of the same run.
    dedupe_key: str
    task_id: str | None = None
    reply_to_message_id: str | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    #: The work snapshot to show with the post, when the caller has one.
    work: dict[str, Any] | None = field(default=None)


@runtime_checkable
class ChannelPoster(Protocol):
    """How a run reaches a channel. Every call is best-effort: a run never
    fails because a channel could not be written to."""

    def post(self, post: ChannelPost) -> str | None:
        """Record ``post`` and return its message id, or ``None`` when the
        channel is gone or silenced against this kind of post."""
        ...

    def channel_for_item(self, item_id: str) -> str | None:
        """The channel a work item answers to, if a channel asked for it."""
        ...


__all__ = [
    "POST_KINDS",
    "TERMINAL_POST_KINDS",
    "ArtifactRef",
    "ChannelPost",
    "ChannelPoster",
    "PostKind",
]
