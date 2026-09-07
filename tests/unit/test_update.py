"""The explicit self-update command never upgrades a different installation."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path

import pytest
from typer.testing import CliRunner

import sbxloop
from sbxloop import update
from sbxloop.cli.app import app
from sbxloop.daemon import versions
from sbxloop.paths import SbxloopHome

runner = CliRunner()


def test_check_reports_installed_and_available_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sbxloop, "__version__", "1.2.9")
    monkeypatch.setattr(versions, "fetch_latest", lambda _name: "1.2.10")
    result = runner.invoke(app, ["update", "--check"])
    assert result.exit_code == 0, result.output
    assert "1.2.9" in result.output
    assert "1.2.10" in result.output
    assert "available" in result.output


class FakeRun:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.verified = '["1.2.10", "1.2.10"]'
        self.failure: Exception | None = None
        self.fail_at = 1

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if self.failure is not None and len(self.calls) == self.fail_at:
            raise self.failure
        return subprocess.CompletedProcess(argv, 0, self.verified, "")


@pytest.fixture
def installation(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SbxloopHome, FakeRun]:
    home = SbxloopHome(isolated_home / ".sbxloop")
    home.venv_python.parent.mkdir(parents=True)
    home.venv_python.touch()
    home.uv.touch()
    home.write_record(sbxloop_version="1.2.9", created_by="original install")
    monkeypatch.setattr(sys, "prefix", str(home.venv))
    monkeypatch.setattr(sbxloop, "__file__", str(home.venv / "lib" / "sbxloop" / "__init__.py"))
    monkeypatch.setattr(sbxloop, "__version__", "1.2.9")
    monkeypatch.setattr(versions, "fetch_latest", lambda _name: "1.2.10")

    def no_sdk(_name: str) -> str:
        raise metadata.PackageNotFoundError

    monkeypatch.setattr(update.metadata, "version", no_sdk)
    run = FakeRun()
    monkeypatch.setattr(update, "_run", run)
    return home, run


def test_update_installs_exact_pair_and_verifies_before_recording(
    installation: tuple[SbxloopHome, FakeRun],
) -> None:
    home, run = installation
    home.config_toml.write_text("# operator config\n")
    home.secrets_env.write_text("OPERATOR_SECRET=kept\n")
    before = home.read_record()
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert run.calls == [
        [
            str(home.uv),
            "--no-config",
            "pip",
            "install",
            "--python",
            str(home.venv_python),
            "sbxloop[discord,slack]==1.2.10",
            "sbxloop-worker==1.2.10",
        ],
        [str(home.venv_python), "-I", "-c", update.VERIFY_SCRIPT],
    ]
    record = home.read_record()
    assert before is not None and record is not None
    assert record.sbxloop_version == "1.2.10"
    assert record.created_by == before.created_by
    assert record.created_at == before.created_at
    assert home.config_toml.read_text() == "# operator config\n"
    assert home.secrets_env.read_text() == "OPERATOR_SECRET=kept\n"
    assert "Updated sbxloop and sbxloop-worker to 1.2.10" in result.output
    assert "Restart any running daemon when idle" in result.output


@pytest.mark.parametrize("flag", ["--check", "--dry-run"])
def test_previews_never_install_or_change_the_record(
    installation: tuple[SbxloopHome, FakeRun], flag: str
) -> None:
    home, run = installation
    record = home.record.read_bytes()
    result = runner.invoke(app, ["update", flag])
    assert result.exit_code == 0, result.output
    assert not run.calls
    assert home.record.read_bytes() == record
    assert "Update available" in result.output
    if flag == "--dry-run":
        assert "Would run:" in result.output
        assert "sbxloop[discord,slack]==1.2.10" in result.output
        assert "sbxloop-worker==1.2.10" in result.output


@pytest.mark.parametrize(
    ("installed", "latest", "message"),
    [
        ("1.2.10", "1.2.10", "Already up to date"),
        ("1.2.10", "1.2.9", "Installed version is newer"),
        ("1.2.10", "1.2.10.0", "Already up to date"),
        ("1.2.10.post1", "1.2.10", "Installed version is newer"),
    ],
)
def test_never_reinstalls_or_downgrades(
    installation: tuple[SbxloopHome, FakeRun],
    monkeypatch: pytest.MonkeyPatch,
    installed: str,
    latest: str,
    message: str,
) -> None:
    home, run = installation
    record = home.record.read_bytes()
    monkeypatch.setattr(sbxloop, "__version__", installed)
    monkeypatch.setattr(versions, "fetch_latest", lambda _name: latest)
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert message in result.output
    assert not run.calls
    assert home.record.read_bytes() == record


@pytest.mark.parametrize("installed", ["1.2.10.dev1", "1.2.9+local"])
def test_development_installs_can_check_but_cannot_self_update(
    installation: tuple[SbxloopHome, FakeRun], monkeypatch: pytest.MonkeyPatch, installed: str
) -> None:
    _, run = installation
    monkeypatch.setattr(sbxloop, "__version__", installed)
    checked = runner.invoke(app, ["update", "--check"])
    assert checked.exit_code == 0, checked.output
    assert "development build" in checked.output
    updated = runner.invoke(app, ["update"])
    assert updated.exit_code == 1, updated.output
    assert "development build" in updated.output
    assert not run.calls


@pytest.mark.parametrize("installed", ["0.0.0", "", "unknown"])
def test_unknown_installed_version_fails_closed(
    installation: tuple[SbxloopHome, FakeRun], monkeypatch: pytest.MonkeyPatch, installed: str
) -> None:
    _, run = installation
    monkeypatch.setattr(sbxloop, "__version__", installed)
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 1, result.output
    assert "installed sbxloop version" in result.output
    assert not run.calls


@pytest.mark.parametrize("latest", [None, "", "bad", "0.0.0", "1.3.0rc1", "1.3.0+local"])
def test_missing_or_invalid_release_never_installs(
    installation: tuple[SbxloopHome, FakeRun],
    monkeypatch: pytest.MonkeyPatch,
    latest: str | None,
) -> None:
    home, run = installation
    record = home.record.read_bytes()
    monkeypatch.setattr(versions, "fetch_latest", lambda _name: latest)
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 1, result.output
    assert "PyPI" in result.output
    assert not run.calls
    assert home.record.read_bytes() == record


@pytest.mark.parametrize("flag", [[], ["--dry-run"]])
@pytest.mark.parametrize("wrong", ["prefix", "editable", "home_override"])
def test_wrong_environment_never_updates_another_installation(
    installation: tuple[SbxloopHome, FakeRun],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flag: list[str],
    wrong: str,
) -> None:
    _, run = installation
    if wrong == "prefix":
        monkeypatch.setattr(sys, "prefix", str(tmp_path / "pipx-venv"))
    elif wrong == "editable":
        monkeypatch.setattr(sbxloop, "__file__", str(tmp_path / "checkout" / "__init__.py"))
    else:
        monkeypatch.setenv("SBXLOOP_HOME", str(tmp_path / "different-home"))
    result = runner.invoke(app, ["update", *flag])
    assert result.exit_code == 1, result.output
    assert "home's own installation" in result.output
    assert not run.calls


def test_check_works_outside_the_home_installation(
    installation: tuple[SbxloopHome, FakeRun], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, run = installation
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "pipx-venv"))
    result = runner.invoke(app, ["update", "--check"])
    assert result.exit_code == 0, result.output
    assert "Update available" in result.output
    assert not run.calls


@pytest.mark.parametrize("missing", ["uv", "venv_python", "record", "invalid_record"])
def test_incomplete_home_names_what_needs_repair(
    installation: tuple[SbxloopHome, FakeRun], missing: str
) -> None:
    home, run = installation
    if missing == "invalid_record":
        home.record.write_text("not json")
    else:
        getattr(home, missing).unlink()
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 1, result.output
    assert "sbxloop init" in result.output
    assert not run.calls


def test_optional_host_sdk_is_preserved(
    installation: tuple[SbxloopHome, FakeRun], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run = installation
    monkeypatch.setattr(update.metadata, "version", lambda _name: "1.0.0")
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert "sbxloop[discord,slack,copilot]==1.2.10" in run.calls[0]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (subprocess.CalledProcessError(1, ["uv"], stderr="resolution failed"), "resolution failed"),
        (subprocess.TimeoutExpired(["uv"], 600), "timed out"),
        (FileNotFoundError("uv missing"), "could not run"),
    ],
)
@pytest.mark.parametrize("fail_at", [1, 2])
def test_failed_installation_or_verification_does_not_claim_success(
    installation: tuple[SbxloopHome, FakeRun],
    failure: Exception,
    message: str,
    fail_at: int,
) -> None:
    home, run = installation
    before = home.record.read_bytes()
    run.failure, run.fail_at = failure, fail_at
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 1, result.output
    assert message in result.output
    assert ("installation" if fail_at == 1 else "verification") in result.output
    assert "Updated sbxloop" not in result.output
    assert len(run.calls) == fail_at
    assert home.record.read_bytes() == before


@pytest.mark.parametrize(
    "verified", ["garbage", '["1.2.10", "1.2.9"]', '["1.2.9", "1.2.9"]', "null", "{}"]
)
def test_both_packages_must_be_verified_before_the_record_changes(
    installation: tuple[SbxloopHome, FakeRun], verified: str
) -> None:
    home, run = installation
    record = home.record.read_bytes()
    run.verified = verified
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 1, result.output
    assert "Updated sbxloop" not in result.output
    assert home.record.read_bytes() == record


def test_record_failure_reports_that_the_packages_were_updated(
    installation: tuple[SbxloopHome, FakeRun], monkeypatch: pytest.MonkeyPatch
) -> None:
    def cannot_write(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read-only record")

    monkeypatch.setattr(SbxloopHome, "write_record", cannot_write)
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 1, result.output
    assert "packages updated, but could not update" in result.output


def test_conflicting_flags_fail_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(_name: str) -> str:
        pytest.fail("should validate options before checking PyPI")

    monkeypatch.setattr(versions, "fetch_latest", unexpected)
    result = runner.invoke(app, ["update", "--check", "--dry-run"])
    assert result.exit_code == 2, result.output
    assert "choose either" in result.output


def test_runner_is_bounded_and_does_not_use_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_subprocess(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps(["1.2.10", "1.2.10"]), "")

    monkeypatch.setattr(subprocess, "run", fake_subprocess)
    update._run(["/home/operator space/.sbxloop/bin/uv", "pip", "install", "sbxloop==1.2.10"])
    assert len(calls) == 1
    argv, options = calls[0]
    assert argv[0] == "/home/operator space/.sbxloop/bin/uv"
    assert options["check"] is True
    assert options["stdin"] == subprocess.DEVNULL
    assert options["timeout"] == 600
    assert not options.get("shell")
