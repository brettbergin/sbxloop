"""SandboxPair + cleanup registry semantics."""

import contextlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import sbxloop.sbx.pair as pair_mod
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxSpec
from sbxloop.sbx.pair import CleanupRegistry, SandboxPair, cleanup_registry
from sbxloop.sbx.sandbox import Sandbox
from tests.conftest import FakeSbx


def make_pair(fake_sbx: FakeSbx, tmp_path: Path, *, keep: bool = False) -> SandboxPair:
    cli = SbxCLI(binary=str(fake_sbx.binary))
    cli.create(SandboxSpec(name="sbxloop-r1-agent", role="agent", workspace=tmp_path))
    cli.create(SandboxSpec(name="sbxloop-r1-github", role="github", workspace=tmp_path))
    return SandboxPair(
        "r1",
        agent=Sandbox(cli, "sbxloop-r1-agent"),
        github=Sandbox(cli, "sbxloop-r1-github"),
        keep=keep,
    )


def gone(fake_sbx: FakeSbx, name: str) -> bool:
    return not (fake_sbx.state / "sandboxes" / name).exists()


def test_context_exit_cleans_up(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    with make_pair(fake_sbx, tmp_path) as pair:
        assert not gone(fake_sbx, "sbxloop-r1-agent")
    assert gone(fake_sbx, "sbxloop-r1-agent")
    assert gone(fake_sbx, "sbxloop-r1-github")
    assert pair not in cleanup_registry._pairs


def test_cleanup_on_exception(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="boom"), make_pair(fake_sbx, tmp_path):
        raise RuntimeError("boom")
    assert gone(fake_sbx, "sbxloop-r1-agent")
    assert gone(fake_sbx, "sbxloop-r1-github")


def test_keep_skips_cleanup(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    with make_pair(fake_sbx, tmp_path, keep=True) as pair:
        pass
    assert not gone(fake_sbx, "sbxloop-r1-agent")
    assert not gone(fake_sbx, "sbxloop-r1-github")
    assert pair not in cleanup_registry._pairs
    pair.cleanup()  # explicit cleanup still works afterwards
    assert gone(fake_sbx, "sbxloop-r1-agent")


def test_cleanup_is_idempotent_and_best_effort(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    pair = make_pair(fake_sbx, tmp_path)
    # stop fails for the agent sandbox; rm should still run for both
    fake_sbx.fail_next("stop sbxloop-r1-agent", returncode=1, stderr="flake")
    pair.cleanup()
    assert gone(fake_sbx, "sbxloop-r1-agent")
    assert gone(fake_sbx, "sbxloop-r1-github")
    pair.cleanup()  # second call is a no-op, not an error


def test_registry_cleanup_all(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    pair = make_pair(fake_sbx, tmp_path)
    cleanup_registry.register(pair)
    cleanup_registry.cleanup_all()
    assert gone(fake_sbx, "sbxloop-r1-agent")
    assert pair not in cleanup_registry._pairs


def test_signal_handler_cleans_and_reraises(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    pair = make_pair(fake_sbx, tmp_path)
    cleanup_registry.register(pair)
    cleanup_registry._previous[signal.SIGINT] = None
    with pytest.raises(KeyboardInterrupt):
        cleanup_registry._handle_signal(signal.SIGINT, None)
    assert gone(fake_sbx, "sbxloop-r1-agent")


def test_signal_handler_quiesces_before_cleanup(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    # The driver's quiesce callback (the TUI signals + joins its engine
    # thread) must run BEFORE the pairs are torn down, and a raising
    # callback must never block cleanup.
    pair = make_pair(fake_sbx, tmp_path)
    cleanup_registry.register(pair)
    cleanup_registry._previous[signal.SIGINT] = None
    order: list[str] = []

    def quiesce() -> None:
        order.append("quiesce")
        assert not gone(fake_sbx, "sbxloop-r1-agent")  # sandboxes still alive
        raise RuntimeError("quiesce hiccup")  # contained, never blocks cleanup

    cleanup_registry.set_quiesce(quiesce)
    try:
        with pytest.raises(KeyboardInterrupt):
            cleanup_registry._handle_signal(signal.SIGINT, None)
    finally:
        cleanup_registry.set_quiesce(None)
    assert order == ["quiesce"]
    assert gone(fake_sbx, "sbxloop-r1-agent")


def test_signal_handler_sigterm_exits(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    pair = make_pair(fake_sbx, tmp_path)
    cleanup_registry.register(pair)
    cleanup_registry._previous[signal.SIGTERM] = None
    with pytest.raises(SystemExit) as excinfo:
        cleanup_registry._handle_signal(signal.SIGTERM, None)
    assert excinfo.value.code == 128 + signal.SIGTERM
    assert gone(fake_sbx, "sbxloop-r1-github")


class TestHandlerInstallation:
    """The install latch: handlers must land on the main thread exactly once,
    even when the first registration happens on a background thread (#64)."""

    @pytest.fixture
    def hooks(self, monkeypatch: pytest.MonkeyPatch) -> tuple[list[int], list[object]]:
        """Record signal/atexit installs instead of mutating process state,
        and make the test's own thread count as the main thread (pytest
        harnesses don't always run tests there)."""
        installed: list[int] = []
        atexits: list[object] = []

        def fake_signal(signum: int, handler: object) -> None:
            installed.append(signum)

        monkeypatch.setattr(pair_mod.signal, "signal", fake_signal)
        monkeypatch.setattr(pair_mod.atexit, "register", atexits.append)
        test_thread = threading.current_thread()
        monkeypatch.setattr(pair_mod.threading, "main_thread", lambda: test_thread)
        return installed, atexits

    def test_off_main_thread_register_does_not_latch(
        self, fake_sbx: FakeSbx, tmp_path: Path, hooks: tuple[list[int], list[object]]
    ) -> None:
        installed, atexits = hooks
        registry = CleanupRegistry()
        pair = make_pair(fake_sbx, tmp_path)
        worker = threading.Thread(target=lambda: registry.register(pair))
        worker.start()
        worker.join()
        assert installed == []  # cannot install signal handlers off-main-thread
        assert len(atexits) == 1  # but the atexit safety net is in place
        # a later main-thread registration must still install them
        registry.register(pair)
        assert sorted(installed) == sorted([signal.SIGINT, signal.SIGTERM])
        # ... and exactly once, no matter how often installation is retried
        registry.register(pair)
        registry.install_handlers()
        assert len(installed) == 2
        assert len(atexits) == 1

    def test_install_handlers_is_explicit_and_idempotent(
        self, hooks: tuple[list[int], list[object]]
    ) -> None:
        installed, atexits = hooks
        registry = CleanupRegistry()
        registry.install_handlers()  # no pair registered yet — still installs
        assert sorted(installed) == sorted([signal.SIGINT, signal.SIGTERM])
        registry.install_handlers()
        assert len(installed) == 2
        assert len(atexits) == 1


def _latest_run_state(db: Path) -> str | None:
    if not db.is_file():
        return None
    try:
        with contextlib.closing(sqlite3.connect(db)) as conn:
            row = conn.execute("SELECT state FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:  # mid-write / not yet initialized
        return None
    return row[0] if row else None


def test_tui_run_sigterm_removes_both_sandboxes(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    """Acceptance for #64: a TUI-mode run (engine on a background thread)
    receiving SIGTERM must clean up the sandbox pair before exiting. Runs the
    real CLI in a subprocess because signal disposition is process-global."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    script = workdir / "echo-script.json"
    # decompose blocks in the worker long enough for SIGTERM to land mid-run
    script.write_text(json.dumps([{"text": "never used", "sleep_s": 60}]))
    env = os.environ.copy()  # PATH already carries the fake sbx shim
    env.update(
        {
            "SBXLOOP_WORKER_BACKEND": "echo",
            "SBXLOOP_ECHO_SCRIPT": str(script),
            "COPILOT_GITHUB_TOKEN": "tok",
            "GH_TOKEN": "tok",
            "SBXLOOP_WORKER_PYTHON": sys.executable,
            "SBXLOOP_INSTALL_WORKERS": "false",
            "SBXLOOP_GITHUB__REPO": "owner/repo",  # so the pair has both roles
        }
    )
    log_path = workdir / "run.log"
    code = "from sbxloop.cli.app import app; app(['run', 'sleep forever'])"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=workdir,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        # wait until the run is inside `with pair:` (state moves past
        # provisioning), i.e. registered for cleanup with both sandboxes up
        db = workdir / ".sbxloop" / "state.db"
        deadline = time.monotonic() + 90
        state = None
        while time.monotonic() < deadline:
            state = _latest_run_state(db)
            if state not in (None, "provisioning") or proc.poll() is not None:
                break
            time.sleep(0.2)
        assert proc.poll() is None, f"run exited early:\n{log_path.read_text()}"
        assert state in ("decomposing", "running"), state
        sandboxes = fake_sbx.state / "sandboxes"
        live = sorted(p.name for p in sandboxes.iterdir())
        assert len(live) == 2, live
        assert live[0].endswith("-agent") and live[1].endswith("-github"), live

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=60)
        assert proc.returncode == 128 + signal.SIGTERM, log_path.read_text()
        leftover = sorted(p.name for p in sandboxes.iterdir()) if sandboxes.is_dir() else []
        assert leftover == []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
