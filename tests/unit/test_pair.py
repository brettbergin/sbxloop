"""SandboxPair + cleanup registry semantics."""

import signal
import threading
from pathlib import Path

import pytest

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


def test_cleanup_all_leaves_kept_pairs_alive(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    """--keep-sandboxes pairs survive aborts too: cleanup_all only drops
    them from the registry (their kept marker is already in the run DB)."""
    pair = make_pair(fake_sbx, tmp_path, keep=True)
    cleanup_registry.register(pair)
    cleanup_registry.cleanup_all()
    assert not gone(fake_sbx, "sbxloop-r1-agent")
    assert not gone(fake_sbx, "sbxloop-r1-github")
    assert pair not in cleanup_registry._pairs


def test_signal_handler_raises_without_in_handler_cleanup(
    fake_sbx: FakeSbx, tmp_path: Path
) -> None:
    """SIGINT converts to KeyboardInterrupt; sandbox teardown happens by
    unwinding (context managers / CLI / atexit), never inside the handler,
    where it would block for seconds and re-enter on a second Ctrl+C."""
    pair = make_pair(fake_sbx, tmp_path)
    cleanup_registry.register(pair)
    try:
        cleanup_registry._previous[signal.SIGINT] = None
        with pytest.raises(KeyboardInterrupt):
            cleanup_registry._handle_signal(signal.SIGINT, None)
        assert not gone(fake_sbx, "sbxloop-r1-agent")
    finally:
        pair.cleanup()


def test_signal_handler_sigterm_exits_without_in_handler_cleanup(
    fake_sbx: FakeSbx, tmp_path: Path
) -> None:
    pair = make_pair(fake_sbx, tmp_path)
    cleanup_registry.register(pair)
    try:
        cleanup_registry._previous[signal.SIGTERM] = None
        with pytest.raises(SystemExit) as excinfo:
            cleanup_registry._handle_signal(signal.SIGTERM, None)
        assert excinfo.value.code == 128 + signal.SIGTERM
        assert not gone(fake_sbx, "sbxloop-r1-github")
    finally:
        pair.cleanup()


def test_signal_handlers_installable_after_offmain_register(
    fake_sbx: FakeSbx, tmp_path: Path
) -> None:
    """register() from a worker thread (the TUI engine thread) cannot install
    signal handlers — but that must not latch: a later main-thread
    install_signal_handlers() call has to succeed."""
    registry = CleanupRegistry()
    registry._atexit_installed = True  # keep the test's throwaway registry out of atexit
    pair = make_pair(fake_sbx, tmp_path)
    thread = threading.Thread(target=lambda: registry.register(pair))
    thread.start()
    thread.join()
    assert not registry._signals_installed
    originals = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        registry.install_signal_handlers()
        assert registry._signals_installed
        for signum in (signal.SIGINT, signal.SIGTERM):
            handler = signal.getsignal(signum)
            assert getattr(handler, "__self__", None) is registry
    finally:
        for signum, original in originals.items():
            signal.signal(signum, original)
        pair.cleanup()
