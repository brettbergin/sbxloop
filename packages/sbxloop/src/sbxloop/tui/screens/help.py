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
| `5` | Sandboxes — `sbx ls` classified against the store; shell, remove, prune, gc |
| `6` | Daemon — the unit, the process, versions, repositories, the journal |
| `7` | Config — the resolved configuration with its sources, the policy, the repos, an editor |
| `8` | Doctor — the host checks and the sbx conformance verdicts; `S` the secret registrations |
| `?` | this help |
| `ctrl+p` | the command palette: every screen and every argument-less verb by name |

## Everywhere

`j`/`k` or the arrows move, `g`/`G` jump to the ends, `ctrl+d`/`ctrl+u`
page, `/` filters a list, `Esc` clears a filter or closes a screen, `r`
refreshes now, `q` quits.

A verb that changes something asks first: `y`/`n` for a bounded one, the
target's name (or the verb) typed out for a destructive one — removing a
sandbox, stopping or restarting the unit, abandoning an item, pruning,
gc, upgrading. Releasing a hold, re-checking a review, resuming a
repository and asking the concierge just run. A verb the daemon executes
(`pause`, `cancel`, `merge`, `retry` …) needs a live daemon; the item
verbs fall back to the CLI's row-only twin when none is running.
`--read-only` removes them all, sandbox shells included.

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
**Phases** (every attempt with its tokens and turns, per persona; spend is
never a currency), **Landing** (the PR, the rounds, what CI and the
reviewer last said), **Artifacts**, **Events** (the dense `sbxloop logs`
lines; `/` sets a type prefix, `f` toggles follow).

The header lists the verbs that apply: `c` cancel (the daemon's current
run through `ctl cancel`; any other in-flight run as the `sbxloop cancel`
store write), `C` cancel and retry, `R` retry the item (or, for a run with
no item, resume it as a detached process here), `u` requeue, `A` abandon,
`m` approve the merge gate, `w` check a review wait now, `+` grant fix
rounds, `s`/`S` a shell in the agent / github sandbox (the console hands
over the terminal and takes it back).

## Queue

`t` retry, `u` requeue, `A` abandon (typed), `w` check the review now,
`m` approve the merge gate, `n` a new run the daemon's way (the concierge
files the issue), `N` a detached `sbxloop run` on this host.

## Sandboxes

`s` shell, `x` remove (typed name), `X` stop, `P` prune the orphans
(typed `prune`), `G` remove run directories past retention (typed `gc`),
`k` include kept sandboxes in the orphan verdicts.

## Daemon

`p` pause, `u` resume, `a` release every hold, `c`/`C` cancel the current
run (and retry), `g` graceful stop (typed `stop`), `S`/`T`/`B` start /
stop / restart the systemd user unit (typed for stop and restart), `D`
spawn a daemon from this console when there is no unit (`e` stops it),
`U` run `[daemon] upgrade_command` (typed `upgrade`), `R` resume polling a
suspended repository. The journal: `/` grep, `l` cycle the level floor,
`f` follow.

## Config

Resolved: `/` filters keys, values and sources (a non-default source is
bold). Edit: `i` focuses the draft of `sbxloop.toml` (`Esc` leaves it),
`V` validates it with the real loader, `W` (or `ctrl+s`) validates and
saves it — typed `save`; the previous file is kept as a timestamped backup
and a restart is offered — `E` opens `$EDITOR` on it, `L` reloads from
disk.

## Doctor and secrets

`d` runs the host checks and the cheap conformance probes, `D` the live
ones (boots a scratch sandbox), `p` asks GitHub about each repository from
a github-ops sandbox. `S` opens the secret registrations: `x` cleans the
stale ones (a dry run first, then typed `clean`), `X` every sbxloop-owned
one, `K` rotates the agent credential's registration from a hidden prompt
(typed `rotate`).
"""


class HelpScreen(ConsoleScreen):
    def compose(self) -> ComposeResult:
        yield from self.compose_frame()
        with VerticalScroll(id="body"):
            yield Markdown(HELP)
        yield from self.compose_footer()
