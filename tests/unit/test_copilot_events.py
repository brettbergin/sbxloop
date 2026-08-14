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
    SessionHealthTracker,
    _tool_args,
    _tool_exit_code,
    _tool_output,
    tool_refusal,
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


class TestSessionHealthTracker:
    def test_clean_session_has_no_health(self) -> None:
        assert SessionHealthTracker().health() is None

    def test_failed_tool_calls_are_tallied_by_tool(self) -> None:
        tracker = SessionHealthTracker()
        tracker.record_tool_end("grep", False)
        tracker.record_tool_end("grep", False)
        tracker.record_tool_end("glob", False)
        health = tracker.health()
        assert health is not None
        assert health.tool_failures == {"grep": 2, "glob": 1}
        assert health.degraded

    def test_successful_and_unreported_calls_do_not_count(self) -> None:
        tracker = SessionHealthTracker()
        tracker.record_tool_end("view", True)
        tracker.record_tool_end("view", None)  # event carried no signal
        assert tracker.health() is None

    def test_unnamed_tool_failure_still_counts(self) -> None:
        tracker = SessionHealthTracker()
        tracker.record_tool_end(None, False)
        health = tracker.health()
        assert health is not None
        assert health.tool_failures == {"(unknown)": 1}

    def test_denials_are_tallied_but_not_degraded(self) -> None:
        tracker = SessionHealthTracker()
        tracker.record_denial("shell")
        tracker.record_denial("shell")
        tracker.record_denial("write")
        health = tracker.health()
        assert health is not None
        assert health.permission_denials == {"shell": 2, "write": 1}
        assert not health.degraded

    def test_denied_call_failure_echo_is_not_a_tool_failure(self) -> None:
        """A rejected permission's tool call also completes with
        success=False; counting that echo made every denial degrade the
        critic (field failure raa2g67kw)."""
        tracker = SessionHealthTracker()
        tracker.record_denial("write", tool_call_id="call-1")
        tracker.record_tool_end("write_file", False, tool_call_id="call-1")
        health = tracker.health()
        assert health is not None
        assert health.permission_denials == {"write": 1}
        assert health.tool_failures == {}
        assert not health.degraded

    def test_genuine_failure_alongside_a_denial_still_counts(self) -> None:
        tracker = SessionHealthTracker()
        tracker.record_denial("write", tool_call_id="call-1")
        tracker.record_tool_end("grep", False, tool_call_id="call-2")  # unrelated
        tracker.record_tool_end("write_file", False, tool_call_id="call-1")  # echo
        health = tracker.health()
        assert health is not None
        assert health.tool_failures == {"grep": 1}
        assert health.degraded

    def test_denied_call_echo_is_excluded_only_once(self) -> None:
        tracker = SessionHealthTracker()
        tracker.record_denial("write", tool_call_id="call-1")
        tracker.record_tool_end("write_file", False, tool_call_id="call-1")
        tracker.record_tool_end("write_file", False, tool_call_id="call-1")
        health = tracker.health()
        assert health is not None
        assert health.tool_failures == {"write_file": 1}

    def test_denial_without_call_id_does_not_swallow_failures(self) -> None:
        tracker = SessionHealthTracker()
        tracker.record_denial("write")
        tracker.record_tool_end("write_file", False, tool_call_id="call-9")
        health = tracker.health()
        assert health is not None
        assert health.tool_failures == {"write_file": 1}

    def test_validator_refusals_are_tallied_but_not_degraded(self) -> None:
        """The CLI validator declining a command (e.g. `kill $(cat pid)`)
        is policy, not lost tooling — it must not distrust the verdict of a
        critic that rephrased and carried on (field failure retn41aa6)."""
        tracker = SessionHealthTracker()
        tracker.record_tool_end("bash", False, tool_call_id="c1", refused=True)
        tracker.record_tool_end("bash", False, tool_call_id="c2", refused=True)
        health = tracker.health()
        assert health is not None
        assert health.tool_refusals == {"bash": 2}
        assert health.tool_failures == {}
        assert not health.degraded

    def test_refused_flag_only_matters_on_failure(self) -> None:
        tracker = SessionHealthTracker()
        tracker.record_tool_end("bash", True, tool_call_id="c1", refused=True)
        tracker.record_tool_end("bash", None, tool_call_id="c2", refused=True)
        assert tracker.health() is None

    def test_denial_shield_takes_precedence_over_refusal(self) -> None:
        tracker = SessionHealthTracker()
        tracker.record_denial("shell", tool_call_id="c1")
        tracker.record_tool_end("bash", False, tool_call_id="c1", refused=True)
        health = tracker.health()
        assert health is not None
        assert health.permission_denials == {"shell": 1}
        assert health.tool_refusals == {}
        assert health.tool_failures == {}

    def test_summary_includes_refusals(self) -> None:
        tracker = SessionHealthTracker()
        tracker.record_tool_end("bash", False, refused=True)
        health = tracker.health()
        assert health is not None
        assert "tool refusals: bash x1" in health.summary()


class TestToolRefusal:
    def test_the_kill_validator_message_is_a_refusal(self) -> None:
        message = (
            "Command not executed. The 'kill' command must specify at least "
            "one numeric PID. Usage: kill <PID> or kill -9 <PID>"
        )
        assert tool_refusal(message, None)
        assert tool_refusal(None, message)

    def test_ordinary_failure_text_is_not_a_refusal(self) -> None:
        assert not tool_refusal("command exited with code 1", "grep: no matches")
        assert not tool_refusal(None, None)

    def test_refusal_prefix_mid_text_does_not_match(self) -> None:
        # Command output that merely *mentions* the phrase (e.g. a grep over
        # logs) must not be classified as a refusal.
        assert not tool_refusal(None, "found: Command not executed. in daemon.log")
