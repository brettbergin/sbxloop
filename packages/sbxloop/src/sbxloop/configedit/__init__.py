"""Editing the operator's ``config/sbxloop.toml`` one key at a time.

Host-side, with no console in it: :mod:`sbxloop.configedit.keys` says what a
dotted path accepts (from the ``Config`` model), :mod:`sbxloop.configedit.toml`
changes one assignment and keeps every comment, and
:mod:`sbxloop.configedit.edit` has the real loader judge the draft before an
atomic save keeps the previous file as a backup. The console, the CLI and
the daemon's own tools all go through here, so a key changed on any surface
lands in the same file the daemon reads, validated the same way.
"""

from __future__ import annotations

from sbxloop.configedit.editor import (
    Applies,
    Change,
    ConfigEditError,
    ConfigEditor,
    Row,
    answered_by,
    applies_for,
)

__all__ = [
    "Applies",
    "Change",
    "ConfigEditError",
    "ConfigEditor",
    "Row",
    "answered_by",
    "applies_for",
]
