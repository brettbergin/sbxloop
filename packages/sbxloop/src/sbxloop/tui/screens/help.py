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
| `4` | Chat — the control channel: the concierge, `!sbx` verbs, daemon notices |
| `?` | this help |

## Everywhere

`j`/`k` or the arrows move, `g`/`G` jump to the ends, `ctrl+d`/`ctrl+u`
page, `/` filters a list, `Esc` clears a filter or closes a screen, `r`
refreshes now, `q` quits.

## Chat

The Chat screen and a run's **Thread** tab are the daemon's local chat
bridge — the same rows Discord or Slack would show. `!sbx …` is a command.
A message **addressed to the bot** — `@sbx` in the text, `ctrl+t` to keep
it on, or `r` to reply to the bot's last row — is a concierge turn in the
control channel and a **steer** in a run's thread. Plain text is left
alone, as on Discord. `Esc` leaves the form and `i` returns to it: while
the form is focused every key types (so `q` and the mode numbers act only
after `Esc`). With it left, on the Chat screen a question with clickable
answers takes `1`-`5` (or a click; with no question open the numbers are
the mode keys again), `r` replies to the bot's latest row, and on a run
with no thread `r` refreshes. In a run's thread answer with the buttons or
by typing the number. A merge gate shows **Approve merge**;
`!sbx merge <item>` is its typed twin.

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
