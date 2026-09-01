"""Tests for mapping the Copilot SDK's per-turn usage sample onto ``Usage``.

The backend module is coverage-omitted (it needs the SDK runtime and a
Copilot subscription), so the mapping lives as a pure function over
sample-shaped objects and is tested here with stand-ins — the same pattern
``read_only_denial`` and ``ripgrep_page_size_plan`` follow.

What this guards: sbxloop reads the prompt-cache counters off
``AssistantUsageData`` because they are genuine per-turn deltas, but does
does not read the sample's per-turn spend attribute. That figure is a
constant (observed 15.0 on every turn of every session in run rrhb28j7n),
so summing it fabricated a total; ``Usage`` has no field for it at all
(#439) and it stays unread until its unit is established.
"""

from __future__ import annotations

from types import SimpleNamespace

from sbxloop_worker.backends.copilot import (
    BACKEND_NAME,
    available_tool_count,
    usage_from_sdk_sample,
)


class TestUsageFromSdkSample:
    def test_maps_token_counters_only(self) -> None:
        """Only the genuine per-turn deltas are mapped; anything else the
        sample carries has nowhere to land on ``Usage``."""
        usage = usage_from_sdk_sample(
            SimpleNamespace(
                model="claude-opus-5",
                input_tokens=22_000,
                output_tokens=310,
                cache_read_tokens=18_500,
                cache_write_tokens=1_200,
            )
        )
        assert usage.model == "claude-opus-5"
        assert usage.input_tokens == 22_000
        assert usage.output_tokens == 310
        assert usage.cache_read_tokens == 18_500
        assert usage.cache_write_tokens == 1_200

    def test_falls_back_to_camel_case_spellings(self) -> None:
        usage = usage_from_sdk_sample(
            SimpleNamespace(model="m", inputTokens=10, outputTokens=2, cacheReadTokens=7)
        )
        assert (usage.input_tokens, usage.output_tokens) == (10, 2)
        assert usage.cache_read_tokens == 7

    def test_zero_is_reported_not_swallowed(self) -> None:
        """A turn that genuinely read no cache must say 0, not "unreported" —
        the two answer the "is this cached?" question in opposite ways."""
        usage = usage_from_sdk_sample(
            SimpleNamespace(model="m", input_tokens=0, output_tokens=0, cache_read_tokens=0)
        )
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.cache_read_tokens == 0

    def test_absent_fields_stay_none(self) -> None:
        """None is "the backend never said", which Usage.merged preserves and
        the concierge renders as "not reported" rather than as zero spend."""
        usage = usage_from_sdk_sample(SimpleNamespace(model="m", input_tokens=5))
        assert usage.output_tokens is None
        assert usage.cache_read_tokens is None
        assert usage.cache_write_tokens is None

    def test_a_sample_carrying_nothing_is_not_an_error(self) -> None:
        """An SDK bump that renames or drops fields must degrade to an empty
        sample, never fail the session that produced it."""
        usage = usage_from_sdk_sample(SimpleNamespace())
        assert usage.model is None and usage.input_tokens is None


class TestAvailableToolCount:
    def test_reads_the_internal_field(self) -> None:
        assert available_tool_count(SimpleNamespace(_available_tool_count=17)) == 17

    def test_absent_or_non_integer_is_none(self) -> None:
        # The field is flagged internal by the SDK, so it may vanish or
        # change shape on a bump; the diagnostic degrades, nothing breaks.
        assert available_tool_count(SimpleNamespace()) is None
        assert available_tool_count(SimpleNamespace(_available_tool_count="17")) is None


class TestBackendStamp:
    def test_sample_names_the_serving_backend(self) -> None:
        """Chat renders backend+model, so the sample must say which backend
        served it instead of leaving the reader to guess from the slug."""
        usage = usage_from_sdk_sample(SimpleNamespace(model="gpt-5", input_tokens=1))
        assert usage.backend == BACKEND_NAME == "copilot"

    def test_backend_survives_merging(self) -> None:
        merged = usage_from_sdk_sample(SimpleNamespace(model="gpt-5")).merged(
            usage_from_sdk_sample(SimpleNamespace(model="gpt-5-mini"))
        )
        assert merged.backend == "copilot"
        assert merged.model == "gpt-5-mini"

    def test_empty_sample_still_names_the_backend(self) -> None:
        assert usage_from_sdk_sample(SimpleNamespace()).backend == "copilot"
