"""extract_json robustness tests.

Field failure (0.5.0): agents sometimes reply with bare JSON embedded in
prose — no fence — and the run died with ExpectedJsonMissing. The parser
must recover any JSON value it can find, preferring the last candidate
(narration first, answer last).
"""

from __future__ import annotations

from sbxloop_worker._json import extract_json


class TestFenced:
    def test_fenced_json_block(self) -> None:
        assert extract_json('narration\n```json\n{"a": 1}\n```\n') == {"a": 1}

    def test_unlabeled_fence(self) -> None:
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_last_fence_wins(self) -> None:
        text = '```json\n{"draft": true}\n```\nrevised:\n```json\n{"final": true}\n```'
        assert extract_json(text) == {"final": True}

    def test_whole_text_is_json(self) -> None:
        assert extract_json(' {"a": [1, 2]} ') == {"a": [1, 2]}


class TestEmbeddedInProse:
    """The 0.5.0 field regression class: valid JSON, no fence, prose around."""

    def test_object_after_prose(self) -> None:
        assert extract_json('Here is the plan: {"steps": ["do it"]}') == {"steps": ["do it"]}

    def test_object_with_trailing_prose(self) -> None:
        assert extract_json('{"steps": []} — let me know if this works!') == {"steps": []}

    def test_array_embedded(self) -> None:
        assert extract_json("The tasks are [1, 2, 3] as requested.") == [1, 2, 3]

    def test_last_embedded_value_wins(self) -> None:
        text = 'First I considered {"draft": 1}. Final answer: {"final": 2}.'
        assert extract_json(text) == {"final": 2}

    def test_nested_braces_inside_value(self) -> None:
        text = 'Plan: {"outer": {"inner": [1]}, "b": "x"} done.'
        assert extract_json(text) == {"outer": {"inner": [1]}, "b": "x"}

    def test_non_json_braces_skipped(self) -> None:
        text = 'in shell use ${VAR} syntax; result: {"ok": true}'
        assert extract_json(text) == {"ok": True}

    def test_fence_still_preferred_over_embedded(self) -> None:
        text = 'inline {"inline": 1} but the answer is\n```json\n{"fenced": 1}\n```'
        assert extract_json(text) == {"fenced": 1}


class TestNothingThere:
    def test_plain_prose(self) -> None:
        assert extract_json("no json here at all") is None

    def test_scalar_json_rejected(self) -> None:
        # bare numbers/strings are not acceptable phase outputs
        assert extract_json("42") is None

    def test_empty(self) -> None:
        assert extract_json("") is None

    def test_broken_braces(self) -> None:
        assert extract_json('{"unclosed": ') is None
