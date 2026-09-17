"""The platform's :class:`~sbxloop.agents.posts.ChannelPoster`.

A run says what it is doing in the channel that asked for it: an
``agent_update`` message authored by the agent doing the work, with the
files it delivered on the work snapshot beside it. Nothing here dispatches
or replays anything; it records what a run reports.

Every post is best-effort. A channel that is gone, silenced against this
kind of post, or already holds this dedupe key gets nothing new, and the
run carries on either way.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from sbxloop.agents.posts import ArtifactRef, ChannelPost
from sbxloop.api.publicids import run_public_id
from sbxloop.db.collaboration_models import ChannelRow
from sbxloop.db.event_scope import channel_for_item
from sbxloop.log import get_logger

if TYPE_CHECKING:
    from sbxloop.api.context import ApiContext

log = get_logger(__name__)


def _artifacts(refs: Sequence[ArtifactRef]) -> list[dict[str, Any]]:
    """The run's files as a channel reads them: the run id is public."""
    return [{**asdict(ref), "run_id": run_public_id(ref.run_id)} for ref in refs]


class ApiChannelPoster:
    """Run posts over the collaboration store."""

    def __init__(self, ctx: ApiContext) -> None:
        self.ctx = ctx

    def post(self, post: ChannelPost) -> str | None:
        turn_id = self.ctx.collaboration.turn_for_post(post.channel_id, post.reply_to_message_id)
        return self.ctx.collaboration.post_agent_update(
            channel_id=post.channel_id,
            author_agent=post.author_agent,
            kind=post.kind,
            text=post.text,
            run_id=post.run_id,
            dedupe_key=post.dedupe_key,
            now=self.ctx.clock(),
            turn_id=turn_id,
            work=self._work(post, turn_id),
        )

    def channel_for_item(self, item_id: str) -> str | None:
        with self.ctx.loop.dstore.read() as session:
            channel_id = channel_for_item(session, item_id)
            if channel_id is None:
                return None
            channel = session.get(ChannelRow, channel_id)
            return None if channel is None or channel.state != "active" else channel_id

    def _work(self, post: ChannelPost, turn_id: str | None) -> dict[str, Any] | None:
        """The work snapshot to show with the post.

        A snapshot belongs to a turn, so a channel that has not had one
        yet carries the post's text alone. Until messages hold their own
        attachments, the snapshot is where a post's files are named.
        """
        if turn_id is None:
            return None
        artifacts = _artifacts(post.artifacts)
        if post.work is not None:
            snapshot = dict(post.work)
            snapshot["turn_id"] = turn_id
            if artifacts:
                snapshot["artifacts"] = artifacts
            return snapshot
        from sbxloop.api.projections import Views

        item = self.ctx.loop.dstore.get(post.item_id)
        if item is None:
            return None
        public_item = Views(self.ctx).item(item)
        return {
            "item_id": public_item.id,
            "turn_id": turn_id,
            "agent_slug": post.author_agent,
            "title": public_item.title,
            "kind": public_item.kind,
            "state": public_item.state,
            "run_id": run_public_id(post.run_id) if post.run_id else public_item.run_id,
            "stage": None,
            "item_revision": public_item.revision,
            "run_revision": None,
            "item_actions": [],
            "run_actions": [],
            "artifacts": artifacts,
        }
