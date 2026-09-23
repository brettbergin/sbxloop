"""Private, durable originals uploaded to collaboration channels.

This module deliberately handles opaque bytes only. Parsing a PDF, image,
archive or executable belongs in a separate no-secret analysis sandbox, never
in the API process that holds credentials and the collaboration database.
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sbxloop.api.collaboration import CollaborationError, Member, _access
from sbxloop.daemon.store import DaemonStore
from sbxloop.db.collaboration_models import ChannelInputFileRow, MessageRow, TurnRow
from sbxloop.ids import _token
from sbxloop.paths import SbxloopHome

MAX_FILE_BYTES = 100_000_000
MAX_WORKSPACE_BYTES = 10_000_000_000
MAX_STAGED_PER_USER = 32
MAX_CHANNEL_FILES = 1_000
STAGED_TTL_S = 7 * 24 * 60 * 60
PART_TTL_S = 24 * 60 * 60
_ID = re.compile(r"^fin_[0-9abcdefghjkmnpqrstvwxyz]{24}$")


@dataclass(frozen=True, slots=True)
class ChannelInputFile:
    id: str
    channel_id: str
    uploader_id: str
    client_upload_id: str
    display_name: str
    declared_size: int | None
    size: int | None
    sha256: str | None
    status: str
    message_id: str | None
    created_at: float
    uploaded_at: float | None


def _file(row: ChannelInputFileRow) -> ChannelInputFile:
    return ChannelInputFile(
        id=str(row.id),
        channel_id=str(row.channel_id),
        uploader_id=str(row.uploader_id),
        client_upload_id=str(row.client_upload_id),
        display_name=str(row.display_name),
        declared_size=None if row.declared_size is None else int(row.declared_size),
        size=None if row.size is None else int(row.size),
        sha256=None if row.sha256 is None else str(row.sha256),
        status=str(row.status),
        message_id=None if row.message_id is None else str(row.message_id),
        created_at=float(row.created_at),
        uploaded_at=None if row.uploaded_at is None else float(row.uploaded_at),
    )


def _not_found() -> CollaborationError:
    return CollaborationError("input_file_not_found", "file not found")


def _clean_name(value: str) -> str:
    # Names are display metadata. Never pass one to a path or shell command.
    name = value.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    name = "".join(char for char in name if not unicodedata.category(char).startswith("C")).strip()
    if not name or len(name) > 255:
        raise CollaborationError("invalid_file_name", "file name must be 1-255 characters")
    return name


class ChannelFileStore:
    """Metadata and byte paths for a daemon's channel input originals."""

    def __init__(self, dstore: DaemonStore, home: SbxloopHome) -> None:
        self.dstore = dstore
        self.home = home

    def directory(self) -> Path:
        root = self.home.channel_files
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink():
            raise OSError("channel file directory must not be a symlink")
        if os.name != "nt":
            root.chmod(0o700)
        else:
            from sbxloop.hostfiles import make_private

            make_private(root, os_name=self.home.os_name)
        return root

    def path(self, file_id: str) -> Path:
        if _ID.fullmatch(file_id) is None:
            raise _not_found()
        return self.directory() / file_id

    def open_original(self, file_id: str, expected_size: int) -> BinaryIO:
        """Open only a regular original, never a link or alternate path."""
        path = self.path(file_id)
        if path.is_symlink():
            raise OSError("channel original must not be a symlink")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            record = os.fstat(descriptor)
            if not stat.S_ISREG(record.st_mode) or record.st_size != expected_size:
                raise OSError("channel original is missing or has changed")
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise

    def reconcile(self, now: float) -> None:
        """Expire abandoned stages and remove unreferenced crash leftovers."""
        root = self.directory()
        with self.dstore.immediate_transaction() as session:
            abandoned = list(
                session.scalars(
                    select(ChannelInputFileRow).where(
                        ChannelInputFileRow.status.in_(("reserved", "uploaded")),
                        ChannelInputFileRow.created_at < now - STAGED_TTL_S,
                    )
                )
            )
            for row in abandoned:
                row.status = "deleted"
                row.deleted_at = now
            session.flush()
            live = set(
                session.scalars(
                    select(ChannelInputFileRow.id).where(
                        ChannelInputFileRow.status.in_(("uploaded", "attached"))
                    )
                )
            )
        for path in root.iterdir():
            if path.name.startswith("input-") and path.name.endswith(".part"):
                if now - path.lstat().st_mtime > PART_TTL_S:
                    path.unlink(missing_ok=True)
            elif _ID.fullmatch(path.name) and path.name not in live:
                path.unlink(missing_ok=True)

    def reserve(
        self,
        member: Member,
        channel_id: str,
        *,
        client_upload_id: str,
        display_name: str,
        declared_size: int | None,
        now: float,
    ) -> ChannelInputFile:
        name = _clean_name(display_name)
        if not client_upload_id or len(client_upload_id) > 128:
            raise CollaborationError("invalid_upload_id", "client_upload_id is required")
        if declared_size is not None and (declared_size < 0 or declared_size > MAX_FILE_BYTES):
            raise CollaborationError(
                "file_too_large", f"files are limited to {MAX_FILE_BYTES} bytes"
            )
        with self.dstore.immediate_transaction() as session:
            channel, _ = _access(session, channel_id, member, "post", now=now)
            existing = session.scalar(
                select(ChannelInputFileRow).where(
                    ChannelInputFileRow.channel_id == channel_id,
                    ChannelInputFileRow.uploader_id == member.user.id,
                    ChannelInputFileRow.client_upload_id == client_upload_id,
                )
            )
            if existing is not None:
                if existing.display_name != name or existing.declared_size != declared_size:
                    raise CollaborationError(
                        "idempotency_conflict", "client_upload_id was used for another file"
                    )
                if existing.status == "deleted":
                    raise CollaborationError(
                        "idempotency_conflict", "client_upload_id belongs to a cancelled file"
                    )
                return _file(existing)
            staged = session.scalar(
                select(func.count())
                .select_from(ChannelInputFileRow)
                .where(
                    ChannelInputFileRow.channel_id == channel_id,
                    ChannelInputFileRow.uploader_id == member.user.id,
                    ChannelInputFileRow.status.in_(("reserved", "uploaded")),
                )
            )
            if int(staged or 0) >= MAX_STAGED_PER_USER:
                raise CollaborationError("file_limit", "too many unsent files in this channel")
            count = session.scalar(
                select(func.count())
                .select_from(ChannelInputFileRow)
                .where(
                    ChannelInputFileRow.channel_id == channel_id,
                    ChannelInputFileRow.status != "deleted",
                )
            )
            if int(count or 0) >= MAX_CHANNEL_FILES:
                raise CollaborationError("file_limit", "this channel has reached its file limit")
            row = ChannelInputFileRow(
                id="fin_" + _token(24),
                workspace_id=str(channel.workspace_id),
                channel_id=channel_id,
                uploader_id=member.user.id,
                client_upload_id=client_upload_id,
                display_name=name,
                declared_size=declared_size,
                size=None,
                sha256=None,
                status="reserved",
                message_id=None,
                created_at=now,
                uploaded_at=None,
                deleted_at=None,
            )
            session.add(row)
            session.flush()
            return _file(row)

    @staticmethod
    def _visible(
        session: Session, member: Member, channel_id: str, file_id: str
    ) -> ChannelInputFileRow:
        _access(session, channel_id, member, "read")
        row = session.get(ChannelInputFileRow, file_id)
        if (
            row is None
            or row.channel_id != channel_id
            or row.status == "deleted"
            or (row.message_id is None and row.uploader_id != member.user.id)
        ):
            raise _not_found()
        return row

    def get(self, member: Member, channel_id: str, file_id: str) -> ChannelInputFile:
        if _ID.fullmatch(file_id) is None:
            raise _not_found()
        with self.dstore.read() as session:
            return _file(self._visible(session, member, channel_id, file_id))

    def list_committed(self, member: Member, channel_id: str) -> list[ChannelInputFile]:
        with self.dstore.read() as session:
            _access(session, channel_id, member, "read")
            rows = session.scalars(
                select(ChannelInputFileRow)
                .where(
                    ChannelInputFileRow.channel_id == channel_id,
                    ChannelInputFileRow.message_id.is_not(None),
                    ChannelInputFileRow.status == "attached",
                )
                .order_by(ChannelInputFileRow.created_at.desc())
                .limit(MAX_CHANNEL_FILES)
            )
            return [_file(row) for row in rows]

    @staticmethod
    def _turn_scope(session: Session, turn_id: str) -> tuple[str, int]:
        turn = session.get(TurnRow, turn_id)
        if turn is None:
            raise _not_found()
        # A turn retains its sequence snapshot, but a removed human must not
        # keep reading channel bytes through an in-flight agent tool call.
        origin = turn
        seen: set[str] = set()
        while origin.author_kind != "human" and origin.parent_turn_id is not None:
            if origin.id in seen:
                raise _not_found()
            seen.add(origin.id)
            parent = session.get(TurnRow, origin.parent_turn_id)
            if parent is None or parent.channel_id != turn.channel_id:
                raise _not_found()
            origin = parent
        if origin.author_kind != "human" or origin.author_id is None:
            raise _not_found()
        _access(session, str(turn.channel_id), str(origin.author_id), "read")
        current = session.get(MessageRow, turn.input_message_id)
        if current is None or current.channel_id != turn.channel_id:
            raise _not_found()
        return str(turn.channel_id), int(current.sequence)

    def current_turn_files(self, turn_id: str) -> list[ChannelInputFile]:
        with self.dstore.read() as session:
            channel_id, _ = self._turn_scope(session, turn_id)
            turn = session.get(TurnRow, turn_id)
            assert turn is not None  # nosec B101 - checked by _turn_scope
            rows = session.scalars(
                select(ChannelInputFileRow)
                .where(
                    ChannelInputFileRow.channel_id == channel_id,
                    ChannelInputFileRow.message_id == turn.input_message_id,
                    ChannelInputFileRow.status == "attached",
                )
                .order_by(ChannelInputFileRow.position.asc())
            )
            return [_file(row) for row in rows]

    def list_for_turn(
        self, turn_id: str, *, offset: int, limit: int
    ) -> tuple[list[ChannelInputFile], int]:
        with self.dstore.read() as session:
            channel_id, sequence = self._turn_scope(session, turn_id)
            predicate = (
                ChannelInputFileRow.channel_id == channel_id,
                ChannelInputFileRow.status == "attached",
                MessageRow.sequence <= sequence,
            )
            base = (
                select(ChannelInputFileRow)
                .join(MessageRow, MessageRow.id == ChannelInputFileRow.message_id)
                .where(*predicate)
            )
            total = session.scalar(
                select(func.count())
                .select_from(ChannelInputFileRow)
                .join(MessageRow, MessageRow.id == ChannelInputFileRow.message_id)
                .where(*predicate)
            )
            rows = session.scalars(
                base.order_by(MessageRow.sequence.desc(), ChannelInputFileRow.position.asc())
                .offset(offset)
                .limit(limit)
            )
            return [_file(row) for row in rows], int(total or 0)

    def read_for_turn(
        self, turn_id: str, file_id: str, *, offset: int, limit: int
    ) -> tuple[ChannelInputFile, bytes]:
        if _ID.fullmatch(file_id) is None:
            raise _not_found()
        with self.dstore.read() as session:
            channel_id, sequence = self._turn_scope(session, turn_id)
            row = session.scalar(
                select(ChannelInputFileRow)
                .join(MessageRow, MessageRow.id == ChannelInputFileRow.message_id)
                .where(
                    ChannelInputFileRow.id == file_id,
                    ChannelInputFileRow.channel_id == channel_id,
                    ChannelInputFileRow.status == "attached",
                    MessageRow.sequence <= sequence,
                )
            )
            if row is None or row.size is None:
                raise _not_found()
            file = _file(row)
        with self.open_original(file_id, file.size or 0) as handle:
            handle.seek(offset)
            return file, handle.read(limit)

    def complete(
        self,
        member: Member,
        channel_id: str,
        file_id: str,
        temporary: Path,
        *,
        size: int,
        sha256: str,
        now: float,
    ) -> ChannelInputFile:
        if size > MAX_FILE_BYTES or size < 0:
            raise CollaborationError(
                "file_too_large", f"files are limited to {MAX_FILE_BYTES} bytes"
            )
        final = self.path(file_id)
        with self.dstore.immediate_transaction() as session:
            _access(session, channel_id, member, "post", now=now)
            row = session.get(ChannelInputFileRow, file_id)
            if row is None or row.channel_id != channel_id or row.uploader_id != member.user.id:
                raise _not_found()
            if row.status in ("uploaded", "attached"):
                if row.size != size or row.sha256 != sha256:
                    raise CollaborationError(
                        "idempotency_conflict", "file bytes differ from upload"
                    )
                return _file(row)
            if row.status != "reserved":
                raise _not_found()
            if row.declared_size is not None and row.declared_size != size:
                raise CollaborationError(
                    "file_size_mismatch", "file length differs from reservation"
                )
            used = session.scalar(
                select(func.coalesce(func.sum(ChannelInputFileRow.size), 0)).where(
                    ChannelInputFileRow.workspace_id == row.workspace_id,
                    ChannelInputFileRow.status.in_(("uploaded", "attached")),
                )
            )
            if int(used or 0) + size > MAX_WORKSPACE_BYTES:
                raise CollaborationError("storage_limit", "workspace file storage limit reached")
            # A crash between the rename and database commit leaves an
            # unreferenced original. The next identical upload can reclaim it.
            if final.is_symlink():
                raise CollaborationError("file_conflict", "stored bytes are unsafe")
            if final.exists():
                import hashlib

                digest = hashlib.sha256()
                with final.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
                if final.stat().st_size != size or digest.hexdigest() != sha256:
                    raise CollaborationError("file_conflict", "stored bytes differ from upload")
            else:
                temporary.replace(final)
            row.size = size
            row.sha256 = sha256
            row.status = "uploaded"
            row.uploaded_at = now
            session.flush()
            return _file(row)

    def cancel(self, member: Member, channel_id: str, file_id: str, now: float) -> None:
        original = self.path(file_id)
        with self.dstore.immediate_transaction() as session:
            _access(session, channel_id, member, "post", now=now)
            row = session.get(ChannelInputFileRow, file_id)
            if row is None or row.channel_id != channel_id or row.uploader_id != member.user.id:
                raise _not_found()
            if row.message_id is not None:
                raise CollaborationError("file_attached", "a sent file cannot be cancelled")
            row.status = "deleted"
            row.deleted_at = now
            session.flush()
        original.unlink(missing_ok=True)


__all__ = [
    "MAX_CHANNEL_FILES",
    "MAX_FILE_BYTES",
    "MAX_STAGED_PER_USER",
    "MAX_WORKSPACE_BYTES",
    "STAGED_TTL_S",
    "ChannelFileStore",
    "ChannelInputFile",
]
