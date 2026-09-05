"""The two-line bar every screen carries: the daemon, the run in flight,
the queue, the cap, the breaker, the bridge, versions and the clock."""

from __future__ import annotations

import time

from rich.text import Text
from textual.widgets import Static

from sbxloop.tui.data import ConsoleState
from sbxloop.tui.format import age, clock

#: The daemon's state markers, and their ASCII twins for `[tui] emoji = false`.
_GLYPHS = {
    "running": "●",
    "idle": "◌",
    "paused": "⏸",
    "breaker": "🛑",
    "starting": "…",
    "down": "✖",
    "stopping": "⏹",
}
_ASCII = {
    "running": "*",
    "idle": "-",
    "paused": "||",
    "breaker": "!!",
    "starting": "..",
    "down": "x",
    "stopping": "[]",
}


def status_lines(
    state: ConsoleState, *, now: float | None = None, emoji: bool = True, unread: int = 0
) -> Text:
    now = time.time() if now is None else now
    line = Text()
    daemon = state.daemon
    status = (daemon.status if daemon else None) or {}
    m = _GLYPHS if emoji else _ASCII
    if daemon is None:
        line.append(f"{m['idle']} probing", style="dim")
    elif daemon.starting:
        line.append(f"{m['starting']} starting", style="yellow")
    elif not daemon.live:
        line.append(f"{m['down']} daemon down", style="bold red")
    elif status.get("stopping"):
        line.append(f"{m['stopping']} stopping", style="yellow")
    elif status.get("breaker_open"):
        line.append(f"{m['breaker']} breaker open", style="bold red")
    elif status.get("paused"):
        holds = ", ".join(status.get("holds") or []) or "operator"
        line.append(f"{m['paused']} paused ({holds})", style="yellow")
    elif status.get("current"):
        line.append(f"{m['running']} running", style="bold green")
    else:
        line.append(f"{m['idle']} idle", style="green")
    current = status.get("current") or {}
    if current:
        run = str(current.get("run_id") or "")
        title = str(current.get("title") or "")
        line.append(f"  {run}", style="bold cyan")
        if title:
            line.append(f" · {title[:40]}", style="dim")
    if daemon and daemon.live:
        line.append(f"   queue {status.get('queued', 0)}")
        line.append(
            f"   runs {status.get('runs_today', 0)}/{status.get('max_runs_per_day', '?')} "
            f"{status.get('run_cap_timezone', 'UTC')}"
        )
        line.append(f"   failures {status.get('consecutive_failures', 0)}")
    elif state.items is not None:
        line.append(f"   queue {len(state.items.queued)}")
    if state.items is not None and (state.items.gates or state.items.holds):
        n_gates, n_holds = len(state.items.gates), len(state.items.holds)
        line.append(f"   waiting on a human: {n_gates} gate(s), {n_holds} hold(s)", style="yellow")
    second = Text()
    version = str(status.get("version") or state.version or "")
    second.append(f"sbxloop {version}" if version else "sbxloop", style="dim")
    if emoji:
        bridge = "✓" if state.bridge_alive else "✗"
    else:
        bridge = "ok" if state.bridge_alive else "down"
    second.append(f"   bridge {bridge}", style="green" if state.bridge_alive else "red")
    if state.errors:
        second.append(f"   {'⚠' if emoji else '!'} {state.errors[-1][:60]}", style="red")
    if state.daemon_started_at:
        second.append(f"   up since {age(state.daemon_started_at, now)}", style="dim")
    if daemon is not None and daemon.live:
        second.append(f"   ctl {daemon.latency_ms} ms", style="dim")
    if unread:
        second.append(f"   chat: {unread} new", style="bold magenta")
    if state.read_only:
        second.append("   [read-only]", style="bold yellow")
    second.append(f"   {clock(now)}", style="dim")
    line.append("\n")
    line.append_text(second)
    return line


class StatusBar(Static):
    DEFAULT_CSS = """
    StatusBar { height: 2; padding: 0 1; background: $surface; color: $text; }
    """

    last: Text = Text()

    def show(self, state: ConsoleState, *, emoji: bool = True, unread: int = 0) -> None:
        self.last = status_lines(state, emoji=emoji, unread=unread)
        self.update(self.last)
