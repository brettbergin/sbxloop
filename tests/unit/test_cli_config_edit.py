"""`sbxloop config set|unset|describe`: the host-side way to change one key
on a machine with no console, through the same editor the console uses."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sbxloop.cli.app import app
from sbxloop.paths import SbxloopHome

runner = CliRunner(env={"COLUMNS": "200"})


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SbxloopHome:
    """HOME is the test's tmp dir (autouse), so the home is `<tmp>/.sbxloop`;
    the developer's own layers must not leak in."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("SBXLOOP_DAEMON__POLL_INTERVAL_S", raising=False)
    return SbxloopHome(tmp_path / ".sbxloop")


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_set_writes_the_homes_config_and_keeps_a_backup(home: SbxloopHome) -> None:
    home.config_toml.write_text("# the cap\n[daemon]\nmax_runs_per_day = 3\n")
    result = runner.invoke(app, ["config", "set", "daemon.max_runs_per_day", "20"])
    assert result.exit_code == 0, result.output
    out = _plain(result.output)
    assert f"set daemon.max_runs_per_day in {home.config_toml}" in out
    assert "previous kept as sbxloop.toml.bak-" in out
    assert "restart to apply" in out
    assert home.config_toml.read_text() == "# the cap\n[daemon]\nmax_runs_per_day = 20\n"
    assert list(home.config.glob("sbxloop.toml.bak-*"))


def test_set_refused_by_the_loader_writes_nothing(home: SbxloopHome) -> None:
    home.config_toml.write_text("[concierge]\ntimeout_s = 60.0\n")
    result = runner.invoke(app, ["config", "set", "concierge.timeout_s", "5"])
    assert result.exit_code == 2, result.output
    out = _plain(result.output)
    assert "draft refused" in out and "nothing written" in out
    assert home.config_toml.read_text() == "[concierge]\ntimeout_s = 60.0\n"
    assert not list(home.config.glob("sbxloop.toml.bak-*"))


def test_set_names_a_bad_value_and_an_unchanged_one(home: SbxloopHome) -> None:
    home.config_toml.write_text("[daemon]\nmax_runs_per_day = 20\n")
    result = runner.invoke(app, ["config", "set", "daemon.max_runs_per_day", "soon"])
    assert result.exit_code == 2 and "whole number" in _plain(result.output)
    result = runner.invoke(app, ["config", "set", "daemon.max_runs_per_day", "20"])
    assert result.exit_code == 2 and "already says that" in _plain(result.output)
    result = runner.invoke(app, ["config", "set", "home", "/elsewhere"])
    assert result.exit_code == 2 and "not a file setting" in _plain(result.output)


def test_set_says_when_another_layer_still_wins(
    home: SbxloopHome, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBXLOOP_DAEMON__POLL_INTERVAL_S", "5.0")
    home.config_toml.write_text("[daemon]\npoll_interval_s = 7.0\n")
    result = runner.invoke(app, ["config", "set", "daemon.poll_interval_s", "9"])
    assert result.exit_code == 0, result.output
    assert "env sets it too and wins" in _plain(result.output)
    assert "poll_interval_s = 9" in home.config_toml.read_text()


def test_a_model_key_needs_no_restart(home: SbxloopHome) -> None:
    result = runner.invoke(app, ["config", "set", "agent.models.build", "gpt-5"])
    assert result.exit_code == 0, result.output
    out = _plain(result.output)
    assert "refresh before the next phase" in out and "restart" not in out


def test_unset_removes_the_line_and_names_the_fallback(home: SbxloopHome) -> None:
    home.config_toml.write_text('[landing]\nmerge_method = "squash"\n')
    result = runner.invoke(app, ["config", "unset", "landing.merge_method"])
    assert result.exit_code == 0, result.output
    out = _plain(result.output)
    assert "unset landing.merge_method" in out
    assert "now comes from its default: 'auto'" in out
    assert "merge_method" not in home.config_toml.read_text()


def test_describe_is_one_keys_card(home: SbxloopHome) -> None:
    home.config_toml.write_text("[daemon]\nmax_runs_per_day = 20\n")
    result = runner.invoke(app, ["config", "describe", "daemon.max_runs_per_day"])
    assert result.exit_code == 0, result.output
    out = _plain(result.output)
    assert "daemon.max_runs_per_day" in out
    assert re.search(r"value\s+20$", out, re.MULTILINE)
    assert re.search(r"set by\s+home config", out)
    assert re.search(r"accepts\s+int", out)
    assert re.search(r"applies\s+restart — the daemon reads it at its next start", out)
    assert re.search(r"about\s+calendar-day cap", out)
    result = runner.invoke(app, ["config", "describe", "home"])
    assert result.exit_code == 2 and "not a file setting" in _plain(result.output)


def test_show_says_when_each_key_applies(home: SbxloopHome) -> None:
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0, result.output
    out = _plain(result.output)
    assert "applies" in out
    (build,) = [line for line in out.splitlines() if "agent.models.build" in line]
    assert "live" in build
    (cap,) = [line for line in out.splitlines() if "daemon.max_runs_per_day" in line]
    assert "restart" in cap
