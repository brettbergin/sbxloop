"""The agent backend descriptor (#617): one place that says what each
`[agent] backend` needs, and the host commands that read it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sbxloop import backends
from sbxloop.backends import BACKENDS, CLAUDE, COPILOT, backend_for, backend_named
from sbxloop.cli import models
from sbxloop.cli.app import app
from sbxloop.config import AgentConfig, load_config
from sbxloop.daemon.discord_format import KNOWN_BACKENDS
from sbxloop.errors import SbxloopError
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.secretstate import tracked_custom_secrets
from tests.conftest import FakeSbx

runner = CliRunner()


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("COLUMNS", "300")
    for name in ("COPILOT_GITHUB_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def claude_config(workdir: Path, extra: str = "", top: str = "") -> None:
    (workdir / "sbxloop.toml").write_text(top + '[agent]\nbackend = "claude"\n' + extra)


class TestDescriptor:
    def test_every_config_backend_has_a_descriptor(self) -> None:
        literal = AgentConfig.model_fields["backend"].annotation
        assert set(literal.__args__) == {b.name for b in BACKENDS}  # type: ignore[union-attr]
        assert tuple(b.name for b in BACKENDS) == KNOWN_BACKENDS
        assert BACKENDS[0] is COPILOT  # the default comes first

    def test_named_and_for_config(self, workdir: Path) -> None:
        assert backend_named("claude") is CLAUDE
        with pytest.raises(ValueError, match=r"unknown agent backend 'gemini'.*copilot, claude"):
            backend_named("gemini")
        assert backend_for(load_config()) is COPILOT
        claude_config(workdir)
        assert backend_for(load_config()) is CLAUDE

    def test_copilot_is_the_historical_wording(self) -> None:
        """The copilot deployment must read byte-identical (#617)."""
        assert COPILOT.doctor_check_name == "COPILOT_GITHUB_TOKEN"
        assert COPILOT.secret == ("COPILOT_GITHUB_TOKEN", "api.github.com")
        assert COPILOT.token_hosts == ("api.githubcopilot.com", "api.github.com")
        assert COPILOT.missing_token_detail == (
            'not set — create a fine-grained PAT with the "Copilot Requests" '
            "permission and export COPILOT_GITHUB_TOKEN"
        )
        assert COPILOT.missing_token_error == (
            "COPILOT_GITHUB_TOKEN is not set on the host. Create a fine-grained PAT "
            'with the "Copilot Requests" permission and export it.'
        )

    def test_claude_is_tagged_with_its_backend(self) -> None:
        assert CLAUDE.doctor_check_name == "ANTHROPIC_API_KEY (agent backend: claude)"
        assert CLAUDE.secret == ("ANTHROPIC_API_KEY", "api.anthropic.com")
        assert CLAUDE.token_hosts == ("api.anthropic.com",)
        assert CLAUDE.has_token({"ANTHROPIC_API_KEY": "sk"})
        assert not CLAUDE.has_token({"COPILOT_GITHUB_TOKEN": "tok"})

    def test_secretstate_reexports_the_constants(self) -> None:
        from sbxloop.sbx import secretstate

        assert secretstate.COPILOT_TOKEN_ENV is backends.COPILOT_TOKEN_ENV
        assert secretstate.ANTHROPIC_TOKEN_HOST is backends.ANTHROPIC_TOKEN_HOST

    def test_tracked_secrets_follow_the_backend(self, workdir: Path) -> None:
        assert tracked_custom_secrets(load_config()) == [COPILOT.secret]
        claude_config(workdir)
        assert tracked_custom_secrets(load_config()) == [CLAUDE.secret]


class TestDoctor:
    def _checks(self, env: dict[str, str], fake_sbx: FakeSbx) -> dict[str, Any]:
        from sbxloop.cli.doctor import collect_checks

        checks = collect_checks(env, cli=SbxCLI(binary=str(fake_sbx.binary)))
        return {c.name: c for c in checks}

    def test_claude_host_with_its_key_is_green_and_copilot_free(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#617 acceptance: backend=claude + ANTHROPIC_API_KEY → green agent
        and concierge rows, and no Copilot row of any kind."""
        from sbxloop.cli import doctor

        monkeypatch.setattr(
            doctor, "installed_sdk_permission_kinds", lambda: pytest.fail("copilot SDK probed")
        )
        claude_config(workdir, "[discord]\nchannel_id = 42\n")
        env = {"ANTHROPIC_API_KEY": "sk-ant", "GH_TOKEN": "tok", "DISCORD_BOT_TOKEN": "tok"}
        rows = self._checks(env, fake_sbx)
        assert rows["ANTHROPIC_API_KEY (agent backend: claude)"].ok
        assert rows["chat concierge"].ok
        assert "COPILOT_GITHUB_TOKEN" not in rows
        assert "copilot sdk permission kinds" not in rows
        assert not any("copilot" in name.lower() for name in rows)
        assert any("api.anthropic.com" in name for name in rows)
        assert not any("api.githubcopilot.com" in name for name in rows)

    def test_claude_host_missing_its_key_names_it(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        claude_config(workdir)
        rows = self._checks({"COPILOT_GITHUB_TOKEN": "tok"}, fake_sbx)
        row = rows["ANTHROPIC_API_KEY (agent backend: claude)"]
        assert not row.ok
        assert row.detail == CLAUDE.missing_token_detail

    def test_copilot_rows_are_unchanged(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        rows = self._checks({}, fake_sbx)
        row = rows["COPILOT_GITHUB_TOKEN"]
        assert not row.ok and row.detail == COPILOT.missing_token_detail
        assert "copilot sdk permission kinds" in rows
        assert any("api.githubcopilot.com" in name for name in rows)


class TestSecretsCli:
    def custom_state(self, fake_sbx: FakeSbx) -> dict[str, dict[str, str]]:
        path = fake_sbx.state / "secrets-state.json"
        data = json.loads(path.read_text()) if path.is_file() else {"custom": {}}
        return data["custom"]

    def test_list_shows_the_claude_registration(self, fake_sbx: FakeSbx, workdir: Path) -> None:
        claude_config(workdir)
        result = runner.invoke(app, ["secrets", "list"])
        assert result.exit_code == 0, result.output
        assert "ANTHROPIC_API_KEY" in result.output
        assert "api.anthropic.com" in result.output
        assert "COPILOT_GITHUB_TOKEN" not in result.output

    def test_rotate_rotates_the_claude_key(
        self, fake_sbx: FakeSbx, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        claude_config(workdir)
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "not-this-one")
        result = runner.invoke(app, ["secrets", "rotate", "--no-verify"])
        assert result.exit_code == 2
        assert "ANTHROPIC_API_KEY is not set" in result.output

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-new")
        result = runner.invoke(app, ["secrets", "rotate", "--no-verify"])
        assert result.exit_code == 0, result.output
        state = self.custom_state(fake_sbx)
        assert state["ANTHROPIC_API_KEY"] == {
            "scope": "-g",
            "host": "api.anthropic.com",
            "value": "sk-ant-new",
        }
        assert "COPILOT_GITHUB_TOKEN" not in state
        assert "rotated" in result.output

    def test_rotate_prompt_names_the_claude_key(self, fake_sbx: FakeSbx, workdir: Path) -> None:
        claude_config(workdir)
        result = runner.invoke(app, ["secrets", "rotate", "--prompt", "--no-verify"], input="k\n")
        assert result.exit_code == 0, result.output
        assert "new ANTHROPIC_API_KEY" in result.output
        assert "runs read ANTHROPIC_API_KEY" in result.output


class TestPrune:
    def test_prune_takes_either_backends_registration(
        self, workdir: Path, fake_sbx: FakeSbx
    ) -> None:
        """A sandbox provisioned under the claude backend carries the
        Anthropic registration; prune has no way to know which backend was
        current then, so it removes every backend's."""
        from sbxloop.engine.store import StateStore
        from sbxloop.sbx.models import SandboxSpec

        store = StateStore(workdir / ".sbxloop" / "state.db")
        store.create_run("rabc12345", "an outcome")
        store.set_run_state("rabc12345", "failed")
        cli = SbxCLI(binary=str(fake_sbx.binary))
        cli.create(SandboxSpec(name="sbxloop-rabc12345-agent", role="agent", workspace=workdir))
        cli.secret_set_custom(
            host="api.anthropic.com",
            env="ANTHROPIC_API_KEY",
            value="sk",
            sandbox="sbxloop-rabc12345-agent",
        )
        result = runner.invoke(app, ["sandbox", "prune", "--min-age", "0", "--force"])
        assert result.exit_code == 0, result.output
        assert cli.ls() == []
        cli.secret_set_custom(
            host="api.anthropic.com",
            env="ANTHROPIC_API_KEY",
            value="sk",
            sandbox="sbxloop-rabc12345-agent",
        )


# --- list-models under the claude backend --------------------------------

PAGE_ONE = {
    "data": [
        {
            "id": "claude-fable-5",
            "display_name": "Claude Fable 5",
            "created_at": "2026-08-01T00:00:00Z",
            "type": "model",
        }
    ],
    "has_more": True,
    "first_id": "claude-fable-5",
    "last_id": "claude-fable-5",
}
PAGE_TWO = {
    "data": [
        {
            "id": "claude-haiku-4-5-20251001",
            "display_name": "Claude Haiku 4.5",
            "created_at": "2025-10-01T00:00:00Z",
            "type": "model",
        }
    ],
    "has_more": False,
    "first_id": "claude-haiku-4-5-20251001",
    "last_id": "claude-haiku-4-5-20251001",
}


def fake_opener(pages: list[dict[str, Any]], seen: list[Any]) -> models.OpenUrl:
    def open_url(request: Any, timeout_s: float) -> bytes:
        seen.append(request)
        return json.dumps(pages[len(seen) - 1]).encode()

    return open_url


class TestAnthropicModels:
    def test_pages_through_with_the_key_header(self) -> None:
        seen: list[Any] = []
        records = models.fetch_anthropic_models(
            env={"ANTHROPIC_API_KEY": "sk-ant"},
            open_url=fake_opener([PAGE_ONE, PAGE_TWO], seen),
        )
        assert [r["id"] for r in records] == ["claude-fable-5", "claude-haiku-4-5-20251001"]
        assert len(seen) == 2
        assert seen[0].get_header("X-api-key") == "sk-ant"
        assert seen[0].get_header("Anthropic-version") == models.ANTHROPIC_VERSION
        assert "after_id" not in seen[0].full_url
        assert "after_id=claude-fable-5" in seen[1].full_url

    def test_missing_key_is_actionable(self) -> None:
        with pytest.raises(SbxloopError, match="ANTHROPIC_API_KEY is not set"):
            models.fetch_anthropic_models(env={}, open_url=fake_opener([], []))

    def test_http_401_names_the_key(self) -> None:
        import urllib.error

        def denied(request: Any, timeout_s: float) -> bytes:
            raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)  # type: ignore[arg-type]

        with pytest.raises(SbxloopError, match="HTTP 401 — the key is invalid"):
            models.fetch_anthropic_models(env={"ANTHROPIC_API_KEY": "sk"}, open_url=denied)

    def test_non_json_is_an_error_not_a_traceback(self) -> None:
        with pytest.raises(SbxloopError, match="not JSON"):
            models.fetch_anthropic_models(
                env={"ANTHROPIC_API_KEY": "sk"}, open_url=lambda r, t: b"<html>"
            )

    def test_row_flattens_the_record(self) -> None:
        row = models.anthropic_model_row(PAGE_ONE["data"][0])
        assert (row.id, row.name, row.created) == ("claude-fable-5", "Claude Fable 5", "2026-08-01")
        assert row.multiplier is None and row.raw["type"] == "model"

    def test_command_lists_claude_models(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        claude_config(workdir, top='model = "claude-fable-5"\n')
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        monkeypatch.setattr(models, "_open_url", fake_opener([PAGE_ONE, PAGE_TWO], []))
        result = runner.invoke(app, ["list-models"])
        assert result.exit_code == 0, result.output
        assert "claude models" in result.output
        assert "claude-haiku-4-5-20251001" in result.output
        assert "2026-08-01" in result.output
        assert "◀ = configured model (claude-fable-5)" in result.output
        assert "reasoning" not in result.output  # copilot-only columns are not rendered

    def test_command_json_is_the_api_records(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        claude_config(workdir)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        monkeypatch.setattr(models, "_open_url", fake_opener([PAGE_TWO], []))
        result = runner.invoke(app, ["list-models", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == PAGE_TWO["data"]

    def test_command_without_the_key_exits_2(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        claude_config(workdir)
        monkeypatch.setattr(
            models, "_open_url", lambda r, t: pytest.fail("no request without a key")
        )
        result = runner.invoke(app, ["list-models"])
        assert result.exit_code == 2
        assert "ANTHROPIC_API_KEY is not set" in result.output
