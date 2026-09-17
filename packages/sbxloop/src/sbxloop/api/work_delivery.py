"""Project managed work into the local conversation that requested it."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select

from sbxloop.api.agents import ANGIE_SLUG
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
    code_title: str | None = None
    code_agent: str | None = None


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


def _agent(link: WorkLink) -> str:
    """Credit the participant that asked; a runner turn with none is Angie's own."""
    if link.code_title is not None:
        return link.code_agent or ANGIE_SLUG
    suffix = link.source_key[len(link.input_message_id) :]
    index = 0
    if suffix.startswith(":"):
        first = suffix[1:].split(":", 1)[0]
        if first.isdigit():
            index = int(first)
    if index < len(link.participants):
        slug = link.participants[index].get("agent_slug")
        return str(slug) if slug else ANGIE_SLUG
    return link.targets[index] if index < len(link.targets) else ANGIE_SLUG


def _code_links(ctx: Any, channel_id: str | None) -> list[WorkLink]:
    """Join exact repository/issue identities; never infer links from agent prose."""
    conditions = [
        TurnRow.channel_id == ChannelRow.id,
        ChannelRow.state == "active",
        TurnRow.participants_json.contains('"code_work"'),
    ]
    if channel_id is not None:
        conditions.append(ChannelRow.id == channel_id)
    links: list[WorkLink] = []
    seen: set[tuple[str, str, str]] = set()
    with ctx.loop.dstore.read() as session:
        turns = session.scalars(
            select(TurnRow).where(and_(*conditions)).order_by(TurnRow.created_at.desc())
        ).all()
        for turn in turns:
            participants = tuple(json.loads(turn.participants_json))
            for index, participant in enumerate(participants):
                for ref_index, ref in enumerate(participant.get("code_work", [])):
                    key = (turn.channel_id, ref["repo"], ref["source_key"])
                    if key in seen:
                        continue
                    seen.add(key)
                    item_id = session.scalar(
                        select(WorkItemRow.item_id).where(
                            WorkItemRow.repo == ref["repo"],
                            WorkItemRow.source_key == ref["source_key"],
                            WorkItemRow.run_kind == "code",
                        )
                    )
                    links.append(
                        WorkLink(
                            item_id=item_id or f"pending_code:{turn.id}:{index}:{ref_index}",
                            source_key=ref["source_key"],
                            channel_id=turn.channel_id,
                            turn_id=turn.id,
                            input_message_id=turn.input_message_id,
                            targets=tuple(json.loads(turn.targets_json)),
                            participants=participants,
                            code_title=ref["title"],
                            code_agent=participant.get("agent_slug") or ANGIE_SLUG,
                        )
                    )
    return links


def _result(ctx: Any, run_id: str, state: str, fallback: str | None) -> str:
    if state == "merged":
        run = ctx.loop.store.get_run(run_id)
        return "Changes merged." + (f"\n\n[View pull request]({run.pr_url})" if run.pr_url else "")
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
    for link in [*_links(ctx, channel_id), *_code_links(ctx, channel_id)]:
        item = ctx.loop.dstore.get(link.item_id)
        if item is None:
            if link.code_title is not None and channel_id is not None:
                snapshots.append(
                    {
                        "item_id": link.item_id,
                        "turn_id": link.turn_id,
                        "agent_slug": link.code_agent,
                        "title": link.code_title,
                        "kind": "code",
                        "state": "awaiting_dispatch",
                        "run_id": None,
                        "stage": None,
                        "item_revision": 0,
                        "run_revision": None,
                        "item_actions": [],
                        "run_actions": [],
                    }
                )
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
        terminal = state in {"merged", "completed", "failed", "blocked", "cancelled", "gated"}
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
