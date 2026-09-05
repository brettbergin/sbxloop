"""Editing ``sbxloop.toml`` from the console: which file, how a draft is
validated (the real loader, against a scratch copy, with the same user
and environment layers the daemon sees) and how a save lands (atomic,
with a timestamped backup)."""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass
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


def read_text(path: Path) -> tuple[str, str | None]:
    """The file's text, or the template with a note saying why (unreadable,
    not UTF-8, gone between the check and the read)."""
    try:
        return path.read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return template_text(), None
    except (OSError, UnicodeDecodeError) as exc:
        return template_text(), f"could not read {path}: {exc}; showing the template"


@dataclass(frozen=True)
class Verdict:
    """What the loader made of a draft."""

    error: str | None
    #: Keys a repository-carried ``sbxloop.toml`` may not set: the loader
    #: ignores them with a warning, so a save would apply nothing for them.
    dropped: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def text(self) -> str:
        if self.error is not None:
            return f"draft refused: {self.error}"
        if self.dropped:
            return (
                "draft loads, but this sbxloop.toml is the repository's (tracked by git): "
                f"the daemon ignores {', '.join(self.dropped)} — operator settings belong "
                "in ~/.config/sbxloop/sbxloop.toml or the environment"
            )
        return "draft loads: the loader accepted it"


def validate_text(text: str, *, cwd: Path, env: Mapping[str, str]) -> Verdict:
    """The loader's own verdict on a draft, in place of the ``sbxloop.toml``
    at the root discovered from ``cwd`` — the same discovery, the same
    user, ``pyproject.toml`` and environment layers, the same project
    cut-down when the file is the repository's. Nothing is written."""
    dropped: list[str] = []
    try:
        load_config_with_sources(
            cwd=cwd, env=dict(env), sbxloop_toml_text=text, dropped_keys=dropped
        )
    except Exception as exc:
        return Verdict(str(exc))
    return Verdict(None, tuple(sorted(set(dropped))))


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
    "Verdict",
    "config_path",
    "read_text",
    "save_text",
    "template_text",
    "validate_text",
]
