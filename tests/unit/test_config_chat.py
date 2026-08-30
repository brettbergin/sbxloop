"""[chat] backend selection: explicit, inferred, ambiguous, dangling — and
the Slack section's own validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop.config import Config, DiscordConfig, SlackConfig, load_config
from sbxloop.errors import ConfigError


class TestBackendSelection:
    def test_headless_by_default(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path, env={})
        assert config.chat.backend is None
        assert config.chat_backend is None
        assert config.chat_settings is None
        assert config.slack.enabled is False and config.discord.enabled is False

    def test_inferred_from_the_one_configured_section(self) -> None:
        discord = Config.model_validate({"discord": {"channel_id": 42}})
        assert discord.chat_backend == "discord"
        assert isinstance(discord.chat_settings, DiscordConfig)
        assert discord.chat_settings.channel_ref == "42"
        slack = Config.model_validate({"slack": {"channel_id": "C0123ABCDEF"}})
        assert slack.chat_backend == "slack"
        assert isinstance(slack.chat_settings, SlackConfig)
        assert slack.chat_settings.channel_ref == "C0123ABCDEF"

    def test_explicit_backend_wins_over_the_other_section(self) -> None:
        config = Config.model_validate(
            {
                "chat": {"backend": "slack"},
                "discord": {"channel_id": 42},
                "slack": {"channel_id": "C0123ABCDEF"},
            }
        )
        assert config.chat_backend == "slack"
        assert config.chat_settings is config.slack

    def test_both_sections_without_a_choice_is_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            '[discord]\nchannel_id = 42\n[slack]\nchannel_id = "C0123ABCDEF"\n'
        )
        with pytest.raises(ConfigError, match=r"both \[discord\] and \[slack\].*\[chat\] backend"):
            load_config(cwd=tmp_path, env={})

    def test_named_backend_without_its_section_is_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text('[chat]\nbackend = "slack"\n')
        with pytest.raises(ConfigError, match=r"backend = \"slack\" but \[slack\] channel_id"):
            load_config(cwd=tmp_path, env={})
        (tmp_path / "sbxloop.toml").write_text(
            '[chat]\nbackend = "discord"\n[slack]\nchannel_id = "C0123ABCDEF"\n'
        )
        with pytest.raises(ConfigError, match=r"\[discord\] channel_id is not set"):
            load_config(cwd=tmp_path, env={})

    def test_unknown_backend_is_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text('[chat]\nbackend = "irc"\n')
        with pytest.raises(ConfigError, match="backend"):
            load_config(cwd=tmp_path, env={})

    def test_env_layer_reaches_both_sections(self, tmp_path: Path) -> None:
        config = load_config(
            cwd=tmp_path,
            env={
                "SBXLOOP_CHAT__BACKEND": "slack",
                "SBXLOOP_SLACK__CHANNEL_ID": "C0123ABCDEF",
                "SBXLOOP_SLACK__CHRONOLOGY_LEVEL": "quiet",
                "SBXLOOP_DISCORD__CHANNEL_ID": "42",
            },
        )
        assert config.chat_backend == "slack"
        assert config.slack.chronology_level == "quiet"
        assert config.discord.enabled  # still parsed, just not the active one

    def test_model_copy_with_a_channel_is_honoured_without_revalidation(self) -> None:
        """The CLI's --slack-channel/--discord-channel overrides copy the
        model; the backend is a property so the copy answers correctly."""
        config = Config()
        assert config.chat_backend is None
        slack = config.model_copy(update={"slack": SlackConfig(channel_id="C0123ABCDEF")})
        assert slack.chat_backend == "slack"


class TestSlackSection:
    def test_shared_knobs_have_the_discord_defaults(self) -> None:
        slack, discord = SlackConfig(), DiscordConfig()
        for name in (
            "command_prefix",
            "thread_per_run",
            "chronology_level",
            "max_message_chars",
            "embeds",
            "status_line",
            "tool_batch_lines",
            "tool_output_lines",
            "tool_fail_output_lines",
        ):
            assert getattr(slack, name) == getattr(discord, name), name

    def test_channel_id_must_be_an_id_not_a_name(self) -> None:
        with pytest.raises(ValueError, match="channel's id"):
            SlackConfig(channel_id="#sbxloop")
        with pytest.raises(ValueError, match="channel's id"):
            SlackConfig(channel_id="general")
        assert SlackConfig(channel_id="  C0123ABCDEF ").channel_id == "C0123ABCDEF"
        assert SlackConfig(channel_id="G0123ABCDEF").enabled
        assert SlackConfig(channel_id="").channel_id is None
