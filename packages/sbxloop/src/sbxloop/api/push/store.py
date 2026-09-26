"""Registered devices and the notifications rendered for them.

A device's push token is never stored: only its SHA-256 digest (what a
second registration of the same token is matched on) and its last six
characters (what a person tells devices apart by). The relay's handle is
stored, because it is what a push is addressed to, and leaves this module
only inside a :class:`DeviceTarget`, whose ``repr`` leaves it out.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from sbxloop.daemon.store import DaemonStore
from sbxloop.db.api_models import PushDeviceRow, PushNotificationRow

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def _token(size: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(size))


def token_digest(token: str) -> str:
    """The stored form of a device token: case-folded, then hashed."""
    return hashlib.sha256(token.strip().lower().encode()).hexdigest()


def default_prefs() -> dict[str, Any]:
    return {"mentions": True, "gates": True, "work": True, "failures": True, "per_channel": {}}


@dataclass(frozen=True, slots=True)
class Device:
    """A device as its owner sees it: never the token, never the handle."""

    id: str
    user_id: str
    platform: str
    env: str
    server_ref: str
    name: str | None
    prefs: dict[str, Any]
    token_suffix: str
    created_at: float
    updated_at: float
    last_push_at: float | None
    #: Whether the relay still recognises this device's handle.
    enrolled: bool = True


@dataclass(frozen=True, slots=True)
class DeviceTarget:
    """What a push is addressed with. ``handle`` never reaches a log line
    through this object's ``repr``."""

    id: str
    user_id: str
    server_ref: str
    prefs: dict[str, Any]
    handle: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Notification:
    ref: str
    user_id: str
    kind: str
    channel_id: str | None
    turn_id: str | None
    title: str
    body: str
    created_at: float


def _device(row: PushDeviceRow) -> Device:
    return Device(
        id=str(row.id),
        user_id=str(row.user_id),
        platform=str(row.platform),
        env=str(row.env),
        server_ref=str(row.server_ref),
        name=row.name,
        prefs={**default_prefs(), **json.loads(row.prefs_json or "{}")},
        token_suffix=str(row.token_suffix),
        created_at=float(row.created_at),
        updated_at=float(row.updated_at),
        last_push_at=None if row.last_push_at is None else float(row.last_push_at),
        enrolled=bool(row.handle),
    )


def _notification(row: PushNotificationRow) -> Notification:
    return Notification(
        ref=str(row.ref),
        user_id=str(row.user_id),
        kind=str(row.kind),
        channel_id=row.channel_id,
        turn_id=row.turn_id,
        title=str(row.title),
        body=str(row.body),
        created_at=float(row.created_at),
    )


class DeviceStore:
    """Devices and notifications over the daemon's one store."""

    def __init__(self, dstore: DaemonStore) -> None:
        self.dstore = dstore

    # -- devices ---------------------------------------------------------------

    def for_user(self, user_id: str) -> list[Device]:
        with self.dstore.read() as session:
            rows = session.scalars(
                select(PushDeviceRow)
                .where(PushDeviceRow.user_id == user_id)
                .order_by(PushDeviceRow.created_at, PushDeviceRow.id)
            )
            return [_device(row) for row in rows]

    def get(self, user_id: str, device_id: str) -> Device | None:
        with self.dstore.read() as session:
            row = session.get(PushDeviceRow, device_id)
            return None if row is None or row.user_id != user_id else _device(row)

    def by_token(self, user_id: str, token: str) -> Device | None:
        with self.dstore.read() as session:
            row = session.scalars(
                select(PushDeviceRow).where(
                    PushDeviceRow.user_id == user_id,
                    PushDeviceRow.token_digest == token_digest(token),
                )
            ).first()
            return None if row is None else _device(row)

    def count(self, user_id: str) -> int:
        with self.dstore.read() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(PushDeviceRow)
                    .where(PushDeviceRow.user_id == user_id)
                )
                or 0
            )

    def insert(
        self,
        user_id: str,
        *,
        platform: str,
        token: str,
        env: str,
        server_ref: str,
        name: str | None,
        prefs: Mapping[str, Any],
        handle: str,
        now: float,
    ) -> Device | None:
        """The new device, or ``None`` when a concurrent registration of the
        same token got there first (the caller updates that one instead)."""
        clean = token.strip().lower()
        row = PushDeviceRow(
            id="dev_" + _token(20),
            user_id=user_id,
            platform=platform,
            token_digest=token_digest(clean),
            token_suffix=clean[-6:],
            env=env,
            server_ref=server_ref,
            name=name,
            prefs_json=json.dumps(dict(prefs)),
            handle=handle,
            created_at=now,
            updated_at=now,
            last_push_at=None,
        )
        try:
            with self.dstore.transaction() as session:
                session.add(row)
                session.flush()
                return _device(row)
        except IntegrityError:
            return None

    def update(
        self,
        device_id: str,
        *,
        env: str,
        server_ref: str,
        name: str | None,
        prefs: Mapping[str, Any],
        handle: str | None,
        now: float,
    ) -> Device | None:
        """Update a device; ``handle`` is left alone when ``None``."""
        with self.dstore.transaction() as session:
            row = session.get(PushDeviceRow, device_id)
            if row is None:
                return None
            row.env = env
            row.server_ref = server_ref
            row.name = name
            row.prefs_json = json.dumps(dict(prefs))
            if handle is not None:
                row.handle = handle
            row.updated_at = now
            session.flush()
            return _device(row)

    def delete(self, user_id: str, device_id: str) -> bool:
        with self.dstore.transaction() as session:
            result = session.execute(
                delete(PushDeviceRow).where(
                    PushDeviceRow.id == device_id, PushDeviceRow.user_id == user_id
                )
            )
            return bool(getattr(result, "rowcount", 0))

    # -- what the dispatcher needs -----------------------------------------------

    def targets(self, user_ids: Iterable[str]) -> dict[str, list[DeviceTarget]]:
        """Every enrolled device of each user, by user."""
        wanted = sorted(set(user_ids))
        out: dict[str, list[DeviceTarget]] = {}
        if not wanted:
            return out
        with self.dstore.read() as session:
            rows = session.scalars(
                select(PushDeviceRow)
                .where(PushDeviceRow.user_id.in_(wanted), PushDeviceRow.handle != "")
                .order_by(PushDeviceRow.created_at, PushDeviceRow.id)
            )
            for row in rows:
                out.setdefault(str(row.user_id), []).append(self._target(row))
        return out

    def target(self, device_id: str) -> DeviceTarget | None:
        with self.dstore.read() as session:
            row = session.get(PushDeviceRow, device_id)
            return None if row is None or not row.handle else self._target(row)

    @staticmethod
    def _target(row: PushDeviceRow) -> DeviceTarget:
        return DeviceTarget(
            id=str(row.id),
            user_id=str(row.user_id),
            server_ref=str(row.server_ref),
            prefs={**default_prefs(), **json.loads(row.prefs_json or "{}")},
            handle=str(row.handle),
        )

    def forget(self, device_id: str) -> None:
        """The relay says the device is gone for good: drop it."""
        with self.dstore.transaction() as session:
            session.execute(delete(PushDeviceRow).where(PushDeviceRow.id == device_id))

    def unenroll(self, device_id: str, now: float) -> None:
        """The relay no longer recognises the handle: keep the device, and
        enroll it again the next time it registers."""
        with self.dstore.transaction() as session:
            session.execute(
                update(PushDeviceRow)
                .where(PushDeviceRow.id == device_id)
                .values(handle="", updated_at=now)
            )

    def pushed(self, device_id: str, now: float) -> None:
        with self.dstore.transaction() as session:
            session.execute(
                update(PushDeviceRow).where(PushDeviceRow.id == device_id).values(last_push_at=now)
            )

    # -- notifications -------------------------------------------------------------

    def record(
        self,
        *,
        user_id: str,
        kind: str,
        channel_id: str | None,
        turn_id: str | None,
        title: str,
        body: str,
        event_seq: int | None,
        now: float,
    ) -> str:
        """Keep what a push is about; returns its ref."""
        ref = "ntf_" + _token(24)
        with self.dstore.transaction() as session:
            session.add(
                PushNotificationRow(
                    ref=ref,
                    user_id=user_id,
                    kind=kind,
                    channel_id=channel_id,
                    turn_id=turn_id,
                    title=title,
                    body=body,
                    event_seq=event_seq,
                    created_at=now,
                )
            )
        return ref

    def notification(self, user_id: str, ref: str) -> Notification | None:
        with self.dstore.read() as session:
            row = session.get(PushNotificationRow, ref)
            return None if row is None or row.user_id != user_id else _notification(row)

    def prune(self, before: float) -> int:
        with self.dstore.transaction() as session:
            result = session.execute(
                delete(PushNotificationRow).where(PushNotificationRow.created_at < before)
            )
            return int(getattr(result, "rowcount", 0) or 0)


__all__ = [
    "Device",
    "DeviceStore",
    "DeviceTarget",
    "Notification",
    "default_prefs",
    "token_digest",
]
