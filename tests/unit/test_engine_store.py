"""StateStore tests."""

import sqlite3
from pathlib import Path

import pytest

from sbxloop.engine.model import PlanModel, TaskRecord, TaskSpec
from sbxloop.engine.store import StateStore
from sbxloop.errors import StateError
from sbxloop_worker.protocol import Event, Usage


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

    def test_phase_usage_roundtrips(self, store: StateStore) -> None:
        store.create_run("r1", "x")
        store.record_phase(
            "r1",
            "execute",
            task_id="t1",
            attempt=1,
            status="ok",
            output_json=None,
            started_at=1.0,
            usage=Usage(
                input_tokens=1200,
                output_tokens=34,
                cache_read_tokens=900,
                cache_write_tokens=10,
                cost=15.0,
            ),
            turns=3,
        )
        row = store.phase_attempts("r1")[0]
        assert row["input_tokens"] == 1200
        assert row["output_tokens"] == 34
        assert row["cache_read_tokens"] == 900
        assert row["cache_write_tokens"] == 10
        assert row["cost"] == 15.0
        assert row["turns"] == 3

    def test_phase_usage_defaults_to_null(self, store: StateStore) -> None:
        """A mechanical phase (verify) records no usage — columns stay NULL."""
        store.create_run("r1", "x")
        store.record_phase(
            "r1", "verify", task_id="t1", attempt=1, status="ok", output_json=None, started_at=1.0
        )
        row = store.phase_attempts("r1")[0]
        assert row["input_tokens"] is None
        assert row["output_tokens"] is None
        assert row["cost"] is None
        assert row["turns"] is None

    def test_pre_usage_database_migrates_in_place(self, tmp_path: Path) -> None:
        """A state.db whose phase_attempts predates the usage columns opens
        cleanly, gains them, and keeps its old rows readable."""
        db = tmp_path / "state.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, outcome TEXT NOT NULL,"
            " state TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}',"
            " created_at REAL NOT NULL, updated_at REAL NOT NULL);"
            "CREATE TABLE phase_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " run_id TEXT NOT NULL, task_id TEXT, phase TEXT NOT NULL,"
            " attempt INTEGER NOT NULL, status TEXT NOT NULL, output_json TEXT,"
            " started_at REAL NOT NULL, ended_at REAL NOT NULL);"
        )
        conn.execute("INSERT INTO runs VALUES ('r1', 'old', 'completed', '{}', 1.0, 1.0)")
        conn.execute(
            "INSERT INTO phase_attempts"
            " (run_id, task_id, phase, attempt, status, output_json, started_at, ended_at)"
            " VALUES ('r1', 't1', 'execute', 1, 'ok', NULL, 1.0, 2.0)"
        )
        conn.commit()
        conn.close()

        store = StateStore(db)
        old_row = store.phase_attempts("r1")[0]
        assert old_row["input_tokens"] is None
        store.record_phase(
            "r1",
            "execute",
            task_id="t1",
            attempt=2,
            status="ok",
            output_json=None,
            started_at=3.0,
            usage=Usage(input_tokens=5),
            turns=1,
        )
        assert store.phase_attempts("r1")[1]["input_tokens"] == 5
        # reopening does not re-apply the ALTERs
        StateStore(db).phase_attempts("r1")

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

    def test_mixed_shape_tool_events_round_trip_verbatim(self, tmp_path: Path) -> None:
        """The chronology stores tool events verbatim, whether or not they
        carry the additive `tool_call_id`/`output_lines`/`duration_ms` fields,
        so a run checkpointed by one worker version replays under another
        without migration (#403 t7)."""
        long_cmd = (
            "cd /home/x/.local/state/sbxloop/sbxloop-work/runs/r1/workspace"
            " && uv run pytest -q " + "x" * 500
        )
        old = {"tool": "bash", "args": long_cmd, "success": True, "exit_code": 0}
        new = {**old, "tool_call_id": "c1", "output_lines": 120, "duration_ms": 1500}
        db = tmp_path / "state.db"
        store = StateStore(db)
        store.append_event(Event(ts=1.0, run_id="r1", type="agent.tool_end", data=dict(old)))
        store.append_event(Event(ts=2.0, run_id="r1", type="agent.tool_end", data=dict(new)))
        replayed = [e.data for _, e in store.events("r1", type_prefix="agent.")]
        assert replayed == [old, new]
        # The stored command is the full one; truncation is display-only.
        assert replayed[0]["args"] == long_cmd
        # A resume reopens the database: the same events come back unchanged.
        store.close()
        reopened = StateStore(db)
        assert [e.data for _, e in reopened.events("r1")] == [old, new]

    def test_last_event_ts(self, store: StateStore) -> None:
        assert store.last_event_ts("r1") is None
        store.append_event(Event(ts=5.0, run_id="r1", type="run.start"))
        store.append_event(Event(ts=9.0, run_id="r1", type="worker.heartbeat"))
        store.append_event(Event(ts=99.0, run_id="other", type="run.start"))
        assert store.last_event_ts("r1") == 9.0


class TestWorkspaceColumns:
    def test_set_and_read_workspace(self, store: StateStore, tmp_path: Path) -> None:
        store.create_run("r1", "x")
        record = store.get_run("r1")
        assert record.workspace is None
        assert record.mounted is False
        store.set_run_workspace("r1", tmp_path / "ws", True)
        record = store.get_run("r1")
        assert record.workspace == tmp_path / "ws"
        assert record.mounted is True
        assert store.list_runs()[0].workspace == tmp_path / "ws"

    def test_set_workspace_unknown_run(self, store: StateStore, tmp_path: Path) -> None:
        with pytest.raises(StateError, match="unknown run"):
            store.set_run_workspace("ghost", tmp_path, False)

    def test_pre_0_3_database_migrates_in_place(self, tmp_path: Path) -> None:
        """A state.db created before the workspace columns opens cleanly and
        gains them (idempotent ALTERs guarded by PRAGMA table_info)."""
        import sqlite3

        db = tmp_path / "state.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, outcome TEXT NOT NULL,"
            " state TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}',"
            " created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute("INSERT INTO runs VALUES ('r1', 'old', 'completed', '{}', 1.0, 1.0)")
        conn.commit()
        conn.close()

        store = StateStore(db)
        record = store.get_run("r1")
        assert record.workspace is None
        assert record.mounted is False
        assert record.kept_reason is None
        store.set_run_workspace("r1", tmp_path / "ws", True)
        assert store.get_run("r1").mounted is True
        # reopening does not re-apply the ALTERs
        StateStore(db).get_run("r1")


class TestKeptMarker:
    def test_set_and_clear(self, store: StateStore) -> None:
        store.create_run("r1", "x")
        assert store.get_run("r1").kept_reason is None
        store.set_run_kept("r1", "debug")
        assert store.get_run("r1").kept_reason == "debug"
        assert store.list_runs()[0].kept_reason == "debug"
        store.set_run_kept("r1", None)
        assert store.get_run("r1").kept_reason is None

    def test_unknown_run(self, store: StateStore) -> None:
        with pytest.raises(StateError, match="unknown run"):
            store.set_run_kept("ghost", "debug")


class TestUserGuidance:
    def test_append_and_get(self, store: StateStore) -> None:
        store.create_run("r1", "x")
        assert store.get_run_guidance("r1") == []
        store.append_run_guidance("r1", "use postgres")
        store.append_run_guidance("r1", "target python 3.13")
        assert store.get_run_guidance("r1") == ["use postgres", "target python 3.13"]

    def test_unknown_run(self, store: StateStore) -> None:
        with pytest.raises(StateError, match="unknown run"):
            store.append_run_guidance("ghost", "g")
        with pytest.raises(StateError, match="unknown run"):
            store.get_run_guidance("ghost")

    def test_pre_guidance_database_migrates_in_place(self, tmp_path: Path) -> None:
        import sqlite3

        db = tmp_path / "state.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, outcome TEXT NOT NULL,"
            " state TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}',"
            " created_at REAL NOT NULL, updated_at REAL NOT NULL,"
            " workspace TEXT, mounted INTEGER NOT NULL DEFAULT 0, kept_reason TEXT)"
        )
        conn.execute(
            "INSERT INTO runs VALUES ('r1', 'old', 'completed', '{}', 1.0, 1.0, NULL, 0, NULL)"
        )
        conn.commit()
        conn.close()

        store = StateStore(db)
        assert store.get_run_guidance("r1") == []
        store.append_run_guidance("r1", "g")
        assert StateStore(db).get_run_guidance("r1") == ["g"]


class TestReconciliation:
    def test_non_terminal_runs_filters_and_orders(self, store: StateStore) -> None:
        for run_id, state in (
            ("r_done", "completed"),
            ("r_failed", "failed"),
            ("r_cancelled", "cancelled"),
            ("r_run", "running"),
            ("r_dec", "decomposing"),
        ):
            store.create_run(run_id, run_id)
            store.set_run_state(run_id, state)
        store.create_run("r_created", "left as created")

        records = store.non_terminal_runs()
        assert {r.run_id for r in records} == {"r_run", "r_dec", "r_created"}
        stamps = [r.updated_at for r in records]
        assert stamps == sorted(stamps)

    def test_reconcile_run_writes_state_and_reason(self, store: StateStore) -> None:
        store.create_run("r_run", "o")
        store.set_run_state("r_run", "running")

        store.reconcile_run("r_run", "cancelled", "work item cancelled")

        record = store.get_run("r_run")
        assert record.state == "cancelled"
        assert record.reason == "work item cancelled"
        assert store.non_terminal_runs() == []

    def test_reconcile_run_rejects_non_terminal_and_unknown(self, store: StateStore) -> None:
        store.create_run("r_run", "o")
        with pytest.raises(StateError):
            store.reconcile_run("r_run", "running", "nope")
        with pytest.raises(StateError):
            store.reconcile_run("missing", "failed", "nope")

    def test_reconcile_does_not_touch_events(self, store: StateStore) -> None:
        store.create_run("r_run", "o")
        store.set_run_state("r_run", "running")
        store.append_event(Event(ts=1.0, run_id="r_run", type="run.start", data={"i": 1}))
        before = list(store.events("r_run"))

        store.reconcile_run("r_run", "failed", "orphaned")

        after = list(store.events("r_run"))
        assert after == before

    def test_opens_db_missing_reason_column(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        StateStore(db).close()
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE runs")
        conn.execute(
            "CREATE TABLE runs ("
            " run_id TEXT PRIMARY KEY, outcome TEXT NOT NULL, state TEXT NOT NULL,"
            " config_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,"
            " updated_at REAL NOT NULL, workspace TEXT,"
            " mounted INTEGER NOT NULL DEFAULT 0, kept_reason TEXT,"
            " user_guidance TEXT NOT NULL DEFAULT '[]')"
        )
        conn.execute(
            "INSERT INTO runs (run_id, outcome, state, created_at, updated_at)"
            " VALUES ('old', 'legacy', 'running', 1.0, 2.0)"
        )
        conn.commit()
        conn.close()

        store = StateStore(db)
        record = store.get_run("old")
        assert record.state == "running"
        assert record.reason is None
        store.reconcile_run("old", "failed", "orphaned: daemon restarted")
        assert store.get_run("old").reason == "orphaned: daemon restarted"
        store.close()

        assert StateStore(db).get_run("old").state == "failed"


class TestWriterSerialization:
    """One connection is shared by every caller (``check_same_thread=False``),
    and ``append_event_if_state`` holds an explicit ``BEGIN IMMEDIATE`` across
    a state check and an insert. Any writer that commits without the lock ends
    that transaction early and silently defeats the atomicity the claim needs
    — which is how the daemon's ``reconcile_run`` (called from cancellation,
    on a different thread from the engine) slipped through.
    """

    def test_every_committing_method_holds_the_lock(self) -> None:
        import inspect
        import re

        from sbxloop.engine import store as store_module

        source = inspect.getsource(store_module).split("\n")
        current, lock_at, unguarded = None, -1, []
        for i, line in enumerate(source):
            match = re.match(r"    def (\w+)", line)
            if match:
                current, lock_at = match.group(1), -1
            if "with self._lock:" in line:
                lock_at = i
            if "_conn.commit()" in line and lock_at < 0 and current != "__init__":
                unguarded.append(current)
        assert not unguarded, f"writers commit without self._lock: {unguarded}"
