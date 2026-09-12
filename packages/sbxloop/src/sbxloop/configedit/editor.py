"""One object over the editor modules, for every surface that changes a key.

The console, ``sbxloop config set`` and the daemon's own tools all want the
same four things: the resolved view with the layer each key came from
(:meth:`ConfigEditor.resolved`), one key's card
(:meth:`ConfigEditor.describe`), a change that the real loader has judged
before anything is written (:meth:`ConfigEditor.set` /
:meth:`ConfigEditor.unset`), and the atomic save with its backup
(:meth:`ConfigEditor.commit`). Each surface renders the result its own way;
none of them re-derives which layer wins or whether a restart is needed.

Whether a change applies live is stated once, in :func:`applies_for`: the
model keys are re-read before every agent phase and concierge turn
(``sbxloop.agentmodels.refreshed_models``); everything else the daemon reads
at start.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sbxloop.config import load_config_with_sources
from sbxloop.configedit import keys as configkeys, toml as configtoml
from sbxloop.configedit.docs import doc_for
from sbxloop.configedit.edit import (
    FILE_LAYER,
    Verdict,
    config_path,
    home_env,
    read_text,
    save_text,
    validate_text,
)
from sbxloop.paths import SbxloopHome

#: When a written change is what the loop sees: ``live`` before the next
#: agent phase or concierge turn, ``restart`` at the daemon's next start.
Applies = Literal["live", "restart"]
NoteLevel = Literal["information", "warning"]


class ConfigEditError(ValueError):
    """A key that cannot be edited as asked, in the operator's terms."""


def applies_for(dotted: str) -> Applies:
    """When a change to ``dotted`` takes effect."""
    return "live" if configkeys.is_model_key(dotted) else "restart"


@dataclass(frozen=True)
class Row:
    """One resolved key."""

    key: str
    value: Any
    #: The layer that answers for the key (``home config``, ``env``,
    #: ``default``…), or ``unset`` for a path the resolved view has no
    #: value at.
    source: str
    spec: configkeys.FieldSpec
    doc: str | None
    applies: Applies
    #: Whether the operator's file itself says anything at this path —
    #: distinct from ``source``, which may name a layer above it.
    in_file: bool

    @property
    def display(self) -> str:
        return configkeys.display(self.value)


@dataclass(frozen=True)
class Change:
    """A draft the loader has judged; nothing is written until :meth:`ConfigEditor.commit`."""

    key: str
    #: The whole file with the change applied.
    text: str
    verdict: Verdict
    old: Any
    new: Any
    unset: bool
    applies: Applies
    #: Which layer answers once the draft applies, when it is not this
    #: file: written-but-shadowed, or unset-and-fell-back-to.
    note: str | None
    note_level: NoteLevel

    @property
    def ok(self) -> bool:
        return self.verdict.ok


def answered_by(key: str, verdict: Verdict, *, unset: bool) -> tuple[str | None, NoteLevel]:
    """Which layer answers for ``key`` once ``verdict``'s draft applies.

    The file is written either way — the operator asked for it — but a key
    the environment or a ``sbxloop.toml`` in the home also sets would
    otherwise look applied when it is not, and an unset key should say what
    it fell back to. ``None`` when this file is the layer that answers."""
    if verdict.config is None:
        return None, "information"
    source = configkeys.source_for(key, verdict.sources)
    if source == FILE_LAYER:
        return None, "information"
    value = configkeys.flatten(verdict.config.model_dump(mode="json")).get(key)
    if unset:
        where = "its default" if source == "default" else source
        return f"{key} unset here; it now comes from {where}: {value!r}", "information"
    return (
        f"{key} is written, but {source} sets it too and wins: the loop still sees {value!r}",
        "warning",
    )


class ConfigEditor:
    """The operator config of one home, edited one key at a time."""

    def __init__(self, home: SbxloopHome, env: Mapping[str, str]) -> None:
        self.home = home
        self.env = home_env(home, env)
        self.path: Path = config_path(home)

    # -- reading ------------------------------------------------------------

    def file_text(self) -> str:
        """The file as it is on disk right now (the template when absent)."""
        return read_text(self.path)[0]

    def resolved(self) -> list[Row]:
        """Every leaf of the resolved configuration, by key."""
        config, sources = load_config_with_sources(cwd=self.home.root, env=self.env)
        flat = configkeys.flatten(config.model_dump(mode="json"))
        text = self.file_text()
        return [self._row(key, flat, sources, text) for key in sorted(flat)]

    def describe(self, dotted: str) -> Row:
        """One key's card. An env-only key is refused by name."""
        key = self._key(dotted)
        config, sources = load_config_with_sources(cwd=self.home.root, env=self.env)
        flat = configkeys.flatten(config.model_dump(mode="json"))
        return self._row(key, flat, sources, self.file_text())

    def _row(self, key: str, flat: Mapping[str, Any], sources: Mapping[str, str], text: str) -> Row:
        _, in_file = configtoml.file_value(text, configkeys.parse_path(key))
        return Row(
            key=key,
            value=flat.get(key),
            source=configkeys.source_for(key, sources) if key in flat else "unset",
            spec=configkeys.describe(key),
            doc=doc_for(key),
            applies=applies_for(key),
            in_file=in_file,
        )

    # -- changing -------------------------------------------------------------

    def set(self, dotted: str, value_text: str) -> Change:
        """``dotted`` set to what ``value_text`` means for its spec, judged
        by the loader with every other layer applied. Nothing is written."""
        key = self._key(dotted)
        spec = configkeys.describe(key)
        try:
            value = configkeys.parse_value(value_text, spec)
        except ValueError as exc:
            raise ConfigEditError(str(exc)) from None
        return self._change(key, value, unset=False)

    def unset(self, dotted: str) -> Change:
        """``dotted`` removed from the file, so the layer beneath answers."""
        return self._change(self._key(dotted), None, unset=True)

    def commit(self, change: Change, *, now: float | None = None) -> Path | None:
        """Write the judged draft; the previous file is kept beside it.
        Returns the backup path, or None when there was no file."""
        if not change.ok:
            raise ConfigEditError(f"{change.key}: {change.verdict.text}")
        return save_text(self.path, change.text, now=now)

    def _change(self, key: str, value: Any, *, unset: bool) -> Change:
        parts = configkeys.parse_path(key)
        current = self.file_text()
        old, _ = configtoml.file_value(current, parts)
        try:
            text = (
                configtoml.unset_value(current, parts)
                if unset
                else configtoml.set_value(current, parts, value)
            )
        except ValueError as exc:
            raise ConfigEditError(f"{key}: {exc}") from None
        if text == current:
            raise ConfigEditError(f"{key} already says that")
        verdict = validate_text(text, home=self.home, env=self.env)
        note, level = (
            answered_by(key, verdict, unset=unset) if verdict.ok else (None, "information")
        )
        return Change(
            key=key,
            text=text,
            verdict=verdict,
            old=old,
            new=value,
            unset=unset,
            applies=applies_for(key),
            note=note,
            note_level=level,
        )

    @staticmethod
    def _key(dotted: str) -> str:
        try:
            key = configkeys.format_path(configkeys.parse_path(dotted))
        except configkeys.PathError as exc:
            raise ConfigEditError(str(exc)) from None
        why = configkeys.ENV_ONLY_KEYS.get(key)
        if why is not None:
            raise ConfigEditError(f"{key} is not a file setting: {why}")
        return key


__all__ = [
    "Applies",
    "Change",
    "ConfigEditError",
    "ConfigEditor",
    "NoteLevel",
    "Row",
    "answered_by",
    "applies_for",
]
