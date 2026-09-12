"""What chat may never change (#970): the channel it reports on, its own
switch and gate, and the keys no file line moves."""

from __future__ import annotations

from sbxloop.daemon.configpolicy import NEVER_FROM_CHAT, never_from_chat


def test_the_chat_sections_and_the_concierges_own_switches_are_never_from_chat() -> None:
    assert never_from_chat("discord.channel_id") == NEVER_FROM_CHAT["discord"]
    assert never_from_chat("slack") == NEVER_FROM_CHAT["slack"]
    assert never_from_chat("mattermost.command_prefix") is not None
    assert never_from_chat("tui.chronology_level") is not None
    assert never_from_chat("chat.backend") == NEVER_FROM_CHAT["chat"]
    assert never_from_chat("concierge.enabled") == "it is the concierge's own switch"
    assert "no self-widening" in (never_from_chat("concierge.edit_config") or "")
    assert "no self-widening" in (never_from_chat("concierge.config_locked") or "")


def test_a_prefix_matches_whole_segments_only() -> None:
    assert never_from_chat("concierge.timeout_s") is None
    assert never_from_chat("concierge.enabled_extra") is None
    assert never_from_chat("chatter.x") is None
    assert never_from_chat("daemon.max_runs_per_day") is None


def test_env_only_keys_are_refused_with_the_loaders_reason() -> None:
    why = never_from_chat("home")
    assert why is not None and why.startswith("not a file setting:") and "SBXLOOP_HOME" in why
    assert never_from_chat("run_model_override") is not None


def test_indices_are_not_part_of_the_key() -> None:
    assert never_from_chat("github.repos[1].deliver_base") is None
