"""Run watches survive in the store, so a daemon restart keeps its promises."""

from __future__ import annotations

from pathlib import Path

from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import DaemonStore


def store(tmp_path: Path, name: str = "d.sqlite3") -> DaemonStore:
    return DaemonStore(tmp_path / name)


def test_add_run_watch_is_idempotent(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.add_run_watch("r1", "u1", 1.0, backend="discord")
    s.add_run_watch("r1", "u1", 2.0, backend="discord")
    assert s.run_watchers("r1") == ["u1"]


def test_watchers_keep_insertion_order(tmp_path: Path) -> None:
    s = store(tmp_path)
    for watcher in ("u3", "u1", "u2"):
        s.add_run_watch("r1", watcher, 1.0, backend="discord")
    assert s.run_watchers("r1") == ["u3", "u1", "u2"]


def test_take_run_watchers_returns_and_clears(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.add_run_watch("r1", "u1", 1.0, backend="discord")
    s.add_run_watch("r1", "u2", 1.0, backend="discord")
    s.add_run_watch("r2", "u9", 1.0, backend="discord")

    assert s.take_run_watchers("r1", "discord") == ["u1", "u2"]
    assert s.run_watchers("r1") == []
    assert s.take_run_watchers("r1", "discord") == []
    # Another run's watch is untouched.
    assert s.run_watchers("r2") == ["u9"]


def test_all_run_watches_reloads_every_run(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.add_run_watch("r1", "u1", 1.0, backend="discord")
    s.add_run_watch("r2", "u2", 1.0, backend="discord")
    s.add_run_watch("r1", "u3", 1.0, backend="discord")
    assert s.all_run_watches("discord") == {"r1": ["u1", "u3"], "r2": ["u2"]}


def test_clear_run_watch(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.add_run_watch("r1", "u1", 1.0, backend="discord")
    s.clear_run_watch("r1")
    assert s.all_run_watches("discord") == {}


def test_watches_survive_reopen(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.add_run_watch("r1", "u1", 1.0, backend="discord")
    s.close()

    again = store(tmp_path)
    assert again.all_run_watches("discord") == {"r1": ["u1"]}


def test_finished_run_ids_reports_only_runs_with_a_ledger_close(tmp_path: Path) -> None:
    """Used by the Discord bridge's watch reload to drop watches for runs
    that already reached a terminal state — a run with no ledger row at
    all (never started, or started by a different process) is not
    reported as finished, only one `finish_ledger` actually closed."""
    s = store(tmp_path)
    s.upsert_new(WorkItem(item_id="i1", source_key="a.md", title="A"), 1.0)
    s.upsert_new(WorkItem(item_id="i2", source_key="b.md", title="B"), 1.0)
    s.mark_running("i1", "r1", 1.0)
    s.mark_running("i2", "r2", 1.0)
    s.finish_ledger("r1", "completed", 2.0)

    assert s.finished_run_ids(["r1", "r2", "r3"]) == {"r1"}
    assert s.finished_run_ids([]) == set()


def test_clear_run_watch_removes_only_the_named_run(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.add_run_watch("r1", "u1", 1.0, backend="discord")
    s.add_run_watch("r2", "u2", 1.0, backend="discord")
    s.clear_run_watch("r1")
    assert s.all_run_watches("discord") == {"r2": ["u2"]}


def test_watches_are_kept_per_backend(tmp_path: Path) -> None:
    """Two bridges (the external one and the console's local one) each
    register their own watchers for a run; one bridge taking its watchers
    must leave the other's in place, and a reload can ask for its own."""
    s = store(tmp_path)
    s.add_run_watch("r1", "u1", 1.0, backend="discord")
    s.add_run_watch("r1", "u1", 1.0, backend="local")
    s.add_run_watch("r1", "u2", 2.0, backend="local")
    assert s.run_watchers("r1", backend="discord") == ["u1"]
    assert s.run_watchers("r1", backend="local") == ["u1", "u2"]
    assert s.run_watchers("r1") == ["u1", "u1", "u2"], "a notify list addresses every backend"
    assert s.all_run_watches(backend="local") == {"r1": ["u1", "u2"]}
    assert s.all_run_watches("discord") == {"r1": ["u1"]}
    assert s.take_run_watchers("r1", backend="local") == ["u1", "u2"]
    assert s.run_watchers("r1") == ["u1"], "the other backend's watch stands"
    s.clear_run_watch("r1")
    assert s.all_run_watches(backend="local") == {}
    assert s.all_run_watches("discord") == {}


def test_watch_table_without_a_backend_column_is_rebuilt(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE daemon_run_watches (run_id TEXT NOT NULL, watcher_id TEXT NOT NULL,
            created_at REAL NOT NULL, UNIQUE(run_id, watcher_id));
        INSERT INTO daemon_run_watches VALUES ('r1', 'u1', 1.0);
        INSERT INTO daemon_run_watches VALUES ('r2', 'u2', 2.0);
        """
    )
    conn.commit()
    conn.close()
    s = DaemonStore(path)
    assert s.all_run_watches("discord") == {"r1": ["u1"], "r2": ["u2"]}
    s.add_run_watch("r1", "u1", 3.0, backend="local")
    assert s.run_watchers("r1", backend="local") == ["u1"]
    s.close()
    assert DaemonStore(path).all_run_watches("discord") == {"r1": ["u1"], "r2": ["u2"]}
