"""Compacting a channel's history into one summary row.

A turn carries at most :data:`~sbxloop.api.collaboration.HISTORY_MESSAGES`
messages and :data:`~sbxloop.api.collaboration.HISTORY_CHARS` characters of
its channel. Past that the oldest messages fall out, and without this the
agents simply forget them. So once a channel has more messages than the
window keeps, a cheap one-shot model call writes what fell out into
``collaboration_channel_summaries``, and a trimmed history opens with the
newest such row.

Deliberately simple: no scheduler, no new configuration section. The job
runs after a turn settles, on the concierge's own model (``[concierge]
model``, falling back to the top-level ``model``), with no tools and no
persona of its own. It is best-effort: a failure leaves the watermark where
it was, so the next turn tries again, and a channel with a failing model
keeps working with a trimmed history and no summary.
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


def summary_prompt(previous: str | None, transcript: str) -> str:
    """The one-shot ask: the standing instruction, what is already summarised
    and the transcript that fell out of the window."""
    carried = "" if not previous else _CONTINUES.format(previous=previous)
    return f"{PROMPT}\n{carried}{_EXCERPT.format(transcript=transcript)}"


class ChannelSummarizer:
    """Writes one summary row per compaction, best effort.

    ``summarize`` is the model call: prompt in, prose out. ``keep`` is the
    history window it compacts behind, so a test can use a small one.
    """

    def __init__(
        self,
        store: CollaborationStore,
        summarize: Callable[[str], str],
        clock: Callable[[], float],
        *,
        keep: int = HISTORY_MESSAGES,
    ) -> None:
        self.store = store
        self.summarize = summarize
        self.clock = clock
        self.keep = keep

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
        try:
            content = self.summarize(summary_prompt(previous, transcript)).strip()
        except Exception:
            log.warning("collaboration.summary_failed", channel=channel_id, exc_info=True)
            return False
        if not content:
            return False
        self.store.put_channel_summary(channel_id, through, content, self.clock())
        log.info("collaboration.summary_written", channel=channel_id, through=through)
        return True


__all__ = ["PROMPT", "ChannelSummarizer", "summary_prompt"]
