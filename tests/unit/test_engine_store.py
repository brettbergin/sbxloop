"""StateStore tests."""

import sqlite3
from pathlib import Path

import pytest

from sbxloop.engine.model import TaskRecord, TaskSpec
from sbxloop.engine.store import StateStore
from sbxloop.errors import StateError
from sbxloop_worker.protocol import Event, Usage


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def make_task(id: str = "t1") -> TaskRecord:
    return TaskRecord(spec=TaskSpec(id=id, title=id.upper()))


class TestPragmas:
    def test_wal_mode_with_normal_synchronous(self, store: StateStore) -> None:
        conn = store._conn
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1


class TestRuns:
    def test_create_get_update(self, store: StateStore) -> None:
        record = store.create_run("r1", "do things", "{}")
        assert record.state == "created"
        store.set_run_state("r1", "building")
        assert store.get_run("r1").state == "building"

    def test_stage_tracks_non_terminal_states_and_survives_terminal_ones(
        self, store: StateStore
    ) -> None:
        """`stage` is where a resume re-enters: every non-terminal state
        writes it, a terminal state leaves it, so a failed/blocked run still
        knows the stage it stopped in."""
        store.create_run("r1", "x")
        assert store.get_run("r1").stage is None
        for state in ("provisioning", "decomposing", "building", "gating", "delivering"):
            store.set_run_state("r1", state)  # type: ignore[arg-type]
            assert store.get_run("r1").stage == state
        store.set_run_state("r1", "awaiting_ci")
        store.set_run_state("r1", "blocked")
        run = store.get_run("r1")
        assert (run.state, run.stage) == ("blocked", "awaiting_ci")
        store.set_run_state("r1", "landing")
        store.set_run_state("r1", "merged")
        run = store.get_run("r1")
        assert (run.state, run.stage) == ("merged", "landing")

    def test_reason_round_trips_and_clears(self, store: StateStore) -> None:
        store.create_run("r1", "x")
        assert store.get_run("r1").reason is None
        store.set_run_reason("r1", "ci fix rounds exhausted")
        assert store.get_run("r1").reason == "ci fix rounds exhausted"
        store.set_run_reason("r1", None)
        assert store.get_run("r1").reason is None
        with pytest.raises(StateError, match="unknown run"):
            store.set_run_reason("ghost", "x")

    def test_legacy_run_states_read_as_building(self, store: StateStore) -> None:
        """A pre-pipeline database holds runs in `running` / `finalizing`;
        both meant "the task graph is being worked", which `building` now
        names, so they read back remapped from get_run and list_runs alike
        and the run still lists and resumes."""
        store.create_run("r1", "x")
        store.create_run("r2", "y")
        store._conn.execute("UPDATE runs SET state = 'running' WHERE run_id = 'r1'")
        store._conn.execute("UPDATE runs SET state = 'finalizing' WHERE run_id = 'r2'")
        store._conn.commit()
        assert store.get_run("r1").state == "building"
        assert store.get_run("r2").state == "building"
        assert {r.run_id: r.state for r in store.list_runs()} == {
            "r1": "building",
            "r2": "building",
        }
        # nothing else is remapped
        store.set_run_state("r1", "gating")
        assert store.get_run("r1").state == "gating"

    def test_touch_run_refreshes_updated_at_only(self, store: StateStore) -> None:
        store.create_run("r1", "x")
        store.set_run_state("r1", "awaiting_ci")
        before = store.get_run("r1")
        store._conn.execute("UPDATE runs SET updated_at = 1.0 WHERE run_id = 'r1'")
        store._conn.commit()
        store.touch_run("r1")
        after = store.get_run("r1")
        assert after.updated_at > 1.0
        assert (after.state, after.stage) == (before.state, before.stage)

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
        store.update_task("r1", task)

        loaded = store.get_tasks("r1")[0]
        assert loaded.state == "executing"
        assert loaded.revisions == 2
        assert loaded.last_feedback == "try harder"
        assert loaded.session_id == "s-1"

    def test_append_task_orders_after_the_saved_graph(self, store: StateStore) -> None:
        """A fix round is appended behind every graph task, and a second
        one behind the first — `save_tasks` numbers from zero and would
        collide with the graph already saved."""
        store.create_run("r1", "x")
        store.save_tasks("r1", [TaskSpec(id="t1", title="A"), TaskSpec(id="t2", title="B")])
        fix1 = store.append_task("r1", TaskSpec(id="fix-1", title="Fix"))
        assert fix1.state == "pending" and fix1.spec.id == "fix-1"
        store.append_task("r1", TaskSpec(id="fix-2", title="Fix again"))
        assert [t.spec.id for t in store.get_tasks("r1")] == ["t1", "t2", "fix-1", "fix-2"]
        # the appended task is updatable like any other
        fix1.state = "done"
        store.update_task("r1", fix1)
        assert [t.state for t in store.get_tasks("r1")] == ["pending", "pending", "done", "pending"]

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
            ),
            turns=3,
        )
        row = store.phase_attempts("r1")[0]
        assert row["input_tokens"] == 1200
        assert row["output_tokens"] == 34
        assert row["cache_read_tokens"] == 900
        assert row["cache_write_tokens"] == 10
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
            ("r_merged", "merged"),
            ("r_blocked", "blocked"),
            ("r_failed", "failed"),
            ("r_cancelled", "cancelled"),
            ("r_run", "building"),
            ("r_ci", "awaiting_ci"),
            ("r_dec", "decomposing"),
        ):
            store.create_run(run_id, run_id)
            store.set_run_state(run_id, state)
        store.create_run("r_created", "left as created")

        records = store.non_terminal_runs()
        assert {r.run_id for r in records} == {"r_run", "r_ci", "r_dec", "r_created"}
        stamps = [r.updated_at for r in records]
        assert stamps == sorted(stamps)

    def test_reconcile_run_writes_state_and_reason(self, store: StateStore) -> None:
        store.create_run("r_run", "o")
        store.set_run_state("r_run", "building")

        store.reconcile_run("r_run", "cancelled", "work item cancelled")

        record = store.get_run("r_run")
        assert record.state == "cancelled"
        assert record.reason == "work item cancelled"
        assert store.non_terminal_runs() == []

    def test_reconcile_run_rejects_non_terminal_and_unknown(self, store: StateStore) -> None:
        store.create_run("r_run", "o")
        with pytest.raises(StateError):
            store.reconcile_run("r_run", "building", "nope")
        with pytest.raises(StateError):
            store.reconcile_run("missing", "failed", "nope")

    def test_reconcile_does_not_touch_events(self, store: StateStore) -> None:
        store.create_run("r_run", "o")
        store.set_run_state("r_run", "building")
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
            " VALUES ('old', 'legacy', 'building', 1.0, 2.0)"
        )
        conn.commit()
        conn.close()

        store = StateStore(db)
        record = store.get_run("old")
        assert record.state == "building"
        assert record.reason is None
        assert record.stage is None and record.pr_number is None
        assert record.review_rounds == 0 and record.update_head is None
        store.reconcile_run("old", "failed", "orphaned: daemon restarted")
        assert store.get_run("old").reason == "orphaned: daemon restarted"
        store.close()

        assert StateStore(db).get_run("old").state == "failed"


class TestPipelineColumns:
    """The pull request and the round budgets live on the runs row so a
    resumed run picks the pipeline up exactly where it stopped."""

    def test_set_run_pr_round_trips_and_moves_the_head(self, store: StateStore) -> None:
        store.create_run("r1", "x")
        run = store.get_run("r1")
        assert (run.pr_number, run.pr_url, run.branch, run.head_sha, run.pr_node_id) == (
            None,
            None,
            None,
            None,
            None,
        )
        store.set_run_pr(
            "r1",
            number=9,
            url="https://x/pull/9",
            branch="sbxloop/r1",
            head_sha="aaa",
            node_id="PR_node9",
        )
        run = store.get_run("r1")
        assert (run.pr_number, run.pr_url, run.branch, run.head_sha, run.pr_node_id) == (
            9,
            "https://x/pull/9",
            "sbxloop/r1",
            "aaa",
            "PR_node9",
        )
        # A re-delivery moves the head; a missing node id keeps the old one.
        store.set_run_pr(
            "r1", number=9, url="https://x/pull/9", branch="sbxloop/r1", head_sha="bbb"
        )
        run = store.get_run("r1")
        assert run.head_sha == "bbb" and run.pr_node_id == "PR_node9"
        assert store.list_runs()[0].pr_number == 9

    def test_set_run_pr_clears_the_update_marker(self, store: StateStore) -> None:
        """An update-branch marker that outlived the head it was requested
        at would park the landing stage forever."""
        store.create_run("r1", "x")
        store.set_update_head("r1", "aaa")
        assert store.get_run("r1").update_head == "aaa"
        store.set_run_pr("r1", number=9, url="u", branch="b", head_sha="ccc")
        assert store.get_run("r1").update_head is None
        store.set_update_head("r1", None)
        assert store.get_run("r1").update_head is None

    def test_set_run_head_and_verdict(self, store: StateStore) -> None:
        store.create_run("r1", "x")
        store.set_run_head("r1", "ddd")
        store.set_run_verdict("r1", "request_changes")
        run = store.get_run("r1")
        assert run.head_sha == "ddd" and run.last_verdict == "request_changes"

    def test_bump_run_counter_is_whitelisted_and_returns_the_count(self, store: StateStore) -> None:
        store.create_run("r1", "x")
        assert store.bump_run_counter("r1", "review_rounds") == 1
        assert store.bump_run_counter("r1", "review_rounds") == 2
        assert store.bump_run_counter("r1", "ci_rounds") == 1
        assert store.bump_run_counter("r1", "update_attempts") == 1
        run = store.get_run("r1")
        assert (run.review_rounds, run.ci_rounds, run.update_attempts) == (2, 1, 1)
        with pytest.raises(StateError, match="not a run counter"):
            store.bump_run_counter("r1", "state")
        with pytest.raises(StateError, match="not a run counter"):
            store.bump_run_counter("r1", "review_rounds; DROP TABLE runs")
        assert store.get_run("r1").review_rounds == 2

    @pytest.mark.parametrize(
        "method",
        ["set_run_pr", "set_run_head", "set_run_verdict", "set_update_head", "bump_run_counter"],
    )
    def test_unknown_run_is_refused(self, store: StateStore, method: str) -> None:
        calls = {
            "set_run_pr": lambda: store.set_run_pr(
                "ghost", number=1, url="u", branch="b", head_sha="h"
            ),
            "set_run_head": lambda: store.set_run_head("ghost", "h"),
            "set_run_verdict": lambda: store.set_run_verdict("ghost", "approve"),
            "set_update_head": lambda: store.set_update_head("ghost", "h"),
            "bump_run_counter": lambda: store.bump_run_counter("ghost", "ci_rounds"),
        }
        with pytest.raises(StateError, match="unknown run"):
            calls[method]()

    def test_pre_pipeline_database_migrates_in_place(self, tmp_path: Path) -> None:
        """A state.db from before the pipeline columns opens cleanly, gains
        them with their defaults, and its old rows stay readable."""
        db = tmp_path / "state.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, outcome TEXT NOT NULL,"
            " state TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}',"
            " created_at REAL NOT NULL, updated_at REAL NOT NULL,"
            " workspace TEXT, mounted INTEGER NOT NULL DEFAULT 0, kept_reason TEXT,"
            " user_guidance TEXT NOT NULL DEFAULT '[]', reason TEXT)"
        )
        conn.execute(
            "INSERT INTO runs (run_id, outcome, state, created_at, updated_at)"
            " VALUES ('old', 'legacy', 'completed', 1.0, 2.0)"
        )
        conn.commit()
        conn.close()

        store = StateStore(db)
        run = store.get_run("old")
        assert run.stage is None and run.pr_number is None and run.head_sha is None
        assert (run.review_rounds, run.ci_rounds, run.update_attempts) == (0, 0, 0)
        assert run.update_head is None and run.last_verdict is None
        store.set_run_pr("old", number=1, url="u", branch="b", head_sha="h")
        assert store.bump_run_counter("old", "ci_rounds") == 1
        # reopening does not re-apply the ALTERs
        assert StateStore(db).get_run("old").pr_number == 1


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


class TestEphemeralDeltas:
    """Streaming deltas are UI telemetry: the full ``agent.message`` carries
    the same text and resume never reads deltas, so no persistence path may
    write one row per chunk."""

    def _event(self, type_: str, text: str) -> Event:
        return Event(ts=1.0, run_id="r1", job_id=None, type=type_, data={"text": text})

    def test_delta_is_not_persisted_but_full_message_is(self, store: StateStore) -> None:
        from sbxloop_worker.protocol import EventTypes

        store.create_run("r1", outcome="o")
        store.append_event(self._event(EventTypes.AGENT_MESSAGE_DELTA, "he"))
        store.append_event(self._event(EventTypes.AGENT_MESSAGE_DELTA, "llo"))
        store.append_event(self._event(EventTypes.AGENT_MESSAGE, "hello"))

        rows = [event for _, event in store.events("r1")]
        assert [e.type for e in rows] == [EventTypes.AGENT_MESSAGE]
        assert rows[0].data == {"text": "hello"}

    def test_append_event_if_state_skips_deltas(self, store: StateStore) -> None:
        from sbxloop_worker.protocol import EventTypes

        store.create_run("r1", outcome="o")
        assert (
            store.append_event_if_state(
                self._event(EventTypes.AGENT_MESSAGE_DELTA, "x"), {"created"}
            )
            is False
        )
        assert list(store.events("r1")) == []
        assert (
            store.append_event_if_state(self._event(EventTypes.AGENT_MESSAGE, "x"), {"created"})
            is True
        )
        assert len(list(store.events("r1"))) == 1

    def test_engine_still_publishes_deltas_to_subscribers(self, store: StateStore) -> None:
        from sbxloop_worker.protocol import EventTypes

        store.create_run("r1", outcome="o")
        seen: list[Event] = []

        from sbxloop.events import EventBus

        bus = EventBus()
        bus.subscribe(lambda e: seen.append(e))  # type: ignore[arg-type]
        bus.subscribe(lambda e: store.append_event(e))  # type: ignore[arg-type]
        bus.publish(self._event(EventTypes.AGENT_MESSAGE_DELTA, "he"))
        bus.publish(self._event(EventTypes.AGENT_MESSAGE, "hello"))

        assert [e.type for e in seen] == [
            EventTypes.AGENT_MESSAGE_DELTA,
            EventTypes.AGENT_MESSAGE,
        ]
        assert [e.type for _, e in store.events("r1")] == [EventTypes.AGENT_MESSAGE]
