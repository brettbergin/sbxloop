"""Clients, secrets and refresh tokens, by verifier only.

A secret is stored as its scrypt hash and compared in constant time; a
refresh token as its SHA-256. A refresh token belongs to a *family* — one
client-credentials grant and every rotation descended from it — and a
token presented a second time revokes the whole family: whoever holds the
stolen copy and whoever holds the legitimate one both lose it, and the
legitimate client re-authenticates with its secret.

The store speaks to whatever gives it sessions: the daemon's own
:class:`~sbxloop.daemon.store.DaemonStore` (``transaction()`` / ``read()``)
in the daemon, or a standalone engine opened ``owns_schema=False`` in the
CLI beside a running daemon.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session

from sbxloop.daemon.controls.principal import CAPABILITIES, Capability
from sbxloop.db import ensure_schema, open_engine
from sbxloop.db.api_models import ClientRow, RefreshTokenRow, TokenRevocationRow
from sbxloop.ids import _token

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_LEN = 2**14, 8, 1, 32


class SessionSource(Protocol):
    """What gives the store its sessions: a transaction that commits on
    the way out, and a read-only one — both under the source's lock."""

    def transaction(self) -> AbstractContextManager[Session]: ...
    def read(self) -> AbstractContextManager[Session]: ...


class StandaloneSessions:
    """Sessions over an engine of this process's own — the CLI's, beside a
    running daemon: no migration, no journal-mode change."""

    def __init__(self, path: Path, *, owns_schema: bool) -> None:
        self._lock = threading.RLock()
        self._engine = open_engine(path, owns_schema=owns_schema)
        if owns_schema:
            ensure_schema(self._engine)

    def close(self) -> None:
        self._engine.dispose()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with self._lock, Session(self._engine) as session:
            yield session
            session.commit()

    @contextmanager
    def read(self) -> Iterator[Session]:
        with self._lock, Session(self._engine) as session:
            yield session


def hash_secret(secret: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        secret.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_LEN
    )
    return "$".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        )
    )


def check_secret(secret: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            secret.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=_SCRYPT_LEN,
        )
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def parse_capabilities(names: list[str]) -> frozenset[Capability]:
    """The capability set, refusing an unknown name by name."""
    known = set(CAPABILITIES)
    unknown = sorted(set(names) - known)
    if unknown:
        raise ValueError(f"unknown capabilities: {', '.join(unknown)}")
    return frozenset(cap for cap in CAPABILITIES if cap in names)


@dataclass(frozen=True, slots=True)
class Client:
    id: str
    name: str
    capabilities: frozenset[Capability]
    created_at: float
    created_by: str | None
    revoked_at: float | None
    last_used_at: float | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class AuthError(Exception):
    """A credential that cannot be accepted; ``code`` is the problem code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _client(row: ClientRow) -> Client:
    return Client(
        id=str(row.id),
        name=str(row.name),
        capabilities=frozenset(
            cap for cap in CAPABILITIES if cap in json.loads(row.capabilities_json or "[]")
        ),
        created_at=float(row.created_at),
        created_by=None if row.created_by is None else str(row.created_by),
        revoked_at=None if row.revoked_at is None else float(row.revoked_at),
        last_used_at=None if row.last_used_at is None else float(row.last_used_at),
    )


class ApiAuthStore:
    def __init__(self, sessions: SessionSource) -> None:
        self.sessions = sessions

    # -- clients --------------------------------------------------------------------

    def create_client(
        self,
        name: str,
        capabilities: frozenset[Capability],
        *,
        created_by: str | None,
        now: float,
    ) -> tuple[Client, str]:
        """Register a client. Returns it with the one-time secret; the
        secret is never recoverable afterwards."""
        name = name.strip()
        if not name:
            raise ValueError("a client needs a name")
        client_id = "cli_" + _token(12)
        secret = "sk_" + secrets.token_urlsafe(32)
        with self.sessions.transaction() as session:
            session.execute(
                insert(ClientRow).values(
                    id=client_id,
                    name=name,
                    secret_hash=hash_secret(secret),
                    capabilities_json=json.dumps(
                        [cap for cap in CAPABILITIES if cap in capabilities]
                    ),
                    created_at=now,
                    created_by=created_by,
                )
            )
            row = session.get(ClientRow, client_id)
            assert row is not None  # nosec B101 - just inserted under the lock
            return _client(row), secret

    def get_client(self, client_id: str) -> Client | None:
        with self.sessions.read() as session:
            row = session.get(ClientRow, client_id)
            return None if row is None else _client(row)

    def list_clients(self) -> list[Client]:
        with self.sessions.read() as session:
            return [
                _client(row)
                for row in session.scalars(select(ClientRow).order_by(ClientRow.created_at.asc()))
            ]

    def revoke_client(self, client_id: str, now: float) -> Client | None:
        """Revoke a client: no new tokens, its refresh tokens dead, every
        request with a live access token refused from now on."""
        with self.sessions.transaction() as session:
            row = session.get(ClientRow, client_id)
            if row is None:
                return None
            if row.revoked_at is None:
                row.revoked_at = now
            session.execute(
                update(RefreshTokenRow)
                .where(RefreshTokenRow.client_id == client_id, RefreshTokenRow.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            session.flush()
            return _client(row)

    def authenticate(self, client_id: str, secret: str, now: float) -> Client:
        """The client behind a credential pair; :class:`AuthError` when
        the pair is wrong or the client revoked — the same code either
        way, so a probe learns nothing about which."""
        with self.sessions.transaction() as session:
            row = session.get(ClientRow, client_id)
            if row is None or row.revoked_at is not None:
                # Burn the same time as a real check so timing says nothing.
                check_secret(secret, hash_secret("x"))
                raise AuthError("invalid_client", "unknown client or wrong secret")
            if not check_secret(secret, str(row.secret_hash)):
                raise AuthError("invalid_client", "unknown client or wrong secret")
            row.last_used_at = now
            session.flush()
            return _client(row)

    def touch(self, client_id: str, now: float) -> None:
        with self.sessions.transaction() as session:
            session.execute(
                update(ClientRow).where(ClientRow.id == client_id).values(last_used_at=now)
            )

    # -- refresh tokens -------------------------------------------------------------

    def issue_refresh(
        self, client_id: str, *, family_id: str | None, now: float, ttl_s: int
    ) -> str:
        """A fresh refresh token, in a new family unless one is continued."""
        token = "rt_" + secrets.token_urlsafe(32)
        with self.sessions.transaction() as session:
            session.execute(
                insert(RefreshTokenRow).values(
                    id="rt_" + _token(12),
                    client_id=client_id,
                    family_id=family_id or "fam_" + _token(12),
                    token_hash=_digest(token),
                    issued_at=now,
                    expires_at=now + ttl_s,
                )
            )
        return token

    def rotate_refresh(self, token: str, *, now: float, ttl_s: int) -> tuple[Client, str]:
        """Exchange a refresh token for a new one in the same family.

        A token already used is a reuse: the whole family is revoked (in a
        transaction of its own, so the refusal cannot roll it back) and the
        caller refused (``refresh_reuse_detected``). Expired, revoked or
        unknown tokens are refused too, without saying which.
        """
        digest = _digest(token)
        with self.sessions.read() as session:
            row = session.scalars(
                select(RefreshTokenRow).where(RefreshTokenRow.token_hash == digest)
            ).first()
            if row is None:
                raise AuthError("invalid_grant", "the refresh token is not valid")
            family_id = str(row.family_id)
            reused = row.used_at is not None
            dead = row.revoked_at is not None or float(row.expires_at) <= now
        if reused:
            self._revoke_family(family_id, now)
            raise AuthError(
                "refresh_reuse_detected",
                "the refresh token was already used; its family is revoked — "
                "authenticate with the client secret again",
            )
        if dead:
            raise AuthError("invalid_grant", "the refresh token is not valid")
        fresh = "rt_" + secrets.token_urlsafe(32)
        fresh_id = "rt_" + _token(12)
        with self.sessions.transaction() as session:
            row = session.scalars(
                select(RefreshTokenRow).where(
                    RefreshTokenRow.token_hash == digest,
                    RefreshTokenRow.used_at.is_(None),
                    RefreshTokenRow.revoked_at.is_(None),
                )
            ).first()
            if row is None:
                # Raced by another rotation of the same token between the
                # two looks: that is a reuse too.
                raise AuthError("invalid_grant", "the refresh token is not valid")
            client_row = session.get(ClientRow, str(row.client_id))
            if client_row is None or client_row.revoked_at is not None:
                raise AuthError("invalid_grant", "the refresh token is not valid")
            session.execute(
                insert(RefreshTokenRow).values(
                    id=fresh_id,
                    client_id=row.client_id,
                    family_id=row.family_id,
                    token_hash=_digest(fresh),
                    issued_at=now,
                    expires_at=now + ttl_s,
                )
            )
            row.used_at = now
            row.replaced_by = fresh_id
            client_row.last_used_at = now
            session.flush()
            return _client(client_row), fresh

    def _revoke_family(self, family_id: str, now: float) -> None:
        with self.sessions.transaction() as session:
            session.execute(
                update(RefreshTokenRow)
                .where(
                    RefreshTokenRow.family_id == family_id,
                    RefreshTokenRow.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )

    def revoke_refresh(self, token: str, now: float) -> bool:
        """Revoke a refresh token's whole family. ``False`` when unknown."""
        with self.sessions.read() as session:
            row = session.scalars(
                select(RefreshTokenRow).where(RefreshTokenRow.token_hash == _digest(token))
            ).first()
            if row is None:
                return False
            family_id = str(row.family_id)
        self._revoke_family(family_id, now)
        return True

    # -- access token revocation ----------------------------------------------------

    def revoke_access(self, jti: str, client_id: str, expires_at: float, now: float) -> None:
        with self.sessions.transaction() as session:
            session.execute(
                insert(TokenRevocationRow)
                .prefix_with("OR IGNORE")
                .values(jti=jti, client_id=client_id, expires_at=expires_at, revoked_at=now)
            )

    def is_revoked(self, jti: str) -> bool:
        with self.sessions.read() as session:
            return session.get(TokenRevocationRow, jti) is not None

    def prune(self, now: float) -> int:
        """Drop revocations past their token's expiry and refresh tokens
        past theirs; both are dead weight once the clock has passed them."""
        with self.sessions.transaction() as session:
            gone = 0
            for statement in (
                delete(TokenRevocationRow).where(TokenRevocationRow.expires_at < now),
                delete(RefreshTokenRow).where(RefreshTokenRow.expires_at < now),
            ):
                gone += int(getattr(session.execute(statement), "rowcount", 0) or 0)
            return gone
