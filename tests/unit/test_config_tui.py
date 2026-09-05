"""[tui]: the operator console's section — always on, no channel, and
never a [chat] backend choice."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop.config import TUI_CONTROL_CHANNEL, Config, TuiConfig, load_config
from sbxloop.errors import ConfigError


def test_defaults_and_always_enabled(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env={})
    assert isinstance(config.tui, TuiConfig)
    assert config.tui.enabled is True
    assert config.tui.channel_ref == TUI_CONTROL_CHANNEL == "control"
    assert config.tui.operator_id == ""
    assert config.tui.emoji is True
    assert config.tui.daemon_unit == "sbxloop-daemon"
    assert config.tui.refresh_s == 0.5
    assert config.tui.retention_days == 14.0
    # The shared rendering knobs come along.
    assert config.tui.command_prefix == "!sbx" and config.tui.thread_per_run is True


def test_local_is_a_bridge_section_but_never_the_chat_backend(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env={})
    assert config.chat_section("local") is config.tui
    assert config.chat_backend is None and config.chat_settings is None
    (tmp_path / "sbxloop.toml").write_text('[chat]\nbackend = "local"\n')
    with pytest.raises(ConfigError):
        load_config(cwd=tmp_path, env={})


def test_knobs_load_and_are_bounded(tmp_path: Path) -> None:
    (tmp_path / "sbxloop.toml").write_text(
        '[tui]\noperator_id = "ops"\nemoji = false\ndaemon_unit = "sbx"\n'
        'refresh_s = 2\nretention_days = 0\nchronology_level = "verbose"\n'
    )
    config = load_config(cwd=tmp_path, env={})
    assert config.tui.operator_id == "ops" and config.tui.emoji is False
    assert config.tui.daemon_unit == "sbx" and config.tui.refresh_s == 2.0
    assert config.tui.retention_days == 0 and config.tui.chronology_level == "verbose"
    with pytest.raises(ValueError, match="refresh_s"):
        Config.model_validate({"tui": {"refresh_s": 0}})
    with pytest.raises(ValueError, match="retention_days"):
        Config.model_validate({"tui": {"retention_days": -1}})
    with pytest.raises(ValueError):
        Config.model_validate({"tui": {"channel_id": 4}})
