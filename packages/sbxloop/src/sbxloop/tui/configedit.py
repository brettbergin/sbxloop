"""Editing the operator's configuration from the console: which file, how
a draft is validated (the real loader, against a scratch copy, with every
other layer the daemon sees) and how a save lands (atomic, with a
timestamped backup).

**Which file.** The home's ``config/sbxloop.toml`` — what ``sbxloop init``
writes, what a deploy preserves and what ``sbxloop backup`` snapshots. It
is the operator's file by construction: the loader reads it out of the
home whatever directory anything was started in, so the console edits the
same file the daemon reads no matter where either was launched. A
``sbxloop.toml`` in a working directory is *project* config a repository
carries, and the console does not write to it.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from sbxloop.config import Config, load_config_with_sources
from sbxloop.paths import SbxloopHome

CONFIG_FILENAME = "sbxloop.toml"

#: What :func:`load_config_with_sources` calls the layer this module edits.
#: A key whose source is anything else is not answered by this file, however
#: the save went.
FILE_LAYER = "home config"


def config_path(home: SbxloopHome) -> Path:
    """The operator config ``home`` carries, whether or not it exists yet."""
    return home.config_toml


def home_env(home: SbxloopHome, env: Mapping[str, str]) -> dict[str, str]:
    """``env`` with the home the console is attached to named in it.

    The loader finds the operator config through ``SBXLOOP_HOME``/``HOME``,
    not through any argument, so a console pointed at another home with
    ``--state-dir`` would otherwise read one file and write another. Both
    ``load`` and ``validate`` go through here, so what the resolved view
    shows and what a save changes are always the same file."""
    return {**env, "SBXLOOP_HOME": str(home.root)}


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
    #: What the draft resolves to, every layer applied, and which layer
    #: answered for each key — how a single-key edit finds out that another
    #: layer still wins. ``None``/empty when the draft was refused.
    #: Excluded from equality: a Config is compared by every field it has,
    #: and nothing needs that here.
    config: Config | None = dataclasses.field(default=None, compare=False, repr=False)
    sources: Mapping[str, str] = dataclasses.field(default_factory=dict, compare=False, repr=False)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def text(self) -> str:
        if self.error is not None:
            return f"draft refused: {self.error}"
        return "draft loads: the loader accepted it"


def validate_text(text: str, *, home: SbxloopHome, env: Mapping[str, str]) -> Verdict:
    """The loader's own verdict on a draft, in place of ``home``'s
    ``config/sbxloop.toml`` — every other layer still applied, and resolved
    from the home, which is where the daemon runs. Nothing is written."""
    try:
        config, sources = load_config_with_sources(
            cwd=home.root, env=home_env(home, env), home_config_text=text
        )
    except Exception as exc:
        return Verdict(str(exc))
    return Verdict(None, config, sources)


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
    "FILE_LAYER",
    "Verdict",
    "config_path",
    "home_env",
    "read_text",
    "save_text",
    "template_text",
    "validate_text",
]
