"""The daemon's fourteen tables: the queue, the ledger, and what it is holding.

The same rules as :mod:`sbxloop.db.engine_models` apply — this describes the
schema that is on disk rather than the one anyone would design now, because
a deployed ``state.db`` has to keep opening (#539). ``REAL`` rather than
``Float``, and ``nullable=True`` on the primary keys the hand-written DDL
declared inline without ``NOT NULL``, for the reasons documented there.

Three shapes here are worth knowing:

* **Work items are keyed twice.** ``item_id`` is the primary key, but
  ``UNIQUE(source_key, repo)`` is the one that matters: without ``repo`` in
  it, two configured repositories with an issue of the same number collide.
  That constraint is why the multi-repo upgrade had to rebuild the table —
  SQLite cannot drop a constraint with ``ALTER``.
* **Chat state is keyed by backend.** A run can have a thread on Discord or
  Slack *and* one on the operator console's local bridge at the same time,
  so ``daemon_chat_threads`` and ``daemon_gate_prompts`` are keyed
  ``(run_id, backend)`` and ``daemon_run_watches`` carries ``backend`` in
  its unique key.
* **JSON lives in TEXT columns**, serialised in Python: ``notify_ids`` on
  both hold tables, ``reactions_json`` on local messages. ``embed_json`` and
  ``choices_json`` are opaque even to the store — the bridge layer owns
  their contents.
"""

from __future__ import annotations

from sqlalchemy import (
    REAL,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from sbxloop.db.base import Base


class WorkItemRow(Base):
    """One thing the daemon has been asked to do, and how far it has got.

    ``item_id`` is a typed id (``gh:issue:12``, ``chat:<message id>``).
    Rows written before typed ids carry the bare ``gh:12`` form and are
    matched by either spelling on read — parsing is lenient, rendering is
    strict — so no migration of existing rows was ever needed.
    """

    __tablename__ = "daemon_work_items"
    __table_args__ = (
        # The key that actually guards correctness; see the module docstring.
        UniqueConstraint("source_key", "repo"),
        Index("idx_daemon_items_state", "state", "created_at"),
    )

    item_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    source_key: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="''")
    url: Mapped[str] = mapped_column(Text, nullable=False, server_default="''")
    state: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    claimed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    run_id: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)
    pending_report: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str | None] = mapped_column(Text)
    # Empty for a row written before multi-repo; settled at daemon startup
    # rather than claimed by whichever repository is polled first.
    repo: Mapped[str] = mapped_column(Text, nullable=False, server_default="''")
    # Earliest the item may be dispatched — a scheduled retry's backoff.
    not_before: Mapped[float | None] = mapped_column(REAL)
    # Held across the two steps of a claim, so a daemon killed mid-claim
    # does not orphan the issue.
    claim_token: Mapped[str | None] = mapped_column(Text)
    # What a previous attempt left behind, so a retry reuses its branch and
    # pull request instead of opening a second one.
    prior_run_id: Mapped[str | None] = mapped_column(Text)
    prior_branch: Mapped[str | None] = mapped_column(Text)
    prior_pr_number: Mapped[int | None] = mapped_column(Integer)
    run_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="'code'")
    profile: Mapped[str | None] = mapped_column(Text)
    entrygraph_target: Mapped[str | None] = mapped_column(Text)


class DaemonRunRow(Base):
    """The ledger: one row per run the daemon started, and how it ended."""

    __tablename__ = "daemon_runs"
    __table_args__ = (Index("idx_daemon_runs_started", "started_at"),)

    run_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    item_id: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[float] = mapped_column(REAL, nullable=False)
    finished_at: Mapped[float | None] = mapped_column(REAL)
    result: Mapped[str | None] = mapped_column(Text)


class RunResumeRow(Base):
    """One resume of one run. Counted against the per-item resume budget."""

    __tablename__ = "daemon_run_resumes"
    __table_args__ = (
        Index("idx_daemon_resumes_at", "resumed_at"),
        Index("idx_daemon_resumes_item", "item_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    item_id: Mapped[str] = mapped_column(Text, nullable=False)
    resumed_at: Mapped[float] = mapped_column(REAL, nullable=False)


class DaemonStateRow(Base):
    """Key/value for everything that is not worth a table of its own.

    The schema version the console handshakes on, the circuit breaker, the
    per-repository health the doctor reads, the local bridge's heartbeat.
    """

    __tablename__ = "daemon_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    value: Mapped[str | None] = mapped_column(Text)


class RunWatchRow(Base):
    """Who asked to be told when a run finishes, per backend."""

    __tablename__ = "daemon_run_watches"
    __table_args__ = (
        UniqueConstraint("run_id", "watcher_id", "backend"),
        Index("idx_daemon_run_watches_run", "run_id"),
    )

    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    watcher_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    backend: Mapped[str] = mapped_column(Text, nullable=False, server_default="'discord'")

    # The table has no PRIMARY KEY on disk — only the UNIQUE above — and it
    # must stay that way, so the mapper is told what identifies a row
    # instead of the DDL being changed to say it.
    __mapper_args__ = {  # noqa: RUF012 - SQLAlchemy's own contract for this
        "primary_key": [run_id, watcher_id, backend]
    }


class RequesterRow(Base):
    """Who asked for a piece of work, so the answer goes back to them."""

    __tablename__ = "daemon_requesters"
    __table_args__ = (PrimaryKeyConstraint("source_key", "repo"),)

    source_key: Mapped[str] = mapped_column(Text, nullable=False)
    repo: Mapped[str] = mapped_column(Text, nullable=False, server_default="''")
    requester_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)


class PriorAttemptRow(Base):
    """What the last attempt on this item left behind, kept past the item.

    Survives a row being requeued or superseded, which is what lets a fresh
    attempt adopt the branch and pull request the previous one opened.
    """

    __tablename__ = "daemon_prior_attempts"
    __table_args__ = (PrimaryKeyConstraint("source_key", "repo"),)

    source_key: Mapped[str] = mapped_column(Text, nullable=False)
    repo: Mapped[str] = mapped_column(Text, nullable=False, server_default="''")
    run_id: Mapped[str | None] = mapped_column(Text)
    branch: Mapped[str | None] = mapped_column(Text)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False, server_default="0")


class ChatThreadRow(Base):
    """The thread a run's chronology is written to, on one backend."""

    __tablename__ = "daemon_chat_threads"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", "backend"),
        Index("idx_chat_threads_thread", "thread_id"),
    )

    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    backend: Mapped[str] = mapped_column(Text, nullable=False, server_default="'discord'")
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    # The headline card and the status line inside the thread, edited in
    # place as the run moves.
    headline_id: Mapped[str | None] = mapped_column(Text)
    status_id: Mapped[str | None] = mapped_column(Text)


class MergeGateRow(Base):
    """A run parked until a person approves the merge.

    ``state`` moves ``open`` to ``approving`` to a terminal value, and the
    move to ``approving`` is a compare-and-set: two people pressing the
    button at once means one of them loses the CAS rather than both merging.
    """

    __tablename__ = "daemon_merge_gates"
    __table_args__ = (
        Index("idx_merge_gates_state", "state"),
        Index("idx_merge_gates_item", "item_id"),
    )

    run_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    item_id: Mapped[str] = mapped_column(Text, nullable=False)
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    pr_url: Mapped[str] = mapped_column(Text, nullable=False, server_default="''")
    branch: Mapped[str | None] = mapped_column(Text)
    notify_ids: Mapped[str] = mapped_column(Text, nullable=False, server_default="'[]'")
    custom_id: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="'open'")
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="'merge'")
    # Where the prompt was posted, before it moved to `daemon_gate_prompts`
    # so it could exist once per backend. Still readable, never written
    # again — an older daemon in a rollback window may write them, and the
    # next start carries them across.
    prompt_channel_id: Mapped[str | None] = mapped_column(Text)
    prompt_message_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    resolved_at: Mapped[float | None] = mapped_column(REAL)
    resolved_by: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)


class GatePromptRow(Base):
    """Where a run's gate prompt is showing, one row per backend."""

    __tablename__ = "daemon_gate_prompts"
    __table_args__ = (PrimaryKeyConstraint("run_id", "backend"),)

    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    backend: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(Text)


class ReviewHoldRow(Base):
    """A run parked because its base branch requires an approval.

    Polled rather than pushed, so it carries its own schedule:
    ``next_poll_at`` and ``polls`` are the backoff, ``since_at`` is what the
    poll asks GitHub about.
    """

    __tablename__ = "daemon_review_holds"
    __table_args__ = (
        Index("idx_review_holds_state", "state"),
        Index("idx_review_holds_item", "item_id"),
    )

    run_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    item_id: Mapped[str] = mapped_column(Text, nullable=False)
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    pr_url: Mapped[str] = mapped_column(Text, nullable=False, server_default="''")
    branch: Mapped[str | None] = mapped_column(Text)
    # Who requested the changes, and whether they were a bot — an automated
    # reviewer gets one answer, never a fix loop.
    login: Mapped[str] = mapped_column(Text, nullable=False, server_default="''")
    is_bot: Mapped[int | None] = mapped_column(Integer)
    approvals_required: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # Held because a person drafted the pull request, rather than because
    # the base demands an approval.
    held_by_draft: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    notify_ids: Mapped[str] = mapped_column(Text, nullable=False, server_default="'[]'")
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="'open'")
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    since_at: Mapped[float] = mapped_column(REAL, nullable=False)
    next_poll_at: Mapped[float] = mapped_column(REAL, nullable=False)
    polls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    resolved_at: Mapped[float | None] = mapped_column(REAL)
    resolved_by: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)


class PendingClarificationRow(Base):
    """A question the concierge asked that nobody has answered yet.

    Intake asks but never blocks: at ``deadline`` the item is filed on the
    stated ``assumption`` anyway.
    """

    __tablename__ = "daemon_pending_clarifications"
    __table_args__ = (Index("idx_pending_clarify_due", "state", "deadline"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=True)
    backend: Mapped[str] = mapped_column(Text, nullable=False, server_default="'discord'")
    channel_id: Mapped[str | None] = mapped_column(Text)
    prompt_message_id: Mapped[str | None] = mapped_column(Text)
    asker_id: Mapped[str | None] = mapped_column(Text)
    asker_name: Mapped[str | None] = mapped_column(Text)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    assumption: Mapped[str] = mapped_column(Text, nullable=False)
    deadline: Mapped[float] = mapped_column(REAL, nullable=False)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="'open'")
    resolved_at: Mapped[float | None] = mapped_column(REAL)


class LocalMessageRow(Base):
    """The operator console's mailbox: one row per message, both directions.

    This is the local chat bridge. The console writes inbound rows on a
    connection of its own while the daemon reads them, which is why the
    pending index is partial — the daemon's poll is a lookup on the few
    rows that are inbound and not yet taken, not a scan of the channel.
    """

    __tablename__ = "daemon_local_messages"
    __table_args__ = (
        Index("idx_local_messages_channel", "channel_id", "id"),
        Index("idx_local_messages_updated", "channel_id", "updated_at"),
        Index(
            "idx_local_messages_pending",
            "id",
            sqlite_where=text("direction = 'in' AND taken_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=True)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="'message'")
    text: Mapped[str] = mapped_column(Text, nullable=False, server_default="''")
    # Opaque here: the bridge layer owns what is inside them.
    embed_json: Mapped[str | None] = mapped_column(Text)
    choices_json: Mapped[str | None] = mapped_column(Text)
    gate_run_id: Mapped[str | None] = mapped_column(Text)
    reply_to_id: Mapped[int | None] = mapped_column(Integer)
    mention_users: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    author_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="'sbx'")
    author_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="'sbx'")
    reactions_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="'[]'")
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    edited_at: Mapped[float | None] = mapped_column(REAL)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False, server_default="0")
    # When the daemon took an inbound message. NULL means still pending.
    taken_at: Mapped[float | None] = mapped_column(REAL)


class ScheduleRowModel(Base):
    """A recurring ask, and where its last firing got to.

    Schedules live here rather than in the config file because they are
    runtime state the concierge creates and removes; the ``[[schedules]]``
    config block is imported once at start and is legacy.
    """

    __tablename__ = "daemon_schedules"

    name: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    anchor: Mapped[float] = mapped_column(REAL, nullable=False)
    last_due: Mapped[float | None] = mapped_column(REAL)
    last_fired_at: Mapped[float | None] = mapped_column(REAL)
    last_item: Mapped[str | None] = mapped_column(Text)
    paused_by: Mapped[str | None] = mapped_column(Text)
    paused_at: Mapped[float | None] = mapped_column(REAL)
    # The schedule itself. NULL on a row written before it moved into the
    # database; the config import fills those in on the next start.
    profile: Mapped[str | None] = mapped_column(Text)
    ask: Mapped[str | None] = mapped_column(Text)
    every: Mapped[str | None] = mapped_column(Text)
    cron: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float | None] = mapped_column(REAL)
