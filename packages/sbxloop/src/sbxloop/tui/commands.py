"""The command palette (``ctrl+p``): every screen and every argument-less
admin verb by name, so an operator who forgot the key types the verb.
Verbs that need a target (an item, a run, a sandbox) live on their
screens' rows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from textual.command import Hit, Hits, Provider

from sbxloop.tui import actions

if TYPE_CHECKING:
    from sbxloop.tui.app import SbxloopTui


@dataclass(frozen=True)
class Command:
    title: str
    help: str
    invoke: Callable[[SbxloopTui], None]
    mutating: bool = False


def _mode(name: str) -> Callable[[SbxloopTui], None]:
    def go(app: SbxloopTui) -> None:
        app.switch_mode(name)

    return go


def _act(build: Callable[[actions.Deps], actions.Action]) -> Callable[[SbxloopTui], None]:
    return lambda app: app.perform(build(app.deps))


CATALOGUE: tuple[Command, ...] = (
    Command("Overview", "the run in flight, the queue, who waits on a human", _mode("overview")),
    Command("Runs", "every run; Enter opens one", _mode("runs")),
    Command("Queue", "dispatch order and every work item", _mode("items")),
    Command("Chat", "the control channel: concierge, !sbx verbs, notices", _mode("chat")),
    Command("Sandboxes", "sbx ls against the store; shell, remove, prune, gc", _mode("sandboxes")),
    Command("Daemon", "the unit, the process, versions, repos, the journal", _mode("daemon")),
    Command("Help", "the keys, screen by screen", _mode("help")),
    Command(
        "Refresh now", "re-read the store and ask the daemon status", lambda a: a.action_refresh()
    ),
    Command(
        "Pause the daemon", "finish the current run, claim nothing new", _act(actions.pause), True
    ),
    Command("Resume the daemon", "release the operator hold", _act(actions.resume), True),
    Command(
        "Release every hold",
        "resume --all: the operator's and every automatic hold",
        _act(partial(actions.resume, every=True)),
        True,
    ),
    Command(
        "Cancel the current run",
        "ctl cancel: settled as cancelled",
        _act(actions.cancel_current),
        True,
    ),
    Command(
        "Cancel the current run and retry",
        "ctl cancel --retry: a fresh run for the same item",
        _act(partial(actions.cancel_current, retry=True)),
        True,
    ),
    Command(
        "Stop the daemon gracefully",
        "ctl stop: finish the run in flight and exit",
        _act(actions.stop_daemon),
        True,
    ),
    Command(
        "Start the unit",
        "systemctl --user start",
        _act(partial(actions.unit_verb, verb="start")),
        True,
    ),
    Command(
        "Stop the unit",
        "systemctl --user stop",
        _act(partial(actions.unit_verb, verb="stop")),
        True,
    ),
    Command(
        "Restart the unit",
        "systemctl --user restart",
        _act(partial(actions.unit_verb, verb="restart")),
        True,
    ),
    Command(
        "Start a daemon from this console",
        "no unit? spawn sbxloop daemon in its own session",
        _act(actions.spawn_daemon),
        True,
    ),
    Command(
        "Upgrade sbxloop", "run [daemon] upgrade_command on this host", _act(actions.upgrade), True
    ),
    Command("Quit", "leave the console", lambda a: a.call_next(a.action_quit)),
)


class ConsoleCommands(Provider):
    async def search(self, query: str) -> Hits:
        app: Any = self.app
        matcher = self.matcher(query)
        read_only = bool(getattr(app, "read_only", False))
        for command in CATALOGUE:
            if read_only and command.mutating:
                continue
            score = matcher.match(command.title)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(command.title),
                    partial(command.invoke, app),
                    help=command.help,
                )


__all__ = ["CATALOGUE", "Command", "ConsoleCommands"]
