"""Editing ``sbxloop.toml`` from the console: which file, how a draft is
validated (the real loader, against a scratch copy, with the same user
and environment layers the daemon sees) and how a save lands (atomic,
with a timestamped backup)."""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Mapping
from importlib import resources
from pathlib import Path

from sbxloop.config import discover_config, load_config_with_sources

CONFIG_FILENAME = "sbxloop.toml"


def config_path(cwd: Path) -> Path:
    """The ``sbxloop.toml`` the loader would read from ``cwd`` — the
    discovered root's, whether or not it exists yet."""
    return discover_config(cwd).root / CONFIG_FILENAME


def template_text() -> str:
    """The commented example, for a host that has no file yet."""
    return resources.files("sbxloop.data").joinpath("sbxloop.toml.example").read_text("utf-8")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else template_text()


def validate_text(text: str, *, scratch: Path, env: Mapping[str, str]) -> str | None:
    """The loader's own verdict on a draft: None when it loads, else the
    message ``sbxloop`` would print. The draft is written to ``scratch``
    and loaded from there, so the file on disk is untouched and the user
    config and environment layers still apply."""
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / CONFIG_FILENAME).write_text(text, encoding="utf-8")
    try:
        load_config_with_sources(cwd=scratch, env=dict(env))
    except Exception as exc:
        return str(exc)
    return None


def save_text(path: Path, text: str, *, now: float | None = None) -> Path | None:
    """Write atomically; an existing file is kept as ``<name>.bak-<stamp>``
    beside it. Returns the backup path, or None when there was no file."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
    backup: Path | None = None
    if path.is_file():
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return backup


__all__ = [
    "CONFIG_FILENAME",
    "config_path",
    "read_text",
    "save_text",
    "template_text",
    "validate_text",
]
