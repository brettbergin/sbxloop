"""What chat may never change (#970): the channel it reports on, its own
switch and gate, and the keys no file line moves."""

from __future__ import annotations

from sbxloop.daemon.configpolicy import NEVER_FROM_CHAT, locked_by, never_from_chat, refusal


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


LOCKED = ["policy", "mcp", "github.repos.token_env", "*.dsn_env", "credentials"]


def test_a_lock_pattern_covers_the_prefix_and_everything_under_it() -> None:
    assert locked_by("policy.allow", LOCKED) == "policy"
    assert locked_by("policy", LOCKED) == "policy"
    assert locked_by("mcp[0].hosts", LOCKED) == "mcp"
    assert locked_by("credentials[2].env", LOCKED) == "credentials"
    assert locked_by("github.repos[1].token_env", LOCKED) == "github.repos.token_env"
    assert locked_by("telemetry.dsn_env", LOCKED) == "*.dsn_env"
    assert locked_by("github.repos[1].deliver_base", LOCKED) is None
    assert locked_by("policyx.allow", LOCKED) is None
    assert locked_by("daemon.max_runs_per_day", []) is None


def test_refusal_names_the_rule_never_from_chat_first() -> None:
    assert refusal("daemon.max_runs_per_day", LOCKED) is None
    assert refusal("policy.allow", LOCKED) == (
        "locked by `[concierge] config_locked` (`policy`) — an operator unlocks it in the "
        "config file on the host"
    )
    why = refusal("concierge.config_locked", ["concierge"])
    assert why is not None and why.startswith("never from chat:") and "no self-widening" in why
    assert refusal("home", []) is not None and "not a file setting" in refusal("home", [])  # type: ignore[operator]
