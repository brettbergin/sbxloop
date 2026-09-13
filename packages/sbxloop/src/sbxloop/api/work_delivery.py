"""Project managed work into the local conversation that requested it."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select

from sbxloop.api.projections import Views
from sbxloop.db.collaboration_models import ChannelRow, MessageRow, TurnRow
from sbxloop.db.daemon_models import WorkItemRow
from sbxloop.errors import SbxloopError


@dataclass(frozen=True, slots=True)
class WorkLink:
    item_id: str
    source_key: str
    channel_id: str
    turn_id: str
    input_message_id: str
    targets: tuple[str, ...]
    participants: tuple[dict[str, Any], ...]


def _links(ctx: Any, channel_id: str | None) -> list[WorkLink]:
    boundary = or_(
        WorkItemRow.source_key == MessageRow.id,
        func.substr(WorkItemRow.source_key, 1, func.length(MessageRow.id) + 1)
        == MessageRow.id + ":",
    )
    conditions = [
        boundary,
        MessageRow.role == "user",
        TurnRow.input_message_id == MessageRow.id,
        TurnRow.channel_id == MessageRow.channel_id,
        ChannelRow.id == MessageRow.channel_id,
        ChannelRow.state == "active",
        WorkItemRow.run_kind.in_(("workload", "tool")),
    ]
    if channel_id is not None:
        conditions.append(ChannelRow.id == channel_id)
    with ctx.loop.dstore.read() as session:
        rows = session.execute(
            select(
                WorkItemRow.item_id,
                WorkItemRow.source_key,
                MessageRow.channel_id,
                TurnRow.id,
                MessageRow.id,
                TurnRow.targets_json,
                TurnRow.participants_json,
            ).where(and_(*conditions))
        ).all()
    return [
        WorkLink(
            item_id=str(row[0]),
            source_key=str(row[1]),
            channel_id=str(row[2]),
            turn_id=str(row[3]),
            input_message_id=str(row[4]),
            targets=tuple(json.loads(row[5] or "[]")),
            participants=tuple(json.loads(row[6] or "[]")),
        )
        for row in rows
    ]


def _agent(link: WorkLink) -> str | None:
    suffix = link.source_key[len(link.input_message_id) :]
    index = 0
    if suffix.startswith(":"):
        first = suffix[1:].split(":", 1)[0]
        if first.isdigit():
            index = int(first)
    if index < len(link.participants):
        slug = link.participants[index].get("agent_slug")
        return str(slug) if slug else None
    return link.targets[index] if index < len(link.targets) else None


def _result(ctx: Any, run_id: str, state: str, fallback: str | None) -> str:
    if state == "completed":
        parts: list[str] = []
        for task in ctx.loop.store.get_tasks(run_id):
            if task.output is None:
                continue
            text = task.output.text.strip() if task.output.text else ""
            summary = task.output.summary.strip() if task.output.summary else ""
            if text or summary:
                parts.append(text or summary)
        return "\n\n".join(parts) or "Work completed."
    return fallback or f"Work ended with state: {state}."


def project_work(ctx: Any, channel_id: str | None = None) -> list[dict[str, Any]]:
    views = Views(ctx)
    snapshots: list[dict[str, Any]] = []
    for link in _links(ctx, channel_id):
        item = ctx.loop.dstore.get(link.item_id)
        if item is None:
            continue
        public_item = views.item(item)
        run = None
        public_run = None
        if item.run_id:
            try:
                run = ctx.loop.store.get_run(item.run_id)
                public_run = views.run(run)
            except SbxloopError:
                pass
        state = run.state if run is not None else item.state
        terminal = state in {"completed", "failed", "blocked", "cancelled", "gated"}
        terminal_key = run.run_id if run is not None else f"item:{item.attempts}:{state}"
        digest = hashlib.sha256(
            f"{link.channel_id}\0{link.item_id}\0{terminal_key}".encode()
        ).hexdigest()[:24]
        message_id = f"msg_work_{digest}"
        if channel_id is None and (not terminal or ctx.collaboration.message_exists(message_id)):
            continue
        snapshot = {
            "item_id": public_item.id,
            "turn_id": link.turn_id,
            "agent_slug": _agent(link),
            "title": item.title,
            "kind": item.kind,
            "state": state,
            "run_id": public_run.id if public_run else None,
            "stage": public_run.stage if public_run else None,
            "item_revision": public_item.revision,
            "run_revision": public_run.revision if public_run else None,
            "item_actions": public_item.available_actions,
            "run_actions": public_run.available_actions if public_run else [],
        }
        snapshots.append(snapshot)
        if not terminal:
            continue
        ctx.collaboration.append_work_result(
            message_id,
            channel_id=link.channel_id,
            turn_id=link.turn_id,
            content=(
                _result(ctx, run.run_id, state, item.last_error or run.reason)
                if run is not None
                else item.last_error or f"Work ended with state: {state}."
            ),
            agent_slug=_agent(link),
            work=snapshot,
            now=ctx.clock(),
        )
    return snapshots
