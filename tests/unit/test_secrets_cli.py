"""CLI tests for the `sbxloop secrets` command group."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sbxloop.cli.app import app
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.secretstate import COPILOT_TOKEN_ENV, COPILOT_TOKEN_HOST
from tests.conftest import FakeSbx

runner = CliRunner()


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(COPILOT_TOKEN_ENV, raising=False)
    # A wide console so rich never wraps table cells mid-scope-name.
    monkeypatch.setenv("COLUMNS", "300")
    return tmp_path


def custom_state(fake_sbx: FakeSbx) -> dict[str, dict[str, str]]:
    path = fake_sbx.state / "secrets-state.json"
    data = json.loads(path.read_text()) if path.is_file() else {"custom": {}}
    return data["custom"]


def register_stale(fake_sbx: FakeSbx, scope: str = "sbxloop-dead-agent") -> None:
    """A leftover registration owned by a sandbox that no longer exists."""
    SbxCLI().secret_set_custom(
        host=COPILOT_TOKEN_HOST, env=COPILOT_TOKEN_ENV, value="old", sandbox=scope
    )


class TestList:
    def test_flags_stale_scope(self, fake_sbx: FakeSbx, workdir: Path) -> None:
        register_stale(fake_sbx)
        result = runner.invoke(app, ["secrets", "list"])
        assert result.exit_code == 0
        assert "sbxloop-dead-agent" in result.output
        assert "stale" in result.output
        assert "clean" in result.output  # points at the remedy

    def test_absent_registration_is_ok(self, fake_sbx: FakeSbx, workdir: Path) -> None:
        result = runner.invoke(app, ["secrets", "list"])
        assert result.exit_code == 0
        assert "not registered" in result.output

    def test_never_mentions_managing_service_secret(self, fake_sbx: FakeSbx, workdir: Path) -> None:
        result = runner.invoke(app, ["secrets", "list"])
        assert "never managed here" in result.output

    def test_sbx_failure_exits_2(self, fake_sbx: FakeSbx, workdir: Path) -> None:
        fake_sbx.script("ls", returncode=1, stderr="daemon down")
        result = runner.invoke(app, ["secrets", "list"])
        assert result.exit_code == 2


class TestClean:
    def test_dry_run_by_default(self, fake_sbx: FakeSbx, workdir: Path) -> None:
        register_stale(fake_sbx)
        result = runner.invoke(app, ["secrets", "clean"])
        assert result.exit_code == 0
        assert "would remove" in result.output
        assert "--apply" in result.output
        assert COPILOT_TOKEN_ENV in custom_state(fake_sbx)  # untouched

    def test_apply_removes_stale_registration(self, fake_sbx: FakeSbx, workdir: Path) -> None:
        register_stale(fake_sbx)
        result = runner.invoke(app, ["secrets", "clean", "--apply"])
        assert result.exit_code == 0
        assert "removed" in result.output
        assert custom_state(fake_sbx) == {}

    def test_global_canonical_needs_all(self, fake_sbx: FakeSbx, workdir: Path) -> None:
        SbxCLI().secret_set_custom(host=COPILOT_TOKEN_HOST, env=COPILOT_TOKEN_ENV, value="tok")
        result = runner.invoke(app, ["secrets", "clean", "--apply"])
        assert result.exit_code == 0
        assert "nothing to clean" in result.output
        assert COPILOT_TOKEN_ENV in custom_state(fake_sbx)
        result = runner.invoke(app, ["secrets", "clean", "--apply", "--all"])
        assert result.exit_code == 0
        assert custom_state(fake_sbx) == {}

    def test_foreign_scope_is_never_removed(self, fake_sbx: FakeSbx, workdir: Path) -> None:
        register_stale(fake_sbx, scope="someones-app")
        result = runner.invoke(app, ["secrets", "clean", "--apply", "--all"])
        assert result.exit_code == 0
        assert COPILOT_TOKEN_ENV in custom_state(fake_sbx)  # untouched

    def test_apply_failure_exits_1(self, fake_sbx: FakeSbx, workdir: Path) -> None:
        register_stale(fake_sbx)
        fake_sbx.script("secret rm", returncode=1, stderr="unknown command")
        result = runner.invoke(app, ["secrets", "clean", "--apply"])
        assert result.exit_code == 1
        assert "rejected" in result.output


class TestRotate:
    def test_requires_token_never_argv(self, fake_sbx: FakeSbx, workdir: Path) -> None:
        result = runner.invoke(app, ["secrets", "rotate", "--no-verify"])
        assert result.exit_code == 2
        assert "--prompt" in result.output
        # rotate takes no token argument at all
        result = runner.invoke(app, ["secrets", "rotate", "newtoken"])
        assert result.exit_code != 0

    def test_rotates_from_env(
        self, fake_sbx: FakeSbx, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        register_stale(fake_sbx)
        monkeypatch.setenv(COPILOT_TOKEN_ENV, "github_pat_new")
        result = runner.invoke(app, ["secrets", "rotate", "--no-verify"])
        assert result.exit_code == 0, result.output
        entry = custom_state(fake_sbx)[COPILOT_TOKEN_ENV]
        assert entry == {"scope": "-g", "host": COPILOT_TOKEN_HOST, "value": "github_pat_new"}
        assert "rotated" in result.output

    def test_rotates_from_hidden_prompt(self, fake_sbx: FakeSbx, workdir: Path) -> None:
        result = runner.invoke(app, ["secrets", "rotate", "--prompt", "--no-verify"], input="tok\n")
        assert result.exit_code == 0, result.output
        assert custom_state(fake_sbx)[COPILOT_TOKEN_ENV]["value"] == "tok"
        # the value is prompted with hidden input, never echoed
        assert "tok" not in result.output.replace("token", "")

    def test_verify_reports_proxy_when_env_visible(
        self, fake_sbx: FakeSbx, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the fake's exec inherits this process env, so the env IS visible
        monkeypatch.setenv(COPILOT_TOKEN_ENV, "github_pat_new")
        result = runner.invoke(app, ["secrets", "rotate"])
        assert result.exit_code == 0, result.output
        assert "proxy" in result.output
        # the throwaway verification sandbox was removed again
        assert not (fake_sbx.state / "sandboxes").exists() or not any(
            (fake_sbx.state / "sandboxes").iterdir()
        )

    def test_verify_reports_plain_env_fallback(
        self, fake_sbx: FakeSbx, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COPILOT_TOKEN_ENV, "github_pat_new")
        fake_sbx.script("exec sbxloop-secretcheck", returncode=1)
        result = runner.invoke(app, ["secrets", "rotate"])
        assert result.exit_code == 0, result.output
        assert "plain-env fallback" in result.output

    def test_plain_env_strategy_skips_verification(
        self, fake_sbx: FakeSbx, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (workdir / "sbxloop.toml").write_text('secret_strategy = "plain-env"\n')
        monkeypatch.setenv(COPILOT_TOKEN_ENV, "github_pat_new")
        result = runner.invoke(app, ["secrets", "rotate"])
        assert result.exit_code == 0, result.output
        assert "plain-env" in result.output
        # no verification sandbox was ever created
        assert fake_sbx.invocations("create") == []

    def test_warns_about_live_sandboxes(
        self, fake_sbx: FakeSbx, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.sbx.models import SandboxSpec

        SbxCLI().create(SandboxSpec(name="sbxloop-r9-agent", role="agent", workspace=workdir))
        monkeypatch.setenv(COPILOT_TOKEN_ENV, "github_pat_new")
        result = runner.invoke(app, ["secrets", "rotate", "--no-verify"])
        assert result.exit_code == 0, result.output
        assert "sbxloop-r9-agent" in result.output
        assert "sandbox rm --all" in result.output
