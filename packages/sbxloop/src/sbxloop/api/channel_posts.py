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

from pydantic import ValidationError

from sbxloop.agents.posts import ArtifactRef, ChannelPost
from sbxloop.api.collaboration_schemas import ChannelWorkOut
from sbxloop.api.publicids import run_public_id
from sbxloop.db.collaboration_models import ChannelRow
from sbxloop.db.event_scope import channel_for_item
from sbxloop.log import get_logger

if TYPE_CHECKING:
    from sbxloop.api.context import ApiContext

log = get_logger(__name__)

#: Files named on one post; the run's own catalog lists the rest.
ARTIFACTS_MAX = 50


def _artifacts(refs: Sequence[ArtifactRef]) -> list[dict[str, Any]]:
    """The run's files as a channel reads them: the run id is public."""
    return [{**asdict(ref), "run_id": run_public_id(ref.run_id)} for ref in refs]


class ApiChannelPoster:
    """Run posts over the collaboration store."""

    def __init__(self, ctx: ApiContext) -> None:
        self.ctx = ctx

    def post(self, post: ChannelPost) -> str | None:
        """Record ``post``, or nothing at all. A run is not a caller this
        can fail: a store that will not take the post costs the channel
        what it had to say, never the work it was saying it about."""
        try:
            turn_id = self.ctx.collaboration.turn_for_post(
                post.channel_id, post.reply_to_message_id, post.item_id
            )
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
        except Exception:
            log.warning(
                "api.channel_post_failed",
                channel=post.channel_id,
                key=post.dedupe_key,
                run=post.run_id,
                exc_info=True,
            )
            return None

    def artifacts_for_run(self, run_id: str) -> tuple[ArtifactRef, ...]:
        """The files ``run_id`` delivered and still has, by path, at most
        :data:`ARTIFACTS_MAX`."""
        return tuple(
            ArtifactRef(
                id=artifact.id,
                run_id=artifact.run_id,
                relpath=artifact.relpath,
                media_type=artifact.media_type,
                size=artifact.size,
            )
            for artifact in self.ctx.artifacts.for_run(run_id)
            if artifact.available
        )[:ARTIFACTS_MAX]

    def channel_for_item(self, item_id: str) -> str | None:
        try:
            with self.ctx.loop.dstore.read() as session:
                channel_id = channel_for_item(session, item_id)
                if channel_id is None:
                    return None
                channel = session.get(ChannelRow, channel_id)
                return None if channel is None or channel.state != "active" else channel_id
        except Exception:
            log.warning("api.channel_for_item_failed", item=item_id, exc_info=True)
            return None

    def _work(self, post: ChannelPost, turn_id: str | None) -> dict[str, Any] | None:
        """The work snapshot to show with the post.

        Until messages hold their own attachments, the snapshot is where a
        post's files are named, so a post keeps one whether or not a turn
        claims it. A run builds its own; one the channel could not show is
        replaced by what the platform knows about the work, because a
        snapshot no reader can parse costs every later read of the channel.
        """
        artifacts = _artifacts(post.artifacts)
        if post.work is not None:
            given = self._shown({**post.work, "turn_id": turn_id}, post, artifacts)
            if given is not None:
                return given
        from sbxloop.api.projections import Views

        item = self.ctx.loop.dstore.get(post.item_id)
        if item is None:
            return None
        public_item = Views(self.ctx).item(item)
        return self._shown(
            {
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
            },
            post,
            artifacts,
        )

    def _shown(
        self, snapshot: dict[str, Any], post: ChannelPost, artifacts: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """``snapshot`` with the post's files on it, if a channel can read
        it back; None, and a line saying so, if it cannot."""
        shown = {**snapshot, "artifacts": artifacts} if artifacts else snapshot
        try:
            ChannelWorkOut.model_validate(shown)
        except ValidationError as exc:
            log.warning(
                "api.channel_post_snapshot_dropped",
                channel=post.channel_id,
                key=post.dedupe_key,
                run=post.run_id,
                reason=str(exc),
            )
            return None
        return shown
