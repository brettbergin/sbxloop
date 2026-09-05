"""The admin verbs as plain functions: what they ask the daemon, what they
run on the host, what they write when no daemon is up, and how each must
be confirmed — against the seeded store, a fake ctl and a fake runner."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from sbxloop.config import TUI_CONTROL_CHANNEL, Config
from sbxloop.daemon.control import CommandReply
from sbxloop.daemon.mailbox import MailboxClient
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.store import StateStore
from sbxloop.ids import new_run_id
from sbxloop.paths import SbxloopHome
from sbxloop.sbx.models import SandboxInfo
from sbxloop.sbx.prune import SandboxVerdict
from sbxloop.tui import actions
from sbxloop.tui.commands import CATALOGUE
from sbxloop.tui.data import DaemonSnapshot, probe_daemon
from sbxloop.tui.runner import RunOutcome, sbxloop_argv
from sbxloop.tui.system import (
    LEVELS,
    journal_argv,
    level_of,
    parse_show,
    passes,
    unit_argv,
    unit_state,
)
from tests.unit.tui.conftest import (
    FakeChild,
    FakeCtl,
    FakeRunner,
    RecordingSbx,
    backdate,
    live_status,
)


def sent(deps: actions.Deps) -> list[str]:
    """What went over ctl, the probe's own `status` aside."""
    ctl: Any = deps.ctl
    return [c for c in ctl.commands if c != "status"]


def make_deps(
    state_dir: SbxloopHome,
    *,
    ctl: FakeCtl | None = None,
    runner: FakeRunner | None = None,
    sbx: RecordingSbx | None = None,
    daemon: DaemonSnapshot | str | None = "live",
    read_only: bool = False,
    **daemon_cfg: Any,
) -> actions.Deps:
    config = Config.model_validate({"home": str(state_dir.root), "daemon": daemon_cfg})
    ctl = ctl or FakeCtl(live_status())
    snapshot: DaemonSnapshot | None
    if daemon == "live":
        snapshot = probe_daemon(ctl, now=1.0)
    else:
        snapshot = None if isinstance(daemon, str) else daemon
    box = sbx or RecordingSbx()
    return actions.Deps(
        ctl=ctl,
        runner=runner or FakeRunner(),
        mailbox=MailboxClient(state_dir.state_db, operator_id="brett"),
        config=config,
        home=state_dir,
        unit="sbxloop-daemon",
        operator="brett",
        sbx=lambda: box,
        daemon=lambda: snapshot,
        read_only=read_only,
        clock=time.time,
        cwd=state_dir.root,
    )


DOWN = DaemonSnapshot(False, False, None, 0, 1.0)
STARTING = DaemonSnapshot(False, True, None, 0, 1.0)


class TestCtlOutcomes:
    def test_replies_are_read_the_way_the_cli_reads_them(self, seeded: Path) -> None:
        deps = make_deps(seeded)
        assert actions.ctl_outcome(deps, "pause") == actions.Outcome(True, "did pause")
        down = make_deps(seeded, ctl=FakeCtl(down=True))
        out = actions.ctl_outcome(down, "pause")
        assert not out.ok and "no daemon took `pause`" in out.text

        class Pending(FakeCtl):
            def submit(self, cmd: str, *, timeout_s: float = 30.0) -> CommandReply | None:
                return CommandReply("still executing", ok=False, pending=True)

        pending = actions.ctl_outcome(make_deps(seeded, ctl=Pending()), "retry gh:issue:44")
        assert not pending.ok and pending.text.startswith("pending:")

    def test_daemon_verbs_build_the_ctl_commands_and_their_tiers(self, seeded: Path) -> None:
        deps = make_deps(seeded)
        assert actions.pause(deps).run().ok
        assert actions.resume(deps, every=True).run().ok
        assert actions.resume(deps).run().ok
        assert actions.cancel_current(deps, retry=True).run().ok
        assert actions.merge(deps, "gh:issue:40").run().ok
        assert actions.grant_rounds(deps, "r_live", 2).run().ok
        assert actions.resume_review(deps, "gh:issue:41").run().ok
        assert actions.resume_repo(deps, "o/r").run().ok
        assert sent(deps) == [
            "pause",
            "resume --all",
            "resume",
            "cancel --retry",
            "merge gh:issue:40",
            "grant-rounds r_live 2",
            "resume gh:issue:41",
            "resume-repo o/r",
        ]
        stop = actions.stop_daemon(deps)
        assert stop.confirm == "typed" and stop.typed == "stop" and stop.needs_live
        assert actions.resume(deps).confirm == "none"
        assert actions.cancel_current(deps).confirm == "yes"
        assert "brett" in actions.cancel_current(deps).prompt


class TestItemVerbs:
    def test_live_daemon_gets_the_ctl_verb(self, seeded: Path) -> None:
        deps = make_deps(seeded)
        assert actions.retry(deps, "gh:issue:44").run().ok
        assert actions.requeue(deps, "gh:issue:41").run().ok
        assert actions.abandon(deps, "gh:issue:41").run().ok
        assert sent(deps) == [
            "retry gh:issue:44",
            "requeue gh:issue:41",
            "abandon gh:issue:41",
        ]
        abandon = actions.abandon(deps, "gh:issue:41")
        assert abandon.confirm == "typed" and abandon.typed == "gh:issue:41"

    def test_no_daemon_changes_the_row_like_the_cli(self, seeded: Path) -> None:
        deps = make_deps(seeded, daemon=DOWN)
        out = actions.requeue(deps, "gh:issue:41").run()
        assert out.ok and "gh:issue:41: queued" in out.text and actions.ROW_ONLY_NOTE in out.text
        store = DaemonStore(seeded.state_db)
        try:
            item = store.get("gh:issue:41")
            assert item is not None and item.state == "queued" and item.run_id is None
        finally:
            store.close()
        refused = actions.retry(deps, "gh:issue:40").run()  # done: not retryable
        assert not refused.ok and "retry refused" in refused.text
        unknown = actions.abandon(deps, "gh:issue:999").run()
        assert not unknown.ok and "unknown work item" in unknown.text
        assert sent(deps) == [], "nothing went over ctl"

    def test_a_starting_daemon_is_waited_for(self, seeded: Path) -> None:
        deps = make_deps(seeded, daemon=STARTING)
        out = actions.retry(deps, "gh:issue:44").run()
        assert not out.ok and "starting" in out.text


class TestRunVerbs:
    def test_cancel_is_decided_against_a_fresh_status(self, seeded: Path) -> None:
        """The bar's last probe may be stale: the verb asks the daemon
        again. Its current run goes through ctl; any other through the
        store; a daemon that cannot say gets nothing."""
        deps = make_deps(seeded)
        record = deps.mailbox.run("r_live")
        assert record is not None
        # The bar said "not current" (current=False) but the daemon drives
        # it: ctl, never the store write that would settle it as a failure.
        stale = actions.cancel_run(deps, record, current=False)
        assert stale.run().ok and sent(deps) == ["cancel"]
        assert actions.cancel_run(deps, record, current=True, retry=True).run().ok
        assert sent(deps)[-1] == "cancel --retry"

        class Busy(FakeCtl):
            def submit(self, cmd: str, *, timeout_s: float = 30.0) -> CommandReply | None:
                return CommandReply("busy", ok=False, pending=True)

        busy = make_deps(seeded, ctl=Busy())
        out = actions.cancel_run(busy, record, current=True).run()
        assert not out.ok and "did not say" in out.text
        idle = make_deps(seeded, ctl=FakeCtl(live_status(current=None)))
        assert not actions.cancel_run(idle, record, current=False, retry=True).run().ok
        assert actions.cancel_run(idle, record, current=False).run().ok
        assert sent(idle) == []
        store = StateStore(seeded.state_db)
        try:
            assert store.get_run("r_live").state == "cancelled"
        finally:
            store.close()
        done = deps.mailbox.run("r_done")
        assert done is not None
        refused = actions.cancel_run(idle, done, current=False).run()
        assert not refused.ok and "already merged" in refused.text

    def test_resume_and_run_spawn_detached_processes(self, seeded: Path) -> None:
        runner = FakeRunner()
        deps = make_deps(seeded, runner=runner)
        out = actions.resume_run(deps, "r_live").run()
        assert out.ok and "started pid" in out.text
        argv, cwd, log_path = runner.spawned[0]
        assert argv == (*sbxloop_argv(), "resume", "r_live", "--no-tui", "--no-chat")
        assert cwd == seeded.root and log_path == seeded.console / "resume-r_live.log"
        assert runner.spawn_env[0] == {"SBXLOOP_HOME": str(seeded.root)}, "the console's store"
        assert "resume r_live" in deps.children.alive()
        assert actions.run_text(deps, "--fix the spinner").run().ok
        assert runner.spawned[1][0][-4:] == ("--no-tui", "--no-chat", "--", "--fix the spinner")
        # A child that dies on its arguments is reported, not "started".
        runner.children[-1].code = 2
        dead = actions.run_text(deps, "again").run()
        assert dead.ok, "the previous child's exit is not this one's"
        FakeChild.exit_at_once = 2
        try:
            gone = actions.resume_run(deps, "r_done").run()
        finally:
            FakeChild.exit_at_once = None
        assert not gone.ok and "exited 2 right away" in gone.text

    def test_a_new_run_the_daemons_way_asks_the_concierge(self, seeded: Path) -> None:
        deps = make_deps(seeded)
        action = actions.ask_concierge_to_file(deps, "the spinner never stops")
        assert action.needs_live and action.confirm == "none"
        assert action.run().ok
        rows = deps.mailbox.messages(TUI_CONTROL_CHANNEL)
        assert rows and rows[-1].text.startswith("@sbx please file this as an issue")
        assert "the spinner never stops" in rows[-1].text


class TestHostVerbs:
    def test_unit_verbs_run_systemctl_with_their_tiers(self, seeded: Path) -> None:
        runner = FakeRunner()
        runner.script("systemctl", "--user", "restart")
        runner.script("systemctl", "--user", "stop", returncode=1, stderr="Failed to stop")
        deps = make_deps(seeded, runner=runner)
        start = actions.unit_verb(deps, "start")
        assert start.confirm == "yes"
        restart = actions.unit_verb(deps, "restart")
        assert restart.confirm == "typed" and restart.typed == "sbxloop-daemon"
        assert restart.run().ok
        stopped = actions.unit_verb(deps, "stop").run()
        assert not stopped.ok and "Failed to stop" in stopped.text
        assert runner.calls == [
            ("systemctl", "--user", "restart", "sbxloop-daemon"),
            ("systemctl", "--user", "stop", "sbxloop-daemon"),
        ]

    def test_upgrade_runs_the_configured_command_or_says_there_is_none(self, seeded: Path) -> None:
        runner = FakeRunner()
        deps = make_deps(seeded, runner=runner)
        none = actions.upgrade(deps).run()
        assert not none.ok and "upgrade_command" in none.text and runner.calls == []
        runner.script("sh", "-lc", stdout="Installed sbxloop 9.9.9")
        command = "~/.venv/bin/pip install -U sbxloop && systemctl --user restart sbxloop-daemon"
        deps = make_deps(seeded, runner=runner, upgrade_command=command)
        action = actions.upgrade(deps)
        assert action.confirm == "typed" and action.typed == "upgrade"
        out = action.run()
        assert out.ok and out.long and "Installed sbxloop 9.9.9" in out.text
        assert "restart the daemon" in out.text
        # Shell text, verbatim: `~` and `&&` are the shell's to read.
        assert runner.calls[-1] == ("sh", "-lc", command)

    def test_spawn_daemon_and_stop_it(self, seeded: Path) -> None:
        runner = FakeRunner()
        deps = make_deps(seeded, runner=runner, daemon=DOWN)
        assert actions.spawn_daemon(deps).run().ok
        argv, _cwd, log_path = runner.spawned[0]
        assert argv == (*sbxloop_argv(), "daemon") and log_path == seeded.console / "daemon.log"
        assert "daemon" in deps.children.alive()
        stop = actions.stop_spawned_daemon(deps)
        assert "interrupted" in stop.prompt and stop.run().ok
        assert runner.children[0].terminated and deps.children.alive() == {}
        assert not actions.stop_child(deps, "daemon").ok

    def test_shell_hands_the_terminal_over(self, seeded: Path) -> None:
        deps = make_deps(seeded)
        action = actions.shell(deps, "sbxloop-r_live-agent")
        assert action.interactive is not None and action.mutating, "read-only gets no shell"
        assert action.interactive[:3] == ("sbx", "exec", "sbxloop-r_live-agent")
        assert "exec bash -l" in action.interactive[-1]


class TestSandboxVerbs:
    def test_remove_stop_and_prune(self, seeded: Path) -> None:
        old = new_run_id()
        store = StateStore(seeded.state_db)
        try:
            store.create_run(old, "an old one")
            store.set_run_state(old, "failed")
        finally:
            store.close()
        backdate(seeded, old, 2.0)
        sbx = RecordingSbx(
            [
                SandboxInfo(name=f"sbxloop-{old}-agent", status="running"),
                SandboxInfo(name=f"sbxloop-{old}-github", status="running"),
                SandboxInfo(name="sbxloop-r_live-agent", status="running"),
            ]
        )
        deps = make_deps(seeded, sbx=sbx)
        assert actions.stop_sandbox(deps, f"sbxloop-{old}-agent").run().ok
        remove = actions.remove_one_sandbox(deps, f"sbxloop-{old}-github", "github")
        assert remove.confirm == "typed" and remove.typed == f"sbxloop-{old}-github"
        assert remove.run().ok
        assert ("stop", f"sbxloop-{old}-agent") in sbx.calls
        assert ("rm", "--force", f"sbxloop-{old}-github") in sbx.calls
        verdicts = [
            SandboxVerdict(
                name=f"sbxloop-{old}-agent",
                run_id=old,
                role="agent",
                orphan=True,
                reason="failed 2d ago",
            ),
            SandboxVerdict(
                name="sbxloop-r_live-agent", run_id="r_live", role="agent", reason="in flight"
            ),
        ]
        prune = actions.prune_sandboxes(deps, verdicts)
        assert prune.typed == "prune" and "1 orphaned" in prune.title
        out = prune.run()
        assert out.ok and out.text == f"removed sbxloop-{old}-agent"
        assert ("rm", "--force", "sbxloop-r_live-agent") not in sbx.calls

    def test_prune_classifies_again_when_it_runs(self, seeded: Path) -> None:
        """Between the poll and the typed word the run may have resumed
        (or been prodded): what the prompt listed is not what is removed."""
        old = new_run_id()
        store = StateStore(seeded.state_db)
        try:
            store.create_run(old, "resumed since")
            store.set_run_state(old, "failed")
        finally:
            store.close()
        sbx = RecordingSbx([SandboxInfo(name=f"sbxloop-{old}-agent", status="running")])
        deps = make_deps(seeded, sbx=sbx)
        stale = [
            SandboxVerdict(
                name=f"sbxloop-{old}-agent", run_id=old, role="agent", orphan=True, reason="was"
            )
        ]
        out = actions.prune_sandboxes(deps, stale).run()  # the run is active again: not orphaned
        assert out.ok and "no longer an orphan" in out.text
        assert not any(c[0] == "rm" for c in sbx.calls)

    def test_gc_removes_run_directories_past_retention(self, seeded: Path) -> None:
        deps = make_deps(seeded)
        run_id = new_run_id()
        store = StateStore(seeded.state_db)
        try:
            store.create_run(run_id, "an old one")
            store.set_run_state(run_id, "merged")
        finally:
            store.close()
        backdate(seeded, run_id, 3.0)
        run_dir = seeded.runs / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "workspace.txt").write_text("x" * 100)
        disabled = actions.gc_run_dirs(deps, 0.0).run()
        assert not disabled.ok and "disabled" in disabled.text and run_dir.exists()
        action = actions.gc_run_dirs(deps, 1.0)
        assert action.typed == "gc"
        out = action.run()
        assert out.ok and "removed 1 run dir(s)" in out.text
        assert not run_dir.exists()


class TestReadOnlyAndPalette:
    def test_the_catalogue_names_every_screen_and_argless_verb(self) -> None:
        titles = [c.title for c in CATALOGUE]
        assert len(titles) == len(set(titles))
        for wanted in ("Sandboxes", "Daemon", "Pause the daemon", "Stop the daemon gracefully"):
            assert wanted in titles
        assert all(
            c.mutating for c in CATALOGUE if "daemon" in c.title.lower() and c.title != "Daemon"
        )


class TestSystem:
    def test_unit_state_reads_systemctl_show(self) -> None:
        argv = unit_argv("show", "sbxloop-daemon")
        assert argv[:4] == ("systemctl", "--user", "show", "sbxloop-daemon") and "-p" in argv
        assert unit_argv("restart", "u") == ("systemctl", "--user", "restart", "u")
        active = unit_state(
            "u",
            RunOutcome(
                argv,
                0,
                "LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=4242\n"
                "NRestarts=2\nResult=success\nExecMainStartTimestamp=Fri 2026-09-05 10:00:00 UTC\n",
            ),
        )
        assert active.running and active.pid == 4242 and "restarted 2 time(s)" in active.summary
        missing = unit_state(
            "u", RunOutcome(argv, 0, "LoadState=not-found\nActiveState=inactive\n")
        )
        assert missing.available and not missing.loaded and "no unit u" in missing.summary
        gone = unit_state("u", RunOutcome(argv, 127, stderr="systemctl: not found on PATH"))
        assert not gone.available and "no systemd here" in gone.summary
        no_bus = unit_state("u", RunOutcome(argv, 1, "", "Failed to connect to bus"))
        assert not no_bus.available and "Failed to connect to bus" in no_bus.summary
        assert parse_show("A=1\nB=x=y\nnoeq\n") == {"A": "1", "B": "x=y"}

    def test_journal_filter_and_argv(self) -> None:
        assert journal_argv("u")[:6] == ("journalctl", "--user", "-u", "u", "-n", "200")
        assert "-f" in journal_argv("u")
        warn = "2026-09-05T10:00:00+0000 host sbxloop[1]: 2026-09-05 10:00:00 [warning  ] x"
        info = "2026-09-05T10:00:00+0000 host sbxloop[1]: 2026-09-05 10:00:00 [info     ] y"
        trace = '  File "x.py", line 1'
        assert level_of(warn) == "warning" and level_of(info) == "info" and level_of(trace) is None
        assert passes(warn, min_level="warning", grep="") and not passes(
            info, min_level="warning", grep=""
        )
        assert passes(trace, min_level="error", grep=""), "a traceback line stays with its error"
        assert passes(info, min_level="debug", grep="Y") and not passes(
            info, min_level="debug", grep="z"
        )
        assert LEVELS == ("debug", "info", "warning", "error"), "the daemon's own levels"
        json_line = '{"event": "daemon.tick", "level": "warning"}'
        assert passes(json_line, min_level="error", grep=""), "no bracket, no floor"
