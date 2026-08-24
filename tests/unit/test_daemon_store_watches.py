"""Run watches survive in the store, so a daemon restart keeps its promises."""

from __future__ import annotations

from pathlib import Path

from sbxloop.daemon.store import DaemonStore


def store(tmp_path: Path, name: str = "d.sqlite3") -> DaemonStore:
    return DaemonStore(tmp_path / name)


def test_add_run_watch_is_idempotent(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.add_run_watch("r1", "u1", 1.0)
    s.add_run_watch("r1", "u1", 2.0)
    assert s.run_watchers("r1") == ["u1"]


def test_watchers_keep_insertion_order(tmp_path: Path) -> None:
    s = store(tmp_path)
    for watcher in ("u3", "u1", "u2"):
        s.add_run_watch("r1", watcher, 1.0)
    assert s.run_watchers("r1") == ["u3", "u1", "u2"]


def test_take_run_watchers_returns_and_clears(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.add_run_watch("r1", "u1", 1.0)
    s.add_run_watch("r1", "u2", 1.0)
    s.add_run_watch("r2", "u9", 1.0)

    assert s.take_run_watchers("r1") == ["u1", "u2"]
    assert s.run_watchers("r1") == []
    assert s.take_run_watchers("r1") == []
    # Another run's watch is untouched.
    assert s.run_watchers("r2") == ["u9"]


def test_all_run_watches_reloads_every_run(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.add_run_watch("r1", "u1", 1.0)
    s.add_run_watch("r2", "u2", 1.0)
    s.add_run_watch("r1", "u3", 1.0)
    assert s.all_run_watches() == {"r1": ["u1", "u3"], "r2": ["u2"]}


def test_clear_run_watch(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.add_run_watch("r1", "u1", 1.0)
    s.clear_run_watch("r1")
    assert s.all_run_watches() == {}


def test_watches_survive_reopen(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.add_run_watch("r1", "u1", 1.0)
    s.close()

    again = store(tmp_path)
    assert again.all_run_watches() == {"r1": ["u1"]}
