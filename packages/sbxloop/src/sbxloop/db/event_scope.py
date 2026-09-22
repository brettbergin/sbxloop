"""Which channel a run or a work item belongs to.

Admission records the channel on the item itself when it knows one; a chat
turn that asks for a workload or tool run records the asking message's id
as the item's ``source_key``; a turn that files an issue for a code run
records the repository and issue on its participant. The same
exact identities :mod:`sbxloop.api.work_delivery` delivers results by
decide which channel's members may see the run's events. External work can
also have a durable presentation association without changing admission.
Nothing is inferred from prose, and a run's recorded association stays
with that attempt even when a later admission names another channel: a
turn that labels an issue again grants its channel the runs that follow,
never the runs that already existed.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from sbxloop.db.collaboration_models import MessageRow, TurnRow
from sbxloop.db.daemon_models import DaemonRunRow, WorkItemRow
from sbxloop.db.job_scope import presentation_channel_for_item, presentation_channel_for_run
from sbxloop.ghids import chat_source_message_id


def admission_channel_for_item(session: Any, item_id: str) -> str | None:
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
    asking = chat_source_message_id(str(item.run_kind), source_key)
    if asking is not None:
        message: MessageRow | None = session.get(MessageRow, asking)
        if message is None or message.role != "user":
            return None
        return str(message.channel_id)
    if item.run_kind != "code":
        return None
    filed = _filing_turn(session, item, before=float(item.created_at))
    return None if filed is None else filed[0]


def channel_for_item(session: Any, item_id: str) -> str | None:
    """The conversation for an item's admission or durable presentation."""
    return admission_channel_for_item(session, item_id) or presentation_channel_for_item(
        session, item_id
    )


def _filing_turns(session: Any, item: WorkItemRow) -> list[tuple[str, str, float]]:
    """Every turn with a participant that filed or labelled this code
    item's issue, by repository and source key alike, oldest first: the
    channel, the turn and when the turn was created."""
    source_key = str(item.source_key)
    fragment = json.dumps({"repo": item.repo, "source_key": source_key})[1:-1]
    rows = session.execute(
        select(TurnRow.channel_id, TurnRow.id, TurnRow.created_at, TurnRow.participants_json)
        .where(TurnRow.participants_json.contains(fragment, autoescape=True))
        .order_by(TurnRow.created_at.asc())
    ).all()
    turns: list[tuple[str, str, float]] = []
    for channel_id, turn_id, created_at, participants_json in rows:
        if any(
            ref.get("repo") == item.repo and ref.get("source_key") == source_key
            for participant in json.loads(participants_json or "[]")
            for ref in participant.get("code_work", [])
        ):
            turns.append((str(channel_id), str(turn_id), float(created_at)))
    return turns


def _filing_turn(
    session: Any, item: WorkItemRow, *, before: float | None = None
) -> tuple[str, str] | None:
    """The channel and turn of the first participant that filed this code
    item's issue, at or before ``before`` when given.

    The turn that asks for an item precedes the poll that creates it, so
    bounding the search by the item's creation keeps a turn that labels
    the same issue again later from claiming an item that already
    existed.
    """
    for channel_id, turn_id, created_at in _filing_turns(session, item):
        if before is None or created_at <= before:
            return channel_id, turn_id
    return None


def _code_run_channel(session: Any, item: WorkItemRow, started_at: float) -> str | None:
    """The channel that asked for one attempt of a code item.

    One item row serves every attempt of its issue, and a later label from
    another channel re-queues that row with the later channel's admission.
    An attempt answers to the ask that preceded it: the item's channel when
    a turn of that channel had asked for the issue by the time the attempt
    started, else the first channel whose turn had. A channel the item
    names that no turn of it ever asked for is kept as recorded, there
    being nothing to date it against.
    """
    turns = _filing_turns(session, item)
    asked_before = [channel for channel, _turn, created_at in turns if created_at <= started_at]
    channel_id = str(item.channel_id) if item.channel_id else None
    if channel_id is not None and (
        channel_id in asked_before or all(channel != channel_id for channel, _, _ in turns)
    ):
        return channel_id
    return asked_before[0] if asked_before else None


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
    """The immutable attempt binding, else the channel that had asked for
    the item when the attempt started, else the item's association."""
    bound = presentation_channel_for_run(session, run_id)
    if bound is not None:
        return bound
    ledger = session.execute(
        select(DaemonRunRow.item_id, DaemonRunRow.started_at).where(DaemonRunRow.run_id == run_id)
    ).first()
    if ledger is not None:
        item_id = str(ledger.item_id)
        item: WorkItemRow | None = session.get(WorkItemRow, item_id)
        if item is not None and item.run_kind == "code":
            return _code_run_channel(
                session, item, float(ledger.started_at)
            ) or presentation_channel_for_item(session, item_id)
        return channel_for_item(session, item_id)
    found = session.scalar(
        select(WorkItemRow.item_id).where(
            (WorkItemRow.run_id == run_id) | (WorkItemRow.prior_run_id == run_id)
        )
    )
    return None if found is None else channel_for_item(session, str(found))


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


__all__ = [
    "admission_channel_for_item",
    "channel_for_item",
    "channel_for_run",
    "event_channel",
    "turn_for_item",
]
