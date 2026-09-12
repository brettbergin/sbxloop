"""The gate itself: unsupported skips by name, unknown fails, and a need
nobody could report is the scenario's mistake."""

from __future__ import annotations

import pytest

from sbxloop.vcs.protocol import CAPABILITIES, Capability
from tests.conformance.gate import check_needs

ALL_SUPPORTED = dict.fromkeys(CAPABILITIES, Capability.SUPPORTED)


def test_a_supported_need_runs() -> None:
    check_needs("x", ALL_SUPPORTED, ["merge_queue", "remote_commit"])
    check_needs("x", ALL_SUPPORTED, [])


def test_an_unsupported_need_skips_naming_the_capability() -> None:
    report = {**ALL_SUPPORTED, "merge_queue": Capability.UNSUPPORTED}
    with pytest.raises(pytest.skip.Exception, match="x: merge_queue is unsupported"):
        check_needs("x", report, ["remote_commit", "merge_queue"])


def test_an_unknown_need_fails_rather_than_skips() -> None:
    report = {**ALL_SUPPORTED, "bot_identity": Capability.UNKNOWN}
    with pytest.raises(pytest.fail.Exception, match="x: bot_identity is unknown"):
        check_needs("x", report, ["bot_identity"])


def test_a_capability_the_backend_left_out_fails() -> None:
    report = {k: v for k, v in ALL_SUPPORTED.items() if k != "draft_changes"}
    with pytest.raises(pytest.fail.Exception, match="draft_changes is None"):
        check_needs("x", report, ["draft_changes"])


def test_a_need_no_backend_reports_is_the_scenarios_mistake() -> None:
    with pytest.raises(pytest.fail.Exception, match="no backend reports"):
        check_needs("x", ALL_SUPPORTED, ["time_travel"])
