"""Snapshots of what a home cannot regenerate: ``sbxloop backup``.

A backup is one directory under ``backups/<stamp>[-label]/`` holding the
config (``sbxloop.toml``, ``secrets.env``, the App key), an online copy of
``state.db`` (SQLite's backup API, so a live WAL database is copied
consistently), the rendered unit files, the launchers and ``home.json``,
plus ``MANIFEST`` (sha256 and size per file) and ``meta.json`` (who, when,
which version, why). Runs, workspaces, caches and the venv are not in it:
every one of those is rebuilt from a repository or an index.
Channel input originals are durable user data and are included with their
database rows.

``init --migrate`` takes one before it moves anything, the deploy takes one
before it installs, and an operator takes one with ``sbxloop backup``
before editing config by hand — the seven hand-made ``sbxloop.toml.pre-*``
copies on the first host are what this replaces. ``restore`` puts the
files back (the daemon must be stopped: it holds the database open) and
the daemon's daily sweep keeps the newest ``[daemon] backups_keep``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sbxloop
from sbxloop.errors import SbxloopError
from sbxloop.hostfiles import make_private
from sbxloop.log import get_logger
from sbxloop.paths import SbxloopHome

log = get_logger(__name__)

MANIFEST = "MANIFEST"
META = "meta.json"
DB_NAME = "state.db"
#: What a backup carries, relative to the home: the files a host cannot
#: regenerate. The database is copied through SQLite, not the filesystem.
CONFIG_FILES: tuple[str, ...] = ("sbxloop.toml", "secrets.env", "github-app.pem")
_CHANNEL_FILE_ID = re.compile(r"fin_[0-9abcdefghjkmnpqrstvwxyz]{24}\Z")


class BackupError(SbxloopError):
    pass


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    stamp: str
    label: str
    created_at: str
    sbxloop_version: str
    files: int
    bytes: int

    @property
    def name(self) -> str:
        return self.path.name


def stamp(clock: Callable[[], float] = time.time) -> str:
    return datetime.fromtimestamp(clock(), UTC).strftime("%Y%m%dT%H%M%SZ")


def _copy_db(source: Path, target: Path) -> None:
    """A consistent copy of a possibly-live WAL database."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(
    home: SbxloopHome,
    *,
    label: str = "",
    reason: str = "",
    extra: dict[str, Path] | None = None,
    clock: Callable[[], float] = time.time,
) -> BackupInfo:
    """Snapshot the home into ``backups/<stamp>[-label]/``.

    ``extra`` names additional files to carry (``{"legacy/secrets.env":
    path}``) — what ``init --migrate`` uses to keep the pre-home files.
    """
    if label and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", label):
        raise BackupError(f"label {label!r}: letters, digits, dots, - and _ only")
    name = stamp(clock) + (f"-{label}" if label else "")
    target = home.backups / name
    if target.exists():
        raise BackupError(f"{target} already exists")
    target.mkdir(parents=True)
    copied: list[tuple[str, Path]] = []
    for filename in CONFIG_FILES:
        src = home.config / filename
        if src.is_file():
            dst = target / "config" / filename
            dst.parent.mkdir(exist_ok=True)
            shutil.copy2(src, dst)
            copied.append((f"config/{filename}", dst))
    if home.state_db.is_file():
        dst = target / "state" / DB_NAME
        dst.parent.mkdir(exist_ok=True)
        _copy_db(home.state_db, dst)
        copied.append((f"state/{DB_NAME}", dst))
        # Read the same database snapshot this backup will restore. A live
        # cancellation may remove bytes after it; fail rather than publish a
        # backup whose database points at a missing or different original.
        # This copy is already complete. Open it immutable so SQLite cannot
        # leave -wal/-shm sidecars inside the finished backup.
        with sqlite3.connect(f"file:{dst}?mode=ro&immutable=1", uri=True) as snapshot:
            has_files = snapshot.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='collaboration_input_files'"
            ).fetchone()
            originals = (
                snapshot.execute(
                    "SELECT id, size, sha256 FROM collaboration_input_files "
                    "WHERE status IN ('uploaded', 'attached') ORDER BY id"
                ).fetchall()
                if has_files
                else []
            )
        for file_id, size, digest in originals:
            if not isinstance(file_id, str) or _CHANNEL_FILE_ID.fullmatch(file_id) is None:
                raise BackupError("database contains an invalid channel input file id")
            src = home.channel_files / file_id
            if src.is_symlink() or not src.is_file():
                raise BackupError(f"channel input {file_id} is missing or unsafe")
            rel = f"channel-files/{file_id}"
            original = target / rel
            original.parent.mkdir(mode=0o700, exist_ok=True)
            if home.os_name == "nt":
                make_private(original.parent, os_name=home.os_name)
            else:
                original.parent.chmod(0o700)
            shutil.copy2(src, original)
            make_private(original, os_name=home.os_name)
            if original.stat().st_size != size or _sha256(original) != digest:
                raise BackupError(f"channel input {file_id} changed during backup")
            copied.append((rel, original))
    for src in sorted(home.systemd.glob("*.service")) if home.systemd.is_dir() else []:
        dst = target / "systemd" / src.name
        dst.parent.mkdir(exist_ok=True)
        shutil.copy2(src, dst)
        copied.append((f"systemd/{src.name}", dst))
    for src in (home.launcher, home.sbx_launcher, home.record):
        if src.is_file():
            rel_path = src.relative_to(home.root)
            dst = target / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append((str(rel_path), dst))
    for rel, src in (extra or {}).items():
        if src.is_file():
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.name == DB_NAME:
                _copy_db(src, dst)
            else:
                shutil.copy2(src, dst)
            copied.append((rel, dst))
    # Secrets stay private in the snapshot too.
    for rel, dst in copied:
        if rel.endswith(("secrets.env", ".pem", ".env")):
            make_private(dst, os_name=home.os_name)
    lines = [f"{_sha256(dst)}  {dst.stat().st_size:>10}  {rel}" for rel, dst in copied]
    (target / MANIFEST).write_text("\n".join(lines) + ("\n" if lines else ""))
    total = sum(dst.stat().st_size for _rel, dst in copied)
    meta = {
        "stamp": name,
        "label": label,
        "reason": reason,
        "created_at": datetime.fromtimestamp(clock(), UTC).isoformat(timespec="seconds"),
        "sbxloop_version": sbxloop.__version__,
        "files": len(copied),
        "bytes": total,
    }
    (target / META).write_text(json.dumps(meta, indent=2) + "\n")
    log.info("backup.created", path=str(target), files=len(copied), bytes=total, label=label)
    return BackupInfo(
        path=target,
        stamp=name,
        label=label,
        created_at=str(meta["created_at"]),
        sbxloop_version=sbxloop.__version__,
        files=len(copied),
        bytes=total,
    )


def list_backups(home: SbxloopHome) -> list[BackupInfo]:
    """Newest first. A directory without ``meta.json`` is not a backup."""
    found: list[BackupInfo] = []
    if not home.backups.is_dir():
        return found
    for path in sorted(home.backups.iterdir(), reverse=True):
        meta_path = path / META
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except ValueError:
            continue
        found.append(
            BackupInfo(
                path=path,
                stamp=str(meta.get("stamp", path.name)),
                label=str(meta.get("label", "")),
                created_at=str(meta.get("created_at", "")),
                sbxloop_version=str(meta.get("sbxloop_version", "")),
                files=int(meta.get("files", 0)),
                bytes=int(meta.get("bytes", 0)),
            )
        )
    return found


def find_backup(home: SbxloopHome, name: str) -> BackupInfo:
    for info in list_backups(home):
        if info.name == name or info.stamp == name:
            return info
    raise BackupError(f"no backup {name!r} under {home.backups}")


def restore_backup(home: SbxloopHome, name: str, *, daemon_live: bool = False) -> list[str]:
    """Put a backup's files back. Refuses while the daemon runs: it holds
    ``state.db`` open, and a config it did not read is a surprise on the
    next restart. Returns the files restored, relative to the home."""
    if daemon_live:
        raise BackupError(
            "the daemon is running; stop it (systemctl --user stop sbxloop-daemon) first"
        )
    info = find_backup(home, name)
    # Verify every backed-up byte before overwriting a live home.
    manifest = (info.path / MANIFEST).read_text().splitlines()
    verified: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for line in manifest:
        match = re.fullmatch(r"([0-9a-f]{64})\s+(\d+)\s+(.+)", line)
        if match is None:
            raise BackupError("backup manifest is malformed")
        digest, size, entry_name = match.groups()
        rel = Path(entry_name)
        if rel.is_absolute() or rel in seen or any(part in (".", "..") for part in rel.parts):
            raise BackupError("backup manifest contains an unsafe or duplicate path")
        seen.add(rel)
        src = info.path / rel
        if (
            src.is_symlink()
            or not src.is_file()
            or not src.resolve().is_relative_to(info.path.resolve())
            or src.stat().st_size != int(size)
            or _sha256(src) != digest
        ):
            raise BackupError(f"backup file {entry_name!r} failed integrity verification")
        verified.append((rel, src))
    restored: list[str] = []
    for rel, src in sorted(verified):
        if rel.parts[0] == "legacy":
            continue
        dst = home.root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if rel.name == DB_NAME:
            # The backup's database is already a consistent SQLite snapshot.
            # Reopening the live destination with SQLite after removing its
            # WAL sidecars can fail when it previously used WAL mode. Replace
            # it from a private same-directory temporary file instead.
            fd, temporary_name = tempfile.mkstemp(prefix="state-restore-", dir=dst.parent)
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                shutil.copyfile(src, temporary)
                make_private(temporary, os_name=home.os_name)
                for suffix in ("-wal", "-shm"):
                    dst.with_name(dst.name + suffix).unlink(missing_ok=True)
                temporary.replace(dst)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            shutil.copy2(src, dst)
        if rel.parts[0] == "channel-files" or rel.name.endswith(("secrets.env", ".pem")):
            if rel.parts[0] == "channel-files":
                if home.os_name == "nt":
                    make_private(dst.parent, os_name=home.os_name)
                else:
                    dst.parent.chmod(0o700)
            make_private(dst, os_name=home.os_name)
        restored.append(str(rel))
    log.info("backup.restored", path=str(info.path), files=len(restored))
    return restored


def prune_backups(home: SbxloopHome, *, keep: int) -> list[BackupInfo]:
    """Remove all but the newest ``keep``; ``keep <= 0`` keeps everything."""
    if keep <= 0:
        return []
    removed: list[BackupInfo] = []
    for info in list_backups(home)[keep:]:
        shutil.rmtree(info.path)
        removed.append(info)
    if removed:
        log.info("backup.pruned", removed=[i.name for i in removed], keep=keep)
    return removed


__all__ = [
    "CONFIG_FILES",
    "MANIFEST",
    "META",
    "BackupError",
    "BackupInfo",
    "create_backup",
    "find_backup",
    "list_backups",
    "prune_backups",
    "restore_backup",
    "stamp",
]
