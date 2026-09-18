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
    filed = _filing_turn(session, item)
    return None if filed is None else filed[0]


def _filing_turn(session: Any, item: WorkItemRow) -> tuple[str, str] | None:
    """The channel and turn of the first participant that filed this code
    item's issue, by repository and source key alike."""
    source_key = str(item.source_key)
    fragment = json.dumps({"repo": item.repo, "source_key": source_key})[1:-1]
    rows = session.execute(
        select(TurnRow.channel_id, TurnRow.id, TurnRow.participants_json)
        .where(TurnRow.participants_json.contains(fragment, autoescape=True))
        .order_by(TurnRow.created_at.asc())
    ).all()
    for channel_id, turn_id, participants_json in rows:
        for participant in json.loads(participants_json or "[]"):
            for ref in participant.get("code_work", []):
                if ref.get("repo") == item.repo and ref.get("source_key") == source_key:
                    return str(channel_id), str(turn_id)
    return None


def turn_for_item(session: Any, item_id: str, channel_id: str) -> str | None:
    """The turn in ``channel_id`` that asked for the item.

    The same exact identities :mod:`sbxloop.api.work_delivery` delivers a
    result by, so a run's commentary and its delivery land on one turn:
    the turn whose input message the item's source key names, the turn that
    filed a code item's issue, and, for an item admitted with this channel
    but naming no turn of it, the turn the channel was on when the item was
    admitted. A channel that had no turn to ask has none to hang the post
    on.
    """
    item: WorkItemRow | None = session.get(WorkItemRow, item_id)
    if item is None:
        return None
    asking = session.scalars(
        select(TurnRow)
        .where(
            TurnRow.input_message_id == str(item.source_key).split(":", 1)[0],
            TurnRow.channel_id == channel_id,
        )
        .limit(1)
    ).first()
    if asking is not None:
        return str(asking.id)
    if item.run_kind == "code":
        filed = _filing_turn(session, item)
        if filed is not None and filed[0] == channel_id:
            return filed[1]
    if str(item.channel_id or "") != channel_id:
        return None
    turns = select(TurnRow).where(TurnRow.channel_id == channel_id)
    turn = (
        session.scalars(
            turns.where(TurnRow.created_at <= item.created_at)
            .order_by(TurnRow.created_at.desc(), TurnRow.id.desc())
            .limit(1)
        ).first()
        or session.scalars(
            turns.order_by(TurnRow.created_at.asc(), TurnRow.id.asc()).limit(1)
        ).first()
    )
    return None if turn is None else str(turn.id)


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


__all__ = ["channel_for_item", "channel_for_run", "event_channel", "turn_for_item"]
