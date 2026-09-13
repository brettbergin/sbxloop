"""Comment-preserving, atomic updates to the home's ``secrets.env``.

Setup owns only the variables selected during its current run.  Existing
comments and unrelated variables stay byte-for-byte, an existing assignment
is replaced in place, and duplicate active assignments for an owned variable
are collapsed to one.  Secret files and their backups are always mode 0600.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import time
from collections.abc import Mapping
from io import StringIO
from pathlib import Path

from dotenv.parser import parse_stream

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _assignment(name: str, value: str) -> str:
    """Render one python-dotenv-compatible assignment without exposing it."""
    quoted = not value.isalnum()
    escaped = value.replace("'", "\\'")
    rendered = f"'{escaped}'" if quoted else value
    return f"{name}={rendered}\n"


def upsert_text(text: str, updates: Mapping[str, str]) -> str:
    """Return ``text`` with exactly one active assignment per updated name."""
    invalid = [name for name in updates if not _ENV_NAME.fullmatch(name)]
    if invalid:
        raise ValueError(f"invalid environment variable name: {invalid[0]!r}")

    remaining = dict(updates)
    rendered: list[str] = []
    for mapping in parse_stream(StringIO(text)):
        name = mapping.key
        if name not in updates:
            rendered.append(mapping.original.string)
            continue
        if name in remaining:
            rendered.append(_assignment(name, remaining.pop(name)))
        # A later active assignment for the same name is deliberately omitted.

    if remaining:
        if rendered and rendered[-1] and not rendered[-1].endswith(("\n", "\r")):
            rendered.append("\n")
        for name, value in remaining.items():
            rendered.append(_assignment(name, value))
    return "".join(rendered)


def save_text(path: Path, text: str, *, now: float | None = None) -> Path | None:
    """Atomically save a secret file and keep a private backup of the old one."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
    backup: Path | None = None
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copy2(path, backup)
        backup.chmod(0o600)

    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        tmp.chmod(0o600)
        tmp.replace(path)
        path.chmod(0o600)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    return backup


__all__ = ["save_text", "upsert_text"]
