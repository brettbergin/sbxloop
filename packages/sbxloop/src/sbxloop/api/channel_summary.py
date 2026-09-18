"""Compacting a channel's history into one summary row.

A turn carries at most :data:`~sbxloop.api.collaboration.HISTORY_MESSAGES`
messages and :data:`~sbxloop.api.collaboration.HISTORY_CHARS` characters of
its channel. Past that the oldest messages fall out, and without this the
agents simply forget them. So once a channel has more messages than the
window keeps, a cheap one-shot model call writes what fell out into
``collaboration_channel_summaries``, and a trimmed history opens with the
newest such row.

Deliberately simple: no scheduler, no new configuration section. The job
runs after a turn settles -- on its own thread, never in the turn's lane --
on the concierge's own model (``[concierge] model``, falling back to the
top-level ``model``), in a session of the channel's own, with no tools and
no persona. It is best-effort: a failure leaves the watermark where it was,
so the next turn tries again, and a channel with a failing model keeps
working with a trimmed history and no summary.

The watermark only ever names a message the model was actually shown: what
did not fit in one excerpt stays backlog for the next compaction rather
than being marked covered and lost.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from sbxloop.api.collaboration import HISTORY_MESSAGES
from sbxloop.log import get_logger

if TYPE_CHECKING:
    from sbxloop.api.collaboration import CollaborationStore

log = get_logger(__name__)

#: What the summarising session is asked for. Domain-neutral: it describes
#: a conversation, never this project or any language.
PROMPT = """Summarise the earlier part of a conversation so the people and \
agents still in it keep what matters.

Write at most 10 short sentences. Keep decisions, constraints, names, \
file names and open questions. Drop pleasantries and anything already \
superseded. Write plain prose, no headings, no preamble.
"""
#: How the previous summary is carried into the next one.
_CONTINUES = "Summary of what came before this excerpt:\n{previous}\n\n"
_EXCERPT = "Conversation excerpt to summarise, oldest first:\n{transcript}"
#: How much has to fall out of the window, once a summary exists, before
#: it is rewritten: this many messages or this many characters, whichever
#: comes first. The first summary is written as soon as anything falls
#: out, so a trimmed history always has one to open with; after that a
#: channel past the cap pays for one summary call per batch rather than
#: one per turn. Until a batch is due, the few messages between the
#: summary and the window are in neither.
SUMMARY_BATCH_MESSAGES = 50
SUMMARY_BATCH_CHARS = 20_000


def summary_prompt(previous: str | None, transcript: str) -> str:
    """The one-shot ask: the standing instruction, what is already summarised
    and the transcript that fell out of the window."""
    carried = "" if not previous else _CONTINUES.format(previous=previous)
    return f"{PROMPT}\n{carried}{_EXCERPT.format(transcript=transcript)}"


class ChannelSummarizer:
    """Writes one summary row per compaction, best effort.

    ``summarize`` is the model call: the channel and the prompt in, prose
    out. It takes the channel because one channel's transcript may never
    be shown while another's is summarised, so the call it makes is scoped
    to the channel it is compacting. ``keep`` is the history window it
    compacts behind, and ``batch_messages`` / ``batch_chars`` how much has
    to fall out before an existing summary is rewritten, so a test can use
    small ones.
    """

    def __init__(
        self,
        store: CollaborationStore,
        summarize: Callable[[str, str], str],
        clock: Callable[[], float],
        *,
        keep: int = HISTORY_MESSAGES,
        batch_messages: int = SUMMARY_BATCH_MESSAGES,
        batch_chars: int = SUMMARY_BATCH_CHARS,
    ) -> None:
        self.store = store
        self.summarize = summarize
        self.clock = clock
        self.keep = keep
        self.batch_messages = batch_messages
        self.batch_chars = batch_chars

    def _is_batch(self, transcript: str) -> bool:
        """Whether enough has fallen out since the last summary to rewrite
        it. The transcript carries one message per line."""
        messages = transcript.count("\n") + 1
        return messages >= self.batch_messages or len(transcript) >= self.batch_chars

    def refresh(self, channel_id: str) -> bool:
        """Summarise whatever has fallen out of ``channel_id``'s window.

        ``False`` when there was nothing to summarise, or when the model
        gave nothing back; the watermark then stays where it was.
        """
        backlog = self.store.summary_backlog(channel_id, keep=self.keep)
        if backlog is None:
            return False
        through, previous, transcript = backlog
        if not transcript.strip():
            return False
        if previous is not None and not self._is_batch(transcript):
            # A summary already covers the start of the window; a message
            # or two falling out is not worth a model call of its own.
            return False
        try:
            content = self.summarize(channel_id, summary_prompt(previous, transcript)).strip()
        except Exception:
            log.warning("collaboration.summary_failed", channel=channel_id, exc_info=True)
            return False
        if not content:
            return False
        self.store.put_channel_summary(channel_id, through, content, self.clock())
        log.info("collaboration.summary_written", channel=channel_id, through=through)
        return True


__all__ = [
    "PROMPT",
    "SUMMARY_BATCH_CHARS",
    "SUMMARY_BATCH_MESSAGES",
    "ChannelSummarizer",
    "summary_prompt",
]
