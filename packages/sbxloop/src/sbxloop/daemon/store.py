"""DaemonStore: work items, the run ledger, requesters, chat threads.

Lives in the same ``state.db`` as the engine's :class:`StateStore` (WAL
mode allows the second connection) so ``sbxloop status``/``logs`` and the
daemon's own bookkeeping share one file, but it owns its own connection and
tables — the engine store stays untouched.

Item ids are normalised rather than migrated. A row written before the
typed-id migration carries a legacy ``gh:1234`` key; every lookup here goes
through :func:`_id_match`, which matches both the typed and the legacy
spelling of the same id, and every row loaded is normalised to the typed
form by :class:`~sbxloop.daemon.model.WorkItem`. So a store written by the
old daemon keeps resolving under either form, and rows written from now on
are typed — no ALTER, no rewrite pass, no window where an in-flight item
becomes unreachable.

Schema version 2 (the 1.0 pipeline). The pre-1.0 daemon kept five more
tables for the lanes that filed their own work — backlog fingerprints,
post-mortems, review charters, per-item PR acceptance state, audit
schedules — none of which exist any more: the PR's fate is now the engine
run's own (``runs.pr_number`` …). A ``state.db`` from that era is not
migrated; :meth:`DaemonStore.archive_legacy` moves it aside and the daemon
starts clean. That archive takes the engine's run history with it, which is
the deliberate "fresh state" of the cutover.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple, cast

from sqlalchemy import (
    ColumnElement,
    Integer,
    Result,
    Select,
    Text,
    delete,
    func,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import InstrumentedAttribute, Session, aliased

from sbxloop.config import ScheduleConfig
from sbxloop.daemon.model import ItemState, PendingReport, WorkItem
from sbxloop.daemon.schedule import ScheduleRow
from sbxloop.db import begin_immediate, ensure_schema, open_engine
from sbxloop.db.daemon_models import (
    ChatThreadRow,
    DaemonRunRow,
    DaemonStateRow,
    GatePromptRow,
    LocalMessageRow,
    MergeGateRow,
    PendingClarificationRow,
    PriorAttemptRow,
    RequesterRow,
    ReviewHoldRow,
    RunResumeRow,
    RunWatchRow,
    ScheduleRowModel,
    WorkItemRow,
)
from sbxloop.engine.model import RunKind
from sbxloop.errors import DaemonError
from sbxloop.ghids import (
    CHAT_PREFIX,
    GH_PREFIX,
    SCHED_PREFIX,
    format_gh_id,
    normalize_item_id,
    try_parse_gh_id,
)
from sbxloop.log import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = "2"
#: The ``daemon_state`` key the version lives under.
SCHEMA_VERSION_KEY = "schema_version"
#: ``daemon_state`` keys the local bridge stamps.
LOCAL_HEARTBEAT_KEY = "local_bridge_alive_at"
LOCAL_STARTED_KEY = "local_bridge_started_at"
#: The merge-gate states a prompt is still offered for.
OPEN_GATE_STATES: tuple[str, ...] = ("open", "approving")
LEGACY_SUFFIX = ".pre-1.0"

_WORK_ITEMS_BODY = """(
    item_id     TEXT PRIMARY KEY,
    source_key  TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    claimed     INTEGER NOT NULL DEFAULT 0,
    run_id      TEXT,
    last_error  TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    pending_report TEXT,
    requested_by TEXT,
    -- The owner/name the item came from, '' for rows written before
    -- multi-repo support (they belong to the sole configured repository).
    -- Identity is (source_key, repo): issue #4 in two repositories is two
    -- work items, which a bare UNIQUE(source_key) would have collided.
    repo        TEXT NOT NULL DEFAULT '',
    -- Earliest dispatch time, when a retry is scheduled rather than backed
    -- off by attempt count (an exhausted run resuming its own PR, #523).
    not_before  REAL,
    -- The claim comment's token, written before the comment is posted so
    -- a half-claim survives the process that made it (#530).
    claim_token TEXT,
    -- What the previous attempt left on the GitHub origin (#600): the run
    -- it ran under, the branch it pushed and the PR it opened, kept across
    -- a re-queue so a restart continues that work instead of redoing it.
    prior_run_id    TEXT,
    prior_branch    TEXT,
    prior_pr_number INTEGER,
    -- The run the item becomes (#760) and the workload profile it runs
    -- under. Named run_kind: a bare `kind` column on this table is the
    -- pre-1.0 lanes' marker the archive check looks for.
    run_kind        TEXT NOT NULL DEFAULT 'code',
    profile         TEXT,
    UNIQUE(source_key, repo)
)"""

_WORK_ITEMS_COLUMNS = (
    "item_id, source_key, title, body, url, state, attempts, claimed, run_id, "
    "last_error, created_at, updated_at, pending_report, requested_by"
)

_REQUESTERS_BODY = """(
    source_key   TEXT NOT NULL,
    repo         TEXT NOT NULL DEFAULT '',
    requester_id TEXT NOT NULL,
    created_at   REAL NOT NULL,
    PRIMARY KEY (source_key, repo)
)"""

_REQUESTERS_COLUMNS = "source_key, requester_id, created_at"


# The repo-less rows that are repo-less by design (#760, #761): a
# chat-started or scheduled workload has no repository to backfill,
# attribute or drop it for. The ids are literal-prefixed, so the clause is
# a constant, not a parameter.
def _not_local() -> ColumnElement[bool]:
    """Rows that belong to a repository at all.

    A chat item or a schedule's item has no repository by design, not by
    age, so the multi-repo settling passes must leave them alone.
    """
    return ~WorkItemRow.item_id.like(f"{CHAT_PREFIX}%") & ~WorkItemRow.item_id.like(
        f"{SCHED_PREFIX}%"
    )


_CHAT_THREADS_BODY = (
    "(run_id TEXT NOT NULL, backend TEXT NOT NULL DEFAULT 'discord', "
    "channel_id TEXT NOT NULL, thread_id TEXT NOT NULL, headline_id TEXT, status_id TEXT, "
    "PRIMARY KEY (run_id, backend))"
)
_CHAT_THREADS_COLUMNS = "run_id, backend, channel_id, thread_id, headline_id, status_id"
_RUN_WATCHES_BODY = (
    "(run_id TEXT NOT NULL, watcher_id TEXT NOT NULL, created_at REAL NOT NULL, "
    "backend TEXT NOT NULL DEFAULT 'discord', UNIQUE(run_id, watcher_id, backend))"
)
_RUN_WATCHES_COLUMNS = "run_id, watcher_id, created_at"

# FROZEN. This schema, the table bodies above it and the ALTERs in
# `apply_daemon_schema` are the daemon half of Alembic revision 0001, and
# 0001 has shipped: every deployed database is already stamped at it, so
# Alembic will never run this code against one again. A column added here
# now reaches a fresh install and nothing else, and the field crashes on
# the first query that selects it.
#
# To change the schema, add a revision under db/migrations/versions instead.
# `tests/unit/test_db_schema.py` freezes the shape this produces and will
# fail if it moves.
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS daemon_work_items {_WORK_ITEMS_BODY};
CREATE INDEX IF NOT EXISTS idx_daemon_items_state ON daemon_work_items(state, created_at);

CREATE TABLE IF NOT EXISTS daemon_runs (
    run_id      TEXT PRIMARY KEY,
    item_id     TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    result      TEXT
);
CREATE INDEX IF NOT EXISTS idx_daemon_runs_started ON daemon_runs(started_at);

CREATE TABLE IF NOT EXISTS daemon_run_resumes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    resumed_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_daemon_resumes_at ON daemon_run_resumes(resumed_at);
CREATE INDEX IF NOT EXISTS idx_daemon_resumes_item ON daemon_run_resumes(item_id);

CREATE TABLE IF NOT EXISTS daemon_state (
    key         TEXT PRIMARY KEY,
    value       TEXT
);

-- Who asked to be pinged when a run finishes. The bridge's watch registry
-- is in-memory, so without this a daemon restart silently drops every
-- pending watch — and runs last minutes to hours, exactly the window in
-- which a restart happens.
CREATE TABLE IF NOT EXISTS daemon_run_watches {_RUN_WATCHES_BODY};
CREATE INDEX IF NOT EXISTS idx_daemon_run_watches_run ON daemon_run_watches(run_id);

-- Who asked for an issue through the concierge, keyed by the issue number
-- it filed, so the work item that discovery later builds from that issue
-- carries the requester — recorded here rather than in the public issue
-- body, which would expose a chat user id.
CREATE TABLE IF NOT EXISTS daemon_requesters {_REQUESTERS_BODY};

-- What the last attempt at an issue pushed to the GitHub origin, kept
-- OUTSIDE the work-item row (#600). The item row is deleted on every path
-- that is not a finish — a lost claim race, a trigger label that went away
-- (:meth:`DaemonStore.discard`), a repo-less row discovery will re-create
-- (:meth:`DaemonStore.drop_repoless`) — and taking the branch/PR down with
-- it would make the very next poll start the restart from scratch. Keyed
-- by (issue, repository) rather than item_id so it survives an item row
-- being re-created under a normalised id.
CREATE TABLE IF NOT EXISTS daemon_prior_attempts (
    source_key  TEXT NOT NULL,
    repo        TEXT NOT NULL DEFAULT '',
    run_id      TEXT,
    branch      TEXT,
    pr_number   INTEGER,
    updated_at  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (source_key, repo)
);

-- Where each run lives on each chat backend, so a restart re-attaches.
-- One row per (run, backend): the daemon runs the operator console's
-- local bridge beside the external one, and both open a thread for the
-- same run. Ids are TEXT: Discord's are integer snowflakes, Slack's are
-- message timestamps ("1724968573.123456") that INTEGER affinity would
-- silently turn into a rounded REAL, the local bridge's are row ids.
CREATE TABLE IF NOT EXISTS daemon_chat_threads {_CHAT_THREADS_BODY};

-- run_for_thread() runs per inbound chat message in a non-control
-- channel; without this it scans a row per run the daemon has ever done.
CREATE INDEX IF NOT EXISTS idx_chat_threads_thread
    ON daemon_chat_threads(thread_id);

-- The opt-in merge gate ([landing] merge_gate): a run that cleared every
-- bar, parked awaiting one human approval. The row is the durable state —
-- prompts and buttons are re-armed from it after a restart. notify_ids is
-- a JSON list of chat user ids to @mention; custom_id is the stable token
-- a persistent Discord button carries. prompt_channel_id/prompt_message_id
-- are where a one-bridge daemon recorded the prompt; they are read once
-- into daemon_gate_prompts and never written again. kind is 'merge' (a
-- parked PR, released by a gh-ops landing) or 'publish' (a workload held
-- at publishing by its profile, #760: no PR — pr_number 0 — released by
-- re-queueing the item with its run pinned).
CREATE TABLE IF NOT EXISTS daemon_merge_gates (
    run_id            TEXT PRIMARY KEY,
    item_id           TEXT NOT NULL,
    repo              TEXT NOT NULL,
    pr_number         INTEGER NOT NULL,
    pr_url            TEXT NOT NULL DEFAULT '',
    branch            TEXT,
    notify_ids        TEXT NOT NULL DEFAULT '[]',
    custom_id         TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'open',
    kind              TEXT NOT NULL DEFAULT 'merge',
    prompt_channel_id TEXT,
    prompt_message_id TEXT,
    created_at        REAL NOT NULL,
    resolved_at       REAL,
    resolved_by       TEXT,
    detail            TEXT
);
CREATE INDEX IF NOT EXISTS idx_merge_gates_state ON daemon_merge_gates(state);
CREATE INDEX IF NOT EXISTS idx_merge_gates_item  ON daemon_merge_gates(item_id);

-- Where each backend posted a gate's approval prompt, so a restart can
-- find (or replace) the prompt on every surface that carries one.
CREATE TABLE IF NOT EXISTS daemon_gate_prompts (
    run_id     TEXT NOT NULL,
    backend    TEXT NOT NULL,
    channel_id TEXT,
    message_id TEXT,
    PRIMARY KEY (run_id, backend)
);

-- A run parked on a base that requires an approving review the loop
-- cannot give its own PR (#675). The row is the wait: the daemon polls
-- the PR every [landing] review_poll_interval_s while it is `open`,
-- `approving` is the landing in progress, `fixing` hands the run back
-- to the engine for a reviewer's changes, `paused` is the wait past
-- [landing] review_wait_s (nothing polls; `resume <item>` re-opens it),
-- `merged` / `dismissed` are the ends. login/is_bot are the loop's own
-- identity on this PR, resolved once so a poll costs no identity read;
-- since_at is when the current wait began (a resume restarts it).
-- held_by_draft (#677) is the other wait a person ends: they converted
-- the PR to draft, and the poll watches for it to be marked ready
-- instead of counting approvals.
CREATE TABLE IF NOT EXISTS daemon_review_holds (
    run_id             TEXT PRIMARY KEY,
    item_id            TEXT NOT NULL,
    repo               TEXT NOT NULL,
    pr_number          INTEGER NOT NULL,
    pr_url             TEXT NOT NULL DEFAULT '',
    branch             TEXT,
    login              TEXT NOT NULL DEFAULT '',
    is_bot             INTEGER,
    approvals_required INTEGER NOT NULL DEFAULT 1,
    held_by_draft      INTEGER NOT NULL DEFAULT 0,
    notify_ids         TEXT NOT NULL DEFAULT '[]',
    state              TEXT NOT NULL DEFAULT 'open',
    created_at         REAL NOT NULL,
    since_at           REAL NOT NULL,
    next_poll_at       REAL NOT NULL,
    polls              INTEGER NOT NULL DEFAULT 0,
    resolved_at        REAL,
    resolved_by        TEXT,
    detail             TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_holds_state ON daemon_review_holds(state);
CREATE INDEX IF NOT EXISTS idx_review_holds_item  ON daemon_review_holds(item_id);

-- A filing-blocking clarifying question with the concierge's own fallback
-- (ask, never block): if the asker never answers, the bridge tells the
-- concierge to proceed on the stated assumption instead of dropping the
-- goal. Persisted so a daemon restart only delays the auto-file.
CREATE TABLE IF NOT EXISTS daemon_pending_clarifications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    backend           TEXT NOT NULL DEFAULT 'discord',
    channel_id        TEXT,
    prompt_message_id TEXT,
    asker_id          TEXT,
    asker_name        TEXT,
    question          TEXT NOT NULL,
    assumption        TEXT NOT NULL,
    deadline          REAL NOT NULL,
    created_at        REAL NOT NULL,
    state             TEXT NOT NULL DEFAULT 'open',
    resolved_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_pending_clarify_due
    ON daemon_pending_clarifications(state, deadline);

-- The local chat bridge's mailbox: what the daemon said (direction 'out')
-- and what an operator typed in `sbxloop tui` (direction 'in'), one id
-- space so a transcript is one ORDER BY id and a reaction or an edit can
-- address either side. The rows are the console's chronology — a detached
-- console catches up by id — and the daemon claims inbound rows by CAS on
-- taken_at, exactly as the ctl queue claims a request file. edited_at is
-- when the text changed (the "(edited)" mark); updated_at moves on every
-- change to a row — an edit, a reaction, a resolved gate, a claim — and is
-- what a console polls to repaint what it already shows.
CREATE TABLE IF NOT EXISTS daemon_local_messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    direction      TEXT NOT NULL,
    channel_id     TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'message',
    text           TEXT NOT NULL DEFAULT '',
    embed_json     TEXT,
    choices_json   TEXT,
    gate_run_id    TEXT,
    reply_to_id    INTEGER,
    mention_users  INTEGER NOT NULL DEFAULT 0,
    author_id      TEXT NOT NULL DEFAULT 'sbx',
    author_name    TEXT NOT NULL DEFAULT 'sbx',
    reactions_json TEXT NOT NULL DEFAULT '[]',
    created_at     REAL NOT NULL,
    edited_at      REAL,
    updated_at     REAL NOT NULL DEFAULT 0,
    taken_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_local_messages_channel
    ON daemon_local_messages(channel_id, id);
CREATE INDEX IF NOT EXISTS idx_local_messages_updated
    ON daemon_local_messages(channel_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_local_messages_pending
    ON daemon_local_messages(id) WHERE direction = 'in' AND taken_at IS NULL;

-- One row per schedule (#761, #818): the schedule itself — profile, ask,
-- one cadence (every or cron), timezone, where it came from and who made
-- it — and its state. The daemon's database is the home of schedules: a
-- `[[schedules]]` entry in sbxloop.toml is imported into a row once and
-- is redundant after that. anchor is when the schedule was first seen —
-- the origin of an `every` grid; last_due the latest due instant acted on
-- (fired, skipped or swallowed while paused), so a late daemon catches up
-- with one tick and never re-fires one; last_fired_at / last_item the
-- fire that actually queued something; paused_by who parked the schedule
-- (`schedules pause <name>`). A row whose ask is NULL is state left by a
-- schedule that was removed, or written by a daemon from before the spec
-- lived here: it fires nothing.
CREATE TABLE IF NOT EXISTS daemon_schedules (
    name          TEXT PRIMARY KEY,
    anchor        REAL NOT NULL,
    last_due      REAL,
    last_fired_at REAL,
    last_item     TEXT,
    paused_by     TEXT,
    paused_at     REAL,
    profile       TEXT,
    ask           TEXT,
    every         TEXT,
    cron          TEXT,
    timezone      TEXT,
    source        TEXT,
    created_by    TEXT,
    created_at    REAL
);
"""
#: The schema's index statements alone, for a rebuild to recreate inside
#: its own transaction (``executescript`` would commit it first).
_INDEX_DDL: tuple[str, ...] = tuple(
    stmt.strip()
    for stmt in "\n".join(
        line for line in _SCHEMA.splitlines() if not line.lstrip().startswith("--")
    ).split(";")
    if "CREATE INDEX" in stmt
)

# States from which nothing more happens on its own. A row in one of
# these is *finished*: re-adding the trigger label to its issue re-queues
# it (#600). ``gated`` is deliberately absent — a parked merge is still
# live work awaiting one human approval — and so are ``awaiting_review``
# and ``paused_review`` (#675): the PR is open and its run is pinned.
TERMINAL_ITEM_STATES: frozenset[str] = frozenset({"done", "failed", "blocked", "cancelled"})
# The two review-wait states (#675), as one set for the callers that
# treat them alike (``resume``, ``abandon``, the operator's listings).
REVIEW_WAIT_STATES: frozenset[str] = frozenset({"awaiting_review", "paused_review"})


class PriorAttempt(NamedTuple):
    """What a previous attempt left on the GitHub origin, so a restart can
    continue on it instead of starting from nothing (#600)."""

    run_id: str | None
    branch: str | None
    pr_number: int | None


class MergeGate(NamedTuple):
    """One parked run awaiting a person: a merge (``[landing] merge_gate``)
    or, ``kind == "publish"``, a workload held at publishing (#760)."""

    run_id: str
    item_id: str
    repo: str
    pr_number: int
    pr_url: str
    branch: str | None
    notify_ids: tuple[str, ...]
    custom_id: str
    state: str  # open | approving | merged | released | dismissed
    prompt_channel_id: str | None
    prompt_message_id: str | None
    created_at: float
    resolved_at: float | None
    resolved_by: str | None
    detail: str | None
    kind: str = "merge"  # merge | publish


class ReviewHold(NamedTuple):
    """One run waiting for an approving review on GitHub (#675)."""

    run_id: str
    item_id: str
    repo: str
    pr_number: int
    pr_url: str
    branch: str | None
    login: str
    is_bot: bool | None
    approvals_required: int
    held_by_draft: bool
    notify_ids: tuple[str, ...]
    state: str  # open | approving | fixing | paused | merged | dismissed
    created_at: float
    since_at: float
    next_poll_at: float
    polls: int
    resolved_at: float | None
    resolved_by: str | None
    detail: str | None


def _row_to_hold(row: ReviewHoldRow) -> ReviewHold:
    try:
        notify = tuple(str(v) for v in json.loads(row.notify_ids or "[]"))
    except ValueError:
        notify = ()
    is_bot = row.is_bot
    return ReviewHold(
        run_id=row.run_id,
        item_id=normalize_item_id(str(row.item_id)),
        repo=row.repo,
        pr_number=int(row.pr_number),
        pr_url=row.pr_url or "",
        branch=row.branch,
        login=str(row.login or ""),
        is_bot=None if is_bot is None else bool(is_bot),
        approvals_required=int(row.approvals_required or 0),
        held_by_draft=bool(row.held_by_draft),
        notify_ids=notify,
        state=row.state,
        created_at=row.created_at,
        since_at=row.since_at,
        next_poll_at=row.next_poll_at,
        polls=int(row.polls or 0),
        resolved_at=row.resolved_at,
        resolved_by=row.resolved_by,
        detail=row.detail,
    )


def _row_to_schedule(row: ScheduleRowModel) -> ScheduleRow:
    return ScheduleRow(
        name=str(row.name),
        anchor=float(row.anchor),
        last_due=None if row.last_due is None else float(row.last_due),
        last_fired_at=None if row.last_fired_at is None else float(row.last_fired_at),
        last_item=None if row.last_item is None else str(row.last_item),
        paused_by=None if row.paused_by is None else str(row.paused_by),
        paused_at=None if row.paused_at is None else float(row.paused_at),
    )


def _row_to_spec(row: ScheduleRowModel) -> ScheduleConfig:
    """The schedule a row holds. Validated on the way out as on the way
    in, so a row an operator edited by hand cannot fire a cadence the
    daemon cannot read."""
    return ScheduleConfig(
        name=str(row.name),
        profile=str(row.profile),
        ask=str(row.ask),
        every=None if row.every is None else str(row.every),
        cron=None if row.cron is None else str(row.cron),
        timezone=None if row.timezone is None else str(row.timezone),
    )


class StoredSchedule(NamedTuple):
    """A schedule as the store holds it: the spec plus its provenance."""

    spec: ScheduleConfig
    source: str  # "config" (imported from sbxloop.toml), "chat", "ctl", …
    created_by: str | None
    created_at: float | None


def _row_to_gate(row: MergeGateRow) -> MergeGate:
    try:
        notify = tuple(str(v) for v in json.loads(row.notify_ids or "[]"))
    except ValueError:
        notify = ()
    return MergeGate(
        run_id=row.run_id,
        item_id=normalize_item_id(str(row.item_id)),
        repo=row.repo,
        pr_number=int(row.pr_number),
        pr_url=row.pr_url or "",
        branch=row.branch,
        notify_ids=notify,
        custom_id=row.custom_id,
        state=row.state,
        prompt_channel_id=row.prompt_channel_id,
        prompt_message_id=row.prompt_message_id,
        created_at=row.created_at,
        resolved_at=row.resolved_at,
        resolved_by=row.resolved_by,
        detail=row.detail,
        kind=str(row.kind or "merge"),
    )


# Open filing-blocking questions are bounded: a daemon nobody answers must
# not accumulate them forever (the question itself still posts; only the
# auto-file fallback is shed).
PENDING_CLARIFICATION_CAP = 32


class PendingClarification(NamedTuple):
    """One filing-blocking ask awaiting its answer (or its deadline)."""

    id: int
    backend: str
    channel_id: str | None
    asker_id: str | None
    asker_name: str | None
    question: str
    assumption: str
    deadline: float
    created_at: float
    state: str


class LocalMessage(NamedTuple):
    """One row of the local bridge's mailbox (either direction)."""

    id: int
    direction: str
    channel_id: str
    kind: str
    text: str
    embed_json: str | None
    choices_json: str | None
    gate_run_id: str | None
    reply_to_id: int | None
    mention_users: bool
    author_id: str
    author_name: str
    reactions: tuple[str, ...]
    created_at: float
    edited_at: float | None
    updated_at: float
    taken_at: float | None
    # The direction of the row this one replies to, when it has one —
    # what tells an inbound "reply to the bot" from a reply to a person.
    reply_to_direction: str | None = None


def _row_to_local_message(row: Any) -> LocalMessage:
    """One mailbox row.

    Typed loosely because it is handed two shapes: a mapped
    :class:`LocalMessageRow`, and the joined row the reads below select —
    the same columns plus ``reply_to_direction`` off the self-join, which
    is what tells an inbound reply to the bot from a reply to a person. A
    bare model has no such attribute, hence the ``getattr``.
    """
    try:
        reactions = tuple(str(r) for r in json.loads(row.reactions_json or "[]"))
    except ValueError:
        reactions = ()
    return LocalMessage(
        id=int(row.id),
        direction=str(row.direction),
        channel_id=str(row.channel_id),
        kind=str(row.kind),
        text=str(row.text or ""),
        embed_json=_text_or_none(row.embed_json),
        choices_json=_text_or_none(row.choices_json),
        gate_run_id=_text_or_none(row.gate_run_id),
        reply_to_id=None if row.reply_to_id is None else int(row.reply_to_id),
        mention_users=bool(row.mention_users),
        author_id=str(row.author_id),
        author_name=str(row.author_name),
        reactions=reactions,
        created_at=float(row.created_at),
        edited_at=None if row.edited_at is None else float(row.edited_at),
        updated_at=float(row.updated_at or 0.0),
        taken_at=None if row.taken_at is None else float(row.taken_at),
        reply_to_direction=_text_or_none(getattr(row, "reply_to_direction", None)),
    )


#: The replied-to row, joined in so a read can tell an inbound reply to the
#: bot from a reply to a person.
_REPLIED_TO = aliased(LocalMessageRow, name="r")


def _local_message_select() -> Select[Any]:
    """Everything a mailbox read selects, with the replied-to direction.

    A function rather than a constant because a Select is mutable-by-copy
    and every caller narrows it differently.
    """
    return select(LocalMessageRow, _REPLIED_TO.direction.label("reply_to_direction")).outerjoin(
        _REPLIED_TO, _REPLIED_TO.id == LocalMessageRow.reply_to_id
    )


def _local_messages(session: Session, stmt: Select[Any]) -> list[LocalMessage]:
    """Run a joined mailbox read and shape every row."""
    return [_row_to_local_message(_Joined(row)) for row in session.execute(stmt)]


class _Joined:
    """The mapped row and its joined ``reply_to_direction`` as one object.

    ``_row_to_local_message`` reads attributes; this puts the extra column
    beside the model's own without copying every field by hand.
    """

    __slots__ = ("_extra", "_row")

    def __init__(self, row: Any) -> None:
        self._row = row[0]
        self._extra = row.reply_to_direction

    def __getattr__(self, name: str) -> Any:
        if name == "reply_to_direction":
            return self._extra
        return getattr(self._row, name)


class ChatThread(NamedTuple):
    """Where a run lives on the chat backend (persisted so a restart
    re-attaches). Ids are the backend's own text form: a Discord snowflake
    as decimal digits, a Slack message ``ts``."""

    channel_id: str
    thread_id: str
    headline_id: str | None
    status_id: str | None
    backend: str = "discord"


def _row_to_chat_thread(row: ChatThreadRow) -> ChatThread:
    return ChatThread(
        str(row.channel_id),
        str(row.thread_id),
        _text_or_none(row.headline_id),
        _text_or_none(row.status_id),
        str(row.backend or "discord"),
    )


def dispatch_eligible_at(item: WorkItem, backoff_s: float) -> float:
    """When the daemon's dispatch rule lets ``item`` go: a scheduled retry
    waits its own clock; a resume-pending run and a first attempt go at
    once; a failed attempt waits ``attempts * backoff`` after its update.
    The one place the rule lives, so `next_queued` and a console agree."""
    eligible = 0.0
    if item.not_before is not None:
        eligible = max(eligible, item.not_before)
    if item.run_id is None and item.attempts > 0:
        eligible = max(eligible, item.updated_at + item.attempts * backoff_s)
    return eligible


def parse_breaker(opened: str | None, failures: str | None) -> tuple[float | None, int]:
    """The two ``daemon_state`` breaker values as the loop keeps them."""
    opened_at = float(opened) if opened not in (None, "") else None
    return opened_at, int(failures) if failures not in (None, "") else 0


class DiscordThread(NamedTuple):
    """The Discord view of a :class:`ChatThread`: the same row with its
    snowflakes as integers, which is what discord.py's lookups take."""

    channel_id: int
    thread_id: int
    headline_id: int | None
    status_id: int | None


def _id_variants(item_id: str) -> tuple[str, str]:
    """Both spellings of a GitHub item id (typed and legacy bare).

    Non-GitHub ids (``inbox:x.md``) come back duplicated, so callers can
    always bind exactly two parameters.

    A **repo-qualified** id comes back duplicated too. The bare form encodes
    only the issue number, so offering it as a variant of a qualified id
    would let a lookup for one repository's issue #24 match a bare
    ``gh:24`` row belonging to a different repository — the same
    number-is-not-identity confusion that collides the primary key in
    :meth:`DaemonStore._free_item_id`. A qualified id names its repository
    and matches only itself.
    """
    parsed = try_parse_gh_id(item_id)
    if parsed is None:
        return (item_id, item_id)
    if parsed.kind == "issue" and parsed.repo is None:
        return (parsed.item_id, f"{GH_PREFIX}{parsed.number}")
    return (parsed.item_id, parsed.item_id)


def _id_match(item_id: str) -> tuple[str, tuple[str, str]]:
    """``("item_id IN (?, ?)", params)`` for a lookup that accepts either
    spelling of the id."""
    return ("item_id IN (?, ?)", _id_variants(item_id))


def _row_to_item(row: WorkItemRow) -> WorkItem:
    return WorkItem(
        item_id=row.item_id,
        source_key=row.source_key,
        title=row.title,
        body=row.body,
        url=row.url,
        state=cast("ItemState", row.state),
        attempts=row.attempts,
        claimed=bool(row.claimed),
        run_id=row.run_id,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        pending_report=cast("PendingReport | None", row.pending_report),
        requested_by=row.requested_by,
        repo=row.repo or None,
        not_before=row.not_before,
        claim_token=row.claim_token,
        prior_run_id=row.prior_run_id,
        prior_branch=row.prior_branch,
        prior_pr_number=row.prior_pr_number,
        kind=cast("RunKind", row.run_kind or "code"),
        profile=row.profile,
        enqueue_seq=row.enqueue_seq or 0,
        queue_order=row.queue_order or 0,
    )


def _loggable(fields: dict[str, object]) -> dict[str, object]:
    """Column assignments as log fields: ``state`` first, long text clipped."""
    out: dict[str, object] = {}
    for key, value in fields.items():
        if key == "state":
            continue  # already logged from the re-read row
        out[key] = value[:120] if isinstance(value, str) else value
    return out


def _repo_from_url(url: object) -> str | None:
    """``owner/name`` out of a GitHub issue URL, or None if it is not one."""
    if not isinstance(url, str) or not url:
        return None
    match = re.search(r"github\.com/([^/\s]+)/([^/\s]+)/(?:issues|pull)/\d+", url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def readonly_uri(path: Path) -> str:
    """A ``mode=ro`` SQLite URI for ``path`` — percent-encoded, so a
    directory name with ``?``, ``#`` or ``%`` still names this file."""
    return f"{path.resolve().as_uri()}?mode=ro"


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}  # nosec B608


def _pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """The table's primary-key columns in key order (``pk`` is 1-based)."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()  # nosec B608
    return [str(row[1]) for row in sorted((r for r in rows if r[5] > 0), key=lambda r: r[5])]


def _migrate_repo_columns(conn: sqlite3.Connection) -> None:
    """Bring a store created before multi-repo to the current shape.

    ``CREATE TABLE IF NOT EXISTS`` leaves an existing table alone, so a
    daemon upgraded in place still has the single-repo shape — and its
    key is the one that must change: ``UNIQUE(source_key)`` on
    ``daemon_work_items`` (``PRIMARY KEY(source_key)`` on
    ``daemon_requesters``) collides the moment two configured
    repositories each have an issue with the same number, which is
    exactly what an upgraded store is about to see. SQLite cannot drop a
    constraint with ALTER, so the tables are rebuilt: new shape, rows
    copied with ``repo = ''``, drop, rename, indexes recreated — all in
    one transaction. Copied rows are then settled at daemon startup —
    backfilled with the sole configured repository
    (:meth:`backfill_repo`) or, when several are configured, named from
    their issue URL (:meth:`attribute_repoless`), with whatever is left
    over dropped if discovery can re-create it (:meth:`drop_repoless`)
    and otherwise failed with an operator notice
    (:meth:`strand_repoless`) — so they are never claimed by whichever
    repo happens to be polled first.
    """
    rebuilds = (
        ("daemon_work_items", _WORK_ITEMS_BODY, _WORK_ITEMS_COLUMNS),
        ("daemon_requesters", _REQUESTERS_BODY, _REQUESTERS_COLUMNS),
    )
    todo = [
        (table, body, f"{columns}, repo", f"{columns}, ''")
        for table, body, columns in rebuilds
        if "repo" not in _columns(conn, table)
    ]
    _rebuild_tables(conn, todo, rebuilt_for="repo")


def _rebuild_tables(
    conn: sqlite3.Connection, todo: list[tuple[str, str, str, str]], *, rebuilt_for: str
) -> None:
    """Rebuild each ``(table, body, insert_columns, select_expr)`` in one
    transaction: new shape, rows copied, drop, rename, indexes recreated.

    The indexes are issued statement by statement: ``executescript``
    commits the open transaction first, which would leave a rebuilt
    table with no indexes if the recreation failed."""
    if not todo:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, body, insert_columns, select_expr in todo:
            tmp = f"{table}__new"
            conn.execute(f"CREATE TABLE {tmp} {body}")  # nosec B608 - literals above
            conn.execute(
                f"INSERT INTO {tmp} ({insert_columns}) "  # nosec B608 - literals above
                f"SELECT {select_expr} FROM {table}"
            )
            conn.execute(f"DROP TABLE {table}")  # nosec B608 - literal above
            conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")  # nosec B608
            log.info("store.migrated", table=table, rebuilt_for=rebuilt_for)
        # Dropping the old table took its indexes with it.
        for ddl in _INDEX_DDL:
            conn.execute(ddl)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# Columns added after the multi-repo rebuild, applied idempotently on
# open so a store written by an older daemon upgrades in place.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (
        "daemon_work_items",
        "not_before",
        "ALTER TABLE daemon_work_items ADD COLUMN not_before REAL",
    ),
    (
        "daemon_work_items",
        "claim_token",
        "ALTER TABLE daemon_work_items ADD COLUMN claim_token TEXT",
    ),
    (
        "daemon_work_items",
        "prior_run_id",
        "ALTER TABLE daemon_work_items ADD COLUMN prior_run_id TEXT",
    ),
    (
        "daemon_work_items",
        "prior_branch",
        "ALTER TABLE daemon_work_items ADD COLUMN prior_branch TEXT",
    ),
    (
        "daemon_work_items",
        "prior_pr_number",
        "ALTER TABLE daemon_work_items ADD COLUMN prior_pr_number INTEGER",
    ),
    (
        "daemon_review_holds",
        "held_by_draft",
        "ALTER TABLE daemon_review_holds ADD COLUMN held_by_draft INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "daemon_work_items",
        "run_kind",
        "ALTER TABLE daemon_work_items ADD COLUMN run_kind TEXT NOT NULL DEFAULT 'code'",
    ),
    (
        "daemon_work_items",
        "profile",
        "ALTER TABLE daemon_work_items ADD COLUMN profile TEXT",
    ),
    (
        "daemon_merge_gates",
        "kind",
        "ALTER TABLE daemon_merge_gates ADD COLUMN kind TEXT NOT NULL DEFAULT 'merge'",
    ),
    # The schedule itself moved into its row (#818); a daemon from
    # before that wrote state-only rows, which the config import
    # fills in on the next start.
    *(
        ("daemon_schedules", column, f"ALTER TABLE daemon_schedules ADD COLUMN {column} {kind}")
        for column, kind in (
            ("profile", "TEXT"),
            ("ask", "TEXT"),
            ("every", "TEXT"),
            ("cron", "TEXT"),
            ("timezone", "TEXT"),
            ("source", "TEXT"),
            ("created_by", "TEXT"),
            ("created_at", "REAL"),
        )
    ),
)


def _migrate_added_columns(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _ADDED_COLUMNS:
        if column in _columns(conn, table):
            continue
        conn.execute(ddl)
        conn.commit()
        log.info("store.migrated", table=table, added=column)


def _migrate_discord_threads(conn: sqlite3.Connection) -> None:
    """Fold a pre-Slack ``daemon_discord_threads`` table into
    ``daemon_chat_threads``: same rows, ids cast to text, backend
    ``discord``; the old table is dropped so this runs once. A row that
    already exists in the new table (a store that was migrated and then
    reopened by an older daemon which recreated the old table) is left
    alone."""
    if "daemon_discord_threads" not in _tables(conn):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        moved = conn.execute(
            "INSERT OR IGNORE INTO daemon_chat_threads "
            "(run_id, backend, channel_id, thread_id, headline_id, status_id) "
            "SELECT run_id, 'discord', CAST(channel_id AS TEXT), CAST(thread_id AS TEXT), "
            "CAST(headline_id AS TEXT), CAST(status_id AS TEXT) "
            "FROM daemon_discord_threads"
        ).rowcount
        conn.execute("DROP TABLE daemon_discord_threads")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    log.info("store.migrated", table="daemon_chat_threads", moved=moved)


def _migrate_backend_keys(conn: sqlite3.Connection) -> None:
    """Key the chat state by backend, once.

    A one-bridge daemon kept one thread and one watcher list per run,
    and the gate's prompt on the gate row. The operator console's local
    bridge runs beside the external one and opens its own thread for
    the same run, so ``daemon_chat_threads`` is rebuilt on
    ``(run_id, backend)`` and ``daemon_run_watches`` gains a ``backend``
    column in its UNIQUE key (SQLite cannot widen a key with ALTER —
    same rebuild as :meth:`_migrate_repo_columns`). The prompt location
    moves into ``daemon_gate_prompts`` under the backend the run's
    thread used; the old columns stay readable and are never written
    again. Each step is guarded by the shape it changes, so a store
    already migrated is left alone.
    """
    rebuilds: list[tuple[str, str, str, str]] = []
    if _pk_columns(conn, "daemon_chat_threads") == ["run_id"]:
        rebuilds.append(
            (
                "daemon_chat_threads",
                _CHAT_THREADS_BODY,
                _CHAT_THREADS_COLUMNS,
                _CHAT_THREADS_COLUMNS,
            )
        )
    watches_rebuilt = "backend" not in _columns(conn, "daemon_run_watches")
    if watches_rebuilt:
        rebuilds.append(
            (
                "daemon_run_watches",
                _RUN_WATCHES_BODY,
                _RUN_WATCHES_COLUMNS,
                _RUN_WATCHES_COLUMNS,
            )
        )
    _rebuild_tables(conn, rebuilds, rebuilt_for="backend")
    # The backend a pre-upgrade row belongs to is the one that opened the
    # run's thread, else the one external backend the store has seen at
    # all (a daemon runs one), else Discord — never 'local', which no
    # released daemon ran.
    external = (
        "(SELECT t.backend FROM daemon_chat_threads t WHERE t.run_id = %s "
        "AND t.backend != 'local' LIMIT 1)"
    )
    sole = (
        "(SELECT backend FROM daemon_chat_threads WHERE backend != 'local' "
        "GROUP BY backend HAVING COUNT(*) = (SELECT COUNT(*) FROM daemon_chat_threads "
        "WHERE backend != 'local') LIMIT 1)"
    )
    fallback = f"COALESCE({external}, {sole}, 'discord')"
    if watches_rebuilt:
        watch_backend = fallback % "daemon_run_watches.run_id"  # nosec B608 - literals only
        conn.execute("UPDATE daemon_run_watches SET backend = " + watch_backend)  # nosec
        conn.commit()
    carry_gate_prompts(conn)


def carry_gate_prompts(conn: sqlite3.Connection) -> None:
    """Move a gate's prompt location off the gate row into the prompt table.

    A one-bridge daemon kept the prompt on the gate row; it now lives in
    ``daemon_gate_prompts``, one row per backend. This runs on **every**
    read-write open, not once: an older daemon in a rollback window writes
    those columns again, and the next start has to carry them again. It is a
    shape-based step, not a one-shot marker — which is why it does not live
    behind the schema version, where a stamped database would skip it.
    """
    external = (
        "(SELECT t.backend FROM daemon_chat_threads t WHERE t.run_id = %s "
        "AND t.backend != 'local' LIMIT 1)"
    )
    sole = (
        "(SELECT backend FROM daemon_chat_threads WHERE backend != 'local' "
        "GROUP BY backend HAVING COUNT(*) = (SELECT COUNT(*) FROM daemon_chat_threads "
        "WHERE backend != 'local') LIMIT 1)"
    )
    fallback = f"COALESCE({external}, {sole}, 'discord')"
    gate_backend = fallback % "g.run_id"  # nosec B608 - literals only
    carry = (
        "INSERT OR IGNORE INTO daemon_gate_prompts (run_id, backend, channel_id, message_id) "  # nosec B608
        "SELECT g.run_id, {backend}, g.prompt_channel_id, g.prompt_message_id "
        "FROM daemon_merge_gates g "
        "WHERE g.prompt_message_id IS NOT NULL AND g.prompt_message_id != ''"
    ).replace("{backend}", gate_backend)
    moved = conn.execute(carry).rowcount
    conn.execute(
        "UPDATE daemon_merge_gates SET prompt_channel_id = NULL, prompt_message_id = NULL "
        "WHERE prompt_message_id IS NOT NULL"
    )
    conn.commit()
    if moved:
        log.info("store.migrated", table="daemon_gate_prompts", moved=moved)


def apply_daemon_schema(conn: sqlite3.Connection) -> None:
    """Bring any daemon database this project ever wrote to the current shape.

    Creates the tables from nothing on a fresh file and upgrades an existing
    one in place: the repo-column rebuild, the idempotent ADD COLUMN set, the
    pre-Slack thread fold and the backend rekey, in that order, then stamps
    the schema version the operator console handshakes on.

    This is the whole of the daemon side of Alembic revision 0001, which is
    why it takes a bare connection rather than a store: it has to run against
    a database no store could open yet.
    """
    conn.executescript(_SCHEMA)
    _migrate_repo_columns(conn)
    _migrate_added_columns(conn)
    _migrate_discord_threads(conn)
    _migrate_backend_keys(conn)
    conn.execute(
        "INSERT OR REPLACE INTO daemon_state (key, value) VALUES (?, ?)",
        (SCHEMA_VERSION_KEY, SCHEMA_VERSION),
    )
    conn.commit()


def _rowcount(result: Result[Any]) -> int:
    """How many rows a DML statement touched.

    Every compare-and-set in this store turns on it: zero means the WHERE
    clause matched nothing, which is how a caller learns it lost a race
    rather than silently writing nothing.
    """
    return cast("CursorResult[Any]", result).rowcount


def _inserted_id(result: Result[Any]) -> int:
    """The autoincrement id an INSERT just assigned."""
    key = cast("CursorResult[Any]", result).inserted_primary_key
    return int(key[0] or 0) if key else 0


def _row_claimed(session: Session, statement: Any) -> bool:
    """Whether a per-row compare-and-set actually took the row.

    The claim *is* the state move, so a second sweeper that read the same
    row loses it here rather than firing the same work twice.
    """
    return _rowcount(session.execute(statement)) == 1


def _id_where(
    item_id: str, column: InstrumentedAttribute[str] | None = None
) -> ColumnElement[bool]:
    """Match a row under either spelling of an item id.

    Rendering is strict and parsing is lenient: every id sbxloop writes is
    typed (``gh:issue:12``), but a row written before typed ids, or an id an
    operator typed by hand, carries the bare ``gh:12`` form. Both resolve to
    the same item, which is why no migration of existing rows was needed.

    Defaults to the work item's own id; the ledger tables key by the same
    value and pass their own column.
    """
    target = WorkItemRow.item_id if column is None else column
    return target.in_(_id_variants(item_id))


def _item_values(fields: Mapping[str, object], *, updated_at: float) -> dict[str, object]:
    """Resolve keyword field names to mapped columns, refusing anything else.

    The names come from this module's own calls rather than from input, but
    resolving them against the mapper means a typo is an error here instead
    of a column invented in a statement.
    """
    unknown = set(fields) - set(WorkItemRow.__mapper__.columns.keys())
    if unknown:
        raise ValueError(f"not work item columns: {sorted(unknown)}")
    return {**fields, "updated_at": updated_at}


def _refuse_pre_1_0(path: Path) -> None:
    """Refuse a pre-1.0 store rather than failing on a missing column.

    ``archive_legacy`` moves one aside, but only the daemon calls it. A CLI
    command opening the file before the daemon's first start would otherwise
    hit a confusing error deep in a query, so it is named here. Raw sqlite3
    on purpose: this reads a schema that predates everything the models
    describe, before any of them could open it.
    """
    if not path.is_file():
        return
    conn = sqlite3.connect(readonly_uri(path), uri=True)
    try:
        if "daemon_work_items" not in _tables(conn):
            return
        if "kind" not in _columns(conn, "daemon_work_items"):
            return
    finally:
        conn.close()
    raise DaemonError(
        f"{path} is a pre-1.0 daemon state database; start `sbxloop daemon` once "
        f"to archive it (to {path.name}{LEGACY_SUFFIX}) and begin a fresh store"
    )


class DaemonStore:
    @classmethod
    def archive_legacy(cls, path: Path, *, clock: Callable[[], float] = time.time) -> Path | None:
        """Move a pre-1.0 ``state.db`` (and its ``-wal``/``-shm``) aside.

        Returns the archive path, or None when there was nothing to do: no
        file, a database the daemon never touched (an engine-only store from
        ``sbxloop run`` on a CLI host is left alone), or one already at the
        current schema. Called before either store opens the file — the
        daemon host is deployed unattended, so the cutover has to be
        something the daemon does for itself on its first start.
        """
        if not path.is_file():
            return None
        conn = sqlite3.connect(readonly_uri(path), uri=True)
        try:
            tables = _tables(conn)
            if "daemon_work_items" not in tables:
                return None
            if "daemon_state" in tables:
                row = conn.execute(
                    "SELECT value FROM daemon_state WHERE key = 'schema_version'"
                ).fetchone()
                if row is not None and str(row[0]) == SCHEMA_VERSION:
                    return None
        finally:
            conn.close()
        target = path.with_name(f"{path.name}{LEGACY_SUFFIX}")
        if target.exists():
            target = path.with_name(f"{path.name}{LEGACY_SUFFIX}.{int(clock())}")
        for suffix in ("", "-wal", "-shm"):
            src = path.with_name(path.name + suffix)
            if src.exists():
                src.rename(target.with_name(target.name + suffix))
        log.warning(
            "store.archived_legacy",
            db=str(path),
            archived_to=str(target),
            hint="pre-1.0 daemon state is not migrated; the daemon starts with a fresh store",
        )
        return target

    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        """Open the store. ``readonly`` opens the file through a read-only
        URI and runs no schema statement — what a second process on the
        daemon host (the operator console) uses, so it never migrates a
        store under a running daemon; every write then fails in SQLite. A
        read-only store must already carry the current schema."""
        self.path = path
        self.readonly = readonly
        # One connection shared by the daemon, bridge and CLI threads:
        # SQLite rejects concurrent use of a connection from two threads, so
        # EVERY statement — reads included — runs under this lock.
        self._lock = threading.RLock()
        if readonly:
            if not path.exists():
                raise DaemonError(
                    f"{path} does not exist; start `sbxloop daemon` once to create it"
                )
            self._engine = open_engine(path, readonly=True)
            tables = set(inspect(self._engine).get_table_names())
            if "daemon_local_messages" not in tables or "daemon_state" not in tables:
                self._engine.dispose()
                raise DaemonError(
                    f"{path} predates the operator console; start (or upgrade and restart) "
                    "`sbxloop daemon` so the store is migrated"
                )
            version = self.get_value(SCHEMA_VERSION_KEY)
            if version is not None and version != SCHEMA_VERSION:
                self._engine.dispose()
                raise DaemonError(
                    f"{path} is at daemon schema {version}; this sbxloop expects "
                    f"{SCHEMA_VERSION} — upgrade the daemon and the console together"
                )
            log.debug("store.opened", db=str(path), readonly=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        _refuse_pre_1_0(path)
        self._engine = open_engine(path)
        ensure_schema(self._engine)
        self._backfill_queue_order()
        # Not part of the schema, and so not behind its version: a stamped
        # database still has to carry a prompt an older daemon wrote back
        # onto a gate row. See `carry_gate_prompts`.
        # `carry_gate_prompts` commits for itself, so it gets the connection
        # rather than a transaction wrapped around one that would then be
        # committed twice.
        raw = self._engine.raw_connection()
        try:
            driver = raw.driver_connection
            assert isinstance(driver, sqlite3.Connection)  # nosec B101 - SQLite is the only backend
            carry_gate_prompts(driver)
        finally:
            raw.close()
        log.debug("store.opened", db=str(path), schema=SCHEMA_VERSION)

    def close(self) -> None:
        self._engine.dispose()

    @contextmanager
    def _write(self, *, immediate: bool = False) -> Iterator[Session]:
        """A session that commits on the way out, under the store's lock.

        Every statement in the block is one transaction, so the pairs that
        have to move together — an item's state and its ledger row — either
        both land or neither does.
        """
        if immediate:
            with self._lock, begin_immediate(self._engine) as conn, Session(conn) as session:
                yield session
                session.flush()
            return
        with self._lock, Session(self._engine) as session:
            yield session
            session.commit()

    @contextmanager
    def _read(self) -> Iterator[Session]:
        """A session for a query, held under the lock for the same reason."""
        with self._lock, Session(self._engine) as session:
            yield session

    def backfill_repo(self, repo: str | None) -> int:
        """Give repo-less rows the daemon's sole configured repository.

        Rows written before multi-repo (or by a single-repo daemon) carry
        ``repo = ''``. They belong to whatever repository *that* daemon
        polled, which only the caller knows — so the daemon names it at
        startup rather than letting the first repository discovered claim
        them. ``UPDATE OR IGNORE``: a row that would collide with an already
        qualified row for the same issue is left as it is. Returns the number
        of rows updated.
        """
        if not repo:
            return 0
        updated = 0
        with self._write() as session:
            # A chat item (#760) has no repository by design, not by age.
            # Requester rows are the concierge's record of who asked for an
            # issue; a chat item carries its requester itself and writes none.
            statements = (
                update(WorkItemRow)
                .prefix_with("OR IGNORE")
                .where(WorkItemRow.repo == "", _not_local())
                .values(repo=repo),
                update(RequesterRow)
                .prefix_with("OR IGNORE")
                .where(RequesterRow.repo == "")
                .values(repo=repo),
            )
            for statement in statements:
                updated += _rowcount(session.execute(statement)) or 0
        if updated:
            log.info("store.repo_backfilled", repo=repo, rows=updated)
        return updated

    def attribute_repoless(self, repos: Sequence[str]) -> int:
        """Give repo-less rows the repository named by their issue URL.

        A row written before multi-repo carries ``repo = ''``, but its
        ``url`` (``https://github.com/<owner>/<name>/issues/<n>``) still
        names the repository it came from. When that repository is one of
        the configured ones the row can be attributed exactly, with no
        guessing — which matters most for rows discovery cannot re-create
        (see :meth:`drop_repoless`). ``UPDATE OR IGNORE``: a row colliding
        with an already qualified row for the same issue is left alone.
        Returns the number of rows updated.
        """
        known = {r.casefold(): r for r in repos if r}
        if not known:
            return 0
        updated = 0
        with self._write() as session:
            rows = session.execute(
                select(WorkItemRow.item_id, WorkItemRow.url).where(
                    WorkItemRow.repo == "", _not_local()
                )
            ).all()
            for item_id, url in rows:
                repo = known.get((_repo_from_url(url) or "").casefold())
                if repo is None:
                    continue
                updated += (
                    _rowcount(
                        session.execute(
                            update(WorkItemRow)
                            .prefix_with("OR IGNORE")
                            .where(WorkItemRow.item_id == item_id, WorkItemRow.repo == "")
                            .values(repo=repo)
                        )
                    )
                    or 0
                )
        if updated:
            log.info("store.repo_attributed_from_url", rows=updated)
        return updated

    def drop_repoless(self) -> int:
        """Discard the repo-less rows discovery can genuinely re-create.

        Called at startup when no single configured repository can own them
        (several repos configured, or the legacy ``[github] repo`` is gone)
        and their issue URL named no configured repository either
        (:meth:`attribute_repoless`). Leaving such a row would be worse than
        dropping it: a ``repo = ''`` row no longer dedups against the
        repo-qualified item discovery builds for the same issue, so the
        issue would be queued twice and the second copy would fail to claim
        its trigger label; and running the legacy row itself would route the
        clone/branch/PR to whichever repository happens to be first in the
        config.

        Only *untouched queue* rows are deleted: ``state = 'queued'`` with
        ``claimed = 0`` and no pinned run. Those are the only ones the next
        poll re-creates, because claiming swaps the trigger label for the
        in-progress one — once an item is claimed its issue no longer
        matches the discovery search and can never be rediscovered. Claimed,
        running and resume-pending repo-less rows are therefore left for
        :meth:`strand_repoless` to settle visibly instead of vanishing.
        Terminal rows are kept as history — they are never dispatched and
        never dedup against a live item (a changed terminal row is
        superseded). Requester notes are kept too: they are read with a
        repo-less fallback only for unqualified items, and are harmless
        otherwise. Returns the number of rows deleted.
        """
        where = (
            WorkItemRow.repo == "",
            WorkItemRow.state == "queued",
            WorkItemRow.claimed == 0,
            WorkItemRow.run_id.is_(None),
            _not_local(),
        )
        with self._write() as session:
            rows = list(session.scalars(select(WorkItemRow.item_id).where(*where)))
            if not rows:
                return 0
            session.execute(delete(WorkItemRow).where(*where))
        log.info(
            "store.repoless_items_dropped",
            rows=len(rows),
            items=[str(r) for r in rows],
            reason="no configured repository to attribute them to; they were never "
            "claimed, so discovery will re-create them repo-qualified",
        )
        return len(rows)

    def strand_repoless(self, reason: str, now: float) -> list[WorkItem]:
        """Settle the repo-less rows that must not simply vanish.

        Whatever :meth:`attribute_repoless` could not name and
        :meth:`drop_repoless` must not delete — claimed, running or
        resume-pending rows — is failed here with an explicit ``reason``, so
        the row stays visible in ``items`` and any pinned run reconciles
        against a real item rather than being force-failed as an orphan.

        No ``pending_report`` debt is left: these are exactly the rows whose
        repository could *not* be named, so a report would be posted against
        whichever repository the source happens to try first — a comment on
        an unrelated issue with the same number. The caller gets the items
        back instead and must raise an operator notice naming each id and
        issue URL, because their issue is left carrying
        ``sbxloop:in-progress`` for a human to clear.
        """
        with self._write() as session:
            rows = list(
                session.scalars(
                    select(WorkItemRow).where(
                        WorkItemRow.repo == "",
                        WorkItemRow.state.not_in(sorted(TERMINAL_ITEM_STATES)),
                    )
                )
            )
            stranded = [_row_to_item(row) for row in rows]
            stored_ids = [row.item_id for row in rows]
            for stored in stored_ids:
                # Bind the id *as stored*, through the same either-spelling
                # match every other mutator uses: ``_row_to_item`` normalises
                # ``gh:<n>`` to ``gh:issue:<n>``, and a pre-upgrade store is
                # exactly where the bare legacy key still lives. Matching on
                # the normalised id updated zero rows there, so the row was
                # reported settled while staying ``running`` (and was
                # re-stranded on every start).
                session.execute(
                    update(WorkItemRow)
                    .where(_id_where(stored))
                    .values(
                        state="failed",
                        last_error=reason[:2000],
                        pending_report=None,
                        updated_at=now,
                    )
                )
        if stranded:
            log.warning(
                "store.repoless_items_stranded",
                rows=len(stranded),
                items=[i.item_id for i in stranded],
                urls=[i.url for i in stranded],
                reason=reason,
            )
        return stranded

    # -- work items ----------------------------------------------------------

    @staticmethod
    def _allocate_queue_order(session: Session) -> tuple[int, int]:
        """Allocate under the caller's immediate transaction, including the
        durable high-water mark so deleting the newest item cannot reuse
        its admission number."""
        previous = session.scalar(
            select(DaemonStateRow.value).where(DaemonStateRow.key == "queue_enqueue_seq")
        )
        highest = session.scalar(select(func.max(WorkItemRow.enqueue_seq))) or 0
        sequence = max(int(previous or 0), highest) + 1
        order = (session.scalar(select(func.max(WorkItemRow.queue_order))) or 0) + 1
        statement = sqlite_insert(DaemonStateRow).values(
            key="queue_enqueue_seq", value=str(sequence)
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["key"], set_={"value": statement.excluded.value}
            )
        )
        return sequence, order

    def _backfill_queue_order(self) -> None:
        """An older release may have inserted rows during a rollback.
        Assign them once on the next writable open, retaining their FIFO.
        The migration itself handles every row present at first upgrade."""
        with self._write(immediate=True) as session:
            rows = list(
                session.scalars(
                    select(WorkItemRow)
                    .where(WorkItemRow.enqueue_seq.is_(None) | WorkItemRow.queue_order.is_(None))
                    .order_by(WorkItemRow.created_at.asc(), text("rowid ASC"))
                )
            )
            for row in rows:
                sequence, order = self._allocate_queue_order(session)
                session.execute(
                    update(WorkItemRow)
                    .where(WorkItemRow.item_id == row.item_id)
                    .values(
                        enqueue_seq=row.enqueue_seq if row.enqueue_seq is not None else sequence,
                        queue_order=row.queue_order if row.queue_order is not None else order,
                    )
                )

    def upsert_new(self, item: WorkItem, now: float) -> bool:
        """Record a discovered item; True if it is work to dispatch now.

        Discovery only ever hands us issues that *carry the trigger label*,
        so seeing one is a human asking for a run (#600). What happens to an
        existing row depends on where that row stands:

        * finished (:data:`TERMINAL_ITEM_STATES`) and the content changed —
          superseded: re-triggering an edited issue is a new work item;
        * finished and the content is unchanged — **re-queued in place**:
          state back to ``queued``, unclaimed, no pinned run, no stale
          error, attempts reset. Re-adding the label used to be silently
          inert here, which left an operator ``!sbx retry`` as the only way
          back in (issue #596). What the finished attempt pushed to origin
          is not lost: its run id, branch and PR are carried onto the
          re-queued row (``prior_*``, read back with :meth:`prior_attempt`)
          so the restart continues that branch instead of redoing it;
        * live (queued, claimed, running, resume-pending, gated) — left
          exactly as it is, so a poll never double-dispatches it.

        The requester the concierge recorded for the issue, if any, is
        copied onto the item.
        """
        with self._write(immediate=True) as session:
            repo = item.repo or ""
            # Identity is (issue, repository), matched exactly — with one
            # concession: an item whose id is *unqualified* can only come
            # from a single-repo daemon (discovery qualifies ids as soon as
            # a second repository is configured), so it may adopt a row
            # written before items carried a repo at all. A qualified item
            # never does: letting it claim a repo-less row would hand rows
            # from the old sole repository to whichever repository happens
            # to be polled first. Those rows are given their repository once,
            # by name, in :meth:`backfill_repo` at daemon startup — or, when
            # no single repository can own them, dropped there
            # (:meth:`drop_repoless`) so discovery re-creates them qualified,
            # once every row that could be attributed has been.
            parsed = try_parse_gh_id(item.item_id)
            adopt_legacy = bool(repo) and (parsed is None or parsed.repo is None)
            projection = select(
                WorkItemRow.item_id,
                WorkItemRow.state,
                WorkItemRow.title,
                WorkItemRow.body,
                WorkItemRow.repo,
                WorkItemRow.run_kind,
            ).where(WorkItemRow.source_key == item.source_key)
            if adopt_legacy:
                # The exact repository wins over the repo-less legacy row.
                found = projection.where(WorkItemRow.repo.in_((repo, ""))).order_by(
                    (WorkItemRow.repo == repo).desc()
                )
            else:
                found = projection.where(WorkItemRow.repo == repo)
            row = session.execute(found.limit(1)).first()
            if row is not None and row.repo == "" and repo:
                # Backfill: the item now knows its repository.
                session.execute(
                    update(WorkItemRow).where(WorkItemRow.item_id == row.item_id).values(repo=repo)
                )
            if row is not None:
                terminal = row.state in TERMINAL_ITEM_STATES
                # An issue re-labelled for the other kind of run (#760) is
                # new work, however familiar its words.
                changed = (row.title, row.body, row.run_kind or "code") != (
                    item.title,
                    item.body,
                    item.kind,
                )
                if not terminal:
                    return False
                if not changed:
                    self._requeue_terminal_row(session, str(row.item_id), str(row.state), now)
                    return True
                log.debug(
                    "store.item_superseded",
                    item=row.item_id,
                    previous_state=row.state,
                    reason="terminal row, content changed",
                )
                session.execute(delete(WorkItemRow).where(WorkItemRow.item_id == row.item_id))
            # Exact repository, plus — for the same reason as above — a note
            # written before notes carried one, but only for an unqualified
            # item (a single-repo daemon, where there is only one repository
            # the note can mean).
            note = select(RequesterRow.requester_id).where(
                RequesterRow.source_key == item.source_key
            )
            if repo and adopt_legacy:
                note = note.where(RequesterRow.repo.in_((repo, "")))
            elif repo:
                note = note.where(RequesterRow.repo == repo)
            # A qualified note wins over one written before notes carried a
            # repository.
            requester = session.scalars(
                note.order_by((RequesterRow.repo != "").desc()).limit(1)
            ).first()
            requested_by = (
                item.requested_by
                if item.requested_by
                else (str(requester) if requester is not None else None)
            )
            # A row created here is not necessarily a first-ever attempt:
            # every non-finish path deletes the row (a lost claim race, a
            # trigger label that went away, a repo-less row discovery
            # re-creates, a superseded terminal row), so the branch and PR
            # the last attempt pushed are recovered from the durable side
            # table — and failing that from the run history — instead of
            # being lost with the row (#600).
            item_id = self._free_item_id(session, normalize_item_id(item.item_id), repo)
            prior = self._recover_prior(session, item.source_key, repo, item_id)
            sequence, order = self._allocate_queue_order(session)
            session.execute(
                insert(WorkItemRow).values(
                    item_id=item_id,
                    source_key=item.source_key,
                    title=item.title,
                    body=item.body,
                    url=item.url,
                    state="queued",
                    attempts=0,
                    claimed=0,
                    run_id=None,
                    last_error=None,
                    created_at=now,
                    updated_at=now,
                    requested_by=requested_by,
                    repo=repo,
                    prior_run_id=prior.run_id if prior else None,
                    prior_branch=prior.branch if prior else None,
                    prior_pr_number=prior.pr_number if prior else None,
                    run_kind=item.kind,
                    profile=item.profile,
                    enqueue_seq=sequence,
                    queue_order=order,
                )
            )
            if prior is not None:
                log.info(
                    "store.prior_attempt_recovered",
                    item=item_id,
                    prior_run=prior.run_id,
                    prior_branch=prior.branch,
                    prior_pr=prior.pr_number,
                    reason="work item row was re-created; prior pushed work carried onto it",
                )
            return True

    @staticmethod
    def _free_item_id(session: Session, item_id: str, repo: str) -> str:
        """``item_id``, repo-qualified if the bare spelling belongs elsewhere.

        Item identity is ``(source_key, repo)`` — the table says so — but
        ``item_id`` is a global primary key, and a single-repo daemon mints
        the unqualified form (``gh:issue:24``) that encodes only the number.
        Those two facts collide the moment one number is used by two
        repositories, which is not a multi-repo daemon's problem alone: a
        daemon repointed at a new repository (``[github] repo``) leaves the
        old repository's rows behind under the same unqualified namespace.
        The insert then fails the primary key and, before this, took the
        whole daemon down with it on every tick.

        The caller has already established that this repository has no row
        for the issue, so a row squatting the bare id belongs to a different
        repository and the incoming item takes the qualified spelling
        instead. The common case — nothing there — keeps the bare id, so
        existing state, watches and operator commands resolve unchanged.

        Both unqualified spellings are checked, because both are squatters:
        the typed ``gh:issue:24`` would not fail the primary key against a
        legacy bare ``gh:24`` row, but :func:`_id_variants` resolves the two
        to each other, so leaving it unqualified would make one lookup match
        two repositories' rows.
        """
        parsed = try_parse_gh_id(item_id)
        if parsed is None or parsed.repo is not None or not repo:
            # Not a GitHub id, already qualified, or no repository to
            # qualify with: nothing to disambiguate it from.
            return item_id
        owner = session.scalars(
            select(WorkItemRow.repo).where(_id_where(item_id), WorkItemRow.repo != repo).limit(1)
        ).first()
        if owner is None:
            return item_id
        qualified = format_gh_id(parsed.kind, parsed.number, repo=repo)
        log.info(
            "store.item_id_qualified",
            item=qualified,
            bare=item_id,
            held_by=owner,
            reason="the unqualified id is held by another repository's row",
        )
        return qualified

    # -- prior attempts (durable across row deletion) -------------------------

    @staticmethod
    def _read_prior_side(session: Session, source_key: str, repo: str) -> PriorAttempt | None:
        """The recorded prior attempt for (issue, repository) from the side
        table, with the pre-multi-repo ``repo = ''`` row as a fallback.
        Runs on the caller's session, inside its transaction."""
        row = session.execute(
            select(PriorAttemptRow.run_id, PriorAttemptRow.branch, PriorAttemptRow.pr_number)
            .where(PriorAttemptRow.source_key == source_key, PriorAttemptRow.repo.in_((repo, "")))
            # The exact repository wins over the pre-multi-repo fallback.
            .order_by((PriorAttemptRow.repo == repo).desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        run_id, branch, pr = row
        if run_id is None and branch is None and pr is None:
            return None
        return PriorAttempt(
            run_id=run_id,
            branch=branch,
            pr_number=int(pr) if pr is not None else None,
        )

    @staticmethod
    def _write_prior_side(
        session: Session,
        source_key: str,
        repo: str,
        *,
        run_id: str | None,
        branch: str | None,
        pr_number: int | None,
    ) -> None:
        """Remember the prior attempt outside the work-item row, so deleting
        that row (discard, drop_repoless, supersede) cannot take it with it.
        Fields left None keep whatever is already recorded. Runs on the
        caller's session, inside its transaction."""
        if run_id is None and branch is None and pr_number is None:
            return
        stmt = sqlite_insert(PriorAttemptRow).values(
            source_key=source_key,
            repo=repo,
            run_id=run_id,
            branch=branch,
            pr_number=pr_number,
            updated_at=0,
        )
        # COALESCE, not assignment: a field this call left None keeps what
        # is already recorded rather than erasing it.
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["source_key", "repo"],
                set_={
                    "run_id": func.coalesce(stmt.excluded.run_id, PriorAttemptRow.run_id),
                    "branch": func.coalesce(stmt.excluded.branch, PriorAttemptRow.branch),
                    "pr_number": func.coalesce(stmt.excluded.pr_number, PriorAttemptRow.pr_number),
                },
            )
        )

    def _recover_prior(
        self, session: Session, source_key: str, repo: str, item_id: str
    ) -> PriorAttempt | None:
        """What a re-created row's item last pushed to origin: the side
        table first, then the engine's own run history for the same issue —
        the recovery :meth:`_requeue_terminal_row` does for a row written
        before the ``prior_*`` columns existed. Runs on the caller's
        session, inside its transaction."""
        prior = self._read_prior_side(session, source_key, repo)
        run_id = prior.run_id if prior else None
        branch = prior.branch if prior else None
        pr_number = prior.pr_number if prior else None
        if run_id is None:
            run_id = session.scalars(
                select(DaemonRunRow.run_id)
                .where(DaemonRunRow.item_id.in_(_id_variants(item_id)))
                .order_by(DaemonRunRow.started_at.desc())
                .limit(1)
            ).first()
        if run_id is None:
            return prior
        branch, pr_number = self._fill_artifacts(session, run_id, branch, pr_number)
        return PriorAttempt(run_id=run_id, branch=branch, pr_number=pr_number)

    def _fill_artifacts(
        self, session: Session, run_id: str, branch: str | None, pr_number: int | None
    ) -> tuple[str | None, int | None]:
        """Fill in whatever branch/PR is missing for ``run_id`` from the
        engine's run record and the merge-gate row. Runs on the caller's
        session, inside its transaction."""
        if branch is None or pr_number is None:
            recorded = self._run_artifacts(session, run_id)
            if recorded is not None:
                branch = branch or recorded[0]
                pr_number = pr_number if pr_number is not None else recorded[1]
        if branch is None or pr_number is None:
            gate = session.execute(
                select(MergeGateRow.branch, MergeGateRow.pr_number).where(
                    MergeGateRow.run_id == run_id
                )
            ).first()
            if gate is not None:
                branch = branch or gate.branch
                pr_number = pr_number if pr_number is not None else gate.pr_number
        return branch, int(pr_number) if pr_number is not None else None

    def _requeue_terminal_row(
        self, session: Session, item_id: str, previous_state: str, now: float
    ) -> None:
        """Put a finished row back in the queue because a human re-applied
        the trigger label (#600). Called with the lock held.

        The row keeps its identity, history and requester; only the
        dispatch bookkeeping is reset. Whatever the finished attempt left
        on origin is preserved in the ``prior_*`` columns — its own run id
        if it had one, otherwise the last run it was ever dispatched under
        — and an already recorded prior attempt is never overwritten with
        nothing.
        """
        prior_run = session.execute(
            select(
                WorkItemRow.source_key,
                WorkItemRow.repo,
                WorkItemRow.run_id,
                WorkItemRow.prior_run_id,
                WorkItemRow.prior_branch,
                WorkItemRow.prior_pr_number,
            ).where(WorkItemRow.item_id == item_id)
        ).first()
        source_key = str(prior_run.source_key) if prior_run else ""
        repo = str(prior_run.repo or "") if prior_run else ""
        # The side table is the durable copy: a row that was discarded and
        # re-created has no prior_* of its own to read (#600).
        side = self._read_prior_side(session, source_key, repo) if source_key else None
        run_id = (
            (prior_run.run_id if prior_run else None)
            or (prior_run.prior_run_id if prior_run else None)
            or (side.run_id if side else None)
        )
        if run_id is None:
            run_id = session.scalars(
                select(DaemonRunRow.run_id)
                .where(DaemonRunRow.item_id.in_(_id_variants(item_id)))
                .order_by(DaemonRunRow.started_at.desc())
                .limit(1)
            ).first()
        branch = (prior_run.prior_branch if prior_run else None) or (side.branch if side else None)
        pr_number = prior_run.prior_pr_number if prior_run else None
        if pr_number is None and side is not None:
            pr_number = side.pr_number
        if run_id is not None:
            # A row written before the prior_* columns existed has nothing
            # recorded, so what the attempt pushed is recovered from the
            # engine's own run record in the same state.db (#600).
            branch, pr_number = self._fill_artifacts(session, run_id, branch, pr_number)
        if source_key:
            self._write_prior_side(
                session, source_key, repo, run_id=run_id, branch=branch, pr_number=pr_number
            )
        session.execute(
            update(WorkItemRow)
            .where(WorkItemRow.item_id == item_id)
            .values(
                state="queued",
                claimed=0,
                run_id=None,
                last_error=None,
                attempts=0,
                not_before=None,
                claim_token=None,
                pending_report=None,
                prior_run_id=run_id,
                prior_branch=branch,
                prior_pr_number=pr_number,
                updated_at=now,
            )
        )
        log.info(
            "store.item_requeued_by_label",
            item=item_id,
            previous_state=previous_state,
            prior_run=run_id,
            prior_branch=branch,
            prior_pr=pr_number,
            reason="trigger label re-applied to an unchanged issue",
        )

    def _run_artifacts(self, session: Session, run_id: str) -> tuple[str | None, int | None] | None:
        """The branch and PR the engine recorded for ``run_id`` in the
        shared ``state.db``, or None when there is nothing to read: no
        ``runs`` table (a daemon store without an engine history yet), a
        pre-1.0 ``runs`` shape with no PR columns, or no such run. Never
        raises — a restart with no recoverable artifact just starts fresh.
        """
        # The engine's table, not one of ours, and possibly at a shape that
        # predates its PR columns — so it is introspected and read as text
        # rather than through a model this store does not own.
        #
        # Bound to the session's own connection, not to the engine. The pool
        # hands out one connection for the whole store, so an Inspector that
        # opened its "own" would get that same one and roll the caller's
        # open transaction back when it closed.
        inspector = inspect(session.connection())
        if "runs" not in inspector.get_table_names():
            return None
        columns = {c["name"] for c in inspector.get_columns("runs")}
        wanted = [c for c in ("branch", "pr_number") if c in columns]
        if not wanted:
            return None
        projection = ", ".join(wanted)
        try:
            row = session.execute(
                text(f"SELECT {projection} FROM runs WHERE run_id = :run_id"),  # nosec B608
                {"run_id": run_id},
            ).first()
        except SQLAlchemyError:
            return None
        if row is None:
            return None
        values = row._mapping
        branch = values.get("branch")
        pr = values.get("pr_number")
        return (str(branch) if branch else None, int(pr) if pr is not None else None)

    def prior_attempt(self, item_id: str) -> PriorAttempt | None:
        """What the item's previous attempt left on origin, if anything
        (#600): the run id, the branch it pushed and the PR it opened. None
        when the item has no recorded prior attempt."""
        with self._read() as session:
            row = session.execute(
                select(
                    WorkItemRow.prior_run_id,
                    WorkItemRow.prior_branch,
                    WorkItemRow.prior_pr_number,
                )
                .where(_id_where(item_id))
                .limit(1)
            ).first()
        if row is None:
            return None
        run_id, branch, pr = row
        if run_id is None and branch is None:
            return None
        return PriorAttempt(
            run_id=run_id,
            branch=branch,
            pr_number=int(pr) if pr is not None else None,
        )

    def record_prior_attempt(
        self,
        item_id: str,
        *,
        run_id: str | None = None,
        branch: str | None = None,
        pr_number: int | None = None,
    ) -> None:
        """Remember the branch/PR this attempt pushed to origin, so a later
        restart of the item can continue on it (#600). Fields left None keep
        whatever is already recorded.

        Written twice: onto the work-item row (what this item reads back
        directly) and into ``daemon_prior_attempts``, which outlives the row
        — every path that is not a finish deletes the row, and a restart
        that lost the branch with it would rebuild the work from scratch."""
        with self._write() as session:
            # COALESCE: a field this call left None keeps what is recorded.
            session.execute(
                update(WorkItemRow)
                .where(_id_where(item_id))
                .values(
                    prior_run_id=func.coalesce(run_id, WorkItemRow.prior_run_id),
                    prior_branch=func.coalesce(branch, WorkItemRow.prior_branch),
                    prior_pr_number=func.coalesce(pr_number, WorkItemRow.prior_pr_number),
                )
            )
            row = session.execute(
                select(WorkItemRow.source_key, WorkItemRow.repo).where(_id_where(item_id)).limit(1)
            ).first()
            if row is not None:
                self._write_prior_side(
                    session,
                    str(row.source_key),
                    str(row.repo or ""),
                    run_id=run_id,
                    branch=branch,
                    pr_number=pr_number,
                )

    def note_requester(
        self, source_key: str, requester_id: str, now: float, repo: str | None = None
    ) -> None:
        """Remember who asked for the issue ``source_key`` (the concierge's
        write path), so the work item discovery builds from it carries the
        requester and the run's finish can ping them. ``repo`` scopes the
        note: the same issue number in two repositories is two requests."""
        with self._write() as session:
            session.execute(
                insert(RequesterRow)
                .prefix_with("OR REPLACE")
                .values(
                    source_key=source_key,
                    repo=repo or "",
                    requester_id=requester_id,
                    created_at=now,
                )
            )

    def get(self, item_id: str) -> WorkItem | None:
        """Look the item up under either spelling of its id: a row stored
        with a legacy ``gh:1234`` key resolves for ``gh:issue:1234`` too."""
        with self._read() as session:
            row = session.scalars(select(WorkItemRow).where(_id_where(item_id))).first()
            return _row_to_item(row) if row else None

    def next_queued(self, now: float, backoff_s: float) -> WorkItem | None:
        """First item in the persisted order whose retry backoff elapsed.

        A queued item that still carries a ``run_id`` is an interrupted run
        awaiting resume (see :meth:`mark_resume_pending`): it was in flight
        when the previous process died, so it goes first and skips the
        retry backoff — that backoff spaces out *failed* attempts, and an
        interruption is not a failure."""
        for item in self.queued_in_order():
            if dispatch_eligible_at(item, backoff_s) <= now:
                return item
        return None

    def queued_in_order(self) -> list[WorkItem]:
        """Every queued item in the order :meth:`next_queued` considers them:
        interrupted runs awaiting resume first, then persisted queue order."""
        with self._read() as session:
            return [_row_to_item(row) for row in session.scalars(self._queued_query())]

    @staticmethod
    def _queued_query() -> Select[tuple[WorkItemRow]]:
        """One ordering rule for display, inspection and claim reservation."""
        return (
            select(WorkItemRow)
            .where(WorkItemRow.state == "queued")
            .order_by(
                WorkItemRow.run_id.is_(None).asc(),
                WorkItemRow.queue_order.asc(),
                WorkItemRow.enqueue_seq.asc(),
            )
        )

    def reserve_next_queued(self, now: float, backoff_s: float) -> WorkItem | None:
        """Select eligible work and reserve its claim in one transaction.

        A move either commits first and determines this selection, or sees
        the selected item's persisted token and refuses. Keeping the token
        on the item also uses the ordinary half-claim recovery if the daemon
        dies before posting its claim. Existing tokens, claim ownership and
        retry timestamps are unchanged.
        """
        with self._write(immediate=True) as session:
            for row in session.scalars(self._queued_query()):
                item = _row_to_item(row)
                if dispatch_eligible_at(item, backoff_s) > now:
                    continue
                if not item.claimed and not item.claim_token:
                    row.claim_token = uuid.uuid4().hex
                return _row_to_item(row)
            return None

    def run_items(self) -> dict[str, str]:
        """``run_id -> item_id`` for every run the daemon dispatched (the ledger)."""
        with self._read() as session:
            return {
                str(run_id): str(item_id)
                for run_id, item_id in session.execute(
                    select(DaemonRunRow.run_id, DaemonRunRow.item_id)
                )
            }

    def queued(self) -> list[WorkItem]:
        """The same order the scheduler considers, including pinned resumes."""
        return self.queued_in_order()

    def move_queued(
        self, item_id: str, *, before: str | None = None, after: str | None = None
    ) -> WorkItem:
        """Move unclaimed pending work relative to another pending item.

        Reuse the pending items' order slots so claimed work and pinned
        resumes keep their slots. Neither admission identity nor retry
        timestamps change. Validation and the permutation commit together.
        """
        if (before is None) == (after is None):
            raise ValueError("provide exactly one of before or after")
        anchor = before if before is not None else after
        assert anchor is not None
        with self._write(immediate=True) as session:
            moving = session.scalars(select(WorkItemRow).where(_id_where(item_id))).first()
            target = session.scalars(select(WorkItemRow).where(_id_where(anchor))).first()
            for key, row in ((item_id, moving), (anchor, target)):
                if row is None:
                    raise KeyError(f"unknown work item {key!r}")
                if row.state != "queued" or row.claimed or row.run_id or row.claim_token:
                    raise ValueError(
                        f"{key}: only unclaimed queued items without a pinned run can move"
                    )
            assert moving is not None and target is not None
            if moving.item_id == target.item_id:
                raise ValueError("an item cannot be moved relative to itself")
            pending = list(
                session.scalars(
                    select(WorkItemRow)
                    .where(
                        WorkItemRow.state == "queued",
                        WorkItemRow.claimed == 0,
                        WorkItemRow.run_id.is_(None),
                        WorkItemRow.claim_token.is_(None),
                    )
                    .order_by(WorkItemRow.queue_order.asc(), WorkItemRow.enqueue_seq.asc())
                )
            )
            slots = [row.queue_order for row in pending]
            pending.remove(moving)
            pending.insert(pending.index(target) + (after is not None), moving)
            for row, order in zip(pending, slots, strict=True):
                if row.queue_order != order:
                    session.execute(
                        update(WorkItemRow)
                        .where(WorkItemRow.item_id == row.item_id)
                        .values(queue_order=order)
                    )
            return _row_to_item(moving)

    def running_items(self) -> list[WorkItem]:
        with self._read() as session:
            return [
                _row_to_item(row)
                for row in session.scalars(
                    select(WorkItemRow).where(WorkItemRow.state == "running")
                )
            ]

    def items(self, states: Sequence[ItemState] | None = None) -> list[WorkItem]:
        """Every known item (optionally filtered by state), oldest first —
        the operator's view for ``sbxloop daemon items`` / ``!sbx items``."""
        stmt = select(WorkItemRow).order_by(WorkItemRow.created_at.asc(), text("rowid ASC"))
        if states:
            stmt = stmt.where(WorkItemRow.state.in_(list(states)))
        with self._read() as session:
            return [_row_to_item(row) for row in session.scalars(stmt)]

    # -- operator controls (#229) ------------------------------------------------

    def abandon(self, item_id: str, reason: str, now: float) -> WorkItem:
        """Operator abandon: queued/running/blocked/gated/awaiting_review/
        paused_review → failed. ``run_id`` is
        kept so the ledger and ``sbxloop logs`` still tie the item to the run
        that made the operator give up on it; the loop treats "abandoned
        while pinned to my run" as its cue to cancel that run. The source is
        owed a report (``pending_report``): whoever delivers it — the loop's
        settle path, recovery, or the tick sweep after a row-only CLI
        abandon — clears the debt."""
        return self._transition(
            item_id,
            now,
            ("queued", "running", "blocked", "gated", "awaiting_review", "paused_review"),
            lambda item: f"{item_id} is already {item.state}",
            state="failed",
            last_error=reason[:2000],
            pending_report="abandoned",
        )

    def retry(self, item_id: str, now: float, reason: str | None = None) -> WorkItem:
        """Operator retry: a failed, blocked, cancelled or backoff-waiting
        item goes back to the queue as if freshly discovered. The attempt
        budget and retry backoff exist to bound *autonomous* churn, so a
        human decision starts both over — attempts reset, eligible on the
        next tick (the daily run cap still applies) — and the run is
        unpinned so the next dispatch plans from scratch instead of
        resuming the plan that failed (#228, #254). The source-side claim is
        kept: re-claiming would fail (the trigger label moved when it was
        first claimed). ``reason`` (e.g. "re-queued by X") is kept as
        ``last_error`` for the audit trail in ``items``; without one the old
        error is cleared. The source is owed a ``requeued`` report
        (re-claim, drop the failed/blocked label)."""
        return self._transition(
            item_id,
            now,
            ("failed", "blocked", "cancelled", "queued"),
            lambda item: (
                f"{item_id} is {item.state}; only failed, blocked, cancelled or queued items "
                "can be retried"
                + (" (abandon it first)" if item.state == "running" else "")
                + (
                    f" — its PR is waiting for a review; `resume {item_id}` keeps that PR "
                    "and checks it now, `abandon` gives it up"
                    if item.state in REVIEW_WAIT_STATES
                    else ""
                )
            ),
            state="queued",
            attempts=0,
            run_id=None,
            last_error=reason[:2000] if reason else None,
            pending_report="requeued",
        )

    def requeue(self, item_id: str, now: float) -> WorkItem:
        """Operator requeue: a running (or queued) item loses its pinned run
        and goes back to the queue with its attempt count intact — the
        next dispatch starts a fresh run rather than resuming. The source
        hears nothing: the item is still claimed and still work."""
        return self._transition(
            item_id,
            now,
            ("running", "queued"),
            lambda item: (
                f"{item_id} is {item.state}; only running or queued items can be requeued"
                + (" (use retry)" if item.state in ("failed", "blocked", "cancelled") else "")
            ),
            state="queued",
            run_id=None,
        )

    def _transition(
        self,
        item_id: str,
        now: float,
        allowed: tuple[str, ...],
        refuse: Callable[[WorkItem], str],
        **fields: object,
    ) -> WorkItem:
        """One conditional statement (``WHERE state IN (...)``) instead of a
        read-then-write: the CLI runs in another process, so a daemon that
        settles the item as done between the two must not have its verdict
        overwritten by a command issued on stale state. No row updated →
        re-read and refuse with the state that is actually there."""
        values = _item_values(fields, updated_at=now)
        item_id = normalize_item_id(item_id)
        with self._lock:
            with self._write() as session:
                result = session.execute(
                    update(WorkItemRow)
                    .where(_id_where(item_id), WorkItemRow.state.in_(allowed))
                    .values(**values)
                )
                changed = _rowcount(result)
            if changed == 1:
                fresh = self._require(item_id)
                log.debug("store.transition", item=item_id, state=fresh.state, **_loggable(fields))
                return fresh
            current = self._require(item_id)
            log.debug(
                "store.transition_refused",
                item=item_id,
                state=current.state,
                allowed=list(allowed),
                wanted=_loggable(fields),
            )
            raise ValueError(refuse(current))

    def pending_reports(self) -> list[WorkItem]:
        """Items whose decision the source has not been told about yet
        (an operator's abandon / retry, or a run's merged / blocked outcome
        whose report did not land), oldest decision first."""
        with self._read() as session:
            return [
                _row_to_item(row)
                for row in session.scalars(
                    select(WorkItemRow)
                    .where(WorkItemRow.pending_report.is_not(None))
                    .order_by(WorkItemRow.updated_at.asc(), text("rowid ASC"))
                )
            ]

    def take_pending_report(self, item_id: str) -> bool:
        """Claim the item's owed report for delivery; False if there was
        none (or another thread already took it). Not ``_update``: this is
        not an item change and must not move ``updated_at``, the
        retry-backoff clock."""
        with self._write() as session:
            result = session.execute(
                update(WorkItemRow)
                .where(_id_where(item_id), WorkItemRow.pending_report.is_not(None))
                .values(pending_report=None)
            )
            return _rowcount(result) == 1

    def _require(self, item_id: str) -> WorkItem:
        item = self.get(item_id)
        if item is None:
            raise KeyError(f"unknown work item {item_id!r}")
        return item

    def mark_claiming(self, item_id: str, token: str, now: float) -> None:
        """The claim is about to be posted under ``token`` (#530). Written
        first, so a process that dies after the comment lands and before
        :meth:`mark_claimed` leaves a row :meth:`half_claimed` can settle."""
        self._update(item_id, now, claim_token=token, claimed=0)

    def mark_claimed(self, item_id: str, now: float) -> None:
        self._update(item_id, now, claimed=1)

    def clear_claim(self, item_id: str, now: float) -> None:
        """The half-claim never reached the source: back to unclaimed, so
        the next tick claims properly."""
        self._update(item_id, now, claim_token=None, claimed=0)

    def half_claimed(self) -> list[WorkItem]:
        """Queued rows whose claim was started (token written) but never
        completed — the shape a crash between comment and persist leaves."""
        with self._read() as session:
            return [
                _row_to_item(row)
                for row in session.scalars(
                    select(WorkItemRow)
                    .where(
                        WorkItemRow.state == "queued",
                        WorkItemRow.claimed == 0,
                        WorkItemRow.claim_token.is_not(None),
                    )
                    .order_by(WorkItemRow.created_at, text("rowid"))
                )
            ]

    def discard(self, item_id: str) -> bool:
        """Forget a queued item that is not ours to run — a claim another
        daemon won, an issue that closed, a trigger label that went away.
        Never a terminal state: ``failed`` is what discovery dedups against,
        and it is what made a lost claim race permanent (#530). If the
        trigger label comes back the next poll re-creates the row."""
        with self._write() as session:
            result = session.execute(
                delete(WorkItemRow).where(_id_where(item_id), WorkItemRow.state == "queued")
            )
            dropped = _rowcount(result) == 1
        log.debug("store.discard", item=normalize_item_id(item_id), dropped=dropped)
        return dropped

    def mark_running(self, item_id: str, run_id: str, now: float) -> None:
        """Move to running, count the attempt, and open the ledger row —
        all before the engine starts, so a crash still leaves the item→run
        link for recovery."""
        stored = item_id
        item_id = normalize_item_id(item_id)
        # One transaction: the item's move and the ledger row land together
        # or not at all. An unknown item must not leave an orphan ledger row
        # that the daily cap would count, and raising inside the block is
        # what rolls the whole thing back.
        with self._write() as session:
            result = session.execute(
                update(WorkItemRow)
                .where(_id_where(stored))
                .values(
                    state="running",
                    attempts=WorkItemRow.attempts + 1,
                    run_id=run_id,
                    updated_at=now,
                )
            )
            if _rowcount(result) != 1:
                raise KeyError(f"unknown work item {item_id!r}")
            session.execute(
                insert(DaemonRunRow)
                .prefix_with("OR REPLACE")
                .values(run_id=run_id, item_id=item_id, started_at=now)
            )

    def mark_resume_pending(self, item_id: str, now: float) -> None:
        """Recovery found the item's run interrupted mid-flight: back to
        the queue with the run pinned, so the next tick resumes it through
        the same breaker/cap/pause gate a fresh dispatch faces (#254) —
        recovery itself never starts engines."""
        self._update(item_id, now, state="queued")

    def mark_resuming(self, item_id: str, run_id: str, now: float) -> None:
        """Resume the pinned run: back to running (the attempt count is
        unchanged — a resume is the same attempt) and record the resume in
        its own ledger so the daily cap and the per-item resume budget see
        it. ``daemon_runs`` is keyed by run id, so a second segment of the
        same run cannot be a second row there."""
        stored = item_id
        item_id = normalize_item_id(item_id)
        # One transaction, for the same reason as `mark_running`.
        with self._write() as session:
            result = session.execute(
                update(WorkItemRow)
                .where(_id_where(stored), WorkItemRow.run_id == run_id)
                .values(state="running", not_before=None, updated_at=now)
            )
            if _rowcount(result) != 1:
                raise KeyError(f"work item {item_id!r} does not carry run {run_id!r}")
            session.execute(
                insert(RunResumeRow).values(run_id=run_id, item_id=item_id, resumed_at=now)
            )

    def mark_done(
        self, item_id: str, now: float, *, pending_report: PendingReport | None = None
    ) -> None:
        """The run merged its PR. ``pending_report="merged"`` records the
        issue close as a debt the tick pays (and retries) — a GitHub hiccup
        on the close must not leave the issue open with no one to close it."""
        self._update(item_id, now, state="done", last_error=None, pending_report=pending_report)

    def mark_blocked(self, item_id: str, reason: str, now: float) -> None:
        """The run cleared its own bar but GitHub would not finish the PR;
        a human has to look. Terminal for the daemon, ``retry``-able by
        hand; the source is owed the blocked report."""
        self._update(
            item_id, now, state="blocked", last_error=reason[:2000], pending_report="blocked"
        )

    def mark_gated(
        self, item_id: str, now: float, *, pending_report: PendingReport = "gated"
    ) -> None:
        """The run parked behind the opt-in merge gate: a waiting state, not
        a terminal one — the run stays pinned, dispatch never sees it, and
        ``approve_merge`` or ``abandon`` resolves it. The source is owed the
        park announcement (label + how-to comment) — ``held`` for a
        workload parked at publishing (#760), which shares the state."""
        self._update(item_id, now, state="gated", last_error=None, pending_report=pending_report)

    def mark_awaiting_review(self, item_id: str, now: float) -> None:
        """The run parked for an approving review (#675): a waiting state
        like ``gated`` — run pinned, invisible to dispatch — ended by the
        review poll, ``resume`` or ``abandon``. The source hears nothing:
        the issue is still in progress."""
        self._update(item_id, now, state="awaiting_review", last_error=None, not_before=None)

    def mark_paused_review(self, item_id: str, reason: str, now: float) -> None:
        """The review wait ran past ``[landing] review_wait_s`` (#675): the
        run stays pinned and nothing polls until ``resume <item>``."""
        self._update(item_id, now, state="paused_review", last_error=reason[:2000])

    def resume_for_fix(self, item_id: str, run_id: str, now: float, reason: str) -> WorkItem:
        """A reviewer requested changes on a parked PR (#675): the item goes
        back to the queue with its run *pinned* and no backoff, so the next
        tick resumes it at the landing stage and the fix round runs. The
        attempt count is untouched — a resume is the same attempt."""
        item = self._require(item_id)
        if item.run_id != run_id:
            raise ValueError(f"{item_id} is not pinned to run {run_id} (its run is {item.run_id})")
        return self._transition(
            item_id,
            now,
            ("awaiting_review", "paused_review"),
            lambda it: f"{item_id} is {it.state}; only a review-wait item resumes for a fix",
            state="queued",
            not_before=None,
            last_error=reason[:2000],
        )

    def resume_for_release(self, item_id: str, run_id: str, now: float, by: str) -> WorkItem:
        """A held workload was released (#760): the item goes back to the
        queue with its run *pinned* and no backoff, so the next tick
        resumes it at the publishing stage. Same attempt, as a fix resume."""
        item = self._require(item_id)
        if item.run_id != run_id:
            raise ValueError(f"{item_id} is not pinned to run {run_id} (its run is {item.run_id})")
        return self._transition(
            item_id,
            now,
            ("gated",),
            lambda it: f"{item_id} is {it.state}; only a held item resumes for a release",
            state="queued",
            not_before=None,
            last_error=f"released by {by}"[:2000],
        )

    def mark_failed(self, item_id: str, error: str, now: float, *, requeue: bool) -> None:
        # A requeued item must not keep its run pinned: queued + run_id
        # means "resume this run", and a failed run is dispatched fresh. A
        # failed item keeps it for forensics.
        fields: dict[str, object] = {
            "state": "queued" if requeue else "failed",
            "last_error": error[:2000],
            "not_before": None,
        }
        if requeue:
            fields["run_id"] = None
        self._update(item_id, now, **fields)

    def mark_exhausted(self, item_id: str, error: str, now: float, *, not_before: float) -> None:
        """The run stopped one fix round short (#523): back to the queue with
        the run *pinned* — the next dispatch resumes it on its own branch and
        PR — but not before ``not_before``, the retry backoff. The attempt
        count is untouched: a resume is the same attempt."""
        self._update(item_id, now, state="queued", last_error=error[:2000], not_before=not_before)

    def resume_exhausted(self, item_id: str, run_id: str, now: float, reason: str) -> WorkItem:
        """Operator ``grant-rounds``: an exhausted item — failed and handed
        over, or queued and waiting out its backoff — resumes its pinned run
        on the next tick. A failed item owes the source a ``requeued`` report
        (drop the failed label, re-claim); a queued one owes nothing new."""
        item = self._require(item_id)
        if item.run_id != run_id:
            raise ValueError(f"{item_id} is not pinned to run {run_id} (its run is {item.run_id})")
        fields: dict[str, object] = {
            "state": "queued",
            "not_before": None,
            "last_error": reason[:2000],
        }
        if item.state == "failed":
            fields["pending_report"] = "requeued"
        return self._transition(
            item_id,
            now,
            ("failed", "queued"),
            lambda it: (
                f"{item_id} is {it.state}; only a failed or queued item can be granted rounds"
            ),
            **fields,
        )

    def mark_cancelled(self, item_id: str, reason: str, now: float) -> None:
        """Operator cancel: terminal for the daemon, unlike ``mark_failed``
        which either re-queues (with backoff) or fails."""
        self._update(item_id, now, state="cancelled", last_error=reason[:2000])

    def mark_requeued_unstarted(self, item_id: str, now: float) -> None:
        """Crash between claim and start: back to the queue, claim kept."""
        self._update(item_id, now, state="queued", run_id=None)

    def _update(self, item_id: str, now: float, **fields: object) -> None:
        values = _item_values(fields, updated_at=now)
        item_id = normalize_item_id(item_id)
        with self._write() as session:
            session.execute(update(WorkItemRow).where(_id_where(item_id)).values(**values))
        log.debug("store.update", item=item_id, **_loggable(fields))

    def set_state(self, item_id: str, state: ItemState, now: float) -> None:
        self._update(item_id, now, state=state)

    # -- run ledger ------------------------------------------------------------

    def runs_started_since(self, ts: float) -> int:
        """Fresh starts plus resumes in the window: each resume spends a
        full engine wall clock, so the daily cap counts it (#254/#234)."""
        with self._read() as session:
            started = (
                select(func.count())
                .select_from(DaemonRunRow)
                .where(DaemonRunRow.started_at >= ts)
                .scalar_subquery()
            )
            resumed = (
                select(func.count())
                .select_from(RunResumeRow)
                .where(RunResumeRow.resumed_at >= ts)
                .scalar_subquery()
            )
            return int(session.scalar(select(started + resumed)) or 0)

    def resumes_since(self, ts: float) -> int:
        with self._read() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(RunResumeRow)
                    .where(RunResumeRow.resumed_at >= ts)
                )
                or 0
            )

    def resumes_for_item(self, item_id: str) -> int:
        """Resumes across ALL of the item's runs: the budget bounds total
        effort per item, not per plan."""
        with self._read() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(RunResumeRow)
                    .where(_id_where(item_id, RunResumeRow.item_id))
                )
                or 0
            )

    def unsettled_runs(self) -> list[tuple[str, str]]:
        """``(run_id, item_id)`` for every ledger row the loop never closed
        with a verdict: still open (the process died mid-run) or closed as
        ``interrupted`` (a clean shutdown expecting to resume). Recovery
        uses it to notice an item that an operator abandoned/requeued
        *offline* — the row-only CLI cannot report to the source or clean
        up the dead run's sandboxes, and the item is no longer ``running``
        so the ordinary reconciliation never sees it (#229)."""
        with self._read() as session:
            rows = session.execute(
                select(DaemonRunRow.run_id, DaemonRunRow.item_id)
                .where(DaemonRunRow.finished_at.is_(None) | (DaemonRunRow.result == "interrupted"))
                .order_by(DaemonRunRow.started_at.asc())
            )
            return [(str(run_id), normalize_item_id(str(item_id))) for run_id, item_id in rows]

    def finished_run_ids(self, run_ids: Sequence[str]) -> set[str]:
        """Which of these runs already have a ledger `finished_at`. Used at
        Discord watch reload to drop watches for runs that completed while
        the daemon was down: the `run_finished` event that would have
        pinged them has already fired, so reviving the entry would just
        leave it waiting for an event that will never come again."""
        if not run_ids:
            return set()
        with self._read() as session:
            return {
                str(r)
                for r in session.scalars(
                    select(DaemonRunRow.run_id).where(
                        DaemonRunRow.run_id.in_(list(run_ids)),
                        DaemonRunRow.finished_at.is_not(None),
                    )
                )
            }

    def finish_ledger(self, run_id: str, result: str, now: float) -> None:
        with self._write() as session:
            session.execute(
                update(DaemonRunRow)
                .where(DaemonRunRow.run_id == run_id)
                .values(finished_at=now, result=result)
            )
        log.debug("store.ledger_closed", run=run_id, result=result)

    def runs_for_item(self, item_id: str) -> list[str]:
        """Run ids the item has been dispatched under, oldest first."""
        with self._read() as session:
            return [
                str(r)
                for r in session.scalars(
                    select(DaemonRunRow.run_id)
                    .where(_id_where(item_id, DaemonRunRow.item_id))
                    .order_by(DaemonRunRow.started_at)
                )
            ]

    # -- schedules (#761, #818) ----------------------------------------------------

    def schedules(self) -> list[StoredSchedule]:
        """Every schedule the daemon fires, by name: the rows that carry a
        spec. State-only rows (a removed schedule's, or a pre-#818
        daemon's) are not schedules."""
        with self._read() as session:
            rows = list(
                session.scalars(
                    select(ScheduleRowModel)
                    .where(ScheduleRowModel.ask.is_not(None))
                    .order_by(ScheduleRowModel.name)
                )
            )
            out: list[StoredSchedule] = []
            for row in rows:
                try:
                    spec = _row_to_spec(row)
                except ValueError:
                    log.warning("store.schedule_unreadable", schedule=str(row.name), exc_info=True)
                    continue
                out.append(
                    StoredSchedule(
                        spec,
                        str(row.source or "config"),
                        None if row.created_by is None else str(row.created_by),
                        None if row.created_at is None else float(row.created_at),
                    )
                )
        return out

    def schedule(self, name: str) -> StoredSchedule | None:
        """The schedule called ``name``, None when there is none."""
        return next((s for s in self.schedules() if s.spec.name == name), None)

    def add_schedule(
        self, spec: ScheduleConfig, *, source: str, by: str | None, now: float
    ) -> bool:
        """Persist a schedule. A name already carrying a spec is left as it
        is (False); a state-only row of that name — a schedule removed
        earlier, or one a pre-#818 daemon anchored — keeps its grid and
        takes the spec. The grid of a new schedule anchors at ``now``."""
        # The check and the write are one transaction: two callers adding
        # the same name must not both see "no spec yet".
        with self._lock, begin_immediate(self._engine) as conn:
            row = conn.execute(
                select(ScheduleRowModel.ask).where(ScheduleRowModel.name == spec.name)
            ).first()
            if row is not None and row.ask is not None:
                return False
            if row is None:
                conn.execute(insert(ScheduleRowModel).values(name=spec.name, anchor=now))
            conn.execute(
                update(ScheduleRowModel)
                .where(ScheduleRowModel.name == spec.name)
                .values(
                    profile=spec.profile,
                    ask=spec.ask,
                    every=spec.every,
                    cron=spec.cron,
                    timezone=spec.timezone,
                    source=source,
                    created_by=by,
                    created_at=now,
                )
            )
            return True

    def remove_schedule(self, name: str) -> bool:
        """Forget a schedule, state and all: a schedule re-added under the
        same name starts a fresh grid. False when there was none."""
        with self._write() as session:
            result = session.execute(
                delete(ScheduleRowModel).where(
                    ScheduleRowModel.name == name, ScheduleRowModel.ask.is_not(None)
                )
            )
            return _rowcount(result) == 1

    def schedule_row(self, name: str, now: float) -> ScheduleRow:
        """The schedule's row, created at ``now`` on first sight — the
        anchor of its grid."""
        with self._write() as session:
            session.execute(
                insert(ScheduleRowModel).prefix_with("OR IGNORE").values(name=name, anchor=now)
            )
            session.flush()
            row = session.scalars(
                select(ScheduleRowModel).where(ScheduleRowModel.name == name)
            ).one()
            return _row_to_schedule(row)

    def schedule_rows(self) -> dict[str, ScheduleRow]:
        """Every schedule row by name, whether or not still configured."""
        with self._read() as session:
            return {
                str(r.name): _row_to_schedule(r)
                for r in session.scalars(select(ScheduleRowModel).order_by(ScheduleRowModel.name))
            }

    def schedule_due_handled(self, name: str, due: float) -> None:
        """Record that the tick due at ``due`` was dealt with without
        queueing anything (skipped, or swallowed while paused)."""
        with self._write() as session:
            session.execute(
                update(ScheduleRowModel).where(ScheduleRowModel.name == name).values(last_due=due)
            )

    def schedule_fired(self, name: str, due: float, item_id: str, now: float) -> None:
        """Record the fire for the tick due at ``due``: the item it queued
        and when."""
        with self._write() as session:
            session.execute(
                update(ScheduleRowModel)
                .where(ScheduleRowModel.name == name)
                .values(last_due=due, last_fired_at=now, last_item=item_id)
            )

    def set_schedule_paused(self, name: str, by: str | None, now: float) -> bool:
        """Park (``by`` a name) or release (``by`` None) the schedule.
        False when it was already so."""
        with self._write() as session:
            row = session.execute(
                select(ScheduleRowModel.paused_by).where(ScheduleRowModel.name == name)
            ).first()
            if row is None or (row.paused_by is None) == (by is None):
                return False
            session.execute(
                update(ScheduleRowModel)
                .where(ScheduleRowModel.name == name)
                .values(paused_by=by, paused_at=now if by is not None else None)
            )
            return True

    def live_schedule_item(self, name: str) -> WorkItem | None:
        """The tick of ``name`` still in flight — queued, running or parked
        behind a gate or a review — None when the last one has finished."""
        # `_` is a LIKE wildcard and a legal name character.
        pattern = SCHED_PREFIX + name.replace("\\", "\\\\").replace("_", "\\_") + ":%"
        with self._read() as session:
            row = session.scalars(
                select(WorkItemRow)
                .where(
                    WorkItemRow.item_id.like(pattern, escape="\\"),
                    WorkItemRow.state.not_in(list(TERMINAL_ITEM_STATES)),
                )
                .order_by(WorkItemRow.created_at.desc(), text("rowid DESC"))
                .limit(1)
            ).first()
            return _row_to_item(row) if row else None

    # -- generic daemon state ---------------------------------------------------

    def get_value(self, key: str) -> str | None:
        """One ``daemon_state`` value (small process-level facts such as the
        concierge's SDK session id)."""
        with self._read() as session:
            value = session.scalars(
                select(DaemonStateRow.value).where(DaemonStateRow.key == key)
            ).first()
            return None if value is None else str(value)

    def set_value(self, key: str, value: str | None) -> None:
        """Set (or, with ``None``, delete) one ``daemon_state`` value."""
        with self._write() as session:
            if value is None:
                session.execute(delete(DaemonStateRow).where(DaemonStateRow.key == key))
            else:
                session.execute(
                    insert(DaemonStateRow).prefix_with("OR REPLACE").values(key=key, value=value)
                )

    # -- pending clarifications (ask, never block) ----------------------------

    @staticmethod
    def _clarification(row: PendingClarificationRow) -> PendingClarification:
        return PendingClarification(
            id=int(row.id),
            backend=str(row.backend),
            channel_id=None if row.channel_id is None else str(row.channel_id),
            asker_id=None if row.asker_id is None else str(row.asker_id),
            asker_name=None if row.asker_name is None else str(row.asker_name),
            question=str(row.question),
            assumption=str(row.assumption),
            deadline=float(row.deadline),
            created_at=float(row.created_at),
            state=str(row.state),
        )

    def create_pending_clarification(
        self,
        *,
        backend: str,
        channel_id: str | None,
        asker_id: str | None,
        asker_name: str | None,
        question: str,
        assumption: str,
        deadline: float,
        now: float,
    ) -> int | None:
        """Persist one filing-blocking ask's fallback; None over the cap
        (the question still posts — only the auto-file is shed)."""
        with self._write() as session:
            # The cap bounds what one bridge's sweeper will ever fire; rows a
            # backend nobody runs any more left behind do not count against
            # the one that does.
            open_count = session.scalar(
                select(func.count())
                .select_from(PendingClarificationRow)
                .where(
                    PendingClarificationRow.state == "open",
                    PendingClarificationRow.backend == backend,
                )
            )
            if int(open_count or 0) >= PENDING_CLARIFICATION_CAP:
                log.warning("store.clarification_cap", cap=PENDING_CLARIFICATION_CAP)
                return None
            result = session.execute(
                insert(PendingClarificationRow).values(
                    backend=backend,
                    channel_id=_text_or_none(channel_id),
                    asker_id=_text_or_none(asker_id),
                    asker_name=_text_or_none(asker_name),
                    question=question,
                    assumption=assumption,
                    deadline=deadline,
                    created_at=now,
                )
            )
            return _inserted_id(result)

    def take_due_clarifications(
        self, now: float, backend: str | None = None
    ) -> list[PendingClarification]:
        """Claim every open ask past its deadline (CAS ``open`` -> ``firing``
        per row), so two sweepers — or a sweep racing a restart — never fire
        the same ask twice. Every bridge sweeps its own ``backend``; a bare
        sweep takes them all."""
        stmt = (
            select(PendingClarificationRow)
            .where(
                PendingClarificationRow.state == "open",
                PendingClarificationRow.deadline <= now,
            )
            .order_by(PendingClarificationRow.deadline)
        )
        if backend is not None:
            stmt = stmt.where(PendingClarificationRow.backend == backend)
        with self._write() as session:
            rows = list(session.scalars(stmt))
            taken: list[PendingClarification] = []
            for row in rows:
                # CAS per row: the claim is the state move, so a second
                # sweeper reading the same row loses it here.
                claimed = _row_claimed(
                    session,
                    update(PendingClarificationRow)
                    .where(
                        PendingClarificationRow.id == int(row.id),
                        PendingClarificationRow.state == "open",
                    )
                    .values(state="firing"),
                )
                if claimed:
                    taken.append(self._clarification(row))
            return taken

    def resolve_pending_clarification(self, clar_id: int, state: str, now: float) -> None:
        with self._write() as session:
            session.execute(
                update(PendingClarificationRow)
                .where(PendingClarificationRow.id == clar_id)
                .values(state=state, resolved_at=now)
            )

    def resolve_open_clarifications_for(
        self, asker_id: str, channel_id: str | None, now: float
    ) -> int:
        """Any engagement from the asker settles their open asks (scoped to
        the surface it happened on when that is known): the concierge
        handles the actual words in-session, so the fallback stands down."""
        stmt = (
            update(PendingClarificationRow)
            .where(
                PendingClarificationRow.state == "open",
                PendingClarificationRow.asker_id == asker_id,
            )
            .values(state="answered", resolved_at=now)
        )
        if channel_id:
            stmt = stmt.where(
                PendingClarificationRow.channel_id.is_(None)
                | (PendingClarificationRow.channel_id == str(channel_id))
            )
        with self._write() as session:
            return int(_rowcount(session.execute(stmt)))

    def open_clarifications(self, backend: str | None = None) -> list[PendingClarification]:
        stmt = (
            select(PendingClarificationRow)
            .where(PendingClarificationRow.state == "open")
            .order_by(PendingClarificationRow.deadline)
        )
        if backend is not None:
            stmt = stmt.where(PendingClarificationRow.backend == backend)
        with self._read() as session:
            return [self._clarification(row) for row in session.scalars(stmt)]

    def values_with_prefix(self, prefix: str) -> dict[str, str]:
        """Every ``daemon_state`` value whose key starts with ``prefix``."""
        pattern = prefix.replace("%", "\\%").replace("_", "\\_") + "%"
        with self._read() as session:
            rows = session.execute(
                select(DaemonStateRow.key, DaemonStateRow.value).where(
                    DaemonStateRow.key.like(pattern, escape="\\")
                )
            )
            return {str(k): str(v) for k, v in rows if v is not None}

    def clear_prefix(self, prefix: str) -> int:
        """Delete every ``daemon_state`` value whose key starts with ``prefix``."""
        pattern = prefix.replace("%", "\\%").replace("_", "\\_") + "%"
        with self._write() as session:
            return int(
                _rowcount(
                    session.execute(
                        delete(DaemonStateRow).where(DaemonStateRow.key.like(pattern, escape="\\"))
                    )
                )
            )

    def item_for_run(self, run_id: str) -> str | None:
        """The work item a run was dispatched for (ledger lookup)."""
        with self._read() as session:
            item_id = session.scalars(
                select(DaemonRunRow.item_id).where(DaemonRunRow.run_id == run_id)
            ).first()
            return None if item_id is None else normalize_item_id(str(item_id))

    # -- merge gates ([landing] merge_gate) --------------------------------------

    def create_merge_gate(
        self,
        run_id: str,
        item_id: str,
        repo: str,
        pr_number: int,
        pr_url: str,
        branch: str | None,
        notify_ids: Sequence[str],
        custom_id: str,
        now: float,
        *,
        kind: str = "merge",
    ) -> None:
        """Persist a parked merge — or, ``kind="publish"``, a workload held
        at publishing (#760; ``pr_number`` 0). ``INSERT OR IGNORE``: a
        recovery re-settle of the same run must not clobber the standing
        gate (or a decision already taken on it)."""
        with self._write() as session:
            session.execute(
                insert(MergeGateRow)
                .prefix_with("OR IGNORE")
                .values(
                    run_id=run_id,
                    item_id=normalize_item_id(item_id),
                    repo=repo,
                    pr_number=pr_number,
                    pr_url=pr_url,
                    branch=branch,
                    notify_ids=json.dumps(list(notify_ids)),
                    custom_id=custom_id,
                    state="open",
                    created_at=now,
                    kind=kind,
                )
            )
        log.info("store.merge_gate_created", run=run_id, item=item_id, pr=pr_number, kind=kind)

    def merge_gate_for(self, target: str) -> MergeGate | None:
        """The gate for a run id or an item id (either spelling)."""
        with self._read() as session:
            row = session.scalars(select(MergeGateRow).where(MergeGateRow.run_id == target)).first()
            if row is None:
                row = session.scalars(
                    select(MergeGateRow)
                    .where(_id_where(target, MergeGateRow.item_id))
                    .order_by(MergeGateRow.created_at.desc())
                    .limit(1)
                ).first()
            return _row_to_gate(row) if row else None

    def open_merge_gates(self) -> list[MergeGate]:
        """Gates a restart must re-arm: standing (open) and interrupted
        mid-approval (approving)."""
        with self._read() as session:
            return [
                _row_to_gate(row)
                for row in session.scalars(
                    select(MergeGateRow)
                    .where(MergeGateRow.state.in_(OPEN_GATE_STATES))
                    .order_by(MergeGateRow.created_at)
                )
            ]

    def claim_merge_gate(self, run_id: str, by: str | None = None) -> bool:
        """CAS ``open → approving``: exactly one click/command wins; a
        double-click loses here instead of double-merging. ``by`` records
        who won, for the resolution that follows to name."""
        with self._write() as session:
            result = session.execute(
                update(MergeGateRow)
                .where(MergeGateRow.run_id == run_id, MergeGateRow.state == "open")
                .values(
                    state="approving",
                    resolved_by=func.coalesce(by, MergeGateRow.resolved_by),
                )
            )
            return _rowcount(result) == 1

    def reopen_merge_gate(self, run_id: str, detail: str | None = None) -> None:
        """A failed or interrupted approval puts the gate back up; the
        prompt (and button) work again."""
        with self._write() as session:
            session.execute(
                update(MergeGateRow)
                .where(
                    MergeGateRow.run_id == run_id,
                    MergeGateRow.state.in_(OPEN_GATE_STATES),
                )
                .values(state="open", detail=detail)
            )
        log.info("store.merge_gate_reopened", run=run_id, detail=detail)

    def resolve_merge_gate(
        self,
        run_id: str,
        state: str,
        by: str | None,
        now: float,
        detail: str | None = None,
    ) -> None:
        """Close the gate: ``merged`` (approved and landed), ``released``
        (a held result published, #760) or ``dismissed`` (abandoned / PR
        closed)."""
        with self._write() as session:
            session.execute(
                update(MergeGateRow)
                .where(MergeGateRow.run_id == run_id)
                .values(state=state, resolved_at=now, resolved_by=by, detail=detail)
            )
        log.info("store.merge_gate_resolved", run=run_id, state=state, by=by)

    def set_gate_prompt(
        self,
        run_id: str,
        channel_id: str | None,
        message_id: str | None,
        *,
        backend: str,
    ) -> None:
        """Where ``backend``'s prompt lives, so a restart can find/refresh
        it; an empty message id forgets it."""
        with self._write() as session:
            if not message_id:
                session.execute(
                    delete(GatePromptRow).where(
                        GatePromptRow.run_id == run_id, GatePromptRow.backend == backend
                    )
                )
            else:
                session.execute(
                    insert(GatePromptRow)
                    .prefix_with("OR REPLACE")
                    .values(
                        run_id=run_id,
                        backend=backend,
                        channel_id=_text_or_none(channel_id),
                        message_id=str(message_id),
                    )
                )

    def gate_prompt(self, run_id: str, backend: str) -> tuple[str | None, str] | None:
        """``(channel_id, message_id)`` of ``backend``'s prompt for the gate."""
        with self._read() as session:
            row = session.execute(
                select(GatePromptRow.channel_id, GatePromptRow.message_id).where(
                    GatePromptRow.run_id == run_id, GatePromptRow.backend == backend
                )
            ).first()
            if row is None or not row.message_id:
                return None
            return (_text_or_none(row.channel_id), str(row.message_id))

    # -- review holds (#675) -----------------------------------------------------

    def create_review_hold(
        self,
        run_id: str,
        item_id: str,
        repo: str,
        pr_number: int,
        pr_url: str,
        branch: str | None,
        *,
        login: str,
        is_bot: bool | None,
        approvals_required: int,
        notify_ids: Sequence[str],
        now: float,
        next_poll_at: float,
        held_by_draft: bool = False,
    ) -> None:
        """Persist a review wait, its first poll due at ``next_poll_at`` (the
        run just looked: nothing to see yet). A run that parks again after
        a fix round re-opens its own row (the wait starts over; the notify
        list is the fresh one, and so is what it waits for — a review, or
        the PR marked ready, #677); a decision already taken on a finished
        row stands."""
        stmt = sqlite_insert(ReviewHoldRow).values(
            run_id=run_id,
            item_id=normalize_item_id(item_id),
            repo=repo,
            pr_number=pr_number,
            pr_url=pr_url,
            branch=branch,
            login=login,
            is_bot=None if is_bot is None else int(is_bot),
            approvals_required=approvals_required,
            held_by_draft=int(held_by_draft),
            notify_ids=json.dumps(list(notify_ids)),
            state="open",
            created_at=now,
            since_at=now,
            next_poll_at=next_poll_at,
            polls=0,
        )
        # The upsert is gated on the existing state: a decision already
        # taken on a finished row stands, and only an unfinished wait is
        # re-opened.
        with self._write() as session:
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["run_id"],
                    set_={
                        "state": "open",
                        "since_at": stmt.excluded.since_at,
                        "next_poll_at": stmt.excluded.next_poll_at,
                        "notify_ids": stmt.excluded.notify_ids,
                        "approvals_required": stmt.excluded.approvals_required,
                        "held_by_draft": stmt.excluded.held_by_draft,
                        "login": stmt.excluded.login,
                        "is_bot": stmt.excluded.is_bot,
                        "detail": None,
                        "resolved_at": None,
                        "resolved_by": None,
                    },
                    where=ReviewHoldRow.state.in_(("open", "fixing", "paused", "approving")),
                )
            )
        log.info("store.review_hold_created", run=run_id, item=item_id, pr=pr_number)

    def review_hold_for(self, target: str) -> ReviewHold | None:
        """The hold for a run id or an item id (either spelling); the newest
        when an item had several runs."""
        with self._read() as session:
            row = session.scalars(
                select(ReviewHoldRow).where(ReviewHoldRow.run_id == target)
            ).first()
            if row is None:
                row = session.scalars(
                    select(ReviewHoldRow)
                    .where(_id_where(target, ReviewHoldRow.item_id))
                    .order_by(ReviewHoldRow.created_at.desc())
                    .limit(1)
                ).first()
            return _row_to_hold(row) if row else None

    def review_holds(self, states: Sequence[str] = ("open",)) -> list[ReviewHold]:
        """Holds in ``states``, oldest first."""
        with self._read() as session:
            return [
                _row_to_hold(row)
                for row in session.scalars(
                    select(ReviewHoldRow)
                    .where(ReviewHoldRow.state.in_(list(states)))
                    .order_by(ReviewHoldRow.created_at)
                )
            ]

    def due_review_holds(self, now: float) -> list[ReviewHold]:
        """The open holds whose next poll is due."""
        with self._read() as session:
            return [
                _row_to_hold(row)
                for row in session.scalars(
                    select(ReviewHoldRow)
                    .where(ReviewHoldRow.state == "open", ReviewHoldRow.next_poll_at <= now)
                    .order_by(ReviewHoldRow.next_poll_at)
                )
            ]

    def review_hold_polled(self, run_id: str, next_poll_at: float) -> None:
        with self._write() as session:
            session.execute(
                update(ReviewHoldRow)
                .where(ReviewHoldRow.run_id == run_id)
                .values(polls=ReviewHoldRow.polls + 1, next_poll_at=next_poll_at)
            )

    def claim_review_hold(self, run_id: str, state: str) -> bool:
        """CAS ``open → approving|fixing``: the poll that saw the review
        acts on it exactly once."""
        with self._write() as session:
            result = session.execute(
                update(ReviewHoldRow)
                .where(ReviewHoldRow.run_id == run_id, ReviewHoldRow.state == "open")
                .values(state=state)
            )
            return _rowcount(result) == 1

    def reopen_review_hold(
        self,
        run_id: str,
        now: float,
        detail: str | None = None,
        *,
        restart: bool = False,
        held_by_draft: bool | None = None,
        approvals_required: int | None = None,
    ) -> bool:
        """Put the wait back up from any unfinished state: a failed landing,
        an interrupted one, or a pause an operator ends. ``restart`` begins
        the wait over (``since_at``); the next poll is due at once. A
        landing that parked again for a different reason (#677: a draft
        hold lifted, then the base wanted a review — or the reverse)
        passes what the wait is for now."""
        with self._write() as session:
            result = session.execute(
                update(ReviewHoldRow)
                .where(
                    ReviewHoldRow.run_id == run_id,
                    ReviewHoldRow.state.in_(("open", "approving", "fixing", "paused")),
                )
                .values(
                    state="open",
                    detail=detail,
                    next_poll_at=now,
                    # `restart` begins the wait over; otherwise the clock
                    # the hold has already been running on is kept.
                    since_at=now if restart else ReviewHoldRow.since_at,
                    held_by_draft=func.coalesce(
                        None if held_by_draft is None else int(held_by_draft),
                        ReviewHoldRow.held_by_draft,
                    ),
                    approvals_required=func.coalesce(
                        approvals_required, ReviewHoldRow.approvals_required
                    ),
                )
            )
            reopened = _rowcount(result) == 1
        log.info("store.review_hold_reopened", run=run_id, detail=detail, restart=restart)
        return reopened

    def pause_review_hold(self, run_id: str, now: float, detail: str) -> None:
        with self._write() as session:
            session.execute(
                update(ReviewHoldRow)
                .where(ReviewHoldRow.run_id == run_id, ReviewHoldRow.state == "open")
                .values(state="paused", detail=detail)
            )
        log.info("store.review_hold_paused", run=run_id, detail=detail)

    def resolve_review_hold(
        self, run_id: str, state: str, by: str | None, now: float, detail: str | None = None
    ) -> None:
        """End the wait: ``merged`` or ``dismissed``."""
        with self._write() as session:
            session.execute(
                update(ReviewHoldRow)
                .where(ReviewHoldRow.run_id == run_id)
                .values(state=state, resolved_at=now, resolved_by=by, detail=detail)
            )
        log.info("store.review_hold_resolved", run=run_id, state=state, by=by)

    # -- circuit breaker ---------------------------------------------------------

    def breaker(self) -> tuple[float | None, int]:
        """(opened_at, consecutive_failures) as last persisted. Kept in the
        db rather than on the loop object so a crash-restart cycle cannot
        reset the breaker (#254)."""
        with self._read() as session:
            rows = {
                str(k): v
                for k, v in session.execute(
                    select(DaemonStateRow.key, DaemonStateRow.value).where(
                        DaemonStateRow.key.in_(("breaker_opened_at", "consecutive_failures"))
                    )
                )
            }
        return parse_breaker(rows.get("breaker_opened_at"), rows.get("consecutive_failures"))

    def set_breaker(self, opened_at: float | None, consecutive_failures: int) -> None:
        log.debug(
            "store.breaker",
            open=opened_at is not None,
            consecutive_failures=consecutive_failures,
        )
        with self._write() as session:
            # Both keys in one statement and one transaction: a breaker that
            # opened without its failure count is not a state worth having.
            session.execute(
                insert(DaemonStateRow).prefix_with("OR REPLACE"),
                [
                    {
                        "key": "breaker_opened_at",
                        "value": "" if opened_at is None else repr(float(opened_at)),
                    },
                    {
                        "key": "consecutive_failures",
                        "value": str(int(consecutive_failures)),
                    },
                ],
            )

    # -- chat threads ----------------------------------------------------------

    def chat_thread(self, run_id: str, backend: str | None = None) -> ChatThread | None:
        """The run's thread on ``backend``; with none named, the external
        backend's — what a link in prose points at. A bridge names its
        own backend; the local console's thread is never the bare answer,
        since an external bridge cannot spell a pointer to it."""
        stmt = select(ChatThreadRow).where(ChatThreadRow.run_id == run_id)
        if backend is None:
            stmt = (
                stmt.where(ChatThreadRow.backend != "local")
                .order_by(ChatThreadRow.backend)
                .limit(1)
            )
        else:
            stmt = stmt.where(ChatThreadRow.backend == backend)
        with self._read() as session:
            row = session.scalars(stmt).first()
            return _row_to_chat_thread(row) if row else None

    def chat_threads(self, backend: str) -> list[tuple[str, ChatThread]]:
        """Every run's thread on ``backend``, newest headline first."""
        with self._read() as session:
            rows = session.scalars(
                select(ChatThreadRow)
                .where(ChatThreadRow.backend == backend)
                # Snowflakes sort by magnitude, not lexically.
                .order_by(
                    func.cast(ChatThreadRow.headline_id, Integer).desc(), ChatThreadRow.run_id
                )
            )
            return [(str(r.run_id), _row_to_chat_thread(r)) for r in rows]

    def set_chat_status_id(
        self, run_id: str, status_id: str | None, *, backend: str | None = None
    ) -> None:
        """Record the status line's message on ``backend``'s thread — with
        none named, on the thread :meth:`chat_thread` would return."""
        with self._lock:
            if backend is None:
                known = self.chat_thread(run_id)
                if known is None:
                    return
                backend = known.backend
            with self._write() as session:
                session.execute(
                    update(ChatThreadRow)
                    .where(ChatThreadRow.run_id == run_id, ChatThreadRow.backend == backend)
                    .values(status_id=_text_or_none(status_id))
                )

    def run_for_thread(self, thread_id: str | int, backend: str | None = None) -> str | None:
        """The run whose thread this is; scoped to ``backend`` when given,
        since the local bridge's ids and a snowflake share no namespace."""
        stmt = select(ChatThreadRow.run_id).where(ChatThreadRow.thread_id == str(thread_id))
        if backend is not None:
            stmt = stmt.where(ChatThreadRow.backend == backend)
        with self._read() as session:
            found = session.scalars(stmt).first()
            return str(found) if found else None

    def record_chat_thread(
        self,
        run_id: str,
        channel_id: str,
        thread_id: str,
        headline_id: str | None,
        *,
        backend: str = "discord",
    ) -> None:
        with self._write() as session:
            session.execute(
                insert(ChatThreadRow)
                .prefix_with("OR REPLACE")
                .values(
                    run_id=run_id,
                    backend=backend,
                    channel_id=str(channel_id),
                    thread_id=str(thread_id),
                    headline_id=_text_or_none(headline_id),
                )
            )

    # The Discord view of the same rows: snowflakes as integers. What the
    # Discord bridge's tests and the concierge's Discord fixtures speak.

    def discord_thread(self, run_id: str) -> DiscordThread | None:
        known = self.chat_thread(run_id)
        if known is None:
            return None
        return DiscordThread(
            int(known.channel_id),
            int(known.thread_id),
            _int_or_none(known.headline_id),
            _int_or_none(known.status_id),
        )

    def set_discord_status_id(self, run_id: str, status_id: int | None) -> None:
        self.set_chat_status_id(run_id, None if status_id is None else str(status_id))

    def record_discord_thread(
        self, run_id: str, channel_id: int, thread_id: int, headline_id: int | None
    ) -> None:
        self.record_chat_thread(
            run_id,
            str(channel_id),
            str(thread_id),
            None if headline_id is None else str(headline_id),
            backend="discord",
        )

    # -- run watches -----------------------------------------------------------

    # Watches belong to the bridge that registered them: only that bridge
    # can @mention the watcher, so each keeps its own rows per run.

    def add_run_watch(self, run_id: str, watcher_id: str, now: float, *, backend: str) -> None:
        """Register interest in a run's completion; idempotent per watcher."""
        with self._write() as session:
            session.execute(
                insert(RunWatchRow)
                .prefix_with("OR IGNORE")
                .values(run_id=run_id, watcher_id=watcher_id, created_at=now, backend=backend)
            )

    def run_watchers(self, run_id: str, backend: str | None = None) -> list[str]:
        """The run's watchers on ``backend`` — every backend's when none is
        named (a gate's or hold's notify list addresses them all)."""
        stmt = (
            select(RunWatchRow.watcher_id)
            .where(RunWatchRow.run_id == run_id)
            .order_by(text("rowid"))
        )
        if backend is not None:
            stmt = stmt.where(RunWatchRow.backend == backend)
        with self._read() as session:
            return [str(r) for r in session.scalars(stmt)]

    def take_run_watchers(self, run_id: str, backend: str) -> list[str]:
        """Return the run's watchers on ``backend`` and clear them in one
        transaction."""
        # Read and clear in one transaction, so a watcher cannot be
        # notified twice or lost between the two statements.
        with self._write() as session:
            watchers = [
                str(r)
                for r in session.scalars(
                    select(RunWatchRow.watcher_id)
                    .where(RunWatchRow.run_id == run_id, RunWatchRow.backend == backend)
                    .order_by(text("rowid"))
                )
            ]
            session.execute(
                delete(RunWatchRow).where(
                    RunWatchRow.run_id == run_id, RunWatchRow.backend == backend
                )
            )
            return watchers

    def all_run_watches(self, backend: str) -> dict[str, list[str]]:
        """Every pending watch on ``backend``, for reloading the bridge
        registry at startup."""
        with self._read() as session:
            watches: dict[str, list[str]] = {}
            for run_id, watcher_id in session.execute(
                select(RunWatchRow.run_id, RunWatchRow.watcher_id)
                .where(RunWatchRow.backend == backend)
                .order_by(text("rowid"))
            ):
                watches.setdefault(str(run_id), []).append(str(watcher_id))
            return watches

    def clear_run_watch(self, run_id: str, backend: str | None = None) -> None:
        """Drop a run's watch row without returning it — used by the bridge's
        `_evict_watch` when an entry is dropped for a reason other than a
        normal finish (a `WATCHERS_CAP` trim, or reconciling a reload
        against a run that already finished while the daemon was down),
        where `take_run_watchers`'s return value would just be discarded."""
        stmt = delete(RunWatchRow).where(RunWatchRow.run_id == run_id)
        if backend is not None:
            stmt = stmt.where(RunWatchRow.backend == backend)
        with self._write() as session:
            session.execute(stmt)

    # -- the local bridge's mailbox ---------------------------------------------

    def local_post(
        self,
        channel_id: str,
        text: str,
        *,
        now: float,
        direction: str = "out",
        kind: str = "message",
        embed_json: str | None = None,
        choices_json: str | None = None,
        gate_run_id: str | None = None,
        reply_to_id: int | None = None,
        mention_users: bool = False,
        author_id: str = "sbx",
        author_name: str = "sbx",
    ) -> int:
        """Append one row and return its id — the daemon's own posts, and
        (``direction="in"``) what a console typed or clicked."""
        with self._write() as session:
            result = session.execute(
                insert(LocalMessageRow).values(
                    direction=direction,
                    channel_id=channel_id,
                    kind=kind,
                    text=text,
                    embed_json=embed_json,
                    choices_json=choices_json,
                    gate_run_id=gate_run_id,
                    reply_to_id=reply_to_id,
                    mention_users=int(mention_users),
                    author_id=author_id,
                    author_name=author_name,
                    created_at=now,
                    updated_at=now,
                )
            )
            return _inserted_id(result)

    def local_edit(
        self,
        message_id: int,
        text: str,
        *,
        now: float,
        embed_json: str | None = None,
        choices_json: str | None = None,
    ) -> bool:
        """Rewrite one of the daemon's rows in place; False when there is no
        such outbound row. ``embed_json`` / ``choices_json`` replace the
        stored ones only when given."""
        values: dict[str, object] = {"text": text, "edited_at": now, "updated_at": now}
        # Only what was given replaces what is stored.
        if embed_json is not None:
            values["embed_json"] = embed_json
        if choices_json is not None:
            values["choices_json"] = choices_json
        with self._write() as session:
            result = session.execute(
                update(LocalMessageRow)
                .where(LocalMessageRow.id == message_id, LocalMessageRow.direction == "out")
                .values(**values)
            )
            return _rowcount(result) == 1

    def local_react(self, message_id: int, emoji: str, *, now: float) -> bool:
        """Add the daemon's reaction to a row (idempotent); False when the
        row does not exist."""
        with self._write() as session:
            stored = session.execute(
                select(LocalMessageRow.reactions_json).where(LocalMessageRow.id == message_id)
            ).first()
            if stored is None:
                return False
            try:
                reactions = [str(r) for r in json.loads(stored.reactions_json or "[]")]
            except ValueError:
                reactions = []
            if emoji not in reactions:
                reactions.append(emoji)
                session.execute(
                    update(LocalMessageRow)
                    .where(LocalMessageRow.id == message_id)
                    .values(reactions_json=json.dumps(reactions), updated_at=now)
                )
            return True

    def local_clear_gate(self, message_id: int, *, now: float) -> None:
        """The gate prompt is resolved: the row no longer offers approval."""
        with self._write() as session:
            session.execute(
                update(LocalMessageRow)
                .where(LocalMessageRow.id == message_id)
                .values(gate_run_id=None, updated_at=now)
            )

    def local_message(self, message_id: int) -> LocalMessage | None:
        with self._read() as session:
            found = _local_messages(
                session, _local_message_select().where(LocalMessageRow.id == message_id)
            )
            return found[0] if found else None

    def local_messages(
        self, channel_id: str, *, after_id: int = 0, limit: int = 500
    ) -> list[LocalMessage]:
        """A channel's transcript after ``after_id``, oldest first."""
        with self._read() as session:
            return _local_messages(
                session,
                _local_message_select()
                .where(LocalMessageRow.channel_id == channel_id, LocalMessageRow.id > after_id)
                .order_by(LocalMessageRow.id)
                .limit(limit),
            )

    def take_local_inbound(self, now: float, *, limit: int = 50) -> list[LocalMessage]:
        """Claim the oldest unclaimed inbound rows (CAS on ``taken_at`` per
        row, so a restart racing a poll never handles one twice)."""
        with self._write() as session:
            rows = _local_messages(
                session,
                _local_message_select()
                .where(LocalMessageRow.direction == "in", LocalMessageRow.taken_at.is_(None))
                .order_by(LocalMessageRow.id)
                .limit(limit),
            )
            taken: list[LocalMessage] = []
            for row in rows:
                # CAS on `taken_at`: the claim is the write, so a restart
                # racing a poll never handles the same row twice.
                if _row_claimed(
                    session,
                    update(LocalMessageRow)
                    .where(
                        LocalMessageRow.id == int(row.id),
                        LocalMessageRow.taken_at.is_(None),
                    )
                    .values(taken_at=now, updated_at=now),
                ):
                    taken.append(row)
            return taken

    def local_changed_since(
        self, channel_id: str, *, after_id: int, since: float
    ) -> list[LocalMessage]:
        """Rows in the channel up to ``after_id`` that changed since
        ``since`` — an edit, a reaction, a claim, a resolved gate — what a
        console repaints in place beside the rows it has not seen."""
        with self._read() as session:
            # The (channel_id, updated_at) index answers this in the usual
            # case of nothing changed; the id bound is checked per hit.
            return _local_messages(
                session,
                _local_message_select()
                .with_hint(LocalMessageRow, "INDEXED BY idx_local_messages_updated")
                .where(
                    LocalMessageRow.channel_id == channel_id,
                    LocalMessageRow.updated_at > since,
                    LocalMessageRow.id <= after_id,
                )
                .order_by(LocalMessageRow.id),
            )

    def local_latest_ids(self) -> dict[str, int]:
        """The newest row id per channel (unread counts)."""
        with self._read() as session:
            rows = session.execute(
                select(
                    LocalMessageRow.channel_id, func.max(LocalMessageRow.id).label("id")
                ).group_by(LocalMessageRow.channel_id)
            )
            return {str(channel_id): int(newest) for channel_id, newest in rows}

    def local_count_after(self, channel_id: str, after_id: int) -> int:
        """How many rows the channel has beyond ``after_id`` (an unread count)."""
        with self._read() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(LocalMessageRow)
                    .where(
                        LocalMessageRow.channel_id == channel_id,
                        LocalMessageRow.id > after_id,
                    )
                )
                or 0
            )

    def local_taken(self, message_ids: Sequence[int]) -> set[int]:
        """Which of the given inbound rows the daemon has claimed."""
        if not message_ids:
            return set()
        with self._read() as session:
            return {
                int(r)
                for r in session.scalars(
                    select(LocalMessageRow.id).where(
                        LocalMessageRow.id.in_(list(message_ids)),
                        LocalMessageRow.taken_at.is_not(None),
                    )
                )
            }

    def prune_local_messages(self, older_than: float) -> int:
        """Drop rows created before ``older_than``, keeping every prompt of
        a gate still open — it has no deadline, and the console's approve
        button is that row — and every row a thread is anchored on (its
        headline and status line), which the bridge still edits."""
        open_gates = select(MergeGateRow.run_id).where(MergeGateRow.state.in_(OPEN_GATE_STATES))

        def anchored(column: InstrumentedAttribute[str | None]) -> Select[Any]:
            """Ids a local thread is anchored on: its headline, its status line."""
            return select(column).where(ChatThreadRow.backend == "local", column.is_not(None))

        with self._write() as session:
            result = session.execute(
                delete(LocalMessageRow).where(
                    LocalMessageRow.created_at < older_than,
                    LocalMessageRow.gate_run_id.is_(None)
                    | LocalMessageRow.gate_run_id.not_in(open_gates),
                    func.cast(LocalMessageRow.id, Text).not_in(anchored(ChatThreadRow.headline_id)),
                    func.cast(LocalMessageRow.id, Text).not_in(anchored(ChatThreadRow.status_id)),
                )
            )
            return int(_rowcount(result))

    def set_local_heartbeat(self, now: float) -> None:
        """The local bridge is alive: what the console reads to say whether
        a daemon is listening without a ctl round trip."""
        with self._write() as session:
            session.execute(
                insert(DaemonStateRow)
                .prefix_with("OR REPLACE")
                .values(key=LOCAL_HEARTBEAT_KEY, value=repr(float(now)))
            )


#: Public names for the row shapers the console's read-only handle reuses
#: (`sbxloop.daemon.mailbox`), so it never re-derives a row's fields.
row_to_item = _row_to_item
row_to_gate = _row_to_gate
row_to_hold = _row_to_hold
row_to_local_message = _row_to_local_message


def _text_or_none(value: object) -> str | None:
    return None if value is None else str(value)


def _int_or_none(value: str | None) -> int | None:
    return None if value is None else int(value)


def apply_item_verb(
    dstore: DaemonStore, verb: str, item_id: str, *, now: float, by: str
) -> WorkItem:
    """The operator's row-only ``abandon`` / ``retry`` / ``requeue`` — what
    ``sbxloop daemon <verb>`` and the console do when no daemon is up to
    take the ctl verb. ``KeyError`` for an unknown item, ``ValueError``
    with the store's reason for a refused transition."""
    item_id = normalize_item_id(item_id)
    if verb == "abandon":
        return dstore.abandon(item_id, f"abandoned by {by}", now)
    if verb == "retry":
        return dstore.retry(item_id, now, f"re-queued by {by}")
    if verb == "requeue":
        return dstore.requeue(item_id, now)
    raise ValueError(f"unknown item verb {verb!r}")
