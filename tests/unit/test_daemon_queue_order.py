"""Durable admission identity and the order the daemon actually dispatches."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command

from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.db import open_engine
from sbxloop.db.schema import _config
from tests.fakes.legacy_db import insert_daemon_row


def item(number: int) -> WorkItem:
    return WorkItem(item_id=f"gh:issue:{number}", source_key=str(number), title=f"Item {number}")


def ids(store: DaemonStore) -> list[str]:
    return [entry.item_id for entry in store.queued()]


def test_sequence_is_monotonic_despite_equal_or_reversed_clocks(tmp_path: Path) -> None:
    store = DaemonStore(tmp_path / "state.db")
    for number, now in [(8, 10.0), (3, 10.0), (4, 1.0)]:
        assert store.upsert_new(item(number), now)
    assert ids(store) == ["gh:issue:8", "gh:issue:3", "gh:issue:4"]
    assert [entry.enqueue_seq for entry in store.queued()] == [1, 2, 3]
    assert not store.upsert_new(item(3), 20.0)
    assert store.discard("gh:issue:4")
    assert store.upsert_new(item(9), 30.0)
    assert store.get("gh:issue:9").enqueue_seq == 4  # type: ignore[union-attr]


def test_move_survives_reopen_and_keeps_identity_and_retry_clock(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = DaemonStore(path)
    for number in (1, 2, 3):
        store.upsert_new(item(number), 100.0)
    store.mark_running("gh:issue:3", "attempt", 110.0)
    store.mark_failed("gh:issue:3", "retry", 120.0, requeue=True)
    before = store.get("gh:issue:3")
    moved = store.move_queued("gh:3", before="gh:1")
    assert before is not None
    assert moved.enqueue_seq == before.enqueue_seq
    assert (moved.created_at, moved.updated_at, moved.attempts) == (100.0, 120.0, 1)
    assert store.next_queued(125.0, 10.0).item_id == "gh:issue:1"  # type: ignore[union-attr]
    assert store.next_queued(131.0, 10.0).item_id == "gh:issue:3"  # type: ignore[union-attr]
    store.close()
    reopened = DaemonStore(path)
    assert ids(reopened) == ["gh:issue:3", "gh:issue:1", "gh:issue:2"]
    assert reopened.queued() == reopened.queued_in_order()
    reopened.move_queued("gh:issue:3", after="gh:issue:2")
    assert ids(reopened) == ["gh:issue:1", "gh:issue:2", "gh:issue:3"]
    reopened.upsert_new(item(4), 90.0)
    assert ids(reopened)[-1] == "gh:issue:4"


@pytest.mark.parametrize("protected", ["running", "claimed", "claiming", "resume", "done"])
@pytest.mark.parametrize("as_anchor", [False, True])
def test_only_unclaimed_pending_work_can_move(
    tmp_path: Path, protected: str, as_anchor: bool
) -> None:
    store = DaemonStore(tmp_path / "state.db")
    for number in (1, 2, 3):
        store.upsert_new(item(number), 100.0)
    key = "gh:issue:2"
    if protected in ("running", "resume"):
        store.mark_running(key, "active", 110.0)
        if protected == "resume":
            store.mark_resume_pending(key, 120.0)
    elif protected == "claimed":
        store.mark_claimed(key, 110.0)
    elif protected == "claiming":
        store.mark_claiming(key, "claim-token", 110.0)
    else:
        store.mark_done(key, 110.0)
    before = store.items()
    with pytest.raises(ValueError, match=r"unclaimed.*queued|queued.*unclaimed"):
        store.move_queued(
            "gh:issue:1" if as_anchor else key, before=key if as_anchor else "gh:issue:1"
        )
    assert store.items() == before


def test_pinned_resumes_remain_first_in_display_and_dispatch(tmp_path: Path) -> None:
    store = DaemonStore(tmp_path / "state.db")
    for number in (1, 2, 3):
        store.upsert_new(item(number), 100.0)
    store.mark_running("gh:issue:3", "resume", 110.0)
    store.mark_resume_pending("gh:issue:3", 120.0)
    store.move_queued("gh:issue:2", before="gh:issue:1")
    assert ids(store) == ["gh:issue:3", "gh:issue:2", "gh:issue:1"]
    assert store.queued() == store.queued_in_order()
    assert store.next_queued(120.0, 100.0).item_id == "gh:issue:3"  # type: ignore[union-attr]


@pytest.mark.parametrize("anchors", [{}, {"before": "gh:2", "after": "gh:3"}, {"before": "gh:1"}])
def test_invalid_move_is_atomic(tmp_path: Path, anchors: dict[str, str]) -> None:
    store = DaemonStore(tmp_path / "state.db")
    for number in (1, 2, 3):
        store.upsert_new(item(number), 100.0)
    before = store.items()
    with pytest.raises(ValueError):
        store.move_queued("gh:1", **anchors)
    assert store.items() == before


def test_upgrade_assigns_existing_fifo_and_preserves_resume_precedence(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    engine = open_engine(path)
    with engine.connect() as connection:
        command.upgrade(_config(connection), "0004")
    engine.dispose()
    for number, created, run_id in [(9, 20.0, None), (7, 10.0, None), (4, 10.0, "resume")]:
        insert_daemon_row(
            path,
            item_id=f"gh:{number}",
            source_key=str(number),
            title="Legacy",
            state="queued",
            created_at=created,
            updated_at=30.0,
            run_id=run_id,
        )
    store = DaemonStore(path)
    assert ids(store) == ["gh:issue:4", "gh:issue:7", "gh:issue:9"]
    assert [store.get(f"gh:{number}").enqueue_seq for number in (7, 4, 9)] == [1, 2, 3]  # type: ignore[union-attr]
    store.upsert_new(item(2), 40.0)
    assert store.get("gh:2").enqueue_seq == 4  # type: ignore[union-attr]


def test_concurrent_stores_allocate_once_per_admission(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first, second = DaemonStore(path), DaemonStore(path)

    def admit(store: DaemonStore, offset: int) -> None:
        for number in range(offset, offset + 10):
            store.upsert_new(item(number), 100.0)

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(admit, first, 1), executor.submit(admit, second, 11)]
        for job in jobs:
            job.result()
    assert [entry.enqueue_seq for entry in first.queued()] == list(range(1, 21))


def test_concurrent_discovery_does_not_consume_another_sequence(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first, second = DaemonStore(path), DaemonStore(path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(store.upsert_new, item(1), 100.0) for store in (first, second)]
        assert sorted(job.result() for job in jobs) == [False, True]
    second.upsert_new(item(2), 100.0)
    assert [entry.enqueue_seq for entry in first.queued()] == [1, 2]


def test_rows_written_during_a_rollback_are_numbered_on_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = DaemonStore(path)
    store.upsert_new(item(1), 100.0)
    store.close()
    for number, created in [(3, 300.0), (2, 200.0)]:
        insert_daemon_row(
            path,
            item_id=f"gh:{number}",
            source_key=str(number),
            title="Rollback",
            state="queued",
            created_at=created,
        )
    reopened = DaemonStore(path)
    assert ids(reopened) == ["gh:issue:1", "gh:issue:2", "gh:issue:3"]
    assert [entry.enqueue_seq for entry in reopened.queued()] == [1, 2, 3]
    original = reopened.queued()
    reopened.close()
    assert DaemonStore(path).queued() == original


def test_reservation_rejects_a_move_that_arrives_after_selection(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    dispatcher, operator = DaemonStore(path), DaemonStore(path)
    for number in (1, 2):
        dispatcher.upsert_new(item(number), 100.0)
    selected = dispatcher.reserve_next_queued(100.0, 0.0)
    assert selected is not None and selected.item_id == "gh:issue:1"
    assert selected.claim_token
    persisted = operator.get(selected.item_id)
    assert persisted is not None and persisted.claim_token == selected.claim_token
    with pytest.raises(ValueError, match="unclaimed queued"):
        operator.move_queued("gh:1", after="gh:2")
    assert ids(operator) == ["gh:issue:1", "gh:issue:2"]


def test_reservation_observes_a_move_that_committed_first(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    dispatcher, operator = DaemonStore(path), DaemonStore(path)
    for number in (1, 2):
        dispatcher.upsert_new(item(number), 100.0)
    operator.move_queued("gh:2", before="gh:1")
    selected = dispatcher.reserve_next_queued(100.0, 0.0)
    assert selected is not None and selected.item_id == "gh:issue:2"
    assert dispatcher.half_claimed() == [selected]


def test_reservation_keeps_backoff_and_existing_recovery_token(tmp_path: Path) -> None:
    store = DaemonStore(tmp_path / "state.db")
    for number in (1, 2):
        store.upsert_new(item(number), 100.0)
    store.mark_running("gh:1", "failed", 110.0)
    store.mark_failed("gh:1", "retry", 120.0, requeue=True)
    store.mark_claiming("gh:2", "recover-this-token", 120.0)
    selected = store.reserve_next_queued(125.0, 10.0)
    assert selected is not None and selected.item_id == "gh:issue:2"
    assert selected.claim_token == "recover-this-token"
    assert selected.updated_at == 120.0
    assert store.get("gh:1").claim_token is None  # type: ignore[union-attr]
    eligible = store.reserve_next_queued(130.0, 10.0)
    assert eligible is not None and eligible.item_id == "gh:issue:1"
    assert eligible.updated_at == 120.0 and eligible.claim_token


def test_reservation_preserves_claimed_pinned_resume_precedence(tmp_path: Path) -> None:
    store = DaemonStore(tmp_path / "state.db")
    for number in (1, 2):
        store.upsert_new(item(number), 100.0)
    store.mark_claimed("gh:2", 100.0)
    store.mark_running("gh:2", "resume", 110.0)
    store.mark_resume_pending("gh:2", 120.0)
    selected = store.reserve_next_queued(120.0, 100.0)
    assert selected is not None and selected.item_id == "gh:issue:2"
    assert selected.run_id == "resume" and selected.claimed and selected.claim_token is None
    assert store.get("gh:1").claim_token is None  # type: ignore[union-attr]


def test_reservation_does_not_stamp_an_ineligible_item(tmp_path: Path) -> None:
    store = DaemonStore(tmp_path / "state.db")
    store.upsert_new(item(1), 100.0)
    store.mark_running("gh:1", "failed", 110.0)
    store.mark_failed("gh:1", "retry", 120.0, requeue=True)
    before = store.get("gh:1")
    assert store.reserve_next_queued(125.0, 10.0) is None
    assert store.get("gh:1") == before
