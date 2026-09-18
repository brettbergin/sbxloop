"""Letting a participant speak when nobody asked it to.

An agent in ``ambient`` mode is listening in, not waiting to be named, so a
message it cares about is one it may answer on its own. The cost of being
wrong is what shapes this: an agent that answers everything is noise, and
one that calls a model to decide on every message is expensive noise. So a
message reaches an ambient agent through three gates, cheapest first:

1. **Interests.** The agent's ``interests`` are matched, case-insensitively,
   against the last few messages. No match and no mention means the agent is
   not considered further and nothing is called.
2. **Guardrails.** The same bounds an agent-to-agent mention passes — chain
   depth, the rate caps, the pair cooldown, the channel's silence and the
   workspace token budget — plus ``ambient_max_per_hour`` for this agent in
   this channel.
3. **Relevance.** One short call on ``ambient_model`` (the concierge's model
   when unset) that answers RELEVANT or PASS. A PASS posts nothing and
   records ``collaboration.followup.suppressed`` with reason
   ``ambient_pass``. Each decision is recorded once, as its final outcome:
   ``queued`` only once the turn exists.

An agent never answers its own message, and one already answering the turn
the message belongs to does not also volunteer. ``ambient = false``, the
shipped default, skips all of it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from sbxloop.api.mentions import addressed_slugs
from sbxloop.daemon.usagepool import Admission
from sbxloop.log import get_logger

if TYPE_CHECKING:
    from sbxloop.api.collaboration import Author, Message

log = get_logger(__name__)

#: The trigger an ambient turn carries.
AMBIENT = "ambient"
#: Why an ambient turn was not taken.
AMBIENT_DECLINED = "ambient_pass"
AMBIENT_CAP = "ambient_cap"
#: One hour, the window ``ambient_max_per_hour`` counts within.
HOUR_S = 3600.0
#: What the classifier is told. It answers with one word and nothing else,
#: and it is never given tools.
CLASSIFIER_PERSONA = (
    "You decide whether one participant in a group conversation has reason "
    "to speak right now, when nobody addressed it.\n\n"
    "Answer with exactly one word: RELEVANT if the newest message is about "
    "something in that participant's stated interests and an answer from it "
    "would add something the conversation does not already have; PASS "
    "otherwise. PASS is the right answer far more often than RELEVANT: "
    "small talk, an aside, a message already answered, and anything only "
    "loosely related are all PASS. Never explain, never greet, never write "
    "anything but the one word."
)


def _word_pattern(phrase: str) -> re.Pattern[str] | None:
    """A case-insensitive match for ``phrase`` on word boundaries, so
    ``bread`` does not match ``breadth``. ``None`` for an empty interest."""
    cleaned = phrase.strip()
    if not cleaned:
        return None
    return re.compile(rf"(?<!\w){re.escape(cleaned)}(?!\w)", re.IGNORECASE)


def interested(interests: Iterable[str], texts: Iterable[str]) -> bool:
    """Whether any interest appears in any of ``texts``. The whole prefilter:
    no model is called to answer it."""
    patterns = [pattern for pattern in map(_word_pattern, interests) if pattern is not None]
    if not patterns:
        return False
    joined = "\n".join(texts)
    return any(pattern.search(joined) for pattern in patterns)


class AmbientAgent(Protocol):
    """What the selector reads about an agent it might wake."""

    @property
    def slug(self) -> str: ...

    @property
    def spec(self) -> Any: ...


class AmbientSelector:
    """Which listening participants answer a message nobody sent them."""

    def __init__(
        self,
        *,
        config: Callable[[], Any],
        participants: Callable[[str], Sequence[Any]],
        resolve: Callable[[str], AmbientAgent | None],
        recent: Callable[[str, int], Sequence[Any]],
        decide: Callable[..., Any],
        record: Callable[..., None],
        spoken_since: Callable[[str, str, float], int],
        classify: Callable[..., bool],
        queue: Callable[..., bool],
        clock: Callable[[], float],
    ) -> None:
        self.config = config
        self.participants = participants
        self.resolve = resolve
        self.recent = recent
        self.decide = decide
        self.record = record
        self.spoken_since = spoken_since
        self.classify = classify
        self.queue = queue
        self.clock = clock
        #: Agents already decided about while this selector lives. One turn
        #: is one conversation: a listening agent gets one look at it, not
        #: one per message the turn happens to post.
        self.decided: set[str] = set()

    def consider(
        self,
        channel_id: str,
        message: Message,
        *,
        author: Author,
        depth: int,
        answering: Sequence[str] = (),
    ) -> tuple[str, ...]:
        """Queue an ambient turn for each listening agent this message
        reaches; the slugs that will speak.

        ``answering`` are the agents already taking this message's turn, or
        already queued by a mention of them in it: they are replying anyway
        and do not also volunteer. An agent this
        selector has already decided about is not looked at again, so one
        turn draws at most one unprompted answer from each of them.
        """
        limits = self.config().collaboration
        if not limits.ambient:
            return ()
        now = self.clock()
        window = list(self.recent(channel_id, limits.ambient_window_messages))
        texts = [str(getattr(entry, "content", "")) for entry in window]
        named = set(addressed_slugs(message.content))
        skip = {slug for slug in answering if slug}
        if author.kind == "agent" and author.id:
            skip.add(author.id)
        spoke: list[str] = []
        for participant in self.participants(channel_id):
            slug = participant.agent_slug
            if participant.mode != AMBIENT or slug in skip or slug in self.decided:
                continue
            if participant.muted_until is not None and participant.muted_until > now:
                continue
            agent = self.resolve(slug)
            if agent is None:
                continue
            if slug not in named and not interested(agent.spec.interests, texts):
                continue
            self.decided.add(slug)

            def record(admission: Admission, slug: str = slug) -> None:
                # Exactly one audit record per decision: the final outcome.
                self.record(
                    channel_id,
                    source=author,
                    target_slug=slug,
                    depth=depth,
                    trigger=AMBIENT,
                    admission=admission,
                )

            if self.spoken_since(channel_id, slug, now - HOUR_S) >= limits.ambient_max_per_hour:
                record(Admission(ok=False, reason=AMBIENT_CAP))
                continue
            admission = self.decide(channel_id, source=author, target_slug=slug, depth=depth)
            if not getattr(admission, "ok", False):
                record(admission)
                continue
            if not self._relevant(channel_id, slug, agent, window):
                record(Admission(ok=False, reason=AMBIENT_DECLINED))
                continue
            try:
                accepted = self.queue(
                    author_slug=author.id or slug,
                    author_kind=author.kind,
                    channel_id=channel_id,
                    source_message_id=message.id,
                    target_slug=slug,
                    depth=depth,
                    trigger=AMBIENT,
                )
            except Exception:
                log.warning(
                    "collaboration.ambient_queue_failed",
                    channel=channel_id,
                    agent=slug,
                    exc_info=True,
                )
                continue
            if not accepted:
                continue
            record(Admission(ok=True))
            spoke.append(slug)
        return tuple(spoke)

    def _relevant(
        self, channel_id: str, slug: str, agent: AmbientAgent, window: Sequence[Any]
    ) -> bool:
        try:
            return bool(self.classify(channel_id, slug, agent.spec.interests, window))
        except Exception:
            log.warning(
                "collaboration.ambient_classify_failed",
                channel=channel_id,
                agent=slug,
                exc_info=True,
            )
            return False


def classifier_prompt(interests: Sequence[str], window: Sequence[Any]) -> str:
    """The one question the classifier answers, with the evidence it needs."""
    lines = []
    for entry in window:
        who = getattr(entry, "agent_slug", None) or getattr(entry, "role", "") or "someone"
        lines.append(f"{who}: {str(getattr(entry, 'content', '')).strip()}")
    transcript = "\n".join(lines[-20:]) or "(no messages)"
    wanted = ", ".join(value.strip() for value in interests if value.strip()) or "(none stated)"
    return (
        f"The participant's stated interests: {wanted}\n\n"
        f"The conversation so far, oldest first:\n{transcript}\n\n"
        "Should this participant speak about the newest message? "
        "Answer RELEVANT or PASS."
    )


def is_relevant(text: str) -> bool:
    """Read the classifier's answer. Anything that is not a clear RELEVANT
    is a PASS, so an unreadable answer keeps the agent quiet."""
    return text.strip().upper().startswith("RELEVANT")


__all__ = [
    "AMBIENT",
    "AMBIENT_CAP",
    "AMBIENT_DECLINED",
    "CLASSIFIER_PERSONA",
    "AmbientSelector",
    "classifier_prompt",
    "interested",
    "is_relevant",
]
