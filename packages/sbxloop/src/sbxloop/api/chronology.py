"""The public chronology: one durable cursor space a client replays from.

``api_events`` holds every public event in ``seq`` order — the operation
transitions the operation store writes, the daemon's own notices and run
lifecycle (written by :class:`~sbxloop.api.frontend.ApiFrontend`), and a
*projection* of the engine's ``events`` table: a thin row per engine event
carrying ``source_seq``, so the engine's chronology is referenced, never
copied, and delivery joins back to it. The projection's high-water mark
lives in ``daemon_state`` and moves in the same transaction as the rows it
covers: a crash between the two leaves nothing half-projected, and a
re-run copies each engine event exactly once.

Retention prunes rows older than ``[api] replay_retention_s``; a cursor
that points below what remains is ``cursor_expired``, never silently
skipped past.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import ColumnElement, and_, delete, func, insert, or_, select

from sbxloop.api.channel_access import MANAGING_ROLES, ChannelAccess
from sbxloop.daemon.controls.steering import SteeringStore
from sbxloop.daemon.store import DaemonStore
from sbxloop.db.api_models import ApiEventRow
from sbxloop.db.collaboration_models import ChannelRow
from sbxloop.db.daemon_models import DaemonStateRow
from sbxloop.db.engine_models import EventRow
from sbxloop.db.event_scope import channel_for_run, event_channel
from sbxloop.events import HostEventTypes
from sbxloop.log import get_logger

if TYPE_CHECKING:
    from sbxloop.api.collaboration import Member

log = get_logger(__name__)

WATERMARK_KEY = "api.projection.watermark"
CHAT_REPLY = HostEventTypes.CHAT_REPLY
PRUNED_KEY = "api.projection.pruned_to"
#: The actor every daemon-originated public event carries: truthful, and
#: distinct from a person or a client.
DAEMON_ACTOR: dict[str, Any] = {
    "kind": "system",
    "id": "daemon",
    "display": "daemon",
    "via": "daemon",
}


@dataclass(frozen=True, slots=True)
class PublicEvent:
    seq: int
    recorded_at: float
    occurred_at: float
    type: str
    run_id: str | None
    item_id: str | None
    operation_id: str | None
    actor: dict[str, Any] | None
    source_seq: int | None
    data: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    @property
    def id(self) -> str:
        return event_id(self.seq)


def event_id(seq: int) -> str:
    return f"evt_{seq}"


def parse_event_id(value: str) -> int | None:
    """The sequence an event id names, or ``None`` for anything else."""
    if not value.startswith("evt_"):
        return None
    tail = value[4:]
    if not tail.isdigit():
        return None
    return int(tail)


def _int_state(session: Any, key: str) -> int:
    value = session.scalars(select(DaemonStateRow.value).where(DaemonStateRow.key == key)).first()
    try:
        return int(value) if value is not None else 0
    except ValueError:
        return 0


def visibility(viewer: Member | None) -> list[ColumnElement[bool]]:
    """SQL conditions on :class:`ApiEventRow` for what ``viewer`` may see.

    No member (a plain client, the daemon): everything. Anyone else sees
    events for everyone or for them alone. A workspace owner or admin sees
    every channel's and every run's events; a member sees the events of the
    channels they can open, and a run's events only when a channel they can
    open asked for the run (``channel_id`` is set when it is recorded).
    """
    if viewer is None:
        return []
    conditions: list[ColumnElement[bool]] = [
        or_(
            ApiEventRow.audience_user_id.is_(None),
            ApiEventRow.audience_user_id == viewer.user.id,
        )
    ]
    if viewer.role in MANAGING_ROLES:
        return conditions
    visible = ChannelAccess.visible_condition(viewer)
    assert visible is not None  # nosec B101 - a member always narrows
    conditions.append(
        or_(
            ApiEventRow.channel_id.in_(select(ChannelRow.id).where(visible)),
            and_(ApiEventRow.channel_id.is_(None), ApiEventRow.run_id.is_(None)),
        )
    )
    return conditions


def _set_state(session: Any, key: str, value: int) -> None:
    session.execute(
        insert(DaemonStateRow).prefix_with("OR REPLACE").values(key=key, value=str(value))
    )


class Chronology:
    """Reads and writes of ``api_events`` over the daemon's store."""

    #: Engine events copied per transaction.
    BATCH = 500

    def __init__(self, dstore: DaemonStore) -> None:
        self.dstore = dstore
        #: A fault seam: called inside the projecting transaction after the
        #: rows are inserted and before the watermark moves. A test raises
        #: here to prove the two commit together.
        self.after_copy: Callable[[int], None] = lambda seq: None
        #: A fault seam after the source rows are read and before they are
        #: copied. A test commits a concurrent engine event here to exercise
        #: SQLite's deferred-transaction read-to-write upgrade race.
        self.after_read: Callable[[], None] = lambda: None
        #: Runs whose asking channel is known; a run never changes channel.
        #: Only found channels are kept: a run's item may be linked later.
        self._run_channels: dict[str, str] = {}

    # -- projection ------------------------------------------------------------

    def project(self, now: float) -> int:
        """Copy every engine event past the watermark into the chronology,
        in batches, each batch and its watermark one transaction. Returns
        how many rows were projected."""
        total = 0
        while True:
            copied = self._project_batch(now)
            total += copied
            if copied < self.BATCH:
                return total

    def _project_batch(self, now: float) -> int:
        # Reserve the write lock before reading the source rows. The engine
        # store writes through another connection; if it committed between a
        # deferred read and this batch's INSERT, WAL would reject the stale
        # snapshot's read-to-write upgrade with SQLITE_BUSY.
        with self.dstore.immediate_transaction() as session:
            watermark = _int_state(session, WATERMARK_KEY)
            rows = session.execute(
                select(
                    EventRow.seq, EventRow.ts, EventRow.run_id, EventRow.type, EventRow.data_json
                )
                .where(EventRow.seq > watermark)
                .order_by(EventRow.seq.asc())
                .limit(self.BATCH)
            ).all()
            if not rows:
                return 0
            self.after_read()
            channels = {
                str(run_id): self._run_channel(session, str(run_id))
                for run_id in {row[2] for row in rows}
            }
            session.execute(
                insert(ApiEventRow).values(
                    [
                        {
                            "recorded_at": now,
                            "occurred_at": float(ts),
                            "type": str(type_),
                            "run_id": str(run_id),
                            "item_id": None,
                            "operation_id": None,
                            "actor_json": None,
                            "source_seq": int(seq),
                            "data_json": None,
                            "channel_id": channels.get(str(run_id)),
                        }
                        for seq, ts, run_id, type_, _data in rows
                    ]
                )
            )
            # A steering instruction is answered by the run's `chat.reply`:
            # its record settles in the same transaction as the event, so
            # a reader never sees the reply without the receipt or the
            # receipt without the reply.
            for _seq, _ts, _run, type_, data_json in rows:
                if str(type_) != CHAT_REPLY:
                    continue
                data = json.loads(data_json) if data_json else {}
                message_id = data.get("message_id")
                if message_id:
                    SteeringStore.settle_reply(
                        session,
                        message_id=str(message_id),
                        now=now,
                        reply=data.get("reply"),
                        action=data.get("action"),
                        error=data.get("error"),
                    )
            last = int(rows[-1][0])
            self.after_copy(last)
            _set_state(session, WATERMARK_KEY, last)
            return len(rows)

    def _run_channel(self, session: Any, run_id: str) -> str | None:
        channel_id = self._run_channels.get(run_id)
        if channel_id is None:
            channel_id = channel_for_run(session, run_id)
            if channel_id is not None:
                self._run_channels[run_id] = channel_id
        return channel_id

    def lag(self) -> int:
        """Engine events not yet in the chronology."""
        with self.dstore.read() as session:
            newest = session.scalar(select(func.max(EventRow.seq)))
            watermark = _int_state(session, WATERMARK_KEY)
        return max(0, int(newest or 0) - watermark)

    # -- writes ----------------------------------------------------------------

    def record(
        self,
        type_: str,
        now: float,
        *,
        run_id: str | None = None,
        item_id: str | None = None,
        operation_id: str | None = None,
        actor: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        occurred_at: float | None = None,
        audience_user_id: str | None = None,
    ) -> int:
        """Append one daemon-originated event; returns its ``seq``. It
        belongs to the channel that asked for its run or item, if any."""
        with self.dstore.transaction() as session:
            channel_id = (
                self._run_channel(session, run_id)
                if run_id
                else event_channel(session, type_, run_id=None, item_id=item_id, data=data)
            )
            result = session.execute(
                insert(ApiEventRow).values(
                    recorded_at=now,
                    occurred_at=now if occurred_at is None else occurred_at,
                    type=type_,
                    run_id=run_id,
                    item_id=item_id,
                    operation_id=operation_id,
                    actor_json=None if actor is None else json.dumps(actor, default=str),
                    source_seq=None,
                    data_json=json.dumps(data or {}, default=str),
                    channel_id=channel_id,
                    audience_user_id=audience_user_id,
                )
            )
            keys = getattr(result, "inserted_primary_key", None)
            return int(keys[0]) if keys else 0

    # -- reads -----------------------------------------------------------------

    def watermark(self) -> int | None:
        """The newest public event's ``seq`` — what a snapshot reports so a
        client subscribes from it without a gap."""
        with self.dstore.read() as session:
            newest = session.scalar(select(func.max(ApiEventRow.seq)))
        return None if newest is None else int(newest)

    def bounds(self) -> tuple[int | None, int]:
        """``(oldest seq still held, highest seq ever pruned)``."""
        with self.dstore.read() as session:
            oldest = session.scalar(select(func.min(ApiEventRow.seq)))
            pruned = _int_state(session, PRUNED_KEY)
        return (None if oldest is None else int(oldest)), pruned

    def expired(self, after: int) -> bool:
        """Whether a cursor points into pruned history: the event after it
        is gone, so replaying from it would skip what a client never saw."""
        if after <= 0:
            _, pruned = self.bounds()
            return pruned > 0
        _, pruned = self.bounds()
        return after < pruned

    def read(
        self,
        *,
        after: int = 0,
        run_id: str | None = None,
        type_prefix: str | None = None,
        limit: int = 100,
        newest_first: bool = False,
        viewer: Member | None = None,
        channel_id: str | None = None,
    ) -> list[PublicEvent]:
        """Events after a cursor, oldest first; an engine event's data is
        joined from the engine's own row. ``viewer`` narrows to what that
        member may see, in the query, so a page is never short."""
        stmt = (
            select(ApiEventRow, EventRow.data_json, EventRow.job_id)
            .outerjoin(EventRow, EventRow.seq == ApiEventRow.source_seq)
            .where(ApiEventRow.seq > after)
            .order_by(ApiEventRow.seq.desc() if newest_first else ApiEventRow.seq.asc())
            .limit(limit)
        )
        if run_id is not None:
            stmt = stmt.where(ApiEventRow.run_id == run_id)
        if type_prefix:
            stmt = stmt.where(ApiEventRow.type.like(type_prefix.replace("%", "") + "%"))
        if channel_id is not None:
            stmt = stmt.where(ApiEventRow.channel_id == channel_id)
        stmt = stmt.where(*visibility(viewer))
        out: list[PublicEvent] = []
        with self.dstore.read() as session:
            for row, source_data, job_id in session.execute(stmt):
                if row.source_seq is not None:
                    data = json.loads(source_data) if source_data else {}
                    if job_id:
                        data = {"job_id": job_id, **data}
                else:
                    data = json.loads(row.data_json) if row.data_json else {}
                out.append(
                    PublicEvent(
                        seq=int(row.seq),
                        recorded_at=float(row.recorded_at),
                        occurred_at=float(row.occurred_at),
                        type=str(row.type),
                        run_id=row.run_id,
                        item_id=row.item_id,
                        operation_id=row.operation_id,
                        actor=None if row.actor_json is None else json.loads(row.actor_json),
                        source_seq=None if row.source_seq is None else int(row.source_seq),
                        data=data,
                        schema_version=int(row.schema_version),
                    )
                )
        return out

    # -- retention -------------------------------------------------------------

    def prune(self, before: float) -> int:
        """Drop public events recorded before ``before``; remembers the
        highest ``seq`` dropped so a cursor below it is refused as expired
        rather than replayed with a hole."""
        with self.dstore.transaction() as session:
            newest_gone = session.scalar(
                select(func.max(ApiEventRow.seq)).where(ApiEventRow.recorded_at < before)
            )
            if newest_gone is None:
                return 0
            result = session.execute(delete(ApiEventRow).where(ApiEventRow.recorded_at < before))
            pruned = max(_int_state(session, PRUNED_KEY), int(newest_gone))
            _set_state(session, PRUNED_KEY, pruned)
            count = getattr(result, "rowcount", 0)
        log.info("api.chronology_pruned", rows=count, pruned_to=pruned)
        return int(count or 0)
