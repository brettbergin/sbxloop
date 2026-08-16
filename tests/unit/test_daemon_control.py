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
    ControlClient,
    ControlServer,
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
from tests.unit.test_daemon_loop import FakeSource, inbox_item

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
        floop.dstore.upsert_new(
            WorkItem(item_id="gh:8", source="github", source_key="8", title="Eight"), 1.0
        )
        floop.dstore.mark_running("gh:8", "r1", 1.0)
        floop.dstore.mark_cancelled("gh:8", "cancelled by op", 2.0)
        assert dispatch(floop, "cancel", by="ops").ok
        assert dispatch(floop, "cancel --retry", by="ops").ok
        assert floop.cancel_calls == [("ops", False), ("ops", True)]
        assert "run again fresh" in dispatch(floop, "cancel --retry", by="ops").text
        assert dispatch(floop, "retry gh:8", by="ops").ok
        assert floop.retried == [("gh:8", "ops")]

    def test_item_verbs_report_the_stores_reason(self, tmp_path: Path) -> None:
        floop = FakeLoop(_dstore(tmp_path))
        floop.dstore.upsert_new(
            WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="Do A"), 1.0
        )
        floop.dstore.mark_running("inbox:a.md", "r1", 1.0)
        reply = dispatch(floop, "abandon")
        assert not reply.ok and reply.known and reply.text.startswith("usage: abandon")
        reply = dispatch(floop, "retry inbox:a.md")
        assert not reply.ok and "retry failed:" in reply.text and "abandon it first" in reply.text
        reply = dispatch(floop, "abandon inbox:a.md plan spiraled")
        assert reply.ok and "abandoned" in reply.text and "`r1`" in reply.text
        reply = dispatch(floop, "requeue inbox:a.md")
        assert not reply.ok and "requeue failed:" in reply.text
        assert "attempts reset" in dispatch(floop, "retry inbox:a.md").text
        assert not dispatch(floop, "abandon gh:404").ok

    def test_verbs_are_case_insensitive_and_take_no_extra_args(self, tmp_path: Path) -> None:
        floop = FakeLoop(_dstore(tmp_path))
        assert dispatch(floop, "PAUSE now please").ok and floop.paused

    def test_unknown_verb_returns_usage_with_the_callers_prefix(self, tmp_path: Path) -> None:
        reply = dispatch(FakeLoop(_dstore(tmp_path)), "bogus", prefix="sbxloop daemon ctl")
        assert not reply.ok and not reply.known
        assert reply.text == usage("sbxloop daemon ctl")
        assert "sbxloop daemon ctl status|pause|resume|cancel [--retry]|queue|items|" in reply.text

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
        source = FakeSource([inbox_item()])
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

        loop = DaemonLoop(config, store=store, dstore=dstore, sources=[source], runner=run)
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

    def test_daemon_group_still_runs_the_loop_bare(self, workdir: Path) -> None:
        # `daemon` became a group so `ctl` could hang off it; the bare
        # invocation must keep its old behaviour (exit 2 without sources).
        result = runner.invoke(app, ["daemon", "--inbox", ""])
        assert result.exit_code == 2 and "no work sources" in result.output
