"""Which channel a run or a work item belongs to.

Admission records the channel on the item itself when it knows one; a chat
turn that asks for a workload or tool run records the asking message's id
as the item's ``source_key``; a turn that files an issue for a code run
records the repository and issue on its participant. The same
exact identities :mod:`sbxloop.api.work_delivery` delivers results by
decide which channel's members may see the run's events. Nothing is
inferred from prose: a run no channel asked for belongs to none.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from sbxloop.db.collaboration_models import MessageRow, TurnRow
from sbxloop.db.daemon_models import DaemonRunRow, WorkItemRow

#: Run kinds whose item's source key is the asking chat message's id.
_CHAT_KINDS = frozenset({"workload", "tool"})


def channel_for_item(session: Any, item_id: str) -> str | None:
    """The channel that asked for the item, if a channel did."""
    item: WorkItemRow | None = session.get(WorkItemRow, item_id)
    if item is None:
        return None
    if item.channel_id:
        # Admission named the channel the work answers to (revision 0027):
        # an exact identity, and the only one a run nobody asked for in a
        # message has.
        return str(item.channel_id)
    source_key = str(item.source_key)
    if item.run_kind in _CHAT_KINDS:
        message: MessageRow | None = session.get(MessageRow, source_key.split(":", 1)[0])
        if message is None or message.role != "user":
            return None
        return str(message.channel_id)
    if item.run_kind != "code":
        return None
    fragment = json.dumps({"repo": item.repo, "source_key": source_key})[1:-1]
    rows = session.execute(
        select(TurnRow.channel_id, TurnRow.participants_json)
        .where(TurnRow.participants_json.contains(fragment, autoescape=True))
        .order_by(TurnRow.created_at.asc())
    ).all()
    for channel_id, participants_json in rows:
        for participant in json.loads(participants_json or "[]"):
            for ref in participant.get("code_work", []):
                if ref.get("repo") == item.repo and ref.get("source_key") == source_key:
                    return str(channel_id)
    return None


def channel_for_run(session: Any, run_id: str) -> str | None:
    """The channel that asked for the run's item, if a channel did."""
    item_id = session.scalar(select(DaemonRunRow.item_id).where(DaemonRunRow.run_id == run_id))
    if item_id is None:
        item_id = session.scalar(
            select(WorkItemRow.item_id).where(
                (WorkItemRow.run_id == run_id) | (WorkItemRow.prior_run_id == run_id)
            )
        )
    return None if item_id is None else channel_for_item(session, str(item_id))


def event_channel(
    session: Any,
    type_: str,
    *,
    run_id: str | None,
    item_id: str | None,
    data: dict[str, Any] | None,
) -> str | None:
    """The channel an event about to be recorded belongs to."""
    if type_.startswith("collaboration.") and data:
        value = data.get("channel_id")
        if isinstance(value, str) and value:
            return value
    if run_id:
        return channel_for_run(session, run_id)
    if item_id:
        return channel_for_item(session, item_id)
    return None


__all__ = ["channel_for_item", "channel_for_run", "event_channel"]
