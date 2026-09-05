"""Help: the keys, screen by screen, and where the data comes from."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Markdown

from sbxloop.tui.screens.base import ConsoleScreen

HELP = """\
# sbxloop tui

The operator console reads the daemon's `state.db` read-only and drives the
daemon through the same `ctl` queue `sbxloop daemon ctl` and chat's `!sbx` use.

## Screens

| key | screen |
| --- | --- |
| `1` | Overview — the run in flight, the queue, who waits on a human, recent runs |
| `2` | Runs — every run; `Enter` opens one |
| `3` | Queue — dispatch order and every work item |
| `?` | this help |

## Everywhere

`j`/`k` or the arrows move, `g`/`G` jump to the ends, `ctrl+d`/`ctrl+u`
page, `/` filters a list, `Esc` clears a filter or closes a screen, `r`
refreshes now, `q` quits.

## A run

Tabs: **Thread** (the transcript `sbxloop run` shows), **Tasks**,
**Phases** (every attempt with its tokens and turns; spend is never a
currency), **Landing** (the PR, the rounds, what CI and the reviewer last
said), **Artifacts**, **Events** (the dense `sbxloop logs` lines; `/` sets
a type prefix, `f` toggles follow).
"""


class HelpScreen(ConsoleScreen):
    def compose(self) -> ComposeResult:
        yield from self.compose_frame()
        with VerticalScroll(id="body"):
            yield Markdown(HELP)
        yield from self.compose_footer()
