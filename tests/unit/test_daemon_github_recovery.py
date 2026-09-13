"""Authentication outages must not strand the daemon's stable sandbox name,
and a box already gone at teardown is not a fault."""

from __future__ import annotations

import logging
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


def test_close_takes_a_box_already_gone_as_removed(
    fake_sbx: FakeSbx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The daemon's box can vanish under it: an operator's `sbx rm`, a
    backend that reaped a dead microVM. Tearing down what is already gone
    reaches the state the teardown wanted; it used to be reported as a
    failure, traceback and all, to the error tracker (#1087)."""
    github = make_github(fake_sbx, tmp_path, monkeypatch)
    github.ops()
    github.sbx.rm(github.name)  # out from under the daemon

    with caplog.at_level(logging.INFO, logger="sbxloop.daemon.github"):
        github.close()

    messages = [record.getMessage() for record in caplog.records]
    assert any("github_sandbox.already_gone" in message for message in messages)
    assert not any("github_sandbox.remove_failed" in message for message in messages)
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    # Closed is closed: the next call provisions afresh, as after any close.
    github.ops()
    assert len(fake_sbx.invocations("create")) == 2


@pytest.mark.parametrize("inventory", ["still lists it", "cannot be read"])
def test_close_keeps_reporting_a_not_found_the_inventory_does_not_confirm(
    fake_sbx: FakeSbx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    inventory: str,
) -> None:
    """`sbx rm` answering "not found" is not proof of absence: a Docker
    authentication failure says "secret not found" (#254). Only an
    inventory that no longer lists the name settles it; a box still listed,
    or an inventory that cannot be read, stays the failure it looks like."""
    github = make_github(fake_sbx, tmp_path, monkeypatch)
    github.ops()
    fake_sbx.fail_next("rm", stderr=AUTH_ERROR)
    if inventory == "cannot be read":
        fake_sbx.fail_next("ls", stderr=AUTH_ERROR)

    with caplog.at_level(logging.INFO, logger="sbxloop.daemon.github"):
        github.close()

    messages = [record.getMessage() for record in caplog.records]
    assert any("github_sandbox.remove_failed" in message for message in messages)
    assert not any("github_sandbox.already_gone" in message for message in messages)
    failed = next(r for r in caplog.records if "github_sandbox.remove_failed" in r.getMessage())
    assert failed.levelno == logging.WARNING
    assert fake_sbx.sandbox_fs(github.name).is_dir()  # nothing was torn down behind the report
