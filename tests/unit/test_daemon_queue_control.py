"""A person can reorder pending work from the same control path chat uses."""

from pathlib import Path

import pytest

from sbxloop.daemon.control import dispatch
from tests.unit.test_daemon_loop import Harness, RecordingFrontend, gh_item


def test_move_changes_dispatch_order_and_announces_requester(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    front = RecordingFrontend()
    h.loop.frontend = front  # type: ignore[assignment]
    for number in ("1", "2", "3"):
        h.dstore.upsert_new(gh_item(number), h.clock())

    reply = dispatch(h.loop, "move gh:3 before gh:1", by="operator", via="chat")

    assert reply.ok, reply.text
    assert "gh:issue:3" in reply.text
    assert [i.item_id for i in h.dstore.queued()] == ["gh:issue:3", "gh:issue:1", "gh:issue:2"]
    assert h.dstore.next_queued(h.clock(), 0).item_id == "gh:issue:3"  # type: ignore[union-attr]
    notice = next(n for n in front.notices if n.kind == "item.moved")
    assert "operator" in notice.text and "before" in notice.text
    assert h.source.calls == []

    reply = dispatch(h.loop, "move gh:3 after gh:2", by="operator")
    assert reply.ok
    assert [i.item_id for i in h.dstore.queued()] == ["gh:issue:1", "gh:issue:2", "gh:issue:3"]


@pytest.mark.parametrize(
    "command",
    [
        "move",
        "move gh:1",
        "move gh:1 first gh:2",
        "move gh:1 before",
        "move gh:1 before gh:2 extra",
    ],
)
def test_bad_move_syntax_is_refused(tmp_path: Path, command: str) -> None:
    reply = dispatch(Harness(tmp_path).loop, command)
    assert reply.known and not reply.ok and "usage: move" in reply.text


def test_move_refuses_an_item_already_claimed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.dstore.upsert_new(gh_item("1"), h.clock())
    h.dstore.upsert_new(gh_item("2"), h.clock())
    h.dstore.mark_claimed("gh:issue:1", h.clock())
    reply = dispatch(h.loop, "move gh:1 after gh:2")
    assert not reply.ok and "move failed" in reply.text
    assert [i.item_id for i in h.dstore.queued()] == ["gh:issue:1", "gh:issue:2"]


def test_queue_numbers_positions_and_shows_resumes_first(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.dstore.upsert_new(gh_item("1"), h.clock())
    h.dstore.upsert_new(gh_item("2"), h.clock())
    h.dstore.mark_running("gh:issue:2", "r2", h.clock())
    h.dstore.mark_resume_pending("gh:issue:2", h.clock())
    reply = dispatch(h.loop, "queue")
    lines = reply.text.splitlines()
    assert "gh:issue:2" in lines[0] and lines[0].startswith("1.")
    assert "gh:issue:1" in lines[1] and lines[1].startswith("2.")
