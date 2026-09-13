"""The public chronology: durable replay after a cursor, per run or for
the whole workspace, and the same cursor space streamed as server-sent
events."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from sbxloop.api.auth.deps import Authenticated, get_ctx, require, resolve_token
from sbxloop.api.chronology import event_id
from sbxloop.api.context import PAGE_DEFAULT, PAGE_MAX, ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import EventOut
from sbxloop.api.pagination import Page
from sbxloop.api.projections import Views, not_found
from sbxloop.api.replay import cursor_after, envelope_page, read_after

router = APIRouter(prefix="/v1", tags=["events"])

#: A comment line keeps the connection alive through proxies; it carries
#: no domain meaning and no id.
PING_EVERY_S = 15.0
#: How long a stream waits on the hub before checking the store anyway.
WAIT_S = 1.0
#: How often a live stream re-checks that its token still stands.
ACCESS_RECHECK_S = 60.0
STREAM_BATCH = 200


@router.get("/events", response_model=Page[EventOut])
async def list_events(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
    after: Annotated[str | None, Query()] = None,
    run_id: Annotated[str | None, Query()] = None,
    type_prefix: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
    latest: bool = False,
) -> Page[EventOut]:
    """Every public event after ``after`` (an event id, or omitted for
    the oldest held), oldest first, in the order it was recorded.
    ``latest`` instead returns the newest bounded snapshot, oldest first;
    it works after retention and cannot be combined with ``after``."""
    if latest and after is not None:
        raise Problem(400, "invalid_cursor", "latest cannot be combined with after")
    start = cursor_after(after)

    def read() -> Page[EventOut]:
        views = Views(ctx)
        internal_run = views.run_by_public_id(run_id).run_id if run_id else None
        if latest:
            ctx.chronology.project(views.now)
            rows = ctx.chronology.read(
                run_id=internal_run, type_prefix=type_prefix, limit=limit, newest_first=True
            )
            return envelope_page(views, list(reversed(rows)), more=False)
        return read_after(views, start, run_id=internal_run, type_prefix=type_prefix, limit=limit)

    return await ctx.call(read)


@router.get("/runs/{run_id}/events", response_model=Page[EventOut])
async def run_events(
    run_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
    after: Annotated[str | None, Query()] = None,
    type_prefix: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
) -> Page[EventOut]:
    """The run's chronology after a cursor: its own engine events, and the
    daemon's notices and transitions that name it."""
    start = cursor_after(after)

    def read() -> Page[EventOut]:
        views = Views(ctx)
        record = views.run_by_public_id(run_id)
        return read_after(views, start, run_id=record.run_id, type_prefix=type_prefix, limit=limit)

    return await ctx.call(read)


def _frame(event: EventOut) -> str:
    body = json.dumps(event.model_dump(mode="json"), separators=(",", ":"), default=str)
    return f"id: {event.id}\nevent: {event.type}\ndata: {body}\n\n"


async def sse_frames(
    ctx: ApiContext,
    auth: Authenticated,
    *,
    after: int,
    run_id: str | None,
    type_prefix: str | None,
) -> AsyncIterator[str]:
    """The stream body: events after the cursor as they land, a comment
    ping while nothing does, and the stream ends when the daemon stops or
    the token no longer stands."""
    cursor = after
    token = auth.token
    last_check = ctx.clock()
    last_ping = asyncio.get_running_loop().time()
    while not ctx.stopping.is_set():

        def read(start: int = cursor) -> Page[EventOut]:
            views = Views(ctx)
            return read_after(
                views, start, run_id=run_id, type_prefix=type_prefix, limit=STREAM_BATCH
            )

        page = await ctx.call(read)
        for event in page.data:
            yield _frame(event)
            cursor = int(event.id.removeprefix("evt_"))
            last_ping = asyncio.get_running_loop().time()
        if page.has_more:
            continue
        now = ctx.clock()
        if now - last_check >= ACCESS_RECHECK_S:
            last_check = now
            try:
                await ctx.call(resolve_token, ctx, token)
            except Problem:
                yield 'event: stream.closed\ndata: {"reason":"access_revoked"}\n\n'
                return
        if asyncio.get_running_loop().time() - last_ping >= PING_EVERY_S:
            last_ping = asyncio.get_running_loop().time()
            yield ": ping\n\n"
        await ctx.hub.wait(min(WAIT_S, PING_EVERY_S))
    yield 'event: stream.closed\ndata: {"reason":"daemon_stopping"}\n\n'


@router.get("/events/stream")
async def stream_events(
    request: Request,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
    after: Annotated[str | None, Query()] = None,
    run_id: Annotated[str | None, Query()] = None,
    type_prefix: Annotated[str | None, Query(max_length=64)] = None,
) -> StreamingResponse:
    """Server-sent events from the same durable cursor space. Resume with
    ``Last-Event-ID`` (or ``after``); each frame's ``id`` is the cursor
    to resume from. Pings are comments. The stream closes when the daemon
    stops or the token is revoked; reconnect with the last id seen."""
    last = request.headers.get("last-event-id") or after
    start = cursor_after(last)

    def prepare() -> tuple[int, str | None]:
        views = Views(ctx)
        internal_run = views.run_by_public_id(run_id).run_id if run_id else None
        # An expired cursor is refused before the stream opens.
        _resolve_start(views, start)
        return start, internal_run

    begin, internal_run = await ctx.call(prepare)
    if not ctx.hub.admit(int(ctx.api.max_stream_clients)):
        raise Problem(
            503,
            "too_many_streams",
            f"this listener serves at most {ctx.api.max_stream_clients} streams at once",
            headers={"Retry-After": "5"},
        )

    async def body() -> AsyncIterator[str]:
        try:
            async for frame in sse_frames(
                ctx, auth, after=begin, run_id=internal_run, type_prefix=type_prefix
            ):
                yield frame
        finally:
            ctx.hub.leave()

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _resolve_start(views: Views, start: int) -> None:
    views.ctx.chronology.project(views.now)
    if views.ctx.chronology.expired(start):
        raise Problem(
            410,
            "cursor_expired",
            f"events after {event_id(start)} were pruned; read a fresh snapshot and "
            "subscribe from its watermark",
            snapshot="/v1/status",
        )


__all__ = ["not_found", "router", "sse_frames"]
