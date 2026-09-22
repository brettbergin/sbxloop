"""Channel traffic on its way out to the bridge surfaces linked to it.

A channel link makes a Slack, Discord or Mattermost surface a window onto a
collaboration channel. Inbound, the bridge turns what people type there into
channel turns (``sbxloop.daemon.chat``). This is the other direction: every
message appended to a linked channel — a person's, an agent's, a run's
delivery — is posted to each surface linked to it, once, under a
``**author**`` header so a reader can tell who said it.

The one rule that keeps a link from looping: a message that *came from* a
surface is never posted back to that surface. Its ``origin`` names where it
arrived, and that surface is skipped; other surfaces on the same channel
still see it, which is what makes two linked services mirror each other.

The mirror is a message observer on the collaboration store, so it runs on
whichever thread wrote the message. It must not block: every post is handed
to the bridge's own asyncio loop and this returns at once.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sbxloop.log import get_logger

if TYPE_CHECKING:
    from sbxloop.api.collaboration import ChannelLink, CollaborationStore, Message

log = get_logger(__name__)


def mirrored_text(message: Message) -> str:
    """One channel message as a bridge post: who said it, then what they
    said. The header is the same ``**name**`` an agent's run chronology
    carries, so a reader sees one convention on the surface."""
    author = message.author
    name = author.display_name or author.id or "sbxloop"
    return f"**{name}**\n{message.content}".strip()


class ChannelMirror:
    """Posts messages appended to a channel out to the surfaces linked to it."""

    def __init__(
        self,
        store: CollaborationStore,
        bridge_for: Callable[[str], Any],
    ) -> None:
        self.store = store
        self.bridge_for = bridge_for

    def message_appended(self, message: Message) -> None:
        for link in self._links(message.channel_id):
            if self._came_from(message, link):
                continue
            bridge = self.bridge_for(link.backend)
            if bridge is None:
                continue
            try:
                bridge.post_to_surface(link.surface_id, link.thread_id, mirrored_text(message))
            except Exception:
                log.warning(
                    "chat.mirror_failed",
                    backend=link.backend,
                    surface=link.surface_id,
                    exc_info=True,
                )

    def _links(self, channel_id: str) -> list[ChannelLink]:
        try:
            # The daemon itself reads the links: no viewer, so a channel
            # nobody in particular is looking at still mirrors.
            return self.store.list_channel_links(None, channel_id)
        except Exception:
            log.debug("chat.mirror_links_unavailable", channel=channel_id, exc_info=True)
            return []

    @staticmethod
    def _came_from(message: Message, link: ChannelLink) -> bool:
        """Did this message arrive on the surface this link names?"""
        origin = message.origin or {}
        return (
            str(origin.get("backend") or "") == link.backend
            and str(origin.get("surface_id") or "") == link.surface_id
        )


__all__ = ["ChannelMirror", "mirrored_text"]
