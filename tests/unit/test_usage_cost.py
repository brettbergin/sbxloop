"""Regression tests for the fabricated-cost bug (#386).

``AssistantUsageData.cost`` is a per-turn *constant* (15.0 on every turn of
every session in run rrhb28j7n), not a delta. Summing it folded a 147-turn
run into ``cost: 2205.0000`` — a number that means nothing, repeated by the
concierge in Discord as fact. These tests pin the three places that must
never let that happen again: ``Usage.merged``, ``_usage_from_event`` and
``_cost_line``.
"""

from __future__ import annotations

from types import SimpleNamespace

from sbxloop.daemon.concierge import _cost_line, _usage_from_event
from sbxloop_worker.backends.copilot import usage_from_sdk_sample
from sbxloop_worker.protocol import Usage

CONSTANT_COST = 15.0
TURNS = 10


def _samples() -> list[dict[str, object]]:
    """Ten turn-shaped ``agent.usage`` payloads: distinct token counts, the
    same constant cost, plus the ``agent`` key the host adds in transit."""
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


class TestUsageMergedCostIsNotAdditive:
    def test_constant_cost_does_not_multiply(self) -> None:
        merged = Usage()
        for sample in _samples():
            merged = merged.merged(Usage(**{k: v for k, v in sample.items() if k != "agent"}))
        assert merged.cost != CONSTANT_COST * TURNS
        assert merged.cost == CONSTANT_COST  # last-wins, like `model`

    def test_token_counters_are_still_summed(self) -> None:
        merged = Usage()
        for sample in _samples():
            merged = merged.merged(Usage(**{k: v for k, v in sample.items() if k != "agent"}))
        assert merged.input_tokens == sum(20_000 + i * 1_000 for i in range(TURNS))
        assert merged.output_tokens == sum(100 + i for i in range(TURNS))
        assert merged.cache_read_tokens == sum(18_000 + i * 900 for i in range(TURNS))
        assert merged.cache_write_tokens == 500 * TURNS
        assert merged.model == "claude-opus-5"


class TestUsageFromEventDropsCost:
    def test_cost_is_never_lifted_out_of_an_event(self) -> None:
        merged = Usage()
        for sample in _samples():
            merged = merged.merged(_usage_from_event(sample))
        assert merged.cost is None
        assert merged.input_tokens == sum(20_000 + i * 1_000 for i in range(TURNS))

    def test_cost_line_for_a_merged_run_says_not_reported(self) -> None:
        merged = Usage()
        for sample in _samples():
            merged = merged.merged(_usage_from_event(sample))
        line = _cost_line(merged)
        assert line == "cost: not reported by the agent backend (tokens above are the whole record)"
        assert "2205" not in line
        assert "15.0" not in line


class TestCostLine:
    def test_absent_cost_is_not_reported(self) -> None:
        assert _cost_line(Usage()).startswith("cost: not reported by the agent backend")

    def test_zero_cost_is_not_reported(self) -> None:
        # A rendered 0.0000 would be read as "this run was free".
        assert _cost_line(Usage(cost=0.0)).startswith("cost: not reported by the agent backend")

    def test_negative_cost_is_not_reported(self) -> None:
        assert _cost_line(Usage(cost=-1.0)).startswith("cost: not reported by the agent backend")

    def test_a_positive_cost_is_rendered(self) -> None:
        assert _cost_line(Usage(cost=1.25)) == "cost: 1.2500"


class TestBackendStillIgnoresCost:
    def test_sdk_sample_cost_is_dropped(self) -> None:
        usage = usage_from_sdk_sample(
            SimpleNamespace(model="m", input_tokens=1, output_tokens=1, cost=CONSTANT_COST)
        )
        assert usage.cost is None
