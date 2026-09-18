"""Who may read, post to and manage a channel.

A channel is either ``private`` (its channel members only) or ``workspace``
(every member of its workspace). The rules, applied inside the caller's
transaction:

- A channel the member may not see does not exist for them: the same
  ``channel_not_found`` an unknown id gets, so its existence never leaks.
  A private channel is seen by its channel members only, whatever their
  workspace role.
- ``read``: any member who can see the channel.
- ``post``: the same; a workspace member posting to a workspace channel they
  have not joined becomes a channel member.
- ``manage`` (rename, change visibility, delete, add or remove others):
  the channel's owner, or a workspace owner or admin who can see it;
  anyone else who can see it is refused with ``channel_forbidden``.
- No member (``None``): a plain API client or the daemon itself, with the
  full access such callers always had.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import ColumnElement, insert, or_, select

from sbxloop.db.collaboration_models import ChannelMemberRow, ChannelRow

if TYPE_CHECKING:
    from sbxloop.api.collaboration import Member

Need = Literal["read", "post", "manage"]
ChannelRole = Literal["owner", "member"]

#: Workspace roles that manage every channel they can see.
MANAGING_ROLES = frozenset({"owner", "admin"})


def _not_found() -> Exception:
    # Imported here: the collaboration store imports this module.
    from sbxloop.api.collaboration import CollaborationError

    return CollaborationError("channel_not_found", "channel not found")


def _forbidden(need: Need) -> Exception:
    from sbxloop.api.collaboration import CollaborationError

    return CollaborationError("channel_forbidden", f"you may not {need} this channel")


class ChannelAccess:
    """The channel authorization rules; stateless."""

    @staticmethod
    def role(session: Any, channel_id: str, user_id: str) -> ChannelRole | None:
        """The user's role in the channel, or ``None`` when not a member."""
        row = session.get(ChannelMemberRow, (channel_id, user_id))
        if row is None:
            return None
        return "owner" if row.role == "owner" else "member"

    @staticmethod
    def visible_condition(member: Member | None) -> ColumnElement[bool] | None:
        """A SQL condition on :class:`ChannelRow` selecting the channels
        ``member`` can see; ``None`` when every channel is visible."""
        if member is None:
            return None
        joined = select(ChannelMemberRow.channel_id).where(
            ChannelMemberRow.user_id == member.user.id
        )
        return (ChannelRow.workspace_id == member.workspace_id) & or_(
            ChannelRow.visibility == "workspace", ChannelRow.id.in_(joined)
        )

    @staticmethod
    def check(
        session: Any,
        channel: ChannelRow | None,
        member: Member | None,
        need: Need,
        *,
        now: float | None = None,
    ) -> None:
        """Raise unless ``member`` may ``need`` the channel. ``post`` by a
        workspace member who has not joined a workspace channel joins them."""
        if channel is None:
            raise _not_found()
        if member is None:
            return
        role = ChannelAccess.role(session, str(channel.id), member.user.id)
        visible = channel.workspace_id == member.workspace_id and (
            role is not None or channel.visibility == "workspace"
        )
        if not visible:
            raise _not_found()
        if need == "manage":
            if role != "owner" and member.role not in MANAGING_ROLES:
                raise _forbidden(need)
            return
        if need == "post" and role is None:
            ChannelAccess.join(session, str(channel.id), member.user.id, member.user.id, now)

    @staticmethod
    def join(
        session: Any,
        channel_id: str,
        user_id: str,
        added_by: str | None,
        now: float | None,
        role: ChannelRole = "member",
    ) -> None:
        """Add a channel member and record ``collaboration.member.added``."""
        from sbxloop.api.collaboration import _event

        at = time.time() if now is None else now
        session.execute(
            insert(ChannelMemberRow).values(
                channel_id=channel_id,
                user_id=user_id,
                role=role,
                added_by=added_by,
                joined_at=at,
            )
        )
        _event(
            session,
            "collaboration.member.added",
            at,
            data={"channel_id": channel_id, "user_id": user_id},
        )


__all__ = ["MANAGING_ROLES", "ChannelAccess", "ChannelRole", "Need"]
