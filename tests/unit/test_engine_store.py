"""StateStore tests."""

from pathlib import Path

import pytest

from sdxloop.engine.model import PlanModel, TaskRecord, TaskSpec
from sdxloop.engine.store import StateStore
from sdxloop.errors import StateError
from sdxloop_worker.protocol import Event


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def make_task(id: str = "t1") -> TaskRecord:
    return TaskRecord(spec=TaskSpec(id=id, title=id.upper()))


class TestRuns:
    def test_create_get_update(self, store: StateStore) -> None:
        record = store.create_run("r1", "do things", "{}")
        assert record.state == "created"
        store.set_run_state("r1", "running")
        assert store.get_run("r1").state == "running"

    def test_duplicate_run_rejected(self, store: StateStore) -> None:
        store.create_run("r1", "x")
        with pytest.raises(StateError, match="already exists"):
            store.create_run("r1", "y")

    def test_unknown_run(self, store: StateStore) -> None:
        with pytest.raises(StateError, match="unknown run"):
            store.get_run("ghost")
        with pytest.raises(StateError, match="unknown run"):
            store.set_run_state("ghost", "failed")

    def test_list_runs_newest_first(self, store: StateStore) -> None:
        store.create_run("r1", "a")
        store.create_run("r2", "b")
        assert [r.run_id for r in store.list_runs()] == ["r2", "r1"]


class TestTasks:
    def test_save_and_get_ordered(self, store: StateStore) -> None:
        store.create_run("r1", "x")
        store.save_tasks("r1", [TaskSpec(id="t1", title="A"), TaskSpec(id="t2", title="B")])
        tasks = store.get_tasks("r1")
        assert [t.spec.id for t in tasks] == ["t1", "t2"]
        assert all(t.state == "pending" for t in tasks)

    def test_update_task_roundtrip(self, store: StateStore) -> None:
        store.create_run("r1", "x")
        store.save_tasks("r1", [TaskSpec(id="t1", title="A")])
        task = store.get_tasks("r1")[0]
        task.state = "executing"
        task.revisions = 2
        task.last_feedback = "try harder"
        task.session_id = "s-1"
        task.plan = PlanModel(steps=["one"], verify_commands=["true"])
        store.update_task("r1", task)

        loaded = store.get_tasks("r1")[0]
        assert loaded.state == "executing"
        assert loaded.revisions == 2
        assert loaded.last_feedback == "try harder"
        assert loaded.session_id == "s-1"
        assert loaded.plan is not None
        assert loaded.plan.steps == ["one"]

    def test_update_unknown_task(self, store: StateStore) -> None:
        store.create_run("r1", "x")
        with pytest.raises(StateError, match="unknown task"):
            store.update_task("r1", make_task("ghost"))


class TestPhasesAndEvents:
    def test_phase_attempts_recorded(self, store: StateStore) -> None:
        store.create_run("r1", "x")
        store.record_phase(
            "r1",
            "decompose",
            task_id=None,
            attempt=1,
            status="ok",
            output_json="{}",
            started_at=1.0,
        )
        store.record_phase(
            "r1", "plan", task_id="t1", attempt=1, status="ok", output_json=None, started_at=2.0
        )
        all_attempts = store.phase_attempts("r1")
        assert [row["phase"] for row in all_attempts] == ["decompose", "plan"]
        t1_attempts = store.phase_attempts("r1", "t1")
        assert [row["phase"] for row in t1_attempts] == ["plan"]

    def test_events_append_and_filter(self, store: StateStore) -> None:
        for i, type_ in enumerate(["run.start", "task.state", "agent.message"]):
            store.append_event(Event(ts=float(i), run_id="r1", type=type_, data={"i": i}))
        store.append_event(Event(ts=9.0, run_id="other", type="run.start"))

        all_events = list(store.events("r1"))
        assert [e.type for _, e in all_events] == ["run.start", "task.state", "agent.message"]

        task_events = list(store.events("r1", type_prefix="task."))
        assert [e.type for _, e in task_events] == ["task.state"]

        last_seq = all_events[-1][0]
        assert list(store.events("r1", after_seq=last_seq)) == []
