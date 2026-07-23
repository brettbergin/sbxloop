"""SandboxPair + cleanup registry semantics."""

import signal
from pathlib import Path

import pytest

from sdxloop.sbx.cli import SbxCLI
from sdxloop.sbx.models import SandboxSpec
from sdxloop.sbx.pair import SandboxPair, cleanup_registry
from sdxloop.sbx.sandbox import Sandbox
from tests.conftest import FakeSbx


def make_pair(fake_sbx: FakeSbx, tmp_path: Path, *, keep: bool = False) -> SandboxPair:
    cli = SbxCLI(binary=str(fake_sbx.binary))
    cli.create(SandboxSpec(name="sdxloop-r1-agent", role="agent", workspace=tmp_path))
    cli.create(SandboxSpec(name="sdxloop-r1-github", role="github", workspace=tmp_path))
    return SandboxPair(
        "r1",
        agent=Sandbox(cli, "sdxloop-r1-agent"),
        github=Sandbox(cli, "sdxloop-r1-github"),
        keep=keep,
    )


def gone(fake_sbx: FakeSbx, name: str) -> bool:
    return not (fake_sbx.state / "sandboxes" / name).exists()


def test_context_exit_cleans_up(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    with make_pair(fake_sbx, tmp_path) as pair:
        assert not gone(fake_sbx, "sdxloop-r1-agent")
    assert gone(fake_sbx, "sdxloop-r1-agent")
    assert gone(fake_sbx, "sdxloop-r1-github")
    assert pair not in cleanup_registry._pairs


def test_cleanup_on_exception(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="boom"), make_pair(fake_sbx, tmp_path):
        raise RuntimeError("boom")
    assert gone(fake_sbx, "sdxloop-r1-agent")
    assert gone(fake_sbx, "sdxloop-r1-github")


def test_keep_skips_cleanup(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    with make_pair(fake_sbx, tmp_path, keep=True) as pair:
        pass
    assert not gone(fake_sbx, "sdxloop-r1-agent")
    assert not gone(fake_sbx, "sdxloop-r1-github")
    assert pair not in cleanup_registry._pairs
    pair.cleanup()  # explicit cleanup still works afterwards
    assert gone(fake_sbx, "sdxloop-r1-agent")


def test_cleanup_is_idempotent_and_best_effort(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    pair = make_pair(fake_sbx, tmp_path)
    # stop fails for the agent sandbox; rm should still run for both
    fake_sbx.fail_next("stop sdxloop-r1-agent", returncode=1, stderr="flake")
    pair.cleanup()
    assert gone(fake_sbx, "sdxloop-r1-agent")
    assert gone(fake_sbx, "sdxloop-r1-github")
    pair.cleanup()  # second call is a no-op, not an error


def test_registry_cleanup_all(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    pair = make_pair(fake_sbx, tmp_path)
    cleanup_registry.register(pair)
    cleanup_registry.cleanup_all()
    assert gone(fake_sbx, "sdxloop-r1-agent")
    assert pair not in cleanup_registry._pairs


def test_signal_handler_cleans_and_reraises(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    pair = make_pair(fake_sbx, tmp_path)
    cleanup_registry.register(pair)
    cleanup_registry._previous[signal.SIGINT] = None
    with pytest.raises(KeyboardInterrupt):
        cleanup_registry._handle_signal(signal.SIGINT, None)
    assert gone(fake_sbx, "sdxloop-r1-agent")


def test_signal_handler_sigterm_exits(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    pair = make_pair(fake_sbx, tmp_path)
    cleanup_registry.register(pair)
    cleanup_registry._previous[signal.SIGTERM] = None
    with pytest.raises(SystemExit) as excinfo:
        cleanup_registry._handle_signal(signal.SIGTERM, None)
    assert excinfo.value.code == 128 + signal.SIGTERM
    assert gone(fake_sbx, "sdxloop-r1-github")
