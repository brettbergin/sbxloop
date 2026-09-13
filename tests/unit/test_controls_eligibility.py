"""Which controls apply, over the three run kinds, the run and item states,
the gate and hold states, and the backend's answer about the forge."""

from __future__ import annotations

import pytest

from sbxloop.daemon.controls.eligibility import ACTIONS, Subject, available_actions, check
from sbxloop.daemon.controls.results import ControlError
from sbxloop.engine.model import RESUMABLE_RUN_STATES, TERMINAL_RUN_STATES, RunState
from sbxloop.vcs.protocol import Capability


def refused(action: str, subject: Subject) -> ControlError:
    with pytest.raises(ControlError) as excinfo:
        check(action, subject)  # type: ignore[arg-type]
    return excinfo.value


class TestToolRuns:
    @pytest.mark.parametrize("action", ["steer", "grant_rounds", "gate_approve"])
    def test_a_tool_run_never_has_an_agent_control(self, action: str) -> None:
        """A fixed recipe has no agent to steer, no fix rounds, no gate —
        whatever state it is in."""
        subject = Subject(run_kind="tool", run_state="executing", is_current=True)
        err = refused(action, subject)
        assert err.code == "unsupported_for_kind"

    def test_a_tool_run_can_still_be_cancelled_and_resumed(self) -> None:
        running = Subject(run_kind="tool", run_state="executing", is_current=True)
        assert "cancel" in available_actions(running)
        interrupted = Subject(run_kind="tool", run_state="executing", item_state="queued")
        assert "resume" in available_actions(interrupted)


class TestCancel:
    def test_the_run_in_flight(self) -> None:
        assert "cancel" in available_actions(Subject(run_state="building", is_current=True))

    @pytest.mark.parametrize("state", sorted(TERMINAL_RUN_STATES))
    def test_a_terminal_run_names_its_state(self, state: RunState) -> None:
        err = refused("cancel", Subject(run_state=state, item_state="failed"))
        assert err.code == "not_eligible" and err.message == f"run is {state}"

    def test_a_pinned_run_waiting_to_resume(self) -> None:
        """A terminal-looking run whose item is queued with it pinned is a
        pending resume: cancelling it is what stops the resume."""
        subject = Subject(run_state="failed", item_state="queued", pinned=True)
        assert "cancel" in available_actions(subject)

    def test_no_run(self) -> None:
        assert refused("cancel", Subject(run_state=None)).message == "no run to cancel"


class TestResume:
    @pytest.mark.parametrize("state", sorted(RESUMABLE_RUN_STATES))
    def test_resumable_states_with_a_settled_item(self, state: RunState) -> None:
        assert "resume" in available_actions(Subject(run_state=state, item_state="queued"))

    def test_the_run_in_flight_is_refused(self) -> None:
        err = refused("resume", Subject(run_state="building", is_current=True))
        assert err.message == "run is in flight"

    @pytest.mark.parametrize("state", ["merged", "completed"])
    def test_a_finished_run_is_refused(self, state: RunState) -> None:
        assert refused("resume", Subject(run_state=state, item_state="done")).message == (
            f"run is {state}"
        )

    def test_an_unpinned_run_is_refused(self) -> None:
        err = refused("resume", Subject(run_state="failed", item_state="failed", pinned=False))
        assert err.message == "work item is failed"


class TestSteerAndRounds:
    def test_steer_needs_the_run_in_flight(self) -> None:
        assert "steer" in available_actions(Subject(run_state="building", is_current=True))
        assert refused("steer", Subject(run_state="building")).message == "run is not in flight"

    def test_grant_rounds_needs_an_exhausted_failed_run(self) -> None:
        ok = Subject(run_state="failed", item_state="failed", exhausted=True)
        assert "grant_rounds" in available_actions(ok)
        not_exhausted = Subject(run_state="failed", item_state="failed")
        assert "fix rounds" in refused("grant_rounds", not_exhausted).message
        running_item = Subject(run_state="failed", item_state="running", exhausted=True)
        assert refused("grant_rounds", running_item).message == "work item is running"


class TestGatesAndForge:
    def test_only_an_open_gate_can_be_approved(self) -> None:
        assert "gate_approve" in available_actions(Subject(run_state="gated", gate_state="open"))
        assert refused("gate_approve", Subject(run_state="gated")).message == (
            "nothing is awaiting approval"
        )
        for state in ("approving", "merged", "released", "dismissed"):
            err = refused("gate_approve", Subject(run_state="gated", gate_state=state))
            assert err.message == f"gate is {state}"

    def test_a_backend_that_cannot_tell_fails_closed(self) -> None:
        """ "Could not tell" is a refusal with its own code, never a guess."""
        subject = Subject(run_state="gated", gate_state="open", forge=Capability.UNKNOWN)
        assert refused("gate_approve", subject).code == "capability_unknown"
        assert refused("review_wait_resume", subject).code == "capability_unknown"
        assert refused("retry", subject).code == "capability_unknown"

    def test_an_unsupported_backend_is_named(self) -> None:
        subject = Subject(run_state="gated", gate_state="open", forge=Capability.UNSUPPORTED)
        assert refused("gate_approve", subject).code == "capability_unsupported"

    def test_a_supported_backend_or_no_forge_involvement_passes(self) -> None:
        assert "gate_approve" in available_actions(
            Subject(run_state="gated", gate_state="open", forge=Capability.SUPPORTED)
        )
        # Cancel never touches the forge: an unknown backend does not stop it.
        assert "cancel" in available_actions(
            Subject(run_state="building", is_current=True, forge=Capability.UNKNOWN)
        )

    def test_review_wait_resume_needs_an_open_or_paused_wait(self) -> None:
        for state in ("open", "paused"):
            subject = Subject(run_state="awaiting_review", review_hold_state=state)
            assert "review_wait_resume" in available_actions(subject)
        for state in ("approving", "fixing", "merged", "dismissed"):
            subject = Subject(run_state="awaiting_review", review_hold_state=state)
            assert refused("review_wait_resume", subject).message == f"review wait is {state}"
        assert refused("review_wait_resume", Subject()).message == "not waiting for a review"


class TestItemVerbs:
    """The store's own transitions, mirrored so a surface can answer
    before asking."""

    @pytest.mark.parametrize(
        ("action", "allowed"),
        [
            (
                "abandon",
                {"queued", "running", "blocked", "gated", "awaiting_review", "paused_review"},
            ),
            ("retry", {"failed", "blocked", "cancelled", "queued"}),
            ("requeue", {"running", "queued"}),
        ],
    )
    def test_item_transitions(self, action: str, allowed: set[str]) -> None:
        every = {
            "queued",
            "running",
            "done",
            "failed",
            "blocked",
            "cancelled",
            "gated",
            "awaiting_review",
            "paused_review",
        }
        for state in every:
            subject = Subject(run_state="failed", item_state=state)
            if state in allowed:
                assert action in available_actions(subject), (action, state)
            else:
                assert refused(action, subject).message == f"work item is {state}"

    def test_no_item(self) -> None:
        assert refused("retry", Subject(item_state=None)).message == "no work item"


def test_available_actions_is_exactly_what_check_allows() -> None:
    subject = Subject(run_state="building", item_state="running", is_current=True)
    expected = set()
    for action in ACTIONS:
        try:
            check(action, subject)
        except ControlError:
            continue
        expected.add(action)
    assert available_actions(subject) == frozenset(expected)
    assert available_actions(subject) == {"cancel", "steer", "abandon", "requeue"}
