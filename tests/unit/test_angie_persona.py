"""The concierge role speaks as Angie, not as a separate agent."""

from __future__ import annotations

from sbxloop.api.agents import AGENTS_BY_SLUG, ANGIE_PERSONA


def test_a_mentioned_concierge_stays_angie() -> None:
    persona = AGENTS_BY_SLUG["concierge"].persona
    assert persona.startswith(ANGIE_PERSONA)
    assert "You are Angie" in persona
    assert "@concierge" in persona and "@angie" in persona
    assert "Concierge**" not in persona and "sbxloop's **" not in persona


def test_other_roles_keep_their_collaboration_persona() -> None:
    persona = AGENTS_BY_SLUG["builder"].persona
    assert "You are sbxloop's **Builder**" in persona
    assert "@builder" in persona
    assert "You are Angie" not in persona
