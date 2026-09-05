"""A run's events as the transcript ``sbxloop run`` shows, or as the dense
lines ``sbxloop logs`` prints — the same renderers, fed from the store's
``seq`` tail."""

from __future__ import annotations

from typing import Any, Literal

from textual.widgets import RichLog

from sbxloop.cli.tui import format_event, render_event
from sbxloop_worker.protocol import Event

View = Literal["transcript", "lines"]


class ChronologyLog(RichLog):
    def __init__(self, view: View = "transcript", **kwargs: Any) -> None:
        super().__init__(wrap=True, markup=False, highlight=False, auto_scroll=True, **kwargs)
        self.view: View = view
        self.count = 0
        # Two loads can race the tail's cursor off-thread; the log itself
        # keeps the last sequence it rendered so a row is never shown twice.
        self.last_seq = 0

    def feed(self, events: list[tuple[int, Event]]) -> None:
        for seq, event in events:
            if seq <= self.last_seq:
                continue
            self.last_seq = seq
            if self.view == "transcript":
                rendered = render_event(event)
                if rendered is not None:
                    self.write(rendered)
                    self.count += 1
            else:
                self.write(format_event(event))
                self.count += 1

    def reset(self, view: View | None = None) -> None:
        if view is not None:
            self.view = view
        self.clear()
        self.count = 0
        self.last_seq = 0
