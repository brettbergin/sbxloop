"""Campaign admission and steering use the shared chat/CLI control path."""

from pathlib import Path

import pytest

from sbxloop.daemon.control import dispatch
from sbxloop.daemon.model import WorkItem
from tests.unit.test_daemon_loop import Harness


def _queued_workloads(h: Harness) -> None:
    for key in ("first", "second"):
        h.dstore.upsert_new(
            WorkItem(item_id=f"chat:{key}", source_key=key, title=key, kind="workload"),
            h.clock(),
        )


def test_start_hold_resume_and_status_from_shared_controls(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    _queued_workloads(h)
    reply = dispatch(h.loop, "campaign start report chat:first chat:second", by="Alex")
    assert reply.ok, reply.text
    assert "report" in reply.text
    status = dispatch(h.loop, "campaigns report")
    assert status.ok and "first" in status.text and "second" in status.text
    hold = dispatch(h.loop, "campaign hold report waiting for input", by="Alex")
    assert hold.ok, hold.text
    assert h.loop.tick().dispatched is None
    status = dispatch(h.loop, "campaigns report")
    assert "waiting for input" in status.text and "Alex" in status.text
    assert dispatch(h.loop, "campaign resume report", by="Alex").ok


def test_move_updates_campaign_order_without_changing_scope(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    _queued_workloads(h)
    assert dispatch(h.loop, "campaign start report chat:first chat:second", by="Alex").ok
    moved = dispatch(h.loop, "campaign move report chat:second before chat:first", by="Alex")
    assert moved.ok, moved.text
    status = dispatch(h.loop, "campaigns report")
    assert status.text.index("chat:second") < status.text.index("chat:first")


@pytest.mark.parametrize(
    "command",
    [
        "campaign",
        "campaign start",
        "campaign start report",
        "campaign hold",
        "campaign resume",
        "campaign resume report extra",
        "campaign move report",
        "campaign move report chat:first sideways chat:second",
        "campaign unknown",
        "campaigns one two",
    ],
)
def test_invalid_campaign_commands_fail_without_mutation(tmp_path: Path, command: str) -> None:
    reply = dispatch(Harness(tmp_path).loop, command)
    assert reply.known and not reply.ok and "usage:" in reply.text


def test_missing_member_does_not_admit_a_partial_campaign(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    _queued_workloads(h)
    reply = dispatch(h.loop, "campaign start report chat:first chat:missing", by="Alex")
    assert not reply.ok
    assert "missing" in reply.text
    assert [i.item_id for i in h.dstore.queued()] == ["chat:first", "chat:second"]
    assert "report" not in dispatch(h.loop, "campaigns").text
