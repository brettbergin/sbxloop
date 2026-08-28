"""Unit tests for the shared bounded output-excerpt helper."""

from __future__ import annotations

from sbxloop.excerpt import (
    TOOL_EXCERPT_LINE_CLIP,
    TOOL_FAIL_OUTPUT_LINES_DEFAULT,
    TOOL_OUTPUT_LINES_DEFAULT,
    clip_line,
    excerpt_lines,
    excerpt_output_lines,
)


def _lines(n: int) -> list[str]:
    return [f"line{i}" for i in range(n)]


def test_caps_are_sane() -> None:
    assert TOOL_OUTPUT_LINES_DEFAULT == 0
    assert TOOL_FAIL_OUTPUT_LINES_DEFAULT > 0
    assert TOOL_EXCERPT_LINE_CLIP > 0


def test_no_elision_when_under_budget() -> None:
    out = excerpt_lines(_lines(3), 10, 3, 100)
    assert out == ["line0", "line1", "line2"]


def test_head_tail_split_with_marker_in_the_middle() -> None:
    out = excerpt_lines(_lines(10), 4, 10, 100)
    assert out == ["line0", "line1", "… 6 lines elided …", "line8", "line9"]


def test_tail_only_puts_marker_first() -> None:
    out = excerpt_lines(_lines(10), 4, 10, 100, tail_only=True)
    assert out == ["… 6 lines elided …", "line6", "line7", "line8", "line9"]


def test_max_lines_zero_or_negative_returns_nothing() -> None:
    assert excerpt_lines(_lines(10), 0, 10, 100) == []
    assert excerpt_lines(_lines(10), -5, 10, 100) == []


def test_max_lines_one_is_tail_only() -> None:
    out = excerpt_lines(_lines(10), 1, 10, 100)
    assert out == ["… 9 lines elided …", "line9"]


def test_per_line_clip_applies() -> None:
    out = excerpt_lines(["x" * 50], 5, 1, 10)
    assert out == ["x" * 9 + "…"]


def test_backticks_are_neutralised() -> None:
    assert excerpt_lines(["a ``` b"], 5, 1, 100) == ["a ''' b"]


def test_clip_line_edges() -> None:
    assert clip_line("abc", 10) == "abc"
    assert clip_line("abcdef", 3) == "ab…"
    assert clip_line("abcdef", 1) == "…"
    assert clip_line("abcdef", 0) == "…"


def test_elided_count_uses_total_larger_than_lines() -> None:
    # Upstream already truncated the stored text: only 4 lines survive but
    # the event knows the real output had 100.
    out = excerpt_lines(_lines(4), 2, 100, 100)
    assert out == ["line0", "… 98 lines elided …", "line3"]


def test_output_lines_drives_elision_even_when_nothing_is_dropped() -> None:
    out = excerpt_output_lines("a\nb", max_lines=10, output_lines=50)
    assert out == ["a", "b", "… 48 lines elided …"]


def test_excerpt_output_lines_skips_blank_lines_and_clips() -> None:
    out = excerpt_output_lines("a\n\n   \nb" + "\n" + "y" * 20, max_lines=10, line_clip=5)
    assert out == ["a", "b", "yyyy…"]


def test_excerpt_output_lines_empty_detail() -> None:
    assert excerpt_output_lines("", max_lines=10) == []
    assert excerpt_output_lines(None, max_lines=10) == []  # type: ignore[arg-type]


def test_excerpt_output_lines_redacts_secrets() -> None:
    out = excerpt_output_lines("token ghp_" + "a" * 36, max_lines=5)
    assert "a" * 36 not in out[0]


def test_excerpt_output_lines_tail_only_and_zero_budget() -> None:
    assert excerpt_output_lines("\n".join(_lines(10)), max_lines=0) == []
    out = excerpt_output_lines("\n".join(_lines(10)), max_lines=3, tail_only=True)
    assert out == ["… 7 lines elided …", "line7", "line8", "line9"]


def test_no_daemon_import() -> None:
    from pathlib import Path

    import sbxloop.excerpt as mod

    assert "sbxloop.daemon" not in Path(mod.__file__).read_text()
