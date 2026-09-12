"""The tables behind the remote operations contract: what was asked, by whom,
with what promised effect, and what became of it.

These are the daemon's tables too — they live in the one ``state.db`` and
ride the one Alembic chain — but they are the record every *surface*
shares (the ``ctl`` queue, chat, the console, a remote client), so they
carry their own prefix rather than ``daemon_``.

* ``api_operations`` is one row per accepted command. Acceptance is durable
  admission, not completion: ``state`` moves ``accepted → running →
  succeeded | failed | expired``, with ``reconciling`` for an effect the
  daemon could not establish after an interruption. ``idempotency_scope``
  and ``idempotency_key`` make a client's retry return the same row;
  ``fingerprint`` is what refuses the same key with a different payload.
  ``claimed_generation`` names the daemon process that took the command,
  so a process that comes back can tell its own claims from a dead one's.
* ``api_events`` is the public chronology: an outbox of operation
  transitions and, later, the daemon's notices and a projection of the
  engine's own ``events`` (``source_seq`` points at that row when it is
  one). ``seq`` is the one cursor every replay pages by.
* ``api_clients`` are the registered clients: a name, the scrypt verifier
  of a secret shown once at creation, and the capabilities granted. The
  secret itself is never stored.
* ``api_refresh_tokens`` are the long-lived halves of a token pair, by
  digest: a ``family`` is one client-credentials grant and every rotation
  descended from it, so a refresh token presented twice revokes the whole
  family. ``api_token_revocations`` is the denylist of access-token ids
  (``jti``) revoked before they expired, pruned past their expiry.

* ``api_public_ids`` maps the opaque id a client sees to the resource
  behind it — a kind, the workspace, and the internal key (for a work
  item the repository and the item id together, so two repositories'
  issue numbers never alias). Assigned lazily on first read, stable
  after.

JSON lives in ``TEXT`` columns, serialised in Python, as everywhere else.
"""

from __future__ import annotations

from sqlalchemy import REAL, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from sbxloop.db.base import Base


class OperationRow(Base):
    __tablename__ = "api_operations"
    __table_args__ = (
        UniqueConstraint("idempotency_scope", "idempotency_key"),
        Index("idx_api_operations_state", "state", "accepted_at"),
        Index("idx_api_operations_target", "target_kind", "target_key", "accepted_at"),
        Index("idx_api_operations_accepted", "accepted_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_key: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    effect: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    actor_json: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_scope: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    fingerprint: Mapped[str | None] = mapped_column(Text)
    expected_revision: Mapped[int | None] = mapped_column(Integer)
    request_json: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'{}'"))
    accepted_at: Mapped[float] = mapped_column(REAL, nullable=False)
    expires_at: Mapped[float | None] = mapped_column(REAL)
    claimed_at: Mapped[float | None] = mapped_column(REAL)
    finished_at: Mapped[float | None] = mapped_column(REAL)
    claimed_generation: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)


class ApiEventRow(Base):
    __tablename__ = "api_events"
    # AUTOINCREMENT: a cursor a client resumes from must never be reused.
    __table_args__ = (
        Index("idx_api_events_run", "run_id", "seq"),
        Index("idx_api_events_recorded", "recorded_at"),
        {"sqlite_autoincrement": True},
    )

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=True)
    recorded_at: Mapped[float] = mapped_column(REAL, nullable=False)
    occurred_at: Mapped[float] = mapped_column(REAL, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[str | None] = mapped_column(Text)
    item_id: Mapped[str | None] = mapped_column(Text)
    operation_id: Mapped[str | None] = mapped_column(Text)
    actor_json: Mapped[str | None] = mapped_column(Text)
    source_seq: Mapped[int | None] = mapped_column(Integer)
    data_json: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class ClientRow(Base):
    __tablename__ = "api_clients"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    capabilities_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'[]'")
    )
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    created_by: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[float | None] = mapped_column(REAL)
    last_used_at: Mapped[float | None] = mapped_column(REAL)


class RefreshTokenRow(Base):
    __tablename__ = "api_refresh_tokens"
    __table_args__ = (
        Index("idx_api_refresh_tokens_client", "client_id", "family_id"),
        Index("idx_api_refresh_tokens_hash", "token_hash"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    family_id: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[float] = mapped_column(REAL, nullable=False)
    expires_at: Mapped[float] = mapped_column(REAL, nullable=False)
    used_at: Mapped[float | None] = mapped_column(REAL)
    replaced_by: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[float | None] = mapped_column(REAL)


class TokenRevocationRow(Base):
    __tablename__ = "api_token_revocations"
    __table_args__ = (Index("idx_api_token_revocations_expires", "expires_at"),)

    jti: Mapped[str] = mapped_column(Text, primary_key=True)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[float] = mapped_column(REAL, nullable=False)
    revoked_at: Mapped[float] = mapped_column(REAL, nullable=False)


class PublicIdRow(Base):
    __tablename__ = "api_public_ids"
    __table_args__ = (UniqueConstraint("kind", "workspace_id", "internal_key"),)

    public_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    internal_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
