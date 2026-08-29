"""A verify command that keeps failing identically is the check's fault (#387).

These drive the engine/phase helpers directly: no model call is involved in
raising the signal, which is the whole point of it being mechanical.
"""

from __future__ import annotations

import pytest

from sbxloop.engine.engine import LoopEngine as Engine
from sbxloop.engine.model import TaskRecord, TaskSpec
from sbxloop.engine.phases import (
    VerifyFailure,
    normalise_verify_output,
    verify_fingerprint,
    verify_suspect_feedback,
)

MYPY = "uv run mypy packages"
MYPY_OUT = (
    "packages/sbxloop/hatch_build.py:20: error: Cannot find implementation or "
    'library stub for module named "hatchling.builders.hooks.plugin.interface"\n'
    "Found 12 errors in 1 file (checked 74 source files)"
)


def task() -> TaskRecord:
    return TaskRecord(spec=TaskSpec(id="t5", title="t5", verify_commands=[MYPY]))


# -- fingerprinting / normalisation ------------------------------------


def test_timing_differences_normalise_equal() -> None:
    a = f"{MYPY_OUT}\nfailed in 208.4s"
    b = f"{MYPY_OUT}\nfailed in 209.1s"
    assert a != b
    assert normalise_verify_output(a) == normalise_verify_output(b)
    assert verify_fingerprint(MYPY, a) == verify_fingerprint(MYPY, b)


def test_absolute_paths_and_timestamps_normalise_equal() -> None:
    a = "/home/agent/work/run-aaa/pkg/mod.py:20: error: boom at 2026-01-01 10:00:00"
    b = "/home/agent/work/run-bbb/pkg/mod.py:20: error: boom at 2026-02-02 11:22:33"
    assert verify_fingerprint(MYPY, a) == verify_fingerprint(MYPY, b)


def test_different_output_fingerprints_differ() -> None:
    assert verify_fingerprint(MYPY, MYPY_OUT) != verify_fingerprint(MYPY, "3 tests failed")


def test_line_column_coordinates_are_not_durations() -> None:
    """A bare `\\d+:\\d{2}` also matches every compiler's `line:column`, which
    collapsed two materially different failures into one fingerprint and made
    the engine call a working check suspect."""
    text = "src/mod.py:12:5: E501 line too long (95 > 88)"
    assert normalise_verify_output(text).endswith(":12:5: E501 line too long (95 > 88)")
    assert "<dur>" not in normalise_verify_output(text)


def test_same_error_at_different_coordinates_differs() -> None:
    a = "packages/x/mod.py:10:12: error: Incompatible return value type"
    b = "packages/x/mod.py:20:15: error: Incompatible return value type"
    assert verify_fingerprint("uv run mypy", a) != verify_fingerprint("uv run mypy", b)


def test_clock_durations_still_normalise_in_a_duration_context() -> None:
    a = "ran 10 tests in 0:00:12"
    b = "ran 10 tests in 0:00:19"
    assert normalise_verify_output(a) == normalise_verify_output(b)
    assert "<dur>" in normalise_verify_output(a)


def test_same_output_different_command_fingerprints_differ() -> None:
    assert verify_fingerprint(MYPY, MYPY_OUT) != verify_fingerprint("uv run mypy", MYPY_OUT)


# -- the repeat signal --------------------------------------------------


def test_second_identical_failure_is_a_repeat() -> None:
    record = task()
    first = VerifyFailure(MYPY, 1, f"{MYPY_OUT}\nin 208.4s")
    second = VerifyFailure(MYPY, 1, f"{MYPY_OUT}\nin 209.7s")

    assert Engine._record_verify_failures(record, [first]) == []
    assert record.verify_fingerprints == [first.fingerprint]

    repeated = Engine._record_verify_failures(record, [second])
    assert [f.command for f in repeated] == [MYPY]
    # No new fingerprint is recorded for a repeat.
    assert len(record.verify_fingerprints) == 1


def test_differing_failures_do_not_repeat() -> None:
    record = task()
    Engine._record_verify_failures(record, [VerifyFailure(MYPY, 1, MYPY_OUT)])
    repeated = Engine._record_verify_failures(
        record, [VerifyFailure(MYPY, 1, "different error: 3 tests failed")]
    )
    assert repeated == []
    assert len(record.verify_fingerprints) == 2


def test_repeat_is_detected_across_a_replan() -> None:
    """A replan clears the session and revision count but not the history."""
    record = task()
    Engine._record_verify_failures(record, [VerifyFailure(MYPY, 1, MYPY_OUT)])
    Engine._discard_session(record)
    record.replans += 1
    assert record.revisions == 0
    assert Engine._record_verify_failures(record, [VerifyFailure(MYPY, 1, MYPY_OUT)])


# -- the feedback the signal produces -----------------------------------


def test_suspect_feedback_quotes_the_command_and_blames_the_check() -> None:
    text = verify_suspect_feedback([VerifyFailure(MYPY, 1, MYPY_OUT)])
    assert f"`{MYPY}`" in text
    assert "suspect" in text.lower()
    assert "hatchling.builders.hooks.plugin.interface" in text


def test_suspect_feedback_does_not_order_an_impossible_re_author() -> None:
    """The builder is the only agent this feedback reaches and build.md tells
    it the verify commands cannot be edited, so the text must ask for what it
    can do: satisfy the command as written, or report it as unpassable."""
    text = verify_suspect_feedback([VerifyFailure(MYPY, 1, MYPY_OUT)])
    assert "You cannot edit the verify commands." in text
    assert "exactly as written" in text
    assert "state that plainly in your report" in text


# -- routing ------------------------------------------------------------


class _Bus:
    def emit(self, *args: object, **kwargs: object) -> None:
        return None


class _FakeEngine:
    """Just enough engine for the two routing helpers under test."""

    _record_verify_failures = staticmethod(Engine._record_verify_failures)
    _register_verify_suspect = Engine._register_verify_suspect
    _register_revision = Engine._register_revision
    _discard_session = staticmethod(Engine._discard_session)

    def __init__(self, max_replans: int = 1) -> None:
        self.bus = _Bus()
        self.states: list[str] = []
        self.max_replans = max_replans

        class _Budgets:
            max_replans_per_task = max_replans
            max_revisions_per_task = 2

        class _Config:
            budgets = _Budgets()

        self.config = _Config()

    def _set_task_state(self, run_id: str, task: TaskRecord, state: str) -> None:
        task.state = state  # type: ignore[assignment]
        self.states.append(state)


def test_suspect_routes_to_a_replan_without_spending_a_revision() -> None:
    engine = _FakeEngine()
    record = task()
    record.revisions = 1
    engine._register_verify_suspect("r", record, [VerifyFailure(MYPY, 1, MYPY_OUT)])
    assert record.replans == 1
    assert record.revisions == 0  # the replan resets; no extra revision spent
    assert record.session_id is None
    assert engine.states == ["executing"]
    assert f"`{MYPY}`" in record.last_feedback


def test_suspect_fails_the_task_when_replans_are_exhausted() -> None:
    engine = _FakeEngine(max_replans=0)
    record = task()
    engine._register_verify_suspect("r", record, [VerifyFailure(MYPY, 1, MYPY_OUT)])
    assert engine.states == ["failed"]


@pytest.mark.parametrize("field", ["verify_suspect", "verify_fingerprints"])
def test_task_record_persists_the_signal(field: str) -> None:
    record = task()
    record.verify_suspect = True
    record.verify_fingerprints.append("abc")
    assert getattr(TaskRecord.model_validate(record.model_dump()), field) == getattr(record, field)


def test_repeat_after_flagging_keeps_the_suspect_wording() -> None:
    """The follow-on branch must not reach _register_revision's exhaustion
    path, which spends a replan and overwrites last_feedback with the generic
    "start over with a fresh approach" text — contradicting that branch's own
    promise never to fall back to plain "revise the code" feedback."""
    engine = _FakeEngine()
    record = task()
    record.verify_suspect = True
    record.revisions = 99  # well past the revision budget
    feedback = verify_suspect_feedback([VerifyFailure(MYPY, 1, MYPY_OUT)])
    engine._register_revision("r", record, feedback)
    assert "VERIFY COMMAND SUSPECT" in record.last_feedback
    assert "start over with a fresh approach" not in record.last_feedback
    assert record.replans == 0
