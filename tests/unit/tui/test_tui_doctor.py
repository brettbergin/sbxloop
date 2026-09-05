"""Doctor and Secrets: the report ``sbxloop doctor`` prints, in a worker
against the fake sbx, and the registrations ``sbxloop secrets`` judges."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input

from sbxloop.cli.doctor import doctor_report
from sbxloop.paths import SbxloopHome
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.secretstate import (
    COPILOT_TOKEN_ENV,
    COPILOT_TOKEN_HOST,
    clean_secrets,
    rotate_registrations,
    secret_rows,
    secrets_context,
)
from sbxloop.tui.screens.doctor import DoctorScreen
from sbxloop.tui.screens.modals import OutcomeScreen, TypedConfirmScreen
from sbxloop.tui.screens.secrets import SecretsScreen
from sbxloop.tui.widgets.panel import TextPanel
from sbxloop.tui.widgets.tables import ConsoleTable
from tests.conftest import FakeSbx
from tests.unit.tui.conftest import drive, make_app

REFRESH: dict[str, Any] = {"refresh_s": 3.0}


@pytest.fixture
def host(
    seeded: SbxloopHome, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A host whose config lives beside the seeded state dir, with tokens
    (set after the fake sbx fixture, which scrubs them)."""
    monkeypatch.chdir(seeded)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "tok")
    (seeded.root / "sbxloop.toml").write_text(f'state_dir = "{seeded}"\n')
    return seeded


async def _wait(check: Any, pilot: Any, *, timeout_s: float = 60.0) -> None:
    waited = 0.0
    while not check() and waited < timeout_s:
        await pilot.pause(0.5)
        waited += 0.5
    assert check(), "timed out"


def test_doctor_report_is_the_cli_data(host: Path, fake_sbx: FakeSbx) -> None:
    import os

    report = doctor_report(dict(os.environ))
    names = [c.name for c in report.checks]
    assert "sbx binary" in names and report.ready
    assert report.conformance is not None and report.conformance_note is None


def test_secret_helpers_judge_like_the_cli(host: Path, fake_sbx: FakeSbx) -> None:
    from sbxloop.config import load_config

    config = load_config()
    SbxCLI().secret_set_custom(
        host=COPILOT_TOKEN_HOST, env=COPILOT_TOKEN_ENV, value="old", sandbox="sbxloop-dead-agent"
    )
    cli, live = secrets_context(config)
    (row,) = secret_rows(config, cli, live)
    assert row.env == COPILOT_TOKEN_ENV and row.judgement.status == "warn"
    assert "stale" in row.judgement.note and "sbxloop-dead-agent" in row.actual
    dry = clean_secrets(config, cli, live, apply=False, all_=False)
    assert dry[0].removed and dry[0].message.startswith("would remove")
    done = clean_secrets(config, cli, live, apply=True, all_=False)
    assert done[0].removed and not done[0].failed
    (row,) = secret_rows(config, cli, live)
    assert row.actual == "not registered" and row.judgement.status == "ok"
    lines = rotate_registrations(config, cli, live, token="new-secret-value")
    kinds = [k for k, _ in lines]
    assert kinds[0] == "ok" and "rotated: COPILOT_GITHUB_TOKEN" in lines[0][1]
    assert any("update your export" in text for _k, text in lines)
    assert all("new-secret-value" not in text for _k, text in lines), "the token never appears"
    (row,) = secret_rows(config, cli, live)
    assert row.state.scope == "global" and row.judgement.status == "ok"


def test_doctor_screen_runs_the_report_and_secrets_clean_behind_a_typed_word(
    host: Path, fake_sbx: FakeSbx
) -> None:
    SbxCLI().secret_set_custom(
        host=COPILOT_TOKEN_HOST, env=COPILOT_TOKEN_ENV, value="old", sandbox="sbxloop-dead-agent"
    )

    async def scenario() -> None:
        app = make_app(host, sbx=SbxCLI(), **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("8")
            await pilot.pause(0.5)
            assert isinstance(app.screen, DoctorScreen)
            screen: Any = app.screen
            await _wait(lambda: screen.report is not None, pilot)
            summary = screen.query_one("#summary", TextPanel).content_text
            assert "ready" in summary and "checked" in summary
            checks = screen.query_one("#checks", ConsoleTable)
            row = checks.get_row_at(checks.get_row_index("sbx binary"))
            assert str(row[1]) == "ok"
            assert screen.query_one("#conformance", ConsoleTable).row_count > 0
            # S: the registrations, the stale one flagged; x cleans it
            # after a dry run and the typed word.
            await pilot.press("S")
            await pilot.pause(0.5)
            assert isinstance(app.screen, SecretsScreen)
            secrets: Any = app.screen
            await _wait(lambda: bool(secrets.rows), pilot)
            table = secrets.query_one("#secrets", ConsoleTable)
            row = table.get_row_at(table.get_row_index(COPILOT_TOKEN_ENV))
            assert str(row[3]) == "warn" and "stale" in str(row[4])
            await pilot.press("x")
            await _wait(lambda: isinstance(app.screen, OutcomeScreen), pilot)
            assert "would remove" in app.screen.text
            await pilot.press("escape")
            await pilot.pause(0.5)
            assert isinstance(app.screen, TypedConfirmScreen)
            app.screen.query_one("#typed", Input).value = "clean"
            await pilot.press("enter")
            await _wait(
                lambda: (
                    isinstance(app.screen, SecretsScreen)
                    and bool(secrets.rows)
                    and secrets.rows[0].actual == "not registered"
                ),
                pilot,
            )
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert isinstance(app.screen, DoctorScreen)

    drive(scenario)
