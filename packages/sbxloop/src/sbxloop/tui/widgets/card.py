"""A bridge card (EmbedSpec) as a bordered panel."""

from __future__ import annotations

from textual.widgets import Static

from sbxloop.daemon.discord_format import EmbedSpec
from sbxloop.tui.format import card


class CardWidget(Static):
    def show(self, spec: EmbedSpec) -> None:
        self.update(card(spec))
