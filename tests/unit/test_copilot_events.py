"""Tests for the Copilot backend's SDK-event extraction helpers.

The backend module itself is exercised end-to-end against real sbx (and is
coverage-omitted), but the extraction helpers are pure functions over
SDK-shaped objects and unit-testable with stand-ins. Field names were
verified against github-copilot-sdk: ``ToolExecutionStartData.arguments``,
``ToolExecutionCompleteData.success/result``, ``result.content/contents``,
and ShellExit's ``exit_code``.
"""

from __future__ import annotations

from types import SimpleNamespace

from sbxloop_worker.backends.copilot import (
    TOOL_ARGS_CLIP,
    TOOL_OUTPUT_CLIP,
    _tool_args,
    _tool_exit_code,
    _tool_output,
)


class TestToolArgs:
    def test_none_stays_none(self) -> None:
        assert _tool_args(None) is None

    def test_shell_command_preferred(self) -> None:
        assert _tool_args({"command": "pip install -e .", "timeout": 30}) == "pip install -e ."

    def test_dict_without_known_keys_becomes_compact_json(self) -> None:
        assert _tool_args({"a": 1, "b": [2]}) == '{"a":1,"b":[2]}'

    def test_plain_string_passes_through(self) -> None:
        assert _tool_args("ls -la") == "ls -la"

    def test_blank_string_is_none(self) -> None:
        assert _tool_args("   ") is None

    def test_clipped_to_bound(self) -> None:
        result = _tool_args({"command": "x" * 5_000})
        assert result is not None
        assert len(result) == TOOL_ARGS_CLIP

    def test_unserializable_dict_falls_back_to_str(self) -> None:
        assert _tool_args({"obj": object()}) is not None


class TestToolExitCode:
    def test_shell_exit_found_in_contents(self) -> None:
        data = SimpleNamespace(
            result=SimpleNamespace(
                contents=[SimpleNamespace(), SimpleNamespace(exit_code=3, shell_id="s1")]
            )
        )
        assert _tool_exit_code(data) == 3

    def test_no_result_is_none(self) -> None:
        assert _tool_exit_code(SimpleNamespace(result=None)) is None

    def test_no_shell_entries_is_none(self) -> None:
        data = SimpleNamespace(result=SimpleNamespace(contents=[SimpleNamespace(text="hi")]))
        assert _tool_exit_code(data) is None


class TestToolOutput:
    def test_content_tail_returned(self) -> None:
        data = SimpleNamespace(result=SimpleNamespace(content="out", detailed_content=None))
        assert _tool_output(data) == "out"

    def test_detailed_content_fallback(self) -> None:
        data = SimpleNamespace(result=SimpleNamespace(content="", detailed_content="details"))
        assert _tool_output(data) == "details"

    def test_long_output_keeps_tail(self) -> None:
        data = SimpleNamespace(
            result=SimpleNamespace(content="a" * 5_000 + "END", detailed_content=None)
        )
        output = _tool_output(data)
        assert output is not None
        assert len(output) == TOOL_OUTPUT_CLIP
        assert output.endswith("END")

    def test_missing_result_is_none(self) -> None:
        assert _tool_output(SimpleNamespace(result=None)) is None
