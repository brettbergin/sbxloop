"""Project managed work into the local conversation that requested it."""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select

from sbxloop.api.agents import ANGIE_SLUG
from sbxloop.api.collaboration import HISTORY_MESSAGES
from sbxloop.api.projections import Views
from sbxloop.api.publicids import run_public_id
from sbxloop.db.collaboration_models import ChannelRow, MessageRow, TurnRow
from sbxloop.db.daemon_models import WorkItemRow
from sbxloop.engine.model import TERMINAL_RUN_STATES, RunRecord
from sbxloop.errors import SbxloopError
from sbxloop.log import get_logger

log = get_logger(__name__)

#: Files named on one work result; the run's own catalog lists the rest.
WORK_ARTIFACTS_MAX = 50
#: How far back a code link is looked for, in turns. The same window the
#: channel's messages page carries (``collaboration.HISTORY_MESSAGES``),
#: named here so the scan cannot quietly become unbounded again.
CODE_LINK_TURNS = HISTORY_MESSAGES
#: Run kinds whose catalogued files are what a sink delivered.
DELIVERING_KINDS = frozenset({"workload", "tool"})


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
    #: The lead the item was admitted with; credited before anyone else.
    lead_agent: str | None = None
    #: Linked by the item's channel alone: its key names no message there.
    detached: bool = False


def _links(ctx: Any, channel_id: str | None) -> list[WorkLink]:
    """Work linked to the conversation that asked for it: by the item's own
    channel when it names one, and by the message its key names otherwise.
    A key that names a message in a channel other than the item's does not
    link it there.

    The message is read from the item's own ``message_id``, recorded at
    admission and backfilled for older rows by revision 0032. It used to be
    matched against a prefix of ``source_key``, which is a function on a
    column: no index could serve it, so one delivery read cost a pass over
    the messages table per work item."""
    conditions = [
        WorkItemRow.message_id == MessageRow.id,
        or_(WorkItemRow.channel_id.is_(None), WorkItemRow.channel_id == MessageRow.channel_id),
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
                WorkItemRow.lead_agent,
            ).where(and_(*conditions))
        ).all()
        links = [
            WorkLink(
                item_id=str(row[0]),
                source_key=str(row[1]),
                channel_id=str(row[2]),
                turn_id=str(row[3]),
                input_message_id=str(row[4]),
                targets=tuple(json.loads(row[5] or "[]")),
                participants=tuple(json.loads(row[6] or "[]")),
                lead_agent=row[7],
            )
            for row in rows
        ]
        links.extend(_channel_links(session, channel_id, {link.item_id for link in links}))
    return links


def _channel_links(
    session: Any,
    channel_id: str | None,
    linked: set[str],
    kinds: tuple[str, ...] = ("workload", "tool"),
) -> list[WorkLink]:
    """Items of ``kinds`` that name their channel but no message in it:
    delivered as part of the latest turn the channel had when the item was
    admitted."""
    conditions = [
        WorkItemRow.channel_id.is_not(None),
        WorkItemRow.run_kind.in_(kinds),
        ChannelRow.id == WorkItemRow.channel_id,
        ChannelRow.state == "active",
    ]
    if channel_id is not None:
        conditions.append(WorkItemRow.channel_id == channel_id)
    rows = session.execute(
        select(
            WorkItemRow.item_id,
            WorkItemRow.source_key,
            WorkItemRow.channel_id,
            WorkItemRow.created_at,
            WorkItemRow.lead_agent,
        ).where(and_(*conditions))
    ).all()
    links: list[WorkLink] = []
    for item_id, source_key, item_channel, created_at, lead in rows:
        if str(item_id) in linked:
            continue
        turns = select(TurnRow).where(TurnRow.channel_id == item_channel)
        turn = (
            session.scalars(
                turns.where(TurnRow.created_at <= created_at)
                .order_by(TurnRow.created_at.desc(), TurnRow.id.desc())
                .limit(1)
            ).first()
            or session.scalars(
                turns.order_by(TurnRow.created_at.asc(), TurnRow.id.asc()).limit(1)
            ).first()
        )
        if turn is None:
            # A result is part of a turn; a channel with none has nowhere
            # to put it yet. Say so: the work ran and finished, and the
            # only sign of it in the channel would otherwise be silence.
            log.info(
                "api.work_delivery_skipped",
                item=str(item_id),
                channel=str(item_channel),
                reason="channel has no turn to deliver into",
            )
            continue
        links.append(
            WorkLink(
                item_id=str(item_id),
                source_key=str(source_key),
                channel_id=str(item_channel),
                turn_id=str(turn.id),
                input_message_id=str(turn.input_message_id),
                targets=tuple(json.loads(turn.targets_json or "[]")),
                participants=tuple(json.loads(turn.participants_json or "[]")),
                lead_agent=lead,
                detached=True,
            )
        )
    return links


def _agent(link: WorkLink) -> str:
    """Credit the lead the item was admitted with, then the participant that
    asked; a runner turn with none is Angie's own."""
    if link.lead_agent:
        return link.lead_agent
    if link.detached:
        return ANGIE_SLUG
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


def _code_items(
    session: Any, refs: set[tuple[str, str]]
) -> dict[tuple[str, str], tuple[str, str | None, str | None]]:
    """``(repo, source_key) -> (item_id, lead_agent, channel_id)`` for the
    code items the scanned turns named, in one query rather than one per
    reference."""
    if not refs:
        return {}
    rows = session.execute(
        select(
            WorkItemRow.repo,
            WorkItemRow.source_key,
            WorkItemRow.item_id,
            WorkItemRow.lead_agent,
            WorkItemRow.channel_id,
        ).where(
            WorkItemRow.run_kind == "code",
            WorkItemRow.repo.in_(sorted({repo for repo, _ in refs})),
            WorkItemRow.source_key.in_(sorted({key for _, key in refs})),
        )
    ).all()
    found: dict[tuple[str, str], tuple[str, str | None, str | None]] = {}
    for repo, source_key, item_id, lead, item_channel in rows:
        key = (str(repo), str(source_key))
        if key in refs and key not in found:
            found[key] = (str(item_id), lead, item_channel)
    return found


def _code_links(ctx: Any, channel_id: str | None) -> list[WorkLink]:
    """Join exact repository/issue identities; never infer links from agent prose.

    The walk is bounded to the channel's most recent
    :data:`CODE_LINK_TURNS` turns (the window the messages page itself
    carries), so a long-lived conversation does not re-read and re-parse
    its whole history on every poll. A code result older than that window
    is already written into the channel as a message; this scan only
    finds work that still has to be delivered.
    """
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
            select(TurnRow)
            .where(and_(*conditions))
            .order_by(TurnRow.created_at.desc(), TurnRow.id.desc())
            .limit(CODE_LINK_TURNS)
        ).all()
        parsed = [(turn, tuple(json.loads(turn.participants_json))) for turn in turns]
        items = _code_items(
            session,
            {
                (str(ref["repo"]), str(ref["source_key"]))
                for _turn, participants in parsed
                for participant in participants
                for ref in participant.get("code_work", [])
            },
        )
        for turn, participants in parsed:
            for index, participant in enumerate(participants):
                for ref_index, ref in enumerate(participant.get("code_work", [])):
                    key = (turn.channel_id, ref["repo"], ref["source_key"])
                    if key in seen:
                        continue
                    seen.add(key)
                    found = items.get((str(ref["repo"]), str(ref["source_key"])))
                    if found is not None and found[2] and found[2] != turn.channel_id:
                        # The admission named a channel, and it is not this
                        # one: the result belongs there, not wherever the
                        # issue happened to be mentioned (_channel_links
                        # below delivers it).
                        continue
                    item_id = found[0] if found is not None else None
                    links.append(
                        WorkLink(
                            item_id=item_id or f"pending_code:{turn.id}:{index}:{ref_index}",
                            lead_agent=found[1] if found is not None else None,
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
        # A code admission that named a channel is delivered to that
        # channel, whether or not any turn there mentioned the issue.
        links.extend(
            _channel_links(
                session,
                channel_id,
                {link.item_id for link in links},
                kinds=("code",),
            )
        )
    return links


def _artifacts(ctx: Any, run: RunRecord) -> list[dict[str, Any]]:
    """The files a workload or tool run delivered, catalogued first when it
    has finished so the first result written already names them. A code
    run delivers a pull request; its checkout is not handed to the channel.

    A catalog that fails costs this result its file list, never the
    delivery of every other channel's work: it names whatever is already
    on record."""
    if run.kind not in DELIVERING_KINDS:
        return []
    if run.state in TERMINAL_RUN_STATES:
        try:
            ctx.artifacts.catalog_run(run)
        except Exception:
            log.warning("api.catalog_failed", run=run.run_id, exc_info=True)
    return [
        {
            "id": artifact.id,
            "run_id": run_public_id(artifact.run_id),
            "relpath": artifact.relpath,
            "media_type": artifact.media_type,
            "size": artifact.size,
        }
        for artifact in ctx.artifacts.for_run(run.run_id)
        if artifact.available
    ][:WORK_ARTIFACTS_MAX]


def _with_files(content: str, artifacts: list[dict[str, Any]]) -> str:
    """Name the files in the text too: a bridge shows only the text."""
    if not artifacts:
        return content
    return content + "\n\nFiles:\n" + "\n".join(f"- {a['relpath']}" for a in artifacts)


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
    links = [*_links(ctx, channel_id), *_code_links(ctx, channel_id)]
    # The items behind the links, then the runs behind those items: two
    # queries for the page rather than two per link. Every one of them
    # runs under the store's single lock, so the count is what the
    # browser's poll costs the daemon.
    items = ctx.loop.dstore.get_many([link.item_id for link in links])
    runs = ctx.loop.store.get_runs([item.run_id for item in items.values() if item.run_id])
    for link in links:
        item = items.get(link.item_id)
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
                        "artifacts": [],
                    }
                )
            continue
        public_item = views.item(item)
        run = runs.get(item.run_id) if item.run_id else None
        public_run = None
        if run is not None:
            with contextlib.suppress(SbxloopError):
                public_run = views.run(run)
        state = run.state if run is not None else item.state
        terminal = state in {"merged", "completed", "failed", "blocked", "cancelled", "gated"}
        terminal_key = run.run_id if run is not None else f"item:{item.attempts}:{state}"
        digest = hashlib.sha256(
            f"{link.channel_id}\0{link.item_id}\0{terminal_key}".encode()
        ).hexdigest()[:24]
        message_id = f"msg_work_{digest}"
        if channel_id is None and (not terminal or ctx.collaboration.message_exists(message_id)):
            continue
        artifacts = _artifacts(ctx, run) if run is not None else []
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
            "artifacts": artifacts,
        }
        snapshots.append(snapshot)
        if not terminal:
            continue
        ctx.collaboration.append_work_result(
            message_id,
            channel_id=link.channel_id,
            turn_id=link.turn_id,
            content=(
                _with_files(
                    _result(ctx, run.run_id, state, item.last_error or run.reason), artifacts
                )
                if run is not None
                else item.last_error or f"Work ended with state: {state}."
            ),
            agent_slug=_agent(link),
            work=snapshot,
            now=ctx.clock(),
            artifacts=artifacts,
        )
    return snapshots
