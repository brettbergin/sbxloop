"""Durably project every daemon-known job into a conversation.

Execution admission stays untouched. A bounded import and a database-backed dirty
queue recover missed notifications without depending on a browser or rescanning
the whole execution history each second.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, func, or_, select, update

from sbxloop.api.chronology import DAEMON_ACTOR
from sbxloop.api.collaboration import CollaborationStore, _event
from sbxloop.api.models import rfc3339
from sbxloop.api.projections import Views
from sbxloop.api.publicids import run_public_id
from sbxloop.api.work_delivery import _artifacts, _result, _with_files, project_work
from sbxloop.daemon.controls.principal import WORKSPACE_ID
from sbxloop.db.api_models import ApiEventRow
from sbxloop.db.collaboration_models import ChannelRow, ChannelRunPostRow, MessageRow
from sbxloop.db.daemon_models import DaemonRunRow, DaemonStateRow, RunResumeRow, WorkItemRow
from sbxloop.db.engine_models import EventRow, Run
from sbxloop.db.event_scope import admission_channel_for_item, turn_for_item
from sbxloop.db.job_models import (
    ExternalItemRow,
    ExternalJobRow,
    ExternalPendingRow,
    ExternalRunRow,
)
from sbxloop.ghids import is_schedule_id, try_parse_gh_id

BATCH = 100
EVENT_BATCH = 250
HISTORY_SECONDS = 30 * 24 * 60 * 60
PREFIX = "api.external_work."
SYSTEM_OWNER = "system:daemon"
ITEM_TERMINAL = frozenset({"done", "failed", "cancelled"})
IMPORT_TERMINAL = frozenset({"merged", "completed", "failed", "cancelled"})
ATTENTION_STATES = {
    "done": "work",
    "merged": "work",
    "completed": "work",
    "failed": "failure",
    "cancelled": "failure",
    "blocked": "action_required",
    "gated": "action_required",
    "held": "action_required",
    "awaiting_review": "action_required",
    "paused_review": "action_required",
}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def _state(session: Any, key: str, default: str = "") -> str:
    row = session.get(DaemonStateRow, PREFIX + key)
    return default if row is None or row.value is None else str(row.value)


def _set_state(session: Any, key: str, value: str) -> None:
    row = session.get(DaemonStateRow, PREFIX + key)
    if row is None:
        session.add(DaemonStateRow(key=PREFIX + key, value=value))
    else:
        row.value = value


def _item_identity(ctx: Any, item: WorkItemRow) -> tuple[str, dict[str, Any]]:
    parsed = try_parse_gh_id(item.item_id)
    prefix = item.item_id.split(":", 1)[0]
    kind = "schedule" if is_schedule_id(item.item_id) else parsed.kind if parsed else prefix
    repo = item.repo or (parsed.repo if parsed is not None else None)
    source = {"kind": kind, "repository": repo, "url": item.url or None}
    if kind in {"issue", "pr"} and parsed is not None:
        host = urlsplit(item.url).netloc.casefold()
        if not host:
            host = urlsplit(ctx.config.forge_web_url(repo)).netloc.casefold()
        identity = [
            WORKSPACE_ID,
            parsed.kind,
            parsed.forge,
            host,
            (repo or "").casefold(),
            parsed.number,
        ]
    else:
        # A schedule key includes its due occurrence; never collapse all ticks.
        identity = [WORKSPACE_ID, kind, item.repo, item.item_id]
    return json.dumps(identity, separators=(",", ":")), source


def _intro(title: str, source: dict[str, Any], state: str) -> str:
    location = source.get("repository")
    parts = [title, f"Source: {source['kind']}" + (f" · {location}" if location else "")]
    if source.get("url"):
        parts.append(f"[Open source]({source['url']})")
    parts.append(f"State: {state}")
    return "\n\n".join(parts)


def _message(
    session: Any,
    job: ExternalJobRow,
    key: str,
    content: str,
    at: float,
    *,
    run_id: str | None = None,
    historical: bool,
    kind: str = "agent_update",
    post_kind: str | None = "progress",
    work: dict[str, Any] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    chronicle_key: str | None = None,
) -> str | None:
    """Commit a truthful no-turn entry and its attachments exactly once."""
    message_id = "msg_job_" + _digest(key)
    if chronicle_key is not None:
        posted = session.get(ChannelRunPostRow, chronicle_key)
        if posted is not None:
            return str(posted.message_id)
    if session.get(MessageRow, message_id) is not None:
        return message_id
    channel = session.get(ChannelRow, job.channel_id)
    if channel is None or channel.state != "active":
        return None
    sequence = CollaborationStore._next_sequence(session, job.channel_id)
    session.add(
        MessageRow(
            id=message_id,
            channel_id=job.channel_id,
            turn_id=None,
            sequence=sequence,
            role="assistant",
            kind=kind,
            content=content,
            agent_slug=None,
            author_kind="system",
            author_id="daemon",
            created_at=at,
            work_json=None if work is None else json.dumps(work),
            origin_json=json.dumps(
                {
                    "source_work_id": job.work_id,
                    "source_run_id": run_public_id(run_id) if run_id else None,
                    "historical": historical,
                }
            ),
            post_kind=post_kind,
        )
    )
    if artifacts:
        CollaborationStore._attach_artifacts(session, message_id, job.channel_id, artifacts, at)
    if chronicle_key is not None and run_id is not None:
        session.add(
            ChannelRunPostRow(
                dedupe_key=chronicle_key,
                run_id=run_id,
                message_id=message_id,
                kind=post_kind or "progress",
                posted_at=at,
            )
        )
    channel.updated_at = max(channel.updated_at, at)
    channel.revision += 1
    if historical and sequence == job.read_baseline + 1:
        job.read_baseline = max(job.read_baseline, sequence)
    _channel_metadata(channel, job)
    session.flush()
    _event(
        session,
        "collaboration.message.created",
        at,
        actor=DAEMON_ACTOR,
        data={
            "channel_id": job.channel_id,
            "work_id": job.work_id,
            "run_id": run_public_id(run_id) if run_id else None,
            "source_run_id": run_public_id(run_id) if run_id else None,
            "message_id": message_id,
            "sequence": sequence,
            "historical": historical,
        },
    )
    return message_id


def _channel_metadata(channel: ChannelRow, job: ExternalJobRow) -> None:
    """Keep system-created channel provenance when future admission moves a job.

    The old conversation still owns its old messages and read baseline. Its
    summary must never acquire a later private admission's title or source.
    """
    settings = json.loads(channel.settings_json or "{}")
    previous = settings.get("external_work")
    if previous is None and not job.system_created:
        return
    settings["external_work"] = {
        "work_id": job.work_id,
        "source": (previous or {}).get("source", json.loads(job.source_json)),
        "system_created": True,
        "historical": bool(job.historical),
        "read_baseline": max((previous or {}).get("read_baseline", 0), job.read_baseline),
        "state": job.state,
    }
    channel.settings_json = json.dumps(settings)


def _ensure_job(
    session: Any,
    *,
    key: str,
    title: str,
    source: dict[str, Any],
    state: str,
    created_at: float,
    updated_at: float,
    historical: bool,
    item_id: str | None = None,
    admitted: str | None = None,
) -> ExternalJobRow:
    work_id = "job_" + _digest(key)
    job: ExternalJobRow | None = session.get(ExternalJobRow, work_id)
    if job is not None:
        # The latest explicit admission owns future attempts; already bound
        # runs retain their original channel, including private/tombstoned ones.
        if admitted is not None and admitted != job.channel_id:
            job.channel_id = admitted
            job.system_created = 0
        job.item_id = item_id or job.item_id
        job.title = title
        job.state = state
        job.updated_at = max(job.updated_at, updated_at)
        return job
    channel_id = admitted or "chn_job_" + _digest(key)
    job = ExternalJobRow(
        work_id=work_id,
        job_key=key,
        workspace_id=WORKSPACE_ID,
        channel_id=channel_id,
        item_id=item_id,
        title=title,
        state=state,
        source_json=json.dumps(source),
        system_created=int(admitted is None),
        historical=int(historical),
        read_baseline=0,
        item_transition=0,
        attempt_cursor=0,
        attempt_run_id="",
        created_at=created_at,
        updated_at=updated_at,
    )
    session.add(job)
    if admitted is None:
        session.add(
            ChannelRow(
                id=channel_id,
                workspace_id=WORKSPACE_ID,
                user_id=SYSTEM_OWNER,
                created_by=None,
                title=title[:200],
                state="active",
                revision=1,
                visibility="workspace",
                created_at=created_at,
                updated_at=updated_at,
            )
        )
        session.flush()
        _event(
            session,
            "collaboration.channel.created",
            created_at,
            actor=DAEMON_ACTOR,
            data={"channel_id": channel_id, "work_id": work_id, "historical": historical},
        )
        _message(
            session,
            job,
            work_id + ":opening",
            _intro(title, source, state),
            created_at,
            historical=historical,
            kind="job_opening",
            post_kind=None,
        )
    session.flush()
    return job


def _bind_item(ctx: Any, session: Any, item: WorkItemRow, historical: bool) -> ExternalJobRow:
    key, source = _item_identity(ctx, item)
    admitted = admission_channel_for_item(session, item.item_id)
    old = session.get(ExternalJobRow, "job_" + _digest(key))
    previous = old.state if old is not None else None
    job = _ensure_job(
        session,
        key=key,
        source=source,
        title=item.title,
        state=item.state,
        created_at=item.created_at,
        updated_at=item.updated_at,
        item_id=item.item_id,
        historical=historical,
        admitted=admitted,
    )
    alias = session.get(ExternalItemRow, item.item_id)
    if alias is None:
        session.add(
            ExternalItemRow(item_id=item.item_id, work_id=job.work_id, channel_id=job.channel_id)
        )
    elif admitted:
        alias.channel_id = admitted
    session.flush()
    channel = session.get(ChannelRow, job.channel_id)
    if channel is not None and job.system_created:
        _channel_metadata(channel, job)
    no_turn = turn_for_item(session, item.item_id, job.channel_id) is None
    if no_turn and not job.system_created:
        _message(
            session,
            job,
            job.work_id + ":opening",
            _intro(item.title, source, item.state),
            item.created_at,
            historical=historical,
            kind="job_opening",
            post_kind=None,
        )
    if not item.run_id and no_turn and item.state in ATTENTION_STATES and previous != item.state:
        job.item_transition += 1
        _message(
            session,
            job,
            f"{job.work_id}:item:{job.item_transition}",
            item.last_error or f"Work is {item.state}.",
            item.updated_at,
            historical=historical,
            kind="work_result",
            post_kind="notice",
        )
        if not historical:
            _event(
                session,
                "collaboration.external_work.attention",
                item.updated_at,
                actor=DAEMON_ACTOR,
                data={
                    "channel_id": job.channel_id,
                    "work_id": job.work_id,
                    "run_id": None,
                    "attention_id": f"{job.work_id}:item:{job.item_transition}",
                    "kind": ATTENTION_STATES[item.state],
                    "title": item.title,
                    "body": item.last_error or f"Work is {item.state}.",
                    "historical": False,
                },
            )
    return job


def _attention(
    session: Any, job: ExternalJobRow, run: Run, previous: str | None, historical: bool
) -> None:
    kind = ATTENTION_STATES.get(run.state)
    if historical or kind is None or previous == run.state:
        return
    if (
        not job.system_created
        and job.item_id
        and turn_for_item(session, job.item_id, job.channel_id) is not None
    ):
        return
    _event(
        session,
        "collaboration.external_work.attention",
        run.updated_at,
        actor=DAEMON_ACTOR,
        data={
            "channel_id": job.channel_id,
            "work_id": job.work_id,
            "run_id": run_public_id(run.run_id),
            "attention_id": f"{job.work_id}:{run.run_id}:{run.revision}:{run.state}",
            "kind": kind,
            "title": job.title,
            "body": run.reason or f"Work is {run.state}.",
            "historical": False,
        },
    )


def _bind_run(ctx: Any, session: Any, run: Run, historical: bool) -> ExternalJobRow:
    bound = session.get(ExternalRunRow, run.run_id)
    resumes = int(
        session.scalar(
            select(func.count()).select_from(RunResumeRow).where(RunResumeRow.run_id == run.run_id)
        )
        or 0
    )
    ledger = session.get(DaemonRunRow, run.run_id)
    item = (
        session.get(WorkItemRow, ledger.item_id)
        if ledger is not None
        else session.scalars(
            select(WorkItemRow)
            .where(or_(WorkItemRow.run_id == run.run_id, WorkItemRow.prior_run_id == run.run_id))
            .limit(1)
        ).first()
    )
    if bound is not None:
        job: ExternalJobRow | None = session.get(ExternalJobRow, bound.work_id)
        assert job is not None  # nosec B101 - bindings and jobs commit together
        previous = bound.state
        if previous != run.state or resumes != bound.resumes:
            bound.transition += 1
            bound.transition_historical = int(historical)
            if resumes != bound.resumes:
                previous = None
        bound.resumes = resumes
        bound.state, bound.revision = run.state, run.revision
        bound.kind, bound.updated_at = run.kind, run.updated_at
    else:
        previous = None
        if item is not None:
            job = _bind_item(ctx, session, item, historical)
        else:
            # A deleted item may have a durable alias even though its mutable
            # queue row no longer exists. The ledger preserves that identity.
            alias = session.get(ExternalItemRow, ledger.item_id) if ledger is not None else None
            known: ExternalJobRow | None = (
                session.get(ExternalJobRow, alias.work_id) if alias is not None else None
            )
            job = known or _ensure_job(
                session,
                key=json.dumps([WORKSPACE_ID, "run", run.run_id]),
                title=run.outcome or run.run_id,
                state=run.state,
                source={"kind": "run", "repository": None, "url": None},
                created_at=run.created_at,
                updated_at=run.updated_at,
                historical=historical,
            )
        bound = ExternalRunRow(
            run_id=run.run_id,
            work_id=job.work_id,
            channel_id=job.channel_id,
            item_id=item.item_id if item is not None else None,
            created_at=run.created_at,
            state=run.state,
            kind=run.kind,
            updated_at=run.updated_at,
            revision=run.revision,
            historical=int(historical),
            event_cursor=0,
            replay_complete=0,
            title=job.title,
            source_json=job.source_json,
            transition=1,
            resumes=resumes,
            transition_historical=int(historical),
            historical_through=int(
                session.scalar(select(func.max(EventRow.seq)).where(EventRow.run_id == run.run_id))
                or 0
            )
            if historical
            else 0,
        )
        session.add(bound)
        session.execute(
            update(ApiEventRow)
            .where(ApiEventRow.run_id == run.run_id, ApiEventRow.channel_id.is_(None))
            .values(channel_id=bound.channel_id)
        )
    # Old attempts never make the sidebar look older than its newest activity.
    if run.updated_at >= job.updated_at:
        job.state, job.updated_at = run.state, run.updated_at
    if bound.channel_id == job.channel_id:
        _attention(session, job, run, previous, historical)
        channel = session.get(ChannelRow, job.channel_id)
        if channel is not None and job.system_created:
            _channel_metadata(channel, job)
    session.flush()
    return job


def _eligible(state: str, updated: float, baseline: float, *, item: bool) -> bool:
    terminal = ITEM_TERMINAL if item else IMPORT_TERMINAL
    return state not in terminal or updated >= baseline - HISTORY_SECONDS


def _backfill(ctx: Any, limit: int) -> None:
    """Walk current work first, then retained history, in resumable pages."""
    with ctx.loop.dstore.read() as session:
        if all(
            _state(session, name) == "complete"
            for name in ("items_active", "runs_active", "items_history", "runs_history")
        ):
            return
    with ctx.loop.dstore.immediate_transaction() as session:
        baseline = float(_state(session, "initialized_at"))
        for name, model, key, terminal in (
            ("items_active", WorkItemRow, WorkItemRow.item_id, ITEM_TERMINAL),
            ("runs_active", Run, Run.run_id, IMPORT_TERMINAL),
            ("items_history", WorkItemRow, WorkItemRow.item_id, ITEM_TERMINAL),
            ("runs_history", Run, Run.run_id, IMPORT_TERMINAL),
        ):
            cursor = _state(session, name)
            if cursor == "complete":
                continue
            active = name.endswith("active")
            conditions = [
                key > cursor,
                model.created_at <= baseline,
                model.state.not_in(terminal) if active else model.state.in_(terminal),
            ]
            if not active:
                conditions.append(model.updated_at >= baseline - HISTORY_SECONDS)
            rows = session.scalars(
                select(model).where(*conditions).order_by(key).limit(limit)
            ).all()
            for row in rows:
                kind = "item" if isinstance(row, WorkItemRow) else "run"
                ident = row.item_id if kind == "item" else row.run_id
                if session.get(ExternalPendingRow, (kind, ident)) is None:
                    session.add(
                        ExternalPendingRow(kind=kind, resource_id=ident, generation=0, historical=1)
                    )
            _set_state(
                session,
                name,
                "complete"
                if len(rows) < limit
                else str(getattr(rows[-1], "item_id" if model is WorkItemRow else "run_id")),
            )
            if rows:
                return


def reconcile(ctx: Any, *, limit: int = BATCH) -> int:
    """One bounded pass; a failed projection leaves its durable queue entry."""
    with ctx.loop.dstore.read() as session:
        raw = _state(session, "initialized_at")
    if not raw:
        with ctx.loop.dstore.immediate_transaction() as session:
            raw = _state(session, "initialized_at")
            if not raw:
                raw = str(ctx.clock())
                _set_state(session, "initialized_at", raw)
                session.execute(update(ExternalPendingRow).values(historical=1))
    baseline = float(raw)
    _backfill(ctx, limit)
    with ctx.loop.dstore.read() as session:
        pending = [
            (row.kind, row.resource_id, row.generation, bool(row.historical))
            for row in session.scalars(
                select(ExternalPendingRow)
                .order_by(
                    ExternalPendingRow.historical,
                    ExternalPendingRow.kind,
                    ExternalPendingRow.resource_id,
                )
                .limit(limit)
            )
        ]
    for kind, ident, generation, historical in pending:
        run_ids: list[str] = []
        complete = True
        with ctx.loop.dstore.immediate_transaction() as session:
            if kind == "item":
                item = session.get(WorkItemRow, ident)
                if item is not None and (
                    _eligible(item.state, item.updated_at, baseline, item=True)
                    or session.get(ExternalItemRow, ident) is not None
                ):
                    job = _bind_item(ctx, session, item, historical)
                    page_size = max(1, limit // len(pending))
                    ledger = list(
                        session.scalars(
                            select(DaemonRunRow)
                            .where(
                                DaemonRunRow.item_id == item.item_id,
                                or_(
                                    DaemonRunRow.started_at > job.attempt_cursor,
                                    (DaemonRunRow.started_at == job.attempt_cursor)
                                    & (DaemonRunRow.run_id > job.attempt_run_id),
                                ),
                            )
                            .order_by(DaemonRunRow.started_at, DaemonRunRow.run_id)
                            .limit(page_size)
                        )
                    )
                    for prior in ledger:
                        if session.get(ExternalPendingRow, ("run", prior.run_id)) is None:
                            session.add(
                                ExternalPendingRow(
                                    kind="run", resource_id=prior.run_id, historical=1, generation=0
                                )
                            )
                        job.attempt_cursor, job.attempt_run_id = prior.started_at, prior.run_id
                    complete = len(ledger) < page_size
                    for rid in (item.run_id, item.prior_run_id):
                        if rid and rid not in run_ids:
                            run_ids.append(rid)
                    for rid in run_ids:
                        run = session.get(Run, rid)
                        if run is not None:
                            _bind_run(
                                ctx,
                                session,
                                run,
                                historical
                                or (
                                    session.get(ExternalRunRow, rid) is None
                                    and run.created_at <= baseline
                                ),
                            )
            else:
                run = session.get(Run, ident)
                if run is not None and (
                    _eligible(run.state, run.updated_at, baseline, item=False)
                    or session.get(ExternalRunRow, ident) is not None
                ):
                    _bind_run(ctx, session, run, historical)
                    run_ids = [ident]
        for run_id in run_ids:
            complete = _project_run(ctx, run_id, historical=historical) and complete
        if complete:
            with ctx.loop.dstore.immediate_transaction() as session:
                session.execute(
                    delete(ExternalPendingRow).where(
                        ExternalPendingRow.kind == kind,
                        ExternalPendingRow.resource_id == ident,
                        ExternalPendingRow.generation == generation,
                    )
                )
    return len(pending)


def _progress(type_: str, data: dict[str, Any]) -> str | None:
    if type_ == "run.tasks":
        return f"Planned {len(data.get('tasks') or [])} tasks."
    if type_ == "task.end":
        title = data.get("title") or data.get("task_id") or "work"
        return f"Task {data.get('state', 'finished')}: {title}."
    if type_ == "review.verdict":
        return f"Review: {data.get('findings', 0)} findings, {data.get('blocking', 0)} blocking."
    if type_ == "chat.reply":
        return str(data.get("reply") or "") or None
    return None


def _live_post_key(run_id: str, type_: str, data: dict[str, Any], resumes: int) -> str | None:
    """Share the live chronicle's ledger when admission already names a channel."""
    segment = f":resume{resumes}" if resumes else ""
    if type_ == "run.tasks":
        return f"{run_id}:plan"
    if type_ == "review.verdict":
        return f"{run_id}:review:{data.get('round') or 1}"
    if type_ == "task.end" and data.get("task_id"):
        if data.get("state", "done") == "failed":
            return f"{run_id}:failed:{data['task_id']}{segment}"
        return f"{run_id}:progress:{data['task_id']}"
    if type_ == "chat.reply" and data.get("message_id"):
        return f"{run_id}:reply:{data['message_id']}"
    return None


def _project_run(ctx: Any, run_id: str, *, historical: bool) -> bool:
    """Replay narrative milestones and terminal results; never execute work."""
    with ctx.loop.dstore.read() as session:
        binding = session.get(ExternalRunRow, run_id)
        job = session.get(ExternalJobRow, binding.work_id) if binding is not None else None
        if binding is None or job is None:
            return True
        projected_transition = binding.transition
        projected_revision = binding.revision
        # Existing admission channels already have their own chronicle and
        # delivery; keep their behavior while /jobs exposes all attempts.
        has_turn = (
            job.item_id is not None
            and turn_for_item(session, job.item_id, binding.channel_id) is not None
        )
        if (not job.system_created and has_turn) or binding.channel_id != job.channel_id:
            return True
        events = list(
            session.scalars(
                select(EventRow)
                .where(EventRow.run_id == run_id, EventRow.seq > binding.event_cursor)
                .order_by(EventRow.seq)
                .limit(EVENT_BATCH)
            )
        )
        # A noisy item update must not recatalog and reread completed
        # artifacts for every old attempt. Events and state transitions keep
        # their durable dirty entries until their actual post commits.
        terminal_key = "msg_job_" + _digest(f"{run_id}:result:{binding.state}:{binding.transition}")
        if (
            not events
            and binding.replay_complete
            and (
                binding.state not in ATTENTION_STATES
                or session.get(MessageRow, terminal_key) is not None
            )
        ):
            return True
    record = Views(ctx).run_record(run_id)
    if record is None:
        return True
    terminal = record.state in ATTENTION_STATES
    files = _artifacts(ctx, record) if terminal and record.state != "held" else []
    result = (
        _with_files(_result(ctx, run_id, record.state, record.reason), files) if terminal else None
    )
    with ctx.loop.dstore.immediate_transaction() as session:
        binding = session.get(ExternalRunRow, run_id)
        assert binding is not None  # nosec B101
        job = session.get(ExternalJobRow, binding.work_id)
        assert job is not None  # nosec B101
        if (
            binding.transition != projected_transition
            or binding.revision != projected_revision
            or record.revision != binding.revision
            or record.state != binding.state
        ):
            return False
        has_turn = (
            job.item_id is not None
            and turn_for_item(session, job.item_id, binding.channel_id) is not None
        )
        if (not job.system_created and has_turn) or binding.channel_id != job.channel_id:
            return True
        for event in events:
            data = json.loads(event.data_json or "{}")
            content = _progress(event.type, data)
            if content:
                _message(
                    session,
                    job,
                    f"{run_id}:event:{event.seq}",
                    content,
                    event.ts,
                    run_id=run_id,
                    historical=event.seq <= binding.historical_through,
                    chronicle_key=_live_post_key(run_id, event.type, data, binding.resumes)
                    if not job.system_created
                    else None,
                )
            binding.event_cursor = max(binding.event_cursor, event.seq)
        complete = len(events) < EVENT_BATCH
        if complete:
            binding.replay_complete = 1
            if result is not None:
                # Resuming and stopping again is a new segment; unchanged
                # terminal polls reuse the same last transition revision.
                _message(
                    session,
                    job,
                    f"{run_id}:result:{record.state}:{binding.transition}",
                    result,
                    record.updated_at,
                    run_id=run_id,
                    historical=bool(binding.transition_historical),
                    kind="work_result",
                    post_kind="delivery" if record.state in {"merged", "completed"} else "notice",
                    artifacts=files,
                    chronicle_key=(
                        f"{run_id}:delivery"
                        if record.state in {"merged", "completed"}
                        else f"{run_id}:notice:{record.state}"
                        + (f":resume{binding.resumes}" if binding.resumes else "")
                    )
                    if not job.system_created
                    else None,
                )
    return complete


def jobs(ctx: Any, channel_id: str) -> list[dict[str, Any]]:
    """Current work and all its immutable attempts in one visible channel."""
    with ctx.loop.dstore.read() as session:
        rows = list(
            session.scalars(select(ExternalJobRow).where(ExternalJobRow.channel_id == channel_id))
        )
        bindings = list(
            session.scalars(
                select(ExternalRunRow)
                .where(ExternalRunRow.channel_id == channel_id)
                .order_by(ExternalRunRow.created_at)
            )
        )
        missing = {row.work_id for row in bindings} - {row.work_id for row in rows}
        if missing:
            rows.extend(
                session.scalars(select(ExternalJobRow).where(ExternalJobRow.work_id.in_(missing)))
            )
    views = Views(ctx)
    output: list[dict[str, Any]] = []
    for job in rows:
        latest_item = (
            ctx.loop.dstore.get(job.item_id)
            if job.item_id and job.channel_id == channel_id
            else None
        )
        attempts = [binding for binding in bindings if binding.work_id == job.work_id]
        if latest_item is not None and (not latest_item.run_id or not attempts):
            attempts = [*attempts, None]
        for binding in attempts:
            item = latest_item
            public_item = views.item(item) if item is not None else None
            record = views.run_record(binding.run_id) if binding is not None else None
            run = views.run(record) if record is not None else None
            unavailable = binding is not None and record is None
            with ctx.loop.dstore.read() as session:
                turn_id = (
                    turn_for_item(session, item.item_id, channel_id) if item is not None else None
                )
            output.append(
                {
                    "work_id": job.work_id,
                    "channel_id": channel_id,
                    "item_id": public_item.id if public_item else None,
                    "turn_id": turn_id,
                    "agent_slug": item.lead_agent if item else None,
                    "title": binding.title if binding else item.title if item else job.title,
                    "kind": record.kind
                    if record
                    else binding.kind
                    if binding
                    else item.kind
                    if item
                    else "code",
                    "state": record.state
                    if record
                    else binding.state
                    if binding
                    else item.state
                    if item
                    else job.state,
                    "run_id": run.id if run else run_public_id(binding.run_id) if binding else None,
                    "stage": run.stage if run else None,
                    "item_revision": public_item.revision if public_item else 0,
                    "run_revision": run.revision if run else None,
                    "item_actions": public_item.available_actions
                    if public_item
                    and item is not None
                    and not unavailable
                    and (binding is None or binding.run_id == item.run_id)
                    else [],
                    "run_actions": run.available_actions if run else [],
                    "artifacts": _artifacts(ctx, record)
                    if record and record.state != "held"
                    else [],
                    "source": json.loads(binding.source_json if binding else job.source_json),
                    "created_at": rfc3339(
                        record.created_at
                        if record
                        else binding.created_at
                        if binding
                        else job.created_at
                    ),
                    "updated_at": rfc3339(
                        record.updated_at
                        if record
                        else binding.updated_at
                        if binding
                        else job.updated_at
                    ),
                    "historical": bool(binding.historical) if binding else bool(job.historical),
                    "unavailable": unavailable,
                }
            )
    represented = {(row["item_id"], row["run_id"]) for row in output}
    unavailable_items = {
        row["item_id"] for row in output if row["unavailable"] and row["item_id"] is not None
    }
    # A concierge may have filed the issue but discovery has not admitted it
    # yet. The additive endpoint must retain that exact pending association.
    for legacy in project_work(ctx, channel_id):
        identity = (legacy["item_id"], legacy["run_id"])
        if identity in represented or (
            legacy["run_id"] is None and legacy["item_id"] in unavailable_items
        ):
            continue
        pending = legacy["item_id"].startswith("pending_code:")
        output.append(
            {
                **legacy,
                "item_id": None if pending else legacy["item_id"],
                "work_id": "job_" + _digest(f"{channel_id}:{legacy['item_id']}"),
                "channel_id": channel_id,
                "source": {"kind": "chat", "repository": None, "url": None},
                "created_at": rfc3339(ctx.clock()),
                "updated_at": rfc3339(ctx.clock()),
                "historical": False,
            }
        )
    return output
