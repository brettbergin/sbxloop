"""Tests for the read-only critic barrier's default-deny permission logic.

The SCRUTINIZE/VALIDATE loop-integrity property rests on this barrier: the
critic session must not be able to modify the workspace it reviews (#62).
The decision is an allowlist with default-deny, so an unknown or novel SDK
permission kind fails closed. The ``kind`` vocabulary in
``SDK_PERMISSION_KINDS`` was field-verified against github-copilot-sdk 1.0.8
(each ``PermissionRequest*`` class carries its wire discriminator as a
``kind`` ClassVar); ``read_only_denial`` is pure over request-shaped objects
and unit-testable with stand-ins, and the handler wiring is exercised here
with a stubbed ``copilot.rpc`` module.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from sbxloop_worker.backends.copilot import (
    READ_ONLY_ALLOWED_KINDS,
    SDK_PERMISSION_KINDS,
    CopilotBackend,
    installed_sdk_permission_kinds,
    read_only_denial,
)
from sbxloop_worker.protocol import JobRequest


class TestReadOnlyDenial:
    def test_allowlist_is_a_subset_of_the_verified_vocabulary(self) -> None:
        assert READ_ONLY_ALLOWED_KINDS <= SDK_PERMISSION_KINDS

    def test_read_kinds_are_allowed(self) -> None:
        for kind in sorted(READ_ONLY_ALLOWED_KINDS):
            assert read_only_denial(SimpleNamespace(kind=kind)) is None

    def test_every_other_verified_kind_is_denied_by_name(self) -> None:
        for kind in sorted(SDK_PERMISSION_KINDS - READ_ONLY_ALLOWED_KINDS):
            feedback = read_only_denial(SimpleNamespace(kind=kind))
            assert feedback is not None
            assert repr(kind) in feedback
            assert "read-only" in feedback

    def test_novel_kind_fails_closed(self) -> None:
        feedback = read_only_denial(SimpleNamespace(kind="file_edit"))
        assert feedback is not None
        assert "'file_edit'" in feedback

    def test_missing_kind_attribute_fails_closed(self) -> None:
        assert read_only_denial(object()) is not None

    def test_non_string_kind_fails_closed(self) -> None:
        assert read_only_denial(SimpleNamespace(kind=7)) is not None


class _StubApproveOnce:
    pass


class _StubReject:
    def __init__(self, feedback: str | None = None) -> None:
        self.feedback = feedback


@pytest.fixture
def stub_copilot_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    rpc = types.ModuleType("copilot.rpc")
    rpc.PermissionDecisionApproveOnce = _StubApproveOnce  # type: ignore[attr-defined]
    rpc.PermissionDecisionReject = _StubReject  # type: ignore[attr-defined]
    package = types.ModuleType("copilot")
    package.rpc = rpc  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "copilot", package)
    monkeypatch.setitem(sys.modules, "copilot.rpc", rpc)


class TestReadOnlyHandler:
    """The handler the SDK actually calls, with copilot.rpc stubbed in."""

    def _handler(self):
        job = JobRequest(
            job_id="j1",
            run_id="r1",
            kind="agent.session",
            prompt="review",
            permission_mode="read_only",
        )
        return CopilotBackend()._permission_handler(job)

    def test_read_and_shell_are_approved_once(self, stub_copilot_rpc: None) -> None:
        for kind in ("read", "shell"):
            decision = self._handler()(SimpleNamespace(kind=kind))
            assert isinstance(decision, _StubApproveOnce)

    def test_write_and_mcp_are_rejected(self, stub_copilot_rpc: None) -> None:
        for kind in ("write", "mcp"):
            decision = self._handler()(SimpleNamespace(kind=kind))
            assert isinstance(decision, _StubReject)
            assert decision.feedback is not None and repr(kind) in decision.feedback

    def test_unknown_kind_is_rejected_not_approved(self, stub_copilot_rpc: None) -> None:
        decision = self._handler()(SimpleNamespace(kind="brand-new-sdk-kind"))
        assert isinstance(decision, _StubReject)

    def test_denials_are_tracked_and_emitted(self, stub_copilot_rpc: None) -> None:
        """A denied request must leave a trace (#123): the tracker tally
        rides back on the JobResult and the event stream records the kind,
        so a crippled critic is auditable instead of invisible."""
        from sbxloop_worker.backends.copilot import SessionHealthTracker
        from sbxloop_worker.protocol import Event, EventTypes

        job = JobRequest(
            job_id="j1",
            run_id="r1",
            kind="agent.session",
            prompt="review",
            permission_mode="read_only",
        )
        tracker = SessionHealthTracker()
        emitted: list[tuple[str, dict[str, object]]] = []

        def emit(type: str, **data: object) -> Event:
            emitted.append((type, data))
            return Event.now(type, "r1", job_id="j1", **data)

        handler = CopilotBackend()._permission_handler(job, emit=emit, tracker=tracker)
        handler(SimpleNamespace(kind="write"))
        handler(SimpleNamespace(kind="write"))
        handler(SimpleNamespace(kind="read"))  # allowed: no trace

        health = tracker.health()
        assert health is not None
        assert health.permission_denials == {"write": 2}
        assert [t for t, _ in emitted] == [EventTypes.AGENT_PERMISSION_DENIED] * 2
        assert emitted[0][1]["kind"] == "write"
        assert "read-only" in str(emitted[0][1]["feedback"])


class TestInstalledSdkPermissionKinds:
    def test_none_when_sdk_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A None entry in sys.modules makes the import raise ImportError.
        monkeypatch.setitem(sys.modules, "copilot", None)
        monkeypatch.setitem(sys.modules, "copilot.session", None)
        assert installed_sdk_permission_kinds() is None

    def test_extracts_kind_classvars_from_the_request_union(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Shell:
            kind = "shell"

        class _Read:
            kind = "read"

        class _Kindless:
            pass

        session = types.ModuleType("copilot.session")
        session.PermissionRequest = _Shell | _Read | _Kindless  # type: ignore[attr-defined]
        package = types.ModuleType("copilot")
        package.session = session  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "copilot", package)
        monkeypatch.setitem(sys.modules, "copilot.session", session)
        assert installed_sdk_permission_kinds() == frozenset({"shell", "read"})
