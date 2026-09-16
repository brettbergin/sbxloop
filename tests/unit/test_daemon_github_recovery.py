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
from sbxloop.errors import DaemonError, SbxNotFoundError
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


def test_a_stale_box_whose_teardown_is_already_in_flight_does_not_fail_the_provision(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Field failure: while sandboxd recovered, `sbx ls` listed the stale box
    and `sbx rm` then answered "not found". The inventory confirming it gone
    settles it, as at close; the provision used to fail on it instead."""
    github = make_github(fake_sbx, tmp_path, monkeypatch)
    github.sbx.create(SandboxSpec(name=github.name, role="github", workspace=tmp_path))
    rm = github.sbx.rm

    def rm_racing_a_teardown(name: str, *, force: bool = True, settle: bool = True) -> None:
        rm(name, force=force, settle=settle)
        raise SbxNotFoundError(f"ERROR: sandbox '{name}' not found")

    monkeypatch.setattr(github.sbx, "rm", rm_racing_a_teardown)

    github.ops()

    assert len(fake_sbx.invocations("create")) == 2


def test_a_stale_box_still_listed_after_not_found_fails_the_provision(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: "not found" while the inventory still lists the name
    proves nothing, and creating over it would collide."""
    github = make_github(fake_sbx, tmp_path, monkeypatch)
    github.sbx.create(SandboxSpec(name=github.name, role="github", workspace=tmp_path))
    fake_sbx.fail_next("rm", stderr=f"ERROR: sandbox '{github.name}' not found")

    with pytest.raises(DaemonError, match="not found"):
        github.ops()
    assert len(fake_sbx.invocations("create")) == 1


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


def test_a_box_the_backend_cannot_remove_is_left_behind_and_the_daemon_moves_on(
    fake_sbx: FakeSbx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Field failure (#1165): the box hung mid-job, and every later poll ran
    `sbx rm` against it, waited out the timeout and failed the provision,
    for a day. The wedged box is reported once and left to the backend; the
    daemon carries on under the next generation of the name."""
    github = make_github(fake_sbx, tmp_path, monkeypatch)
    base = github.name
    github.sbx.create(SandboxSpec(name=base, role="github", workspace=tmp_path))
    fake_sbx.script(f"rm --force {base}", returncode=1, stderr="ERROR: context deadline exceeded")

    with caplog.at_level(logging.INFO, logger="sbxloop.daemon.github"):
        github.ops()

    assert github.name == f"{base}-g1"
    assert {base, github.name} <= {info.name for info in github.sbx.ls()}
    wedged = [r for r in caplog.records if "github_sandbox.wedged" in r.getMessage()]
    assert len(wedged) == 1 and wedged[0].levelno == logging.ERROR
    assert "sbx-sandboxd" in wedged[0].getMessage()
    # Once per process: a re-provision never waits on the wedged box again.
    github.close()
    github.ops()
    assert github.name == f"{base}-g1"
    assert [c for c in fake_sbx.invocations("rm") if c[-1] == base] == [["rm", "--force", base]]


def test_a_create_the_backend_refuses_is_retried_under_the_next_generation(
    fake_sbx: FakeSbx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The backend came back from a crash without the box but with its
    volume, and refused the name on every create while the inventory had
    nothing to remove. One retry under a fresh name, not an outage."""
    github = make_github(fake_sbx, tmp_path, monkeypatch)
    base = github.name
    fake_sbx.fail_next(f"create --name={base}", stderr="ERROR: failed to run sandbox container")

    with caplog.at_level(logging.INFO, logger="sbxloop.daemon.github"):
        github.ops()

    assert github.name == f"{base}-g1"
    assert [c[1] for c in fake_sbx.invocations("create")] == [
        f"--name={base}",
        f"--name={base}-g1",
    ]
    assert any("github_sandbox.create_retried" in r.getMessage() for r in caplog.records)
    assert len([r for r in caplog.records if "github_sandbox.wedged" in r.getMessage()]) == 1


def test_a_sign_in_failure_at_create_is_not_a_wedged_box(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody signed in fails every name alike: no generation is spent on it."""
    github = make_github(fake_sbx, tmp_path, monkeypatch)
    base = github.name
    fake_sbx.fail_next("create", stderr=AUTH_ERROR)

    with pytest.raises(DaemonError, match="401 Unauthorized"):
        github.ops()

    assert len(fake_sbx.invocations("create")) == 1
    assert github.name == base


def test_a_close_that_cannot_remove_the_box_starts_a_new_generation_next_time(
    fake_sbx: FakeSbx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A job timeout drops the box; when that removal hangs too, the next
    provision must not pay for the same hang again before creating."""
    github = make_github(fake_sbx, tmp_path, monkeypatch)
    github.ops()
    base = github.name
    fake_sbx.script(f"rm --force {base}", returncode=1, stderr="ERROR: context deadline exceeded")

    with caplog.at_level(logging.INFO, logger="sbxloop.daemon.github"):
        github.close()
        github.ops()

    assert github.name == f"{base}-g1"
    assert [c for c in fake_sbx.invocations("rm") if c[-1] == base] == [["rm", "--force", base]]
    assert any("github_sandbox.remove_failed" in r.getMessage() for r in caplog.records)
