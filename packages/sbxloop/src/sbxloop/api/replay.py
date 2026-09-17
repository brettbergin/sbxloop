"""Reading the public chronology into its public shape, for the page
routes, the SSE stream and the WebSocket alike."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sbxloop.api.chronology import PublicEvent, event_id, parse_event_id
from sbxloop.api.errors import Problem
from sbxloop.api.models import Actor, EventOut, rfc3339
from sbxloop.api.pagination import Page
from sbxloop.api.projections import Views
from sbxloop.api.publicids import item_key, run_public_id

if TYPE_CHECKING:
    from sbxloop.api.collaboration import Member


def cursor_after(value: str | None) -> int:
    """The sequence a cursor names; ``0`` for none."""
    if value is None or value == "":
        return 0
    seq = parse_event_id(value)
    if seq is None:
        raise Problem(400, "invalid_cursor", "the cursor is not an event id")
    return seq


def envelope_page(views: Views, events: list[PublicEvent], *, more: bool) -> Page[EventOut]:
    """Public envelopes for a batch, item ids mapped in one write."""
    wanted = {e.item_id for e in events if e.item_id}
    items = {}
    for item_id in wanted:
        stored = views.dstore.get(item_id)
        if stored is not None:
            items[item_id] = stored
    public = views.ids.item_ids(list(items.values()), views.now) if items else {}
    data = [
        EventOut(
            id=e.id,
            schema_version=e.schema_version,
            type=e.type,
            occurred_at=rfc3339(e.occurred_at) or "",
            recorded_at=rfc3339(e.recorded_at) or "",
            run_id=run_public_id(e.run_id) if e.run_id else None,
            item_id=(
                public.get(item_key(items[e.item_id])) if e.item_id and e.item_id in items else None
            ),
            operation_id=e.operation_id,
            actor=(
                Actor(
                    kind=str(e.actor.get("kind", "system")),
                    id=str(e.actor.get("id", "")),
                    display=e.actor.get("display"),
                    via=str(e.actor.get("via", "")),
                )
                if e.actor
                else None
            ),
            native_seq=e.source_seq,
            data=e.data,
        )
        for e in events
    ]
    return Page(data=data, next_cursor=data[-1].id if more and data else None, has_more=more)


def read_after(
    views: Views,
    after: int,
    *,
    run_id: str | None,
    type_prefix: str | None,
    limit: int,
    viewer: Member | None = None,
    channel_id: str | None = None,
) -> Page[EventOut]:
    """Project what the engine wrote since, refuse a pruned cursor, and
    return the next page of what ``viewer`` may see."""
    chronology = views.ctx.chronology
    chronology.project(views.now)
    if chronology.expired(after):
        raise Problem(
            410,
            "cursor_expired",
            f"events after {event_id(after)} were pruned; read a fresh snapshot and "
            "subscribe from its watermark",
            snapshot="/v1/status",
        )
    rows = chronology.read(
        after=after,
        run_id=run_id,
        type_prefix=type_prefix,
        limit=limit + 1,
        viewer=viewer,
        channel_id=channel_id,
    )
    more = len(rows) > limit
    return envelope_page(views, rows[:limit], more=more)


def follow(
    views: Views,
    after: int,
    *,
    run_id: str | None,
    type_prefix: str | None,
    limit: int,
    viewer: Member | None,
    channel_id: str | None = None,
) -> tuple[Page[EventOut], int]:
    """A live subscription's next page and the cursor to resume from.

    The cursor moves past the last event sent and, once nothing more
    matches, past every event already recorded when the read began: rows
    the subscriber may not see (or did not ask for) are scanned once, not
    on every wake-up. Sequences commit in order, so nothing at or below
    that mark can still appear.
    """
    recorded = views.ctx.chronology.watermark() or 0
    page = read_after(
        views,
        after,
        run_id=run_id,
        type_prefix=type_prefix,
        limit=limit,
        viewer=viewer,
        channel_id=channel_id,
    )
    cursor = int(page.data[-1].id.removeprefix("evt_")) if page.data else after
    if not page.has_more:
        cursor = max(cursor, recorded)
    return page, cursor
