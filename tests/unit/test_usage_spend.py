"""Regression tests for the fabricated-spend bug (#386, #439).

The Copilot SDK's per-turn spend figure is a *constant* (15.0 on every turn
of every session in run rrhb28j7n), not a delta. Summing it folded a
147-turn run into a number that means nothing, repeated by the concierge in
Discord as fact. The field is now absent from ``Usage`` entirely, so these
tests pin that absence: nothing may reintroduce it through the wire model,
through ``_usage_from_event``, or through the SDK mapping.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sbxloop.daemon.concierge import _SPEND_NOT_REPORTED
from sbxloop.daemon.usage import usage_from_event as _usage_from_event
from sbxloop_worker.backends.copilot import usage_from_sdk_sample
from sbxloop_worker.protocol import Usage

CONSTANT_COST = 15.0
TURNS = 10


def _samples() -> list[dict[str, object]]:
    """Ten turn-shaped ``agent.usage`` payloads: distinct token counts, the
    same constant ``cost`` figure a pre-#439 event still carries, plus the
    ``agent`` key the host adds."""
    return [
        {
            "agent": "executor",
            "model": "claude-opus-5",
            "input_tokens": 20_000 + i * 1_000,
            "output_tokens": 100 + i,
            "cache_read_tokens": 18_000 + i * 900,
            "cache_write_tokens": 500,
            "cost": CONSTANT_COST,
        }
        for i in range(TURNS)
    ]


class TestUsageRejectsTheRemovedField:
    def test_the_wire_model_rejects_a_cost_figure(self) -> None:
        """``cost`` was the field's real name; asserting on ``spend`` here
        would only prove pydantic's ``extra="forbid"`` works, since ``spend``
        was never a field either."""
        with pytest.raises(ValueError):
            Usage(model="m", cost=CONSTANT_COST)

    def test_token_counters_are_still_summed(self) -> None:
        merged = Usage()
        for sample in _samples():
            merged = merged.merged(_usage_from_event(sample))
        assert merged.input_tokens == sum(20_000 + i * 1_000 for i in range(TURNS))
        assert merged.output_tokens == sum(100 + i for i in range(TURNS))
        assert merged.cache_read_tokens == sum(18_000 + i * 900 for i in range(TURNS))
        assert merged.cache_write_tokens == 500 * TURNS
        assert merged.model == "claude-opus-5"


class TestUsageFromEventDropsUnknownFields:
    def test_a_cost_key_is_never_lifted_out_of_an_event(self) -> None:
        """A worker or event predating #439 may still emit ``cost``;
        ``_usage_from_event`` must silently drop it rather than error, since
        ``Usage`` no longer has anywhere for it to land."""
        usage = _usage_from_event(_samples()[0])
        assert not hasattr(usage, "cost")
        assert usage.input_tokens == 20_000


class TestSpendLine:
    def test_the_usage_block_never_renders_a_number(self) -> None:
        assert _SPEND_NOT_REPORTED.startswith("spend: not reported by the agent backend")
        assert "$" not in _SPEND_NOT_REPORTED
        assert "2205" not in _SPEND_NOT_REPORTED


class TestUsageCarriesOnlyTokenFields:
    def test_the_wire_model_has_no_money_shaped_field(self) -> None:
        """Whatever the SDK sample carries, the mapping can only produce the
        token/model fields — there is nowhere for a spend figure to land."""
        assert set(Usage.model_fields) == {
            "model",
            # Identity of the serving backend, so chat can say
            # provider+model; not a counter and never money-shaped.
            "backend",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        }
        usage = usage_from_sdk_sample(SimpleNamespace(model="m", input_tokens=1, output_tokens=1))
        assert usage.input_tokens == 1
