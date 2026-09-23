"""Presentation associations, independent of execution admission and scheduling."""

from sqlalchemy import DDL, REAL, Index, Integer, PrimaryKeyConstraint, Text, event, text
from sqlalchemy.orm import Mapped, mapped_column

from sbxloop.db.base import Base


class ExternalJobRow(Base):
    __tablename__ = "collaboration_jobs"
    __table_args__ = (Index("idx_collaboration_jobs_channel", "channel_id"),)

    work_id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    item_id: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    source_json: Mapped[str] = mapped_column(Text, nullable=False)
    system_created: Mapped[int] = mapped_column(Integer, nullable=False)
    historical: Mapped[int] = mapped_column(Integer, nullable=False)
    read_baseline: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    item_transition: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    attempt_cursor: Mapped[float] = mapped_column(REAL, nullable=False, server_default=text("0"))
    attempt_run_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)


class ExternalItemRow(Base):
    __tablename__ = "collaboration_job_items"
    __table_args__ = (Index("idx_collaboration_job_items_work", "work_id"),)

    item_id: Mapped[str] = mapped_column(Text, primary_key=True)
    work_id: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)


class ExternalRunRow(Base):
    __tablename__ = "collaboration_job_runs"
    __table_args__ = (Index("idx_collaboration_job_runs_work", "work_id", "created_at"),)

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    work_id: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    item_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'code'"))
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False, server_default=text("0"))
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    historical: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_json: Mapped[str] = mapped_column(Text, nullable=False)
    transition: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    resumes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    transition_historical: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    historical_through: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: Event replay is per run: backfill cannot skip a run discovered late.
    event_cursor: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    replay_complete: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class ExternalPendingRow(Base):
    """Durable dirtiness: written in the same transaction as source changes."""

    __tablename__ = "collaboration_job_pending"
    __table_args__ = (PrimaryKeyConstraint("kind", "resource_id"),)

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[str] = mapped_column(Text, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    historical: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


# Metadata-built databases must have the same transactional queue as migrated
# ones. Register on metadata so every source table exists before its trigger.
# The migration keeps its own frozen DDL for upgrading existing installations.
for _kind, _table, _key in (
    ("item", "daemon_work_items", "item_id"),
    ("run", "runs", "run_id"),
    ("run", "daemon_runs", "run_id"),
    ("run", "events", "run_id"),
):
    for _action in ("INSERT", "UPDATE"):
        event.listen(
            Base.metadata,
            "after_create",
            DDL(  # type: ignore[no-untyped-call]  # SQLAlchemy's DDL constructor is untyped.
                f"CREATE TRIGGER IF NOT EXISTS external_job_{_table}_{_action.lower()} "
                f"AFTER {_action} ON {_table} BEGIN "
                "INSERT INTO collaboration_job_pending(kind, resource_id) "
                f"VALUES ('{_kind}', NEW.{_key}) "
                "ON CONFLICT(kind, resource_id) DO UPDATE SET "
                "generation=generation+1, historical=0; END"  # nosec B608
            ).execute_if(dialect="sqlite"),
        )
