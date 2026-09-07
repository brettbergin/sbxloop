"""Campaign admission and delivery checkpoints survive independent daemon runs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from sbxloop.daemon.campaigns import (
    CampaignError,
    CampaignPlan,
    CampaignStepPlan,
    CampaignStore,
    DeliveryEvidence,
)
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.db import Base, open_engine
from sbxloop.db.schema import _config
from sbxloop.engine.model import Published


def item(number: int, *, repo: str = "o/r") -> WorkItem:
    return WorkItem(
        item_id=f"gh:{repo}:issue:{number}",
        source_key=str(number),
        title=f"Step {number}",
        repo=repo,
    )


def plan(campaign_id: str = "release", *numbers: int) -> CampaignPlan:
    return CampaignPlan(
        campaign_id=campaign_id,
        title="Ship the release",
        requested_by="operator",
        steps=tuple(
            CampaignStepPlan(item=item(number), expected_base="main")
            for number in numbers or (1, 2)
        ),
    )


def evidence(number: int = 1, **changes: object) -> DeliveryEvidence:
    fields: dict[str, object] = {
        "kind": "code",
        "run_id": f"run-{number}",
        "verified_at": 2.0,
        "repo": "o/r",
        "base": "main",
        "pr_number": number + 100,
        "commit_sha": str(number) * 40,
    }
    fields.update(changes)
    return DeliveryEvidence(**fields)  # type: ignore[arg-type]


def test_admission_persists_the_whole_plan_without_enqueuing_children(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = CampaignStore(path)
    plan = CampaignPlan(
        campaign_id="release",
        title="Ship the release",
        requested_by="operator",
        steps=(
            CampaignStepPlan(
                item=WorkItem(item_id="gh:o/r:issue:1", source_key="1", title="First", repo="o/r"),
                expected_base="main",
            ),
            CampaignStepPlan(
                item=WorkItem(item_id="gh:o/r:issue:2", source_key="2", title="Second", repo="o/r"),
                expected_base="main",
            ),
        ),
    )
    assert store.admit(plan, now=1.0)
    assert not store.admit(plan, now=2.0)
    store.close()
    reopened = CampaignStore(path)
    snapshot = reopened.get("release")
    assert snapshot is not None
    assert snapshot.plan == plan
    assert snapshot.created_at == 1.0
    assert snapshot.steps[1].prerequisites == ()
    assert not snapshot.prepared
    assert reopened.ready() == []
    assert not reopened.allows(item(1))
    assert reopened.mark_prepared("release", now=3.0)
    assert [step.item.item_id for step in reopened.ready()] == ["gh:o/r:issue:1"]
    assert DaemonStore(path).queued() == []


def test_empty_campaign_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        CampaignPlan(campaign_id="empty", title="Empty", requested_by="operator", steps=())


def test_replaying_admission_cannot_change_scope_or_provenance(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "state.db")
    original = plan()
    store.admit(original, now=1.0)
    for changed in (
        plan("release", 1, 3),
        original.model_copy(update={"requested_by": "someone-else"}),
    ):
        with pytest.raises(CampaignError, match="different plan"):
            store.admit(changed, now=3.0)
    assert store.get("release").plan == original


def test_collision_rolls_back_every_member_of_second_campaign(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "state.db")
    store.admit(plan("first", 2), now=1.0)
    with pytest.raises(CampaignError, match="already belongs"):
        store.admit(plan("second", 1, 2), now=2.0)
    assert store.get("second") is None
    assert store.member(item(1)) is None
    assert store.member(item(2)).campaign_id == "first"


def test_two_stores_cannot_admit_the_same_member_concurrently(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first, second = CampaignStore(path), CampaignStore(path)

    def admit(store: CampaignStore, campaign_id: str) -> bool:
        try:
            return store.admit(plan(campaign_id), now=1.0)
        except CampaignError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(lambda args: admit(*args), ((first, "first"), (second, "second")))
        )
    assert sorted(results) == [False, True]
    assert len(first.list()) == 1
    assert len(first.list()[0].steps) == 2


def test_repo_aliases_cannot_join_multiple_campaigns(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "state.db")
    store.admit(plan("first", 1), now=1.0)
    alias = item(1, repo="O/R").model_copy(update={"item_id": "gh:1"})
    duplicate = CampaignPlan(
        campaign_id="second",
        title="Other",
        requested_by="operator",
        steps=(CampaignStepPlan(item=alias, expected_base="main"),),
    )
    with pytest.raises(CampaignError, match="already belongs"):
        store.admit(duplicate, now=2.0)
    assert store.member(alias).campaign_id == "first"
    assert store.member(item(1, repo="other/repo")) is None


@pytest.mark.parametrize(
    "state",
    [
        "running",
        "done",
        "failed",
        "blocked",
        "cancelled",
        "gated",
        "awaiting_review",
        "paused_review",
    ],
)
def test_existing_progressed_member_refuses_entire_admission(tmp_path: Path, state: str) -> None:
    path = tmp_path / "state.db"
    daemon = DaemonStore(path)
    daemon.upsert_new(item(1), now=1.0)
    daemon.upsert_new(item(2), now=1.0)
    daemon.set_state(item(2).item_id, state, now=2.0)
    store = CampaignStore(path)
    with pytest.raises(CampaignError, match="execution history"):
        store.admit(plan(), now=3.0)
    assert store.list() == []
    assert daemon.get(item(1).item_id).state == "queued"
    assert daemon.get(item(2).item_id).state == state


def test_claimed_and_interrupted_queued_rows_need_reconciliation(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    daemon = DaemonStore(path)
    daemon.upsert_new(item(1), now=1.0)
    daemon.mark_claimed(item(1).item_id, 2.0)
    store = CampaignStore(path)
    with pytest.raises(CampaignError, match="execution history"):
        store.admit(plan(), now=3.0)
    assert store.list() == []


def test_preexisting_queued_children_obey_the_campaign_frontier(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    daemon = DaemonStore(path)
    daemon.upsert_new(item(2), now=1.0)
    daemon.upsert_new(item(1), now=2.0)
    store = CampaignStore(path)
    store.admit(plan(), now=3.0)
    assert daemon.queued_in_order() == []
    assert not store.allows(item(1))
    store.mark_prepared("release", now=4.0)
    assert store.allows(item(1))
    assert not store.allows(item(2))
    assert store.allows(item(99))
    daemon.upsert_new(item(2), now=5.0)
    assert not store.allows(daemon.get(item(2).item_id))


def test_only_verified_success_advances_and_evidence_is_immutable(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = CampaignStore(path)
    store.admit(plan(), now=1.0)
    store.mark_prepared("release", now=1.5)
    with pytest.raises(CampaignError, match="predecessor"):
        store.record_success("release", item(2).item_id, evidence(2), now=2.0)
    assert store.record_success("release", item(1).item_id, evidence(), now=3.0)
    assert not store.record_success("release", item(1).item_id, evidence(), now=4.0)
    with pytest.raises(CampaignError, match="different delivery evidence"):
        store.record_success("release", item(1).item_id, evidence(commit_sha="f" * 40), now=4.0)
    store.close()
    reopened = CampaignStore(path)
    assert reopened.get("release").steps[0].completed_at == 3.0
    assert reopened.get("release").steps[0].evidence == evidence()
    assert [step.item.item_id for step in reopened.ready()] == [item(2).item_id]
    assert not reopened.allows(item(1))
    assert reopened.allows(item(2))


@pytest.mark.parametrize("changes", [{"base": "release"}, {"repo": "other/repo"}])
def test_delivery_to_wrong_base_or_repo_holds_the_frontier(
    tmp_path: Path, changes: dict[str, str]
) -> None:
    store = CampaignStore(tmp_path / "state.db")
    store.admit(plan(), now=1.0)
    with pytest.raises(CampaignError, match="intended repository/base"):
        store.record_success("release", item(1).item_id, evidence(**changes), now=2.0)
    assert store.get("release").steps[0].evidence is None
    assert not store.allows(item(2))


def test_workload_publication_receipts_advance_a_mixed_campaign(tmp_path: Path) -> None:
    workload = WorkItem(
        item_id="chat:brief", source_key="brief", title="Write brief", kind="workload"
    )
    mixed = CampaignPlan(
        campaign_id="mixed",
        title="Ship and report",
        requested_by="operator",
        steps=(plan().steps[0], CampaignStepPlan(item=workload)),
    )
    store = CampaignStore(tmp_path / "state.db")
    store.admit(mixed, now=1.0)
    receipt = Published(sink="chat", location="thread:123", tasks=["brief"], files=1)
    published = DeliveryEvidence(
        kind="workload", run_id="brief-run", verified_at=3.0, published=(receipt,)
    )
    receipt.tasks.append("later-edit")
    assert published.published[0].tasks == ("brief",)
    with pytest.raises(CampaignError, match="wrong kind"):
        store.record_success("mixed", item(1).item_id, published, now=2.0)
    store.record_success("mixed", item(1).item_id, evidence(), now=2.0)
    store.record_success("mixed", workload.item_id, published, now=3.0)
    assert store.get("mixed").complete
    assert store.get("mixed").next_step is None
    assert store.ready() == []
    assert not store.allows(workload)
    with pytest.raises(ValueError, match="publication receipts"):
        DeliveryEvidence(kind="workload", run_id="incomplete", verified_at=4.0)


def test_manual_hold_and_automatic_blocker_survive_reopen_independently(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = CampaignStore(path)
    store.admit(plan(), now=1.0)
    store.mark_prepared("release", now=1.5)
    assert store.set_blocker("release", item(1).item_id, "Waiting for review", now=2.0)
    assert store.set_hold("release", True, reason="Wait for launch", actor="operator", now=3.0)
    store.close()
    reopened = CampaignStore(path)
    assert reopened.ready() == []
    snapshot = reopened.get("release")
    assert snapshot.held and snapshot.hold_reason == "Wait for launch"
    assert snapshot.hold_actor == "operator"
    assert snapshot.blocker_reason == "Waiting for review"
    reopened.set_blocker("release", None, None, now=4.0)
    assert reopened.ready() == []
    reopened.set_hold("release", False, reason="Launch is ready", actor="operator", now=5.0)
    assert [step.item.item_id for step in reopened.ready()] == [item(1).item_id]


def test_success_clears_automatic_blocker_but_preserves_manual_hold(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "state.db")
    store.admit(plan(), now=1.0)
    store.set_blocker("release", item(1).item_id, "Read pending", now=2.0)
    store.set_hold("release", True, reason="Stop here", actor="operator", now=2.0)
    store.record_success("release", item(1).item_id, evidence(), now=3.0)
    snapshot = store.get("release")
    assert snapshot.blocker_reason is None
    assert snapshot.held
    assert store.ready() == []


def test_completed_campaign_keeps_membership_after_work_item_rediscovery(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = CampaignStore(path)
    store.admit(plan("one", 1), now=1.0)
    store.record_success("one", item(1).item_id, evidence(), now=2.0)
    daemon = DaemonStore(path)
    daemon.upsert_new(item(1), now=3.0)
    assert not store.allows(daemon.get(item(1).item_id))
    assert store.get("one").complete
    with pytest.raises(CampaignError, match="already belongs"):
        store.admit(plan("two", 1), now=4.0)


def test_snapshots_are_deeply_immutable_and_detached_from_callers(tmp_path: Path) -> None:
    original = item(1)
    campaign = CampaignPlan(
        campaign_id="immutable",
        title="Pinned ask",
        requested_by="operator",
        steps=(CampaignStepPlan(item=original, expected_base="main"),),
    )
    original.title = "Changed after admission"
    assert campaign.steps[0].item.title == "Step 1"
    with pytest.raises(ValidationError):
        campaign.steps[0].item.title = "Mutation"
    store = CampaignStore(tmp_path / "state.db")
    store.admit(campaign, now=1.0)
    assert store.get("immutable").steps[0].item.title == "Step 1"


def test_prerequisites_must_be_earlier_explicit_members() -> None:
    for dependency in ("gh:o/r:issue:1", "gh:o/r:issue:2", "gh:o/r:issue:99"):
        with pytest.raises(ValueError, match="earlier member"):
            CampaignPlan(
                campaign_id="invalid",
                title="Invalid",
                requested_by="operator",
                steps=(
                    CampaignStepPlan(
                        item=item(1), expected_base="main", prerequisites=(dependency,)
                    ),
                    plan().steps[1],
                ),
            )
    with pytest.raises(ValueError, match="duplicate campaign member"):
        plan("duplicate", 1, 1)


def test_explicit_dependencies_do_not_encode_the_changeable_serial_order() -> None:
    first = plan("dependencies", 1, 2, 3)
    third = CampaignStepPlan(item=item(3), expected_base="main", prerequisites=("gh:issue:1",))
    campaign = CampaignPlan(
        campaign_id="dependencies",
        title="Dependencies",
        requested_by="operator",
        steps=(first.steps[0], first.steps[1], third),
    )
    assert campaign.steps[2].prerequisites == (item(1).item_id,)


def test_legacy_standalone_items_remain_eligible_without_campaign_membership(
    tmp_path: Path,
) -> None:
    store = CampaignStore(tmp_path / "state.db")
    assert store.allows(WorkItem(item_id="gh:7", source_key="7", title="Legacy"))


def test_repository_must_be_known_and_consistent() -> None:
    with pytest.raises(ValueError, match="explicit repository"):
        CampaignStepPlan(
            item=WorkItem(item_id="gh:1", source_key="1", title="Unknown"), expected_base="main"
        )
    with pytest.raises(ValueError, match="disagrees with repository"):
        CampaignStepPlan(
            item=item(1).model_copy(update={"repo": "other/repo"}), expected_base="main"
        )
    from_id = CampaignStepPlan(item=item(1).model_copy(update={"repo": None}), expected_base="main")
    assert from_id.item.repo == "o/r"


def test_additive_migration_preserves_an_existing_ordered_queue(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    engine = open_engine(path)
    with engine.connect() as connection:
        command.upgrade(_config(connection), "0005")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO daemon_work_items"
            " (item_id, source_key, title, repo, state, created_at, updated_at,"
            " enqueue_seq, queue_order)"
            " VALUES ('gh:o/r:issue:7', '7', 'Existing', 'o/r', 'queued', 1, 1, 4, 2)"
        )
    engine.dispose()
    campaign_store = CampaignStore(path)
    campaign_store.admit(plan(), now=2.0)
    queue_store = DaemonStore(path)
    existing = queue_store.get("gh:o/r:issue:7")
    assert (existing.enqueue_seq, existing.queue_order) == (4, 2)
    engine = open_engine(path)
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        assert (
            connection.exec_driver_sql("SELECT title FROM daemon_work_items").scalar_one()
            == "Existing"
        )
    engine.dispose()


def test_readonly_campaign_view_cannot_mutate_a_live_database(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    writer = CampaignStore(path)
    writer.admit(plan(), now=1.0)
    reader = CampaignStore(path, readonly=True)
    assert reader.get("release").plan == plan()
    with pytest.raises(OperationalError, match="readonly"):
        reader.set_hold("release", True, reason="Hold", actor="operator", now=2.0)
    assert not writer.get("release").held


def test_deleting_a_member_cannot_make_the_campaign_look_complete(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = CampaignStore(path)
    store.admit(plan(), now=1.0)
    engine = open_engine(path)
    with engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM daemon_campaign_steps WHERE position = 1")
    engine.dispose()
    with pytest.raises(CampaignError, match="incomplete membership"):
        store.ready()


def test_move_swaps_independent_steps_without_changing_admission_identity(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = CampaignStore(path)
    original = plan()
    store.admit(original, now=1.0)
    store.mark_prepared("release", now=2.0)
    assert store.move("release", item(2).item_id, before=item(1).item_id, actor="operator", now=3.0)
    store.close()
    reopened = CampaignStore(path)
    snapshot = reopened.get("release")
    assert snapshot.plan == original
    assert [step.item.item_id for step in snapshot.steps] == [item(2).item_id, item(1).item_id]
    assert not snapshot.prepared
    reopened.mark_prepared("release", now=3.5)
    assert [step.item.item_id for step in reopened.ready()] == [item(2).item_id]
    assert snapshot.order_actor == "operator" and snapshot.order_changed_at == 3.0
    assert not reopened.admit(original, now=4.0)
    assert [step.item.item_id for step in reopened.get("release").steps] == [
        item(2).item_id,
        item(1).item_id,
    ]
    assert not reopened.move(
        "release", item(2).item_id, before=item(1).item_id, actor="operator", now=5.0
    )


def test_move_refuses_to_reverse_an_explicit_prerequisite(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "state.db")
    independent = plan("dependencies", 1, 2)
    dependent = CampaignStepPlan(
        item=item(3), expected_base="main", prerequisites=(item(1).item_id,)
    )
    original = CampaignPlan(
        campaign_id="dependencies",
        title="Dependencies",
        requested_by="operator",
        steps=(*independent.steps, dependent),
    )
    store.admit(original, now=1.0)
    assert store.move(
        "dependencies", item(2).item_id, before=item(1).item_id, actor="operator", now=2.0
    )
    with pytest.raises(CampaignError, match="must follow prerequisite"):
        store.move(
            "dependencies", item(3).item_id, before=item(1).item_id, actor="operator", now=3.0
        )
    assert [step.item.item_id for step in store.get("dependencies").steps] == [
        item(2).item_id,
        item(1).item_id,
        item(3).item_id,
    ]


@pytest.mark.parametrize("finished", [False, True])
def test_move_protects_running_or_completed_prefix(tmp_path: Path, finished: bool) -> None:
    path = tmp_path / "state.db"
    store = CampaignStore(path)
    store.admit(plan("release", 1, 2, 3), now=1.0)
    store.mark_prepared("release", now=2.0)
    if finished:
        store.record_success("release", item(1).item_id, evidence(), now=3.0)
    else:
        daemon = DaemonStore(path)
        daemon.upsert_new(item(1), now=2.0)
        daemon.mark_running(item(1).item_id, "run-1", now=3.0)
    with pytest.raises(CampaignError, match="already started"):
        store.move("release", item(2).item_id, before=item(1).item_id, actor="operator", now=4.0)
    assert store.move("release", item(3).item_id, before=item(2).item_id, actor="operator", now=5.0)
    assert [step.item.item_id for step in store.get("release").steps] == [
        item(1).item_id,
        item(3).item_id,
        item(2).item_id,
    ]


def test_success_follows_current_order_after_a_move(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "state.db")
    store.admit(plan(), now=1.0)
    store.mark_prepared("release", now=2.0)
    store.move("release", item(2).item_id, before=item(1).item_id, actor="operator", now=3.0)
    store.mark_prepared("release", now=3.5)
    with pytest.raises(CampaignError, match="predecessor"):
        store.record_success("release", item(1).item_id, evidence(), now=4.0)
    store.record_success("release", item(2).item_id, evidence(2), now=4.0)
    assert [step.item.item_id for step in store.ready()] == [item(1).item_id]


def test_preparation_recovery_does_not_release_a_human_hold(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = CampaignStore(path)
    store.admit(plan(), now=1.0)
    store.set_hold("release", True, reason="Wait for launch", actor="operator", now=2.0)
    store.close()
    reopened = CampaignStore(path)
    assert not reopened.get("release").prepared
    assert reopened.mark_prepared("release", now=3.0)
    assert reopened.get("release").held
    assert reopened.ready() == []
    assert not reopened.mark_prepared("release", now=4.0)


def test_unqualified_cross_repo_asks_receive_distinct_queue_identities(tmp_path: Path) -> None:
    first = item(1).model_copy(update={"item_id": "gh:issue:1"})
    second = item(1, repo="other/repo").model_copy(update={"item_id": "gh:issue:1"})
    campaign = CampaignPlan(
        campaign_id="multi",
        title="Two repositories",
        requested_by="operator",
        steps=(
            CampaignStepPlan(item=first, expected_base="main"),
            CampaignStepPlan(item=second, expected_base="main"),
        ),
    )
    assert [step.item.item_id for step in campaign.steps] == [
        "gh:o/r:issue:1",
        "gh:other/repo:issue:1",
    ]
    store = CampaignStore(tmp_path / "state.db")
    store.admit(campaign, now=1.0)
    assert store.member(first).position == 1
    assert store.member(second).position == 2


@pytest.mark.parametrize("old_id", ["gh:issue:1", "gh:O/R:issue:1", "gh:o/r:issue:1"])
def test_discarded_work_item_cannot_erase_prior_execution_during_admission(
    tmp_path: Path, old_id: str
) -> None:
    path = tmp_path / "state.db"
    daemon = DaemonStore(path)
    old_item = item(1).model_copy(update={"item_id": old_id})
    daemon.upsert_new(old_item, now=1.0)
    daemon.mark_running(old_item.item_id, "old-run", now=2.0)
    daemon.mark_resume_pending(old_item.item_id, now=3.0)
    assert daemon.discard(old_item.item_id)
    store = CampaignStore(path)
    with pytest.raises(CampaignError, match="execution history"):
        store.admit(plan(), now=4.0)
    assert store.list() == []


def test_qualified_history_in_another_repository_does_not_block_admission(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    daemon = DaemonStore(path)
    unrelated = item(1, repo="other/repo")
    daemon.upsert_new(unrelated, now=1.0)
    daemon.mark_running(unrelated.item_id, "other-run", now=2.0)
    daemon.mark_resume_pending(unrelated.item_id, now=3.0)
    daemon.discard(unrelated.item_id)
    assert CampaignStore(path).admit(plan(), now=4.0)


def test_move_cannot_shift_a_started_step_after_its_mutable_row_disappears(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = CampaignStore(path)
    store.admit(plan(), now=1.0)
    daemon = DaemonStore(path)
    daemon.upsert_new(item(1), now=2.0)
    daemon.mark_running(item(1).item_id, "first-run", now=3.0)
    daemon.mark_resume_pending(item(1).item_id, now=4.0)
    daemon.discard(item(1).item_id)
    with pytest.raises(CampaignError, match="already started"):
        store.move("release", item(2).item_id, before=item(1).item_id, actor="operator", now=5.0)
    assert store.get("release").next_step.item.item_id == item(1).item_id


def test_reordering_requires_source_preparation_before_new_frontier_is_ready(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    store = CampaignStore(path)
    store.admit(plan(), now=1.0)
    store.mark_prepared("release", now=2.0)
    store.move("release", item(2).item_id, before=item(1).item_id, actor="operator", now=3.0)
    store.close()
    reopened = CampaignStore(path)
    assert not reopened.get("release").prepared
    assert reopened.ready() == []
    assert not reopened.allows(item(1)) and not reopened.allows(item(2))
    reopened.mark_prepared("release", now=4.0)
    assert [step.item.item_id for step in reopened.ready()] == [item(2).item_id]


def test_issue_context_is_pinned_with_the_accepted_ask(tmp_path: Path) -> None:
    original = CampaignPlan(
        campaign_id="context",
        title="Context",
        requested_by="operator",
        steps=(
            CampaignStepPlan(
                item=item(1),
                expected_base="main",
                context="Discussion: acceptance requires a visible receipt.",
            ),
        ),
    )
    path = tmp_path / "state.db"
    store = CampaignStore(path)
    store.admit(original, now=1.0)
    store.close()
    reopened = CampaignStore(path)
    assert reopened.get("context").steps[0].context == original.steps[0].context
    changed = original.model_copy(
        update={"steps": (original.steps[0].model_copy(update={"context": "Changed discussion"}),)}
    )
    with pytest.raises(CampaignError, match="different plan"):
        reopened.admit(changed, now=2.0)
