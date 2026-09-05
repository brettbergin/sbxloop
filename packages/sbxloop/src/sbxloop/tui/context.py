"""The typed console app a widget or screen belongs to — resolved lazily,
so widget modules never import the app module at import time."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sbxloop.tui.app import SbxloopTui


def console_of(node: Any) -> SbxloopTui:
    from sbxloop.tui.app import SbxloopTui

    app = node.app
    assert isinstance(app, SbxloopTui)
    return app


__all__ = ["console_of"]
