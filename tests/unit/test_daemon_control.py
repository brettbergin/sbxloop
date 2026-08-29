"""The shared command dispatcher and the file-based control queue (#232).

Discord's ``!sbx`` and ``sbxloop daemon ctl`` must be the same code path,
and ``ctl`` must reach a daemon that is blocked inside a run — the whole
point is stopping a spiral without a human in Discord.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sbxloop.cli.app import app
from sbxloop.config import Config
from sbxloop.daemon.control import (
    CTL_SUBDIR,
    CommandReply,
    ControlClient,
    ControlServer,
    _reply_from,
    dispatch,
    plain,
    usage,
)
from sbxloop.daemon.loop import DaemonLoop
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import RunResult
from sbxloop.engine.store import StateStore
from sbxloop.events import EventBus
from tests.unit.test_daemon_discord import FakeLoop
from tests.unit.test_daemon_loop import FakeSource, gh_item

runner = CliRunner()


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    # The daemon anchors its default state dir under XDG state home (#255);
    # `ctl` must find the daemon there, not under the runner dir's `.sbxloop`.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    return tmp_path


def daemon_state(workdir: Path) -> Path:
    return workdir / "xdg-state" / "sbxloop" / workdir.name


def _dstore(tmp_path: Path) -> DaemonStore:
    return DaemonStore(tmp_path / "state" / "state.db")


class TestDispatch:
    def test_every_verb_reaches_the_loop(self, tmp_path: Path) -> None:
        floop = FakeLoop(_dstore(tmp_path))
        status = dispatch(floop, "status")
        assert "**queued:** 2" in status.text and status.status == floop.status()
        assert dispatch(floop, "pause").ok and floop.paused
        assert dispatch(floop, "resume").ok and not floop.paused
        assert dispatch(floop, "cancel").ok and floop.cancelled == 1
        assert dispatch(floop, "queue").text == "queue is empty."
        assert dispatch(floop, "items").text == "no work items."

    def test_cancel_and_retry_carry_the_operator(self, tmp_path: Path) -> None:
        """#246: whoever asked is what the source hears — Discord passes its
        author, ctl its OS user; the dispatcher must not drop it."""
        floop = FakeLoop(_dstore(tmp_path))
        floop.dstore.upsert_new(WorkItem(item_id="gh:issue:8", source_key="8", title="Eight"), 1.0)
        floop.dstore.mark_running("gh:issue:8", "r1", 1.0)
        floop.dstore.mark_cancelled("gh:issue:8", "cancelled by op", 2.0)
        assert dispatch(floop, "cancel", by="ops").ok
        assert dispatch(floop, "cancel --retry", by="ops").ok
        assert floop.cancel_calls == [("ops", False), ("ops", True)]
        assert "run again fresh" in dispatch(floop, "cancel --retry", by="ops").text
        assert dispatch(floop, "retry gh:issue:8", by="ops").ok
        assert floop.retried == [("gh:issue:8", "ops")]

    def test_item_verbs_report_the_stores_reason(self, tmp_path: Path) -> None:
        floop = FakeLoop(_dstore(tmp_path))
        floop.dstore.upsert_new(WorkItem(item_id="gh:issue:9", source_key="9", title="Do A"), 1.0)
        floop.dstore.mark_running("gh:issue:9", "r1", 1.0)
        reply = dispatch(floop, "abandon")
        assert not reply.ok and reply.known and reply.text.startswith("usage: abandon")
        reply = dispatch(floop, "retry gh:issue:9")
        assert not reply.ok and "retry failed:" in reply.text and "abandon it first" in reply.text
        reply = dispatch(floop, "abandon gh:issue:9 plan spiraled")
        assert reply.ok and "abandoned" in reply.text and "`r1`" in reply.text
        reply = dispatch(floop, "requeue gh:issue:9")
        assert not reply.ok and "requeue failed:" in reply.text
        assert "attempts reset" in dispatch(floop, "retry gh:issue:9").text
        assert not dispatch(floop, "abandon gh:issue:404").ok

    def test_item_verbs_take_legacy_and_typed_ids(self, tmp_path: Path) -> None:
        """#508: both spellings reach the row; only the typed one is echoed."""
        import re as _re

        floop = FakeLoop(_dstore(tmp_path))
        floop.dstore.upsert_new(WorkItem(item_id="gh:issue:7", source_key="7", title="Seven"), 1.0)
        floop.dstore.mark_running("gh:issue:7", "r1", 1.0)
        reply = dispatch(floop, "requeue gh:7")  # legacy spelling
        assert reply.ok and "`gh:issue:7`" in reply.text
        assert not _re.search(r"gh:\d", reply.text)
        reply = dispatch(floop, "abandon gh:7 enough")
        assert reply.ok and "`gh:issue:7`" in reply.text
        assert not _re.search(r"gh:\d", reply.text)
        reply = dispatch(floop, "retry gh:issue:7", by="ops")  # typed spelling
        assert reply.ok and "`gh:issue:7`" in reply.text
        assert floop.retried[-1] == ("gh:issue:7", "ops")

    def test_items_and_queue_render_typed_ids_for_legacy_rows(self, tmp_path: Path) -> None:
        """Rows written by the pre-#508 daemon list as `gh:issue:<n>`."""
        import re as _re
        import sqlite3

        db = tmp_path / "state" / "state.db"
        floop = FakeLoop(_dstore(tmp_path))
        floop.dstore.upsert_new(WorkItem(item_id="gh:issue:7", source_key="7", title="Seven"), 1.0)
        floop.dstore.close()
        conn = sqlite3.connect(db)
        conn.execute("UPDATE daemon_work_items SET item_id = 'gh:7'")
        conn.commit()
        conn.close()
        floop = FakeLoop(DaemonStore(db))
        for word in ("items", "queue"):
            text = dispatch(floop, word).text
            assert "gh:issue:7" in text and not _re.search(r"gh:\d", text)

    def test_verbs_are_case_insensitive_and_take_no_extra_args(self, tmp_path: Path) -> None:
        floop = FakeLoop(_dstore(tmp_path))
        assert dispatch(floop, "PAUSE now please").ok and floop.paused

    def test_unknown_verb_returns_usage_with_the_callers_prefix(self, tmp_path: Path) -> None:
        reply = dispatch(FakeLoop(_dstore(tmp_path)), "bogus", prefix="sbxloop daemon ctl")
        assert not reply.ok and not reply.known
        assert reply.text == usage("sbxloop daemon ctl")
        assert "sbxloop daemon ctl status|pause|resume|cancel [--retry]|queue|items|" in reply.text

    def test_cancel_rejects_unknown_arguments(self, tmp_path: Path) -> None:
        # A typo (`--rety`) must not silently become a terminal no-retry
        # cancel: the two outcomes differ materially.
        floop = FakeLoop(_dstore(tmp_path))
        reply = dispatch(floop, "cancel --rety", prefix="sbxloop daemon ctl")
        assert not reply.ok and reply.known
        assert "unknown cancel argument" in reply.text and "--rety" in reply.text
        assert "sbxloop daemon ctl cancel [--retry]" in reply.text
        assert floop.cancel_calls == []

    def test_cancel_with_nothing_running_is_not_ok(self, tmp_path: Path) -> None:
        floop = FakeLoop(_dstore(tmp_path))
        floop.cancel_current = lambda *a, **k: False  # type: ignore[method-assign]
        reply = dispatch(floop, "cancel")
        assert not reply.ok and "nothing is running" in reply.text

    def test_plain_strips_discord_markdown(self) -> None:
        assert plain("**current:** `r1`") == "current: r1"


class TestControlQueue:
    def test_client_request_is_answered_by_the_server(self, tmp_path: Path) -> None:
        floop = FakeLoop(_dstore(tmp_path))
        server = ControlServer(floop, tmp_path, poll_s=0.02)
        server.start()
        try:
            reply = ControlClient(tmp_path).submit("pause", timeout_s=5)
            assert reply is not None and reply.ok and "paused" in reply.text
            assert floop.paused
            # request and reply files are both gone: nothing to replay later
            assert list((tmp_path / CTL_SUBDIR).iterdir()) == []
        finally:
            server.close()

    def test_requests_are_served_in_submission_order(self, tmp_path: Path) -> None:
        floop = FakeLoop(_dstore(tmp_path))
        server = ControlServer(floop, tmp_path, poll_s=0.02)
        client = ControlClient(tmp_path)
        # Submit both before the server runs, then serve once.
        server.dir.mkdir(parents=True)
        for cmd in ("pause", "resume"):
            (server.dir / f"{time.time():.6f}-{cmd}.json").write_text(json.dumps({"cmd": cmd}))
            time.sleep(0.001)
        assert server.serve_once() == 2
        assert not floop.paused  # pause then resume, not the other way round
        assert client.submit("status", timeout_s=0.1) is None  # server thread never started

    def test_timeout_withdraws_the_request(self, tmp_path: Path) -> None:
        """A `cancel` nobody answered must not fire when a daemon starts later."""
        client = ControlClient(tmp_path)
        assert client.submit("cancel", timeout_s=0.1) is None
        assert list((tmp_path / CTL_SUBDIR).iterdir()) == []

    def test_slow_command_the_daemon_took_is_reported_pending_not_absent(
        self, tmp_path: Path
    ) -> None:
        """Item verbs cross the ops sandbox (seconds): a client that gives up
        while the daemon is mid-command must say so — the abandon still
        lands, so "no reply from the daemon" would send the operator to
        check whether the daemon is even running."""
        floop = FakeLoop(_dstore(tmp_path))
        entered = threading.Event()
        release = threading.Event()

        def slow_pause() -> None:
            entered.set()
            release.wait(5)
            floop.paused = True

        floop.pause = slow_pause  # type: ignore[method-assign]
        server = ControlServer(floop, tmp_path, poll_s=0.02)
        server.start()
        try:
            reply = ControlClient(tmp_path).submit("pause", timeout_s=0.3)
            assert entered.is_set()
            assert reply is not None and reply.pending and not reply.ok
            assert "still executing" in reply.text
            # Not withdrawn: the daemon owns it, and the command completes.
            release.set()
            deadline = time.time() + 5
            while not floop.paused and time.time() < deadline:
                time.sleep(0.01)
            assert floop.paused
        finally:
            release.set()
            server.close()

    def test_withdrawn_request_is_never_claimed(self, tmp_path: Path) -> None:
        # The claim is an atomic rename, so a request the client already
        # withdrew cannot be half-executed; only a still-present one runs.
        floop = FakeLoop(_dstore(tmp_path))
        server = ControlServer(floop, tmp_path, poll_s=0.02)
        client = ControlClient(tmp_path)
        assert client.submit("pause", timeout_s=0.05) is None
        assert server.serve_once() == 0 and not floop.paused

    def test_requests_predating_the_daemon_are_refused_not_executed(self, tmp_path: Path) -> None:
        floop = FakeLoop(_dstore(tmp_path))
        ctl_dir = tmp_path / CTL_SUBDIR
        ctl_dir.mkdir(parents=True)
        (ctl_dir / "1.000000-stale.json").write_text(json.dumps({"cmd": "pause"}))
        server = ControlServer(floop, tmp_path, poll_s=0.02)
        server.start()
        try:
            assert not floop.paused
            reply = json.loads((ctl_dir / "1.000000-stale.reply.json").read_text())
            assert reply["ok"] is False and "before the daemon started" in reply["text"]
        finally:
            server.close()

    def test_request_stamped_before_start_is_refused_even_if_seen_later(
        self, tmp_path: Path
    ) -> None:
        # The start-up scan cannot see a request whose client paused between
        # writing its temp file and the atomic rename; the timestamp is what
        # keeps a pre-start `pause`/`cancel` from firing at boot.
        floop = FakeLoop(_dstore(tmp_path))
        server = ControlServer(floop, tmp_path, poll_s=0.02)
        server.start()
        server.close()
        ctl_dir = tmp_path / CTL_SUBDIR
        (ctl_dir / "1.000000-late.json").write_text(
            json.dumps({"cmd": "pause", "submitted_at": server._started_at - 1})
        )
        (ctl_dir / "2.000000-unstamped.json").write_text(json.dumps({"cmd": "pause"}))
        assert server.serve_once() == 2
        assert not floop.paused
        for name in ("1.000000-late", "2.000000-unstamped"):
            reply = json.loads((ctl_dir / f"{name}.reply.json").read_text())
            assert reply["ok"] is False and "before the daemon started" in reply["text"]
        # A request stamped after start is served as usual.
        (ctl_dir / "3.000000-fresh.json").write_text(
            json.dumps({"cmd": "pause", "submitted_at": time.time()})
        )
        assert server.serve_once() == 1 and floop.paused

    def test_a_request_that_races_a_starting_daemon_is_resent_not_failed(
        self, tmp_path: Path
    ) -> None:
        """The regression that rolled back 0.7.23. The control server starts
        only after ``loop.recover()``, so anything submitted while recovery
        runs — over a minute on a daemon with orphaned runs to settle, which
        is the state a restart creates — is swept as stale. The deploy's
        health check read that refusal as "the daemon never came up" and
        rolled back a daemon that was healthy. The client resends instead."""
        floop = FakeLoop(_dstore(tmp_path))
        server = ControlServer(floop, tmp_path, poll_s=0.02)
        replies: list[CommandReply | None] = []
        caller = threading.Thread(
            target=lambda: replies.append(ControlClient(tmp_path).submit("pause", timeout_s=10))
        )
        caller.start()
        try:
            # The request lands while there is no server, exactly as a health
            # check racing recovery does; start() then sweeps it as stale.
            time.sleep(0.2)
            assert not floop.paused
            server.start()
            caller.join(timeout=10)
        finally:
            server.close()
        assert not caller.is_alive()
        assert replies and replies[0] is not None
        assert replies[0].ok and "paused" in replies[0].text
        assert floop.paused
        # Nothing left behind to be replayed by a later daemon.
        assert list((tmp_path / CTL_SUBDIR).iterdir()) == []

    def test_resending_is_bounded_and_answers_with_the_refusal(self, tmp_path: Path) -> None:
        """A daemon that never finishes starting must fail at the deadline
        rather than spin, and must fail *as the refusal it got*.

        The final attempt is the one the deadline interrupts, so it reports
        `pending` whenever the server had claimed it — which would tell the
        operator the daemon is executing a command it refused every time.
        This test used to pass only when that race went the other way; it
        was flaky on CI and blocked a release."""
        floop = FakeLoop(_dstore(tmp_path))
        server = ControlServer(floop, tmp_path, poll_s=0.02)
        server.start()
        # Every request from here on looks pre-start, so each is refused.
        server._started_at = time.time() + 3600
        try:
            started = time.monotonic()
            reply = ControlClient(tmp_path).submit("pause", timeout_s=0.5)
            elapsed = time.monotonic() - started
        finally:
            server.close()
        assert reply is not None and not reply.ok and reply.stale
        assert not floop.paused
        assert elapsed < 5.0, f"resend loop did not respect the budget ({elapsed:.1f}s)"

    def test_the_stale_verdict_rides_on_the_reply_file(self, tmp_path: Path) -> None:
        """Carried structurally so the client never has to match on prose."""
        floop = FakeLoop(_dstore(tmp_path))
        ctl_dir = tmp_path / CTL_SUBDIR
        ctl_dir.mkdir(parents=True)
        (ctl_dir / "1.000000-stale.json").write_text(json.dumps({"cmd": "pause"}))
        server = ControlServer(floop, tmp_path, poll_s=0.02)
        server.start()
        try:
            reply = json.loads((ctl_dir / "1.000000-stale.reply.json").read_text())
            assert reply["stale"] is True and reply["ok"] is False
        finally:
            server.close()

    def test_a_reply_from_a_daemon_that_predates_the_flag_is_not_resent(self) -> None:
        """An older daemon writes no ``stale`` key. That must read as a plain
        refusal, not send the client into a resend loop against a daemon
        that will never set the flag."""
        assert _reply_from({"ok": False, "text": "no such item"}).stale is False
        assert _reply_from({"ok": True, "text": "paused"}) == CommandReply("paused", True)

    def test_a_crashing_command_answers_with_an_error_and_keeps_serving(
        self, tmp_path: Path
    ) -> None:
        floop = FakeLoop(_dstore(tmp_path))

        def boom() -> dict[str, Any]:
            raise RuntimeError("status exploded")

        floop.status = boom  # type: ignore[method-assign]
        server = ControlServer(floop, tmp_path, poll_s=0.02)
        server.start()
        try:
            client = ControlClient(tmp_path)
            reply = client.submit("status", timeout_s=5)
            assert reply is not None and not reply.ok and "status exploded" in reply.text
            assert client.submit("pause", timeout_s=5) is not None and floop.paused
        finally:
            server.close()

    def test_cancel_reaches_a_daemon_blocked_inside_a_run(self, tmp_path: Path) -> None:
        """tick() joins the engine thread for the whole run, so the control
        server must live on its own thread or `cancel` could never land
        mid-run — the field scenario that motivated the issue."""
        config = Config.model_validate({"state_dir": str(tmp_path / "state")})
        store = StateStore(config.state_dir / "state.db")
        dstore = DaemonStore(config.state_dir / "state.db")
        source = FakeSource([gh_item()])
        released = threading.Event()
        cancelled = threading.Event()

        def run(item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool) -> RunResult:
            store.create_run(run_id, "outcome")
            engine = loop.current.engine  # type: ignore[union-attr]
            while not released.is_set():
                if engine._cancel_event.is_set():
                    cancelled.set()
                    released.set()
                released.wait(0.02)
            store.set_run_state(run_id, "cancelled")
            return RunResult(run_id=run_id, state="cancelled")

        loop = DaemonLoop(config, store=store, dstore=dstore, source=source, runner=run)
        server = ControlServer(loop, config.state_dir, poll_s=0.02)
        server.start()
        ticker = threading.Thread(target=loop.tick, daemon=True)
        ticker.start()
        try:
            client = ControlClient(config.state_dir)
            deadline = time.time() + 5
            while loop.current is None and time.time() < deadline:
                time.sleep(0.01)
            reply = client.submit("cancel", timeout_s=5)
            assert reply is not None and reply.ok and "cancel requested" in reply.text
            assert cancelled.wait(5)
        finally:
            released.set()
            ticker.join(timeout=10)
            server.close()


class TestDaemonCtlCommand:
    def test_no_daemon_exits_2(self, workdir: Path) -> None:
        result = runner.invoke(app, ["daemon", "ctl", "status", "--timeout", "0.2"])
        assert result.exit_code == 2
        assert "no reply from the daemon" in result.output

    def test_reply_is_printed_plain(self, workdir: Path) -> None:
        # The server listens where the daemon keeps its state (the anchored
        # XDG default, #255); a `ctl` from the runner dir must resolve to it.
        state_dir = daemon_state(workdir)
        floop = FakeLoop(_dstore(state_dir))
        server = ControlServer(floop, state_dir, poll_s=0.02)
        server.start()
        try:
            result = runner.invoke(app, ["daemon", "ctl", "status"])
            assert result.exit_code == 0, result.output
            assert "queued: 2" in result.output and "**" not in result.output
            result = runner.invoke(app, ["daemon", "ctl", "cancel", "--retry"])
            assert result.exit_code == 0, result.output
            assert floop.cancel_calls[-1][1] is True
            assert floop.cancel_calls[-1][0].endswith("via sbxloop daemon ctl")  # type: ignore[union-attr]
            result = runner.invoke(app, ["daemon", "ctl", "bogus"])
            assert result.exit_code == 1
            assert "commands: sbxloop daemon ctl status|pause" in result.output
        finally:
            server.close()

    def test_pending_reply_exits_1_and_says_so(self, workdir: Path) -> None:
        state_dir = daemon_state(workdir)
        floop = FakeLoop(_dstore(state_dir))
        release = threading.Event()
        floop.pause = lambda: release.wait(5)  # type: ignore[method-assign]
        server = ControlServer(floop, state_dir, poll_s=0.02)
        server.start()
        try:
            result = runner.invoke(app, ["daemon", "ctl", "pause", "--timeout", "0.3"])
            assert result.exit_code == 1, result.output
            out = " ".join(result.output.split())  # rich wraps the long line at 80 cols
            assert "pending: the daemon took pause" in out and "still executing" in out
            assert "no reply from the daemon" not in out
        finally:
            release.set()
            server.close()

    def test_control_server_starts_only_after_recovery(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An `abandon` served while recover() is still settling the item it
        # snapshotted would be overwritten by recovery's own verdict, so
        # requests stay refused-as-stale until recovery is done.
        from sbxloop.daemon.github import DaemonGithub
        from sbxloop.daemon.sources import GitHubIssueSource

        order: list[str] = []
        monkeypatch.setattr(DaemonLoop, "recover", lambda self: order.append("recover"))
        monkeypatch.setattr(ControlServer, "start", lambda self: order.append("ctl.start"))
        # No sandbox, no GitHub: the poll finds nothing and the tick idles.
        monkeypatch.setattr(DaemonGithub, "remove_stale", lambda self: None)
        monkeypatch.setattr(GitHubIssueSource, "poll", lambda self: [])
        result = runner.invoke(app, ["daemon", "--repo", "o/r", "--once"])
        assert result.exit_code == 0, result.output
        assert order == ["recover", "ctl.start"]
        assert "tick:" in result.output and "no_work" in result.output

    def test_daemon_group_still_runs_the_loop_bare(self, workdir: Path) -> None:
        # `daemon` became a group so `ctl` could hang off it; the bare
        # invocation must keep its old behaviour (exit 2 without a repository).
        result = runner.invoke(app, ["daemon"])
        assert result.exit_code == 2 and "daemon.no_repository" in result.output


class TestCommandAudit:
    """Every operator command leaves a host-side record: who, what, over
    which channel, and whether it was accepted (mutations at INFO,
    read-only status/queue/items at DEBUG)."""

    def test_mutating_command_logged_at_info_with_by_and_via(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        floop = FakeLoop(_dstore(tmp_path))
        with caplog.at_level(logging.INFO, logger="sbxloop.daemon.control"):
            dispatch(floop, "pause", by="brett", via="discord")
        (record,) = [r for r in caplog.records if "operator.command" in r.getMessage()]
        assert record.levelno == logging.INFO
        text = record.getMessage()
        assert "'by': 'brett'" in text and "'via': 'discord'" in text
        assert "'command': 'pause'" in text and "'ok': True" in text

    def test_read_only_command_logged_at_debug(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        floop = FakeLoop(_dstore(tmp_path))
        with caplog.at_level(logging.DEBUG, logger="sbxloop.daemon.control"):
            dispatch(floop, "status")
        (record,) = [r for r in caplog.records if "operator.command" in r.getMessage()]
        assert record.levelno == logging.DEBUG

    def test_unknown_command_logged_as_not_ok(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        with caplog.at_level(logging.INFO, logger="sbxloop.daemon.control"):
            dispatch(FakeLoop(_dstore(tmp_path)), "bogus", by="x")
        (record,) = [r for r in caplog.records if "operator.command" in r.getMessage()]
        assert "'ok': False" in record.getMessage() and "'known': False" in record.getMessage()
