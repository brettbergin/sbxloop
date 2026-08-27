"""Regression tests for the fabricated-spend bug (#386, #439).

The Copilot SDK's per-turn spend figure is a *constant* (15.0 on every turn
of every session in run rrhb28j7n), not a delta. Summing it folded a
147-turn run into a number that means nothing, repeated by the concierge in
Discord as fact. The field is now absent from ``Usage`` entirely, so these
tests pin that absence: nothing may reintroduce it through the wire model,
through ``_usage_from_event``, or through the SDK mapping.

They also pin the contract any *future* spend figure must obey (#431):
until the unit of ``AssistantUsageData.cost`` is established against SDK or
billing documentation, it stays unread; if it is ever surfaced it must merge
non-additively (last/max wins, as ``model`` already does) and must never be
rendered in a currency shape.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from sbxloop.daemon.concierge import _SPEND_NOT_REPORTED, Concierge, _usage_from_event
from sbxloop.engine.store import StateStore
from sbxloop_worker.backends.copilot import usage_from_sdk_sample
from sbxloop_worker.protocol import Event, Usage

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
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        }
        usage = usage_from_sdk_sample(SimpleNamespace(model="m", input_tokens=1, output_tokens=1))
        assert usage.input_tokens == 1


class TestMergeContract:
    """The shape any future spend field must follow (#431).

    ``AssistantUsageData.cost`` is still of unknown unit — a constant 15.0
    per turn, far more likely a premium-request multiplier or quota unit
    than currency. If it is ever surfaced it must merge the way ``model``
    does (non-additive, last-wins), never the way the token counters do
    (summed). These tests pin both halves of that contract so the
    distinction is enforced mechanically rather than only documented.
    """

    def test_model_is_carried_non_additively_while_tokens_sum(self) -> None:
        first = Usage(model="claude-opus-5", input_tokens=10, output_tokens=1)
        second = Usage(model="gpt-5.6-sol", input_tokens=5, output_tokens=2)
        merged = first.merged(second)
        assert merged.model == "gpt-5.6-sol"
        assert merged.input_tokens == 15
        assert merged.output_tokens == 3

    def test_a_none_model_does_not_erase_the_one_already_seen(self) -> None:
        """Last *non-None* wins: a turn that omits the model must not blank
        it, exactly as a non-additive spend figure would have to behave."""
        merged = Usage(model="claude-opus-5", input_tokens=10).merged(Usage(input_tokens=5))
        assert merged.model == "claude-opus-5"
        assert merged.input_tokens == 15

    def test_a_constant_field_summed_would_fabricate_a_total(self) -> None:
        """The bug in one line: additive merging of a per-turn constant
        yields TURNS x 15.0, which is why the token counters are the only
        fields allowed to accumulate."""
        merged = Usage()
        for sample in _samples():
            merged = merged.merged(_usage_from_event(sample))
        assert merged.model == "claude-opus-5"
        assert not hasattr(merged, "cost")
        assert CONSTANT_COST * TURNS == 150.0  # what summing would have invented


class TestSdkSampleIgnoresSpend:
    """``usage_from_sdk_sample`` must not read the SDK's spend attribute in
    either spelling until its unit is established (#431)."""

    def test_a_cost_attribute_on_the_sample_is_ignored(self) -> None:
        sample = SimpleNamespace(
            model="claude-opus-5",
            input_tokens=20_000,
            output_tokens=100,
            cache_read_tokens=18_000,
            cache_write_tokens=500,
            cost=CONSTANT_COST,
        )
        usage = usage_from_sdk_sample(sample)
        assert usage == Usage(
            model="claude-opus-5",
            input_tokens=20_000,
            output_tokens=100,
            cache_read_tokens=18_000,
            cache_write_tokens=500,
        )
        assert not hasattr(usage, "cost")
        assert "cost" not in usage.model_dump()

    def test_the_camelcase_spelling_is_ignored_too(self) -> None:
        """``_sdk_field`` falls back to camelCase for the fields it does
        read, so a wire-shaped ``totalCost``/``cost`` must be checked as
        well as the snake_case one."""
        usage = usage_from_sdk_sample(
            SimpleNamespace(input_tokens=1, totalCost=CONSTANT_COST, cost=CONSTANT_COST)
        )
        assert usage == Usage(input_tokens=1)
        assert not hasattr(usage, "totalCost")


class TestRenderedUsageBlock:
    """The rendered ``run_usage`` block — the real render path, not just the
    constant — must carry no currency symbol and no spend number (#431)."""

    @staticmethod
    def _rendered(tmp_path: Path) -> str:
        store = StateStore(tmp_path / "state.db")
        store.create_run("r431abcde", "Ship it")
        for i, sample in enumerate(_samples()):
            store.append_event(Event.now("agent.usage", "r431abcde", job_id=f"j{i}", **sample))
        # The real render path, with only the state store wired: building a
        # whole Concierge would drag in a sandbox host and an LLM client
        # without changing a line of the block under test.
        concierge = Concierge.__new__(Concierge)
        concierge._store = store
        return concierge._tool_run_usage({"run_id": "r431abcde"}, "tester")

    def test_the_block_reports_tokens_and_the_no_spend_line(self, tmp_path: Path) -> None:
        text = self._rendered(tmp_path)
        assert "r431abcde" in text
        assert "claude-opus-5" in text
        assert f"{TURNS} sample(s)" in text
        assert _SPEND_NOT_REPORTED in text

    def test_the_block_has_no_currency_symbol(self, tmp_path: Path) -> None:
        text = self._rendered(tmp_path)
        for symbol in ("$", "€", "£", "¥", "USD", "usd"):
            assert symbol not in text

    def test_the_block_has_no_spend_figure(self, tmp_path: Path) -> None:
        """Neither the per-turn constant nor any fabricated total of it may
        appear anywhere in the rendered block."""
        text = self._rendered(tmp_path)
        for forbidden in ("15.0", "150.0", "2205", "cost"):
            assert forbidden not in text
        # Nothing decimal-shaped at all: every number in the block is a
        # token count or a turn/job count.
        assert re.search(r"\d+\.\d+", text) is None
