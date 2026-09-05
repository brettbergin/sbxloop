"""Every admin verb the console offers, as a plain function over injected
dependencies: what it asks the daemon (``ctl``), what it runs on the host
(the runner), what it writes when no daemon is up (the row-only twins the
CLI has), and how it must be confirmed. Screens and the command palette
build an :class:`Action` here and hand it to the app, which owns the
confirmation, the worker and the outcome toast — so the confirmation
tiers and the read-only refusal live in exactly one place."""

from __future__ import annotations

import shlex
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from sbxloop.config import TUI_CONTROL_CHANNEL, Config
from sbxloop.daemon.control import plain
from sbxloop.daemon.mailbox import MailboxClient
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import TERMINAL_RUN_STATES, RunRecord
from sbxloop.engine.store import StateStore
from sbxloop.errors import SbxloopError
from sbxloop.gc import format_bytes, prune_run_dirs
from sbxloop.ghids import normalize_item_id
from sbxloop.sbx.cli import INTERACTIVE_SHELL_ARGV, SbxCLI
from sbxloop.sbx.prune import SandboxVerdict, remove_run_sandbox, remove_sandbox
from sbxloop.tui.data import CtlClient, DaemonSnapshot
from sbxloop.tui.runner import ChildHandle, CommandRunner, sbxloop_argv
from sbxloop.tui.system import unit_argv

#: Item verbs cross the ops sandbox; the ctl client's own default.
CTL_TIMEOUT_S = 30.0
#: An upgrade command installs packages; generous, and the outcome shows.
UPGRADE_TIMEOUT_S = 900.0
#: What the CLI prints after a row-only item verb, in plain text.
ROW_ONLY_NOTE = (
    "no daemon is running: the row was changed in the store; the next daemon start "
    "reports it to the source and closes a dead run. The item's next dispatch, if "
    "any, starts a fresh run."
)

Confirm = Literal["none", "yes", "typed"]


@dataclass(frozen=True)
class Outcome:
    ok: bool
    text: str
    #: Long output (a prune table, an upgrade log) opens a screen, not a toast.
    long: bool = False


@dataclass(frozen=True)
class Action:
    """One verb, ready to perform: what to run and how to ask first."""

    title: str
    run: Callable[[], Outcome]
    confirm: Confirm = "yes"
    prompt: str = ""
    #: For ``confirm="typed"``: what the operator must type, exactly.
    typed: str = ""
    mutating: bool = True
    #: Refused while the daemon is down or still starting.
    needs_live: bool = False
    #: Run under ``App.suspend`` with the terminal attached, instead of ``run``.
    interactive: tuple[str, ...] | None = None


@dataclass
class Children:
    """Processes the console spawned and still owns (a daemon started here)."""

    by_name: dict[str, ChildHandle] = field(default_factory=dict)

    def add(self, name: str, handle: ChildHandle) -> None:
        self.by_name[name] = handle

    def alive(self) -> dict[str, ChildHandle]:
        return {name: h for name, h in self.by_name.items() if h.poll() is None}


@dataclass
class Deps:
    ctl: CtlClient
    runner: CommandRunner
    mailbox: MailboxClient
    config: Config
    state_dir: Path
    unit: str
    operator: str
    sbx: Callable[[], SbxCLI]
    daemon: Callable[[], DaemonSnapshot | None]
    read_only: bool = False
    clock: Callable[[], float] = time.time
    cwd: Path = field(default_factory=Path.cwd)
    children: Children = field(default_factory=Children)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "state.db"

    def daemon_live(self) -> bool:
        snapshot = self.daemon()
        return snapshot is not None and snapshot.live

    def daemon_starting(self) -> bool:
        snapshot = self.daemon()
        return snapshot is not None and snapshot.starting

    @property
    def console_dir(self) -> Path:
        """Where the console keeps the logs of what it spawned."""
        return self.state_dir / "console"


# -- the daemon's own verbs, over ctl -------------------------------------------


def ctl_outcome(deps: Deps, cmd: str, *, timeout_s: float = CTL_TIMEOUT_S) -> Outcome:
    """One ctl round trip, read the way ``sbxloop daemon ctl`` reads it:
    no taker is "no daemon", a claimed-but-unanswered request is still
    executing and is never reported as not done."""
    try:
        reply = deps.ctl.submit(cmd, timeout_s=timeout_s)
    except Exception as exc:
        return Outcome(False, f"ctl failed: {exc}")
    if reply is None:
        return Outcome(
            False,
            f"no daemon took `{cmd}` within {timeout_s:g}s — is it running against "
            f"{deps.state_dir}?",
        )
    text = plain(reply.text)
    if reply.pending:
        return Outcome(False, f"pending: {text}")
    return Outcome(bool(reply.ok), text, long=text.count("\n") > 6)


def ctl_action(
    deps: Deps,
    cmd: str,
    *,
    title: str,
    confirm: Confirm = "yes",
    prompt: str = "",
    typed: str = "",
) -> Action:
    return Action(
        title,
        lambda: ctl_outcome(deps, cmd),
        confirm=confirm,
        prompt=prompt or f"{title}?",
        typed=typed,
        needs_live=True,
    )


def pause(deps: Deps, hold: str | None = None) -> Action:
    cmd = f"pause --hold {hold}" if hold else "pause"
    return ctl_action(
        deps,
        cmd,
        title="pause the daemon",
        prompt="Pause the daemon? The current run finishes; nothing new is claimed.",
    )


def resume(deps: Deps, hold: str | None = None, *, every: bool = False) -> Action:
    if every:
        cmd, title = "resume --all", "release every hold"
    elif hold:
        cmd, title = f"resume --hold {hold}", f"release the {hold} hold"
    else:
        cmd, title = "resume", "resume the daemon"
    return ctl_action(deps, cmd, title=title, confirm="none")


def stop_daemon(deps: Deps) -> Action:
    return ctl_action(
        deps,
        "stop",
        title="stop the daemon gracefully",
        confirm="typed",
        typed="stop",
        prompt=(
            "Stop the daemon? It claims nothing new, finishes the run in flight and exits; "
            "under systemd the unit restarts it (use the unit's stop for good). "
            "Cancel the current run first to stop that now."
        ),
    )


def cancel_current(deps: Deps, *, retry: bool = False) -> Action:
    cmd = "cancel --retry" if retry else "cancel"
    what = "cancel the current run and queue a fresh one" if retry else "cancel the current run"
    return ctl_action(
        deps,
        cmd,
        title=what,
        prompt=f"{what.capitalize()}? The issue is told it was cancelled by {deps.operator}.",
    )


def merge(deps: Deps, target: str) -> Action:
    return ctl_action(
        deps,
        f"merge {target}",
        title=f"approve the merge of {target}",
        prompt=f"Approve the merge gate for {target}? The daemon merges its PR now.",
    )


def grant_rounds(deps: Deps, run_id: str, rounds: int) -> Action:
    return ctl_action(
        deps,
        f"grant-rounds {run_id} {rounds}",
        title=f"grant {rounds} more fix round(s) to {run_id}",
        prompt=f"Give {run_id} {rounds} more fix round(s) and resume it now?",
    )


def resume_review(deps: Deps, target: str) -> Action:
    return ctl_action(
        deps,
        f"resume {target}",
        title=f"check {target}'s review now",
        confirm="none",
    )


def resume_repo(deps: Deps, repo: str) -> Action:
    return ctl_action(
        deps,
        f"resume-repo {repo}",
        title=f"resume polling {repo}",
        confirm="none",
    )


# -- item verbs: ctl when live, the CLI's row-only twin when down ----------------


def _row_only(deps: Deps, verb: str, item_id: str, reason: str | None) -> Outcome:
    item_id = normalize_item_id(item_id)
    dstore = DaemonStore(deps.db_path)
    try:
        now = deps.clock()
        if verb == "abandon":
            item = dstore.abandon(item_id, reason or f"abandoned by {deps.operator}", now)
        elif verb == "retry":
            item = dstore.retry(item_id, now, f"re-queued by {deps.operator} via sbxloop tui")
        else:
            item = dstore.requeue(item_id, now)
    except KeyError:
        return Outcome(False, f"unknown work item: {item_id}")
    except ValueError as exc:
        return Outcome(False, f"{verb} refused: {exc}")
    finally:
        dstore.close()
    text = f"{item.item_id}: {item.state} (attempts {item.attempts}"
    text += f", run {item.run_id})" if item.run_id else ")"
    return Outcome(True, f"{text}\n{ROW_ONLY_NOTE}")


def _item_verb(deps: Deps, verb: str, item_id: str, reason: str | None = None) -> Outcome:
    if deps.daemon_live():
        cmd = f"{verb} {item_id}" + (f" {reason}" if reason and verb == "abandon" else "")
        return ctl_outcome(deps, cmd)
    if deps.daemon_starting():
        return Outcome(False, "the daemon is starting; retry in a moment")
    return _row_only(deps, verb, item_id, reason)


def abandon(deps: Deps, item_id: str, reason: str | None = None) -> Action:
    return Action(
        f"abandon {item_id}",
        lambda: _item_verb(deps, "abandon", item_id, reason),
        confirm="typed",
        typed=item_id,
        prompt=(
            f"Abandon {item_id}? Its run is cancelled, the issue is told, and it is not "
            "retried. Type the item id to confirm."
        ),
    )


def retry(deps: Deps, item_id: str) -> Action:
    return Action(
        f"retry {item_id}",
        lambda: _item_verb(deps, "retry", item_id),
        prompt=f"Retry {item_id}? Attempts reset; the next dispatch plans from scratch.",
    )


def requeue(deps: Deps, item_id: str) -> Action:
    return Action(
        f"requeue {item_id}",
        lambda: _item_verb(deps, "requeue", item_id),
        prompt=f"Requeue {item_id}? It loses its pinned run; the next dispatch starts fresh.",
    )


# -- run verbs --------------------------------------------------------------------


def _store_cancel(deps: Deps, run_id: str) -> Outcome:
    store = StateStore(deps.db_path)
    try:
        record = store.get_run(run_id)
        if record.state in TERMINAL_RUN_STATES:
            return Outcome(False, f"run {run_id} is already {record.state}; nothing to cancel")
        store.set_run_state(run_id, "cancelled")
    except SbxloopError as exc:
        return Outcome(False, str(exc))
    finally:
        store.close()
    return Outcome(True, f"run {run_id} cancelled (takes effect at the next phase boundary)")


def cancel_run(deps: Deps, record: RunRecord, *, current: bool, retry: bool = False) -> Action:
    """The daemon's run goes through ``ctl cancel`` (attributed, settled on
    the item); any other in-flight run is the ``sbxloop cancel`` store
    write — a resume driven from a terminal, or a run the daemon lost."""
    if current:
        return cancel_current(deps, retry=retry)
    return Action(
        f"cancel {record.run_id}",
        lambda: _store_cancel(deps, record.run_id),
        prompt=(
            f"Cancel {record.run_id}? It is not the daemon's current run: the store row is "
            "marked cancelled and whatever drives it stops at its next phase boundary."
        ),
    )


def _spawn(deps: Deps, name: str, argv: Sequence[str], *, log_name: str) -> Outcome:
    log_path = deps.console_dir / log_name
    try:
        child = deps.runner.spawn(argv, cwd=deps.cwd, log_path=log_path)
    except OSError as exc:
        return Outcome(False, f"could not start {shlex.join(argv)}: {exc}")
    deps.children.add(name, child)
    return Outcome(True, f"started pid {child.pid}: {shlex.join(argv[-3:])}\nlog: {log_path}")


def resume_run(deps: Deps, run_id: str) -> Action:
    """A detached ``sbxloop resume`` — not in-process, which would tie the
    run to the console's lifetime."""
    argv = (*sbxloop_argv(), "resume", run_id, "--no-tui", "--no-chat")
    return Action(
        f"resume {run_id} here",
        lambda: _spawn(deps, f"resume {run_id}", argv, log_name=f"resume-{run_id}.log"),
        prompt=(
            f"Resume {run_id} as a detached process on this host? It runs outside the "
            "daemon, with this checkout's config; its log lands under the state dir."
        ),
    )


def run_text(deps: Deps, text: str) -> Action:
    argv = (*sbxloop_argv(), "run", text, "--no-tui", "--no-chat")
    return Action(
        "run this outcome here",
        lambda: _spawn(deps, "run", argv, log_name=f"run-{int(deps.clock())}.log"),
        prompt="Start a run for this outcome as a detached process on this host?",
    )


def ask_concierge_to_file(deps: Deps, text: str) -> Action:
    """The daemon's way to a run: a human asks the concierge in the control
    channel, which files the issue with the trigger label — the daemon
    never files work for itself."""
    message = f"@sbx please file this as an issue for the daemon to run: {text}"

    def post() -> Outcome:
        row = deps.mailbox.post(TUI_CONTROL_CHANNEL, message, now=deps.clock())
        return Outcome(True, f"asked the concierge (row {row}); watch the Chat screen")

    return Action("ask the concierge to file it", post, confirm="none", needs_live=True)


# -- the unit and the process -----------------------------------------------------


def unit_verb(deps: Deps, verb: str) -> Action:
    argv = unit_argv(verb, deps.unit)

    def run() -> Outcome:
        outcome = deps.runner.run(argv, timeout_s=180.0)
        if outcome.ok:
            return Outcome(True, f"{shlex.join(argv)}: done")
        return Outcome(False, f"{shlex.join(argv)} failed ({outcome.returncode}): {outcome.text}")

    if verb == "start":
        return Action(f"start {deps.unit}", run, prompt=f"Start the {deps.unit} unit?")
    return Action(
        f"{verb} {deps.unit}",
        run,
        confirm="typed",
        typed=deps.unit,
        prompt=(
            f"{verb.capitalize()} the {deps.unit} unit? The run in flight is interrupted "
            "(resumable by design; the daemon recovers it on the next start). Type the "
            "unit name to confirm."
        ),
    )


def upgrade(deps: Deps) -> Action:
    command = deps.config.daemon.upgrade_command

    def run() -> Outcome:
        if not command:
            return Outcome(
                False,
                "no [daemon] upgrade_command is configured; set it to what upgrades this "
                "host (pip, pipx, uv tool …) and try again",
            )
        outcome = deps.runner.run(shlex.split(command), timeout_s=UPGRADE_TIMEOUT_S)
        head = f"$ {command}\nexit {outcome.returncode}\n"
        tail = "\nrestart the daemon to run the new version" if outcome.ok else ""
        return Outcome(outcome.ok, head + outcome.text + tail, long=True)

    return Action(
        "upgrade sbxloop on this host",
        run,
        confirm="typed",
        typed="upgrade",
        prompt=(
            f"Run `{command or '<no upgrade_command>'}` now? The daemon keeps running the "
            "code it started with until it is restarted. Type upgrade to confirm."
        ),
    )


def spawn_daemon(deps: Deps) -> Action:
    argv = (*sbxloop_argv(), "daemon")
    return Action(
        "start a daemon from this console",
        lambda: _spawn(deps, "daemon", argv, log_name="daemon.log"),
        prompt=(
            "Start `sbxloop daemon` here, in its own session? It reads this directory's "
            "config and outlives the console; on quit you are offered to stop it."
        ),
    )


def stop_child(deps: Deps, name: str) -> Outcome:
    child = deps.children.by_name.get(name)
    if child is None or child.poll() is not None:
        return Outcome(False, f"{name}: not running")
    child.terminate()
    code = child.wait(30.0)
    return Outcome(True, f"{name}: stopped" if code is not None else f"{name}: still stopping")


# -- sandboxes ----------------------------------------------------------------------


def shell(deps: Deps, name: str) -> Action:
    cli = deps.sbx()
    argv = tuple(cli.argv("exec", name, *INTERACTIVE_SHELL_ARGV))
    return Action(
        f"shell into {name}",
        lambda: Outcome(True, ""),
        confirm="none",
        mutating=False,
        interactive=argv,
    )


def stop_sandbox(deps: Deps, name: str) -> Action:
    def run() -> Outcome:
        try:
            deps.sbx().stop(name)
        except SbxloopError as exc:
            return Outcome(False, str(exc))
        return Outcome(True, f"stopped {name}")

    return Action(f"stop {name}", run, prompt=f"Stop the sandbox {name}?")


def remove_one_sandbox(deps: Deps, name: str, role: str | None) -> Action:
    def run() -> Outcome:
        try:
            if role in ("agent", "github", "service"):
                remove_run_sandbox(deps.sbx(), name, role)  # type: ignore[arg-type]
            else:
                remove_sandbox(deps.sbx(), name)
        except SbxloopError as exc:
            return Outcome(False, str(exc))
        return Outcome(True, f"removed {name}")

    return Action(
        f"remove {name}",
        run,
        confirm="typed",
        typed=name,
        prompt=f"Remove the sandbox {name} (and its secret registrations)? Type its name.",
    )


def prune_sandboxes(deps: Deps, verdicts: Sequence[SandboxVerdict]) -> Action:
    orphans = [v for v in verdicts if v.orphan]

    def run() -> Outcome:
        cli = deps.sbx()
        store = StateStore(deps.db_path)
        lines: list[str] = []
        failures = 0
        try:
            for v in orphans:
                try:
                    if v.role in ("agent", "github", "service"):
                        remove_run_sandbox(cli, v.name, v.role)  # type: ignore[arg-type]
                    else:
                        remove_sandbox(cli, v.name)
                except SbxloopError as exc:
                    failures += 1
                    lines.append(f"skip {v.name}: {exc}")
                    continue
                lines.append(f"removed {v.name}")
                if v.kept_reason is not None and v.run_id is not None:
                    store.set_run_kept(v.run_id, None)
        finally:
            store.close()
        return Outcome(failures == 0, "\n".join(lines) or "nothing to prune", long=len(lines) > 6)

    return Action(
        f"prune {len(orphans)} orphaned sandbox(es)",
        run,
        confirm="typed",
        typed="prune",
        prompt=(
            f"Remove {len(orphans)} orphaned sandbox(es): "
            f"{', '.join(v.name for v in orphans[:6])}{' …' if len(orphans) > 6 else ''}? "
            "Type prune to confirm."
        ),
    )


def gc_run_dirs(deps: Deps, days: float) -> Action:
    def run() -> Outcome:
        store = StateStore(deps.db_path)
        try:
            result = prune_run_dirs(
                store,
                deps.state_dir,
                older_than_s=days * 86400.0,
                actor=f"{deps.operator} via sbxloop tui",
            )
        finally:
            store.close()
        text = f"removed {len(result.pruned)} run dir(s), freed {format_bytes(result.bytes_freed)}"
        for run_id in result.failed:
            text += f"\ncould not remove {run_id} (see the log)"
        return Outcome(not result.failed, text)

    return Action(
        f"remove run directories older than {days:g} days",
        run,
        confirm="typed",
        typed="gc",
        prompt=(
            f"Remove every prunable run directory older than {days:g} days (workspace "
            "clones, harvested artifacts; run rows stay)? Type gc to confirm."
        ),
    )


__all__ = [
    "CTL_TIMEOUT_S",
    "ROW_ONLY_NOTE",
    "Action",
    "Children",
    "Deps",
    "Outcome",
    "abandon",
    "ask_concierge_to_file",
    "cancel_current",
    "cancel_run",
    "ctl_action",
    "ctl_outcome",
    "gc_run_dirs",
    "grant_rounds",
    "merge",
    "pause",
    "prune_sandboxes",
    "remove_one_sandbox",
    "requeue",
    "resume",
    "resume_repo",
    "resume_review",
    "resume_run",
    "retry",
    "run_text",
    "shell",
    "spawn_daemon",
    "stop_child",
    "stop_daemon",
    "stop_sandbox",
    "unit_verb",
    "upgrade",
]
