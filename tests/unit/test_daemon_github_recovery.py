"""Authentication outages must not strand the daemon's stable sandbox name."""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sbxloop.config import Config
from sbxloop.daemon.github import DaemonGithub
from sbxloop.errors import DaemonError
from sbxloop.events import EventBus
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxSpec
from tests.conftest import FakeSbx

AUTH_ERROR = (
    "ERROR: request failed: 401 Unauthorized: user is not authenticated to Docker: "
    "no default account profile set: secret not found"
)


def make_github(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DaemonGithub:
    monkeypatch.setenv("GH_TOKEN", "github_pat_test")
    return DaemonGithub(
        Config.model_validate({"home": str(tmp_path / "home")}),
        SbxCLI(binary=str(fake_sbx.binary)),
        EventBus(),
        worker_python=sys.executable,
        install_workers=False,
    )


@pytest.mark.parametrize("failed_command", ["ls", "rm"])
def test_cleanup_retries_after_authentication_recovers(
    fake_sbx: FakeSbx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_command: str,
) -> None:
    github = make_github(fake_sbx, tmp_path, monkeypatch)
    for name in (github.name, "another-instance"):
        github.sbx.create(SandboxSpec(name=name, role="github", workspace=tmp_path))
        github.sbx.stop(name)
    keep = fake_sbx.sandbox_fs("another-instance") / "keep.txt"
    keep.write_text("another daemon's data")
    fake_sbx.fail_next(failed_command, stderr=AUTH_ERROR)

    with pytest.raises(DaemonError, match="401 Unauthorized"):
        github.ops()
    # Unknown inventory or a failed removal must not be treated as absence.
    assert len(fake_sbx.invocations("create")) == 2
    assert fake_sbx.sandbox_fs(github.name).is_dir()

    ops = github.ops()
    assert len(fake_sbx.invocations("create")) == 3
    assert keep.read_text() == "another daemon's data"
    calls = fake_sbx.invocations()
    assert github.ops() is ops
    assert fake_sbx.invocations() == calls  # A live handle is never cleaned up again.


def test_failed_close_is_cleaned_up_before_reprovision(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = make_github(fake_sbx, tmp_path, monkeypatch)
    first = github.ops()
    fake_sbx.fail_next("rm", stderr=AUTH_ERROR)
    github.close()

    assert github.ops() is not first
    assert len(fake_sbx.invocations("create")) == 2


def test_concurrent_requests_share_one_provision(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = make_github(fake_sbx, tmp_path, monkeypatch)
    entered, release, competing = threading.Event(), threading.Event(), threading.Event()
    create = github.sbx.create

    def delayed_create(spec: SandboxSpec) -> None:
        if entered.is_set():
            competing.set()
        entered.set()
        assert release.wait(10)
        create(spec)

    monkeypatch.setattr(github.sbx, "create", delayed_create)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(github.ops)
        assert entered.wait(10)
        second = pool.submit(github.ops)
        try:
            assert not competing.wait(0.5), "two requests attempted to create the same sandbox"
        finally:
            release.set()
        assert first.result(timeout=10) is second.result(timeout=10)
    assert len(fake_sbx.invocations("create")) == 1
