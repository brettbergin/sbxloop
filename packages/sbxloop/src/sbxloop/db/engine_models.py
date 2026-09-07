"""The engine's five tables: one run, its tasks, its attempts, its events.

These map the schema as it stands, column for column, and nothing here is an
improvement on it (#539). A deployed ``state.db`` has to keep opening, and
``e2e.yml`` reads ``runs`` and ``events`` raw, so a name that moved would be
a break rather than a tidy-up.

Two shapes are worth knowing before reading further:

* **JSON lives in TEXT columns.** ``config_json``, ``user_guidance``,
  ``credentials``, ``published``, ``spec_json``, ``verify_fingerprints``,
  ``output_json`` and ``data_json`` are serialised in Python and stored as
  text. They stay ``Text`` rather than becoming ``JSON``: the type would
  change how SQLite stores them, and the store already owns the round trip.
* **Timestamps are ``REAL``, not ``Float``.** Both take REAL affinity in
  SQLite, but ``Float`` renders the column as ``FLOAT`` and these columns
  were written ``REAL``. A model that emits different DDL than the database
  holds is a model that will eventually rebuild a table it did not need to.
* **``reconciliations`` is six tables wearing one.** ``round`` is a
  namespace as much as a number — at or above zero it is a real review round,
  and the sentinels below it (see :mod:`sbxloop.engine.store`) carry human
  replies, advisory and bot round spend, confirmations and noted findings. A
  ``kind`` column to say so honestly is coming in its own revision; until
  then the model documents the overloading rather than hiding it.
"""

from __future__ import annotations

from sqlalchemy import REAL, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from sbxloop.db.base import Base


class Run(Base):
    """One run, from the ask that started it to the state it ended in."""

    __tablename__ = "runs"

    # `nullable=True` on a primary key looks wrong and is not: SQLite
    # reports a column's nullability from the literal DDL, and the DDL
    # these tables were created with left NOT NULL off where PRIMARY KEY
    # already implied it. Saying so here keeps `alembic revision
    # --autogenerate` from proposing a rebuild that changes nothing.
    run_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    # A `RunState`. Two spellings written before the pipeline existed,
    # `running` and `finalizing`, are still on disk and are remapped when the
    # store reads them — never rewritten, so an older version still reads
    # its own rows.
    state: Mapped[str] = mapped_column(Text, nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="'{}'")
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)

    # Host workspace, and whether it was live-mounted into the agent VM.
    workspace: Mapped[str | None] = mapped_column(Text)
    mounted: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    kept_reason: Mapped[str | None] = mapped_column(Text)
    user_guidance: Mapped[str] = mapped_column(Text, nullable=False, server_default="'[]'")
    reason: Mapped[str | None] = mapped_column(Text)

    # Pipeline bookkeeping. `stage` is the last non-terminal state, so a
    # resume of a failed run knows where to re-enter; the PR fields are set
    # at first delivery and `head_sha` moves with every re-delivery.
    stage: Mapped[str | None] = mapped_column(Text)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    pr_url: Mapped[str | None] = mapped_column(Text)
    pr_node_id: Mapped[str | None] = mapped_column(Text)
    branch: Mapped[str | None] = mapped_column(Text)
    head_sha: Mapped[str | None] = mapped_column(Text)

    # Fix-round budgets spent, and the head an update-branch was asked for
    # so a later poll can tell one still in flight from one that landed.
    review_rounds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    ci_rounds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    update_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    update_head: Mapped[str | None] = mapped_column(Text)
    last_verdict: Mapped[str | None] = mapped_column(Text)
    exhausted: Mapped[str | None] = mapped_column(Text)
    granted_rounds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    pr_title: Mapped[str | None] = mapped_column(Text)
    credentials: Mapped[str] = mapped_column(Text, nullable=False, server_default="'[]'")
    # Every run before the workload kind was a developer run, which is why
    # the default is the migration: a legacy row reads back as `code`.
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="'code'")
    # Where a workload's result went; nothing published before it existed.
    published: Mapped[str] = mapped_column(Text, nullable=False, server_default="'[]'")


class Task(Base):
    """One task in a run's graph, keyed by the run and the task's own id."""

    __tablename__ = "tasks"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    order_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    spec_json: Mapped[str] = mapped_column(Text, nullable=False)
    revisions: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    replans: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_feedback: Mapped[str] = mapped_column(Text, nullable=False, server_default="''")
    session_id: Mapped[str | None] = mapped_column(Text)
    verify_fingerprints: Mapped[str] = mapped_column(Text, nullable=False, server_default="'[]'")
    verify_suspect: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # A workload task's TaskOutput; NULL for every code task.
    output_json: Mapped[str | None] = mapped_column(Text)


class PhaseAttempt(Base):
    """One attempt at one phase, with what the agent spent on it.

    ``task_id`` is NULL for a run-level phase. The usage columns are NULL for
    every attempt recorded before they existed, which is why none of them is
    NOT NULL: an old row has no number to give.
    """

    __tablename__ = "phase_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(Text)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    output_json: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[float] = mapped_column(REAL, nullable=False)
    ended_at: Mapped[float] = mapped_column(REAL, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer)
    turns: Mapped[int | None] = mapped_column(Integer)


class Reconciliation(Base):
    """What a review round said about one anchor, and whether it was settled.

    See the module docstring on ``round``: it is a discriminator as well as a
    round number, and the negative values are the other five concerns this
    table carries.
    """

    __tablename__ = "reconciliations"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    round: Mapped[int] = mapped_column(Integer, primary_key=True)
    anchor: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    resolved: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    ts: Mapped[float] = mapped_column(REAL, nullable=False)


class EventRow(Base):
    """One line of a run's chronology.

    Named for the table rather than the concept because ``Event`` is already
    the worker protocol's model, which is what the store converts these to
    and from. ``seq`` is the ordering every consumer pages by, so the index
    is on ``(run_id, seq)`` rather than ``run_id`` alone.
    """

    __tablename__ = "events"
    __table_args__ = (Index("idx_events_run", "run_id", "seq"),)

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[float] = mapped_column(REAL, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[str | None] = mapped_column(Text)
    data_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="'{}'")
