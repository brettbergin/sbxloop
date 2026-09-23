"""Presentation bindings expose live controls without changing admission."""

from pathlib import Path
from typing import Any

import pytest

from sbxloop.daemon.controls.results import ControlError
from sbxloop.db.event_scope import admission_channel_for_item, channel_for_item, channel_for_run
from sbxloop.db.job_models import ExternalItemRow, ExternalRunRow
from tests.unit.test_daemon_loop import Harness, gh_item
from tests.unit.test_steering_by_mention import (
    MEMBER,
    FakeHandle,
    assignment_json,
)


def _bind(loop: Any, run_id: str, channel_id: str, item_id: str | None = None) -> None:
    with loop.dstore.transaction() as session:
        session.add(
            ExternalRunRow(
                run_id=run_id,
                work_id="job_test",
                channel_id=channel_id,
                item_id=item_id,
                created_at=1.0,
                state="running",
                title="External job",
                source_json='{"kind":"api"}',
                revision=0,
                historical=0,
            )
        )
        if item_id:
            session.add(ExternalItemRow(item_id=item_id, work_id="job_test", channel_id=channel_id))


def test_bound_external_run_is_a_live_target_without_changing_admission(tmp_path: Path) -> None:
    loop = Harness(tmp_path).loop
    handle = FakeHandle("external", None, assignment_json({}))
    loop._runs["external"] = handle
    _bind(loop, "external", "conversation", handle.item.item_id)

    assert loop.live_runs_in_channel("conversation") == ["external"]
    assert [target.run_id for target in loop.live_runs_for_agent("conversation", "scout")] == [
        "external"
    ]
    assert loop.live_runs_in_channel("unrelated") == []
    assert handle.item.channel_id is None


def test_presentation_does_not_grant_cancel_capability(tmp_path: Path) -> None:
    loop = Harness(tmp_path).loop
    loop._runs["external"] = FakeHandle("external", None, assignment_json({}))
    _bind(loop, "external", "conversation")

    with pytest.raises(ControlError) as refused:
        loop.stop_channel("conversation", MEMBER)
    assert refused.value.code == "forbidden"


def test_run_binding_stays_with_its_attempt_when_a_later_admission_changes(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    item = gh_item("1", channel_id="new-private-channel")
    harness.dstore.upsert_new(item, harness.clock())
    _bind(harness.loop, "old-attempt", "original-channel", item.item_id)

    with harness.dstore.read() as session:
        assert admission_channel_for_item(session, item.item_id) == "new-private-channel"
        assert channel_for_item(session, item.item_id) == "new-private-channel"
        assert channel_for_run(session, "old-attempt") == "original-channel"


def test_a_run_without_an_item_can_have_a_presentation_channel(tmp_path: Path) -> None:
    loop = Harness(tmp_path).loop
    _bind(loop, "standalone", "conversation")
    with loop.dstore.read() as session:
        assert channel_for_run(session, "standalone") == "conversation"
