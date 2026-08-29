"""Run-directory retention: the sbxloop.gc policy, `sbxloop gc`, and the
daemon's periodic sweep."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from sbxloop.cli.app import app
from sbxloop.config import Config
from sbxloop.daemon.loop import DaemonLoop
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.store import StateStore
from sbxloop.events import Event, HostEventTypes
from sbxloop.gc import (
    DAY_S,
    classify_run_dirs,
    dir_size,
    format_bytes,
    prune_run_dirs,
    workspace_pruned,
)

runner = CliRunner()

NOW = 2_000_000.0
RETENTION = 14 * DAY_S


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def store(state_dir: Path) -> StateStore:
    return StateStore(state_dir / "state.db")


def seed_run(
    store: StateStore,
    state_dir: Path,
    run_id: str,
    *,
    state: str = "completed",
    age_days: float = 30,
    workspace: bool = True,
    kept: str | None = None,
    payload: bytes = b"x" * 1024,
) -> Path:
    """A run row plus its runs/<id>/ payload, aged by rewriting updated_at
    (the store stamps time.time(); the policy reads updated_at)."""
    run_dir = state_dir / "runs" / run_id
    (run_dir / "workspace").mkdir(parents=True)
    (run_dir / "workspace" / "file.bin").write_bytes(payload)
    (run_dir / "artifacts").mkdir()
    store.create_run(run_id, "outcome")
    if workspace:
        store.set_run_workspace(run_id, run_dir / "workspace", mounted=True)
    store.set_run_state(run_id, state)  # type: ignore[arg-type]
    if kept:
        store.set_run_kept(run_id, kept)
    store._conn.execute(
        "UPDATE runs SET updated_at = ? WHERE run_id = ?", (NOW - age_days * DAY_S, run_id)
    )
    store._conn.commit()
    return run_dir


def verdict_for(store: StateStore, state_dir: Path, run_id: str):  # type: ignore[no-untyped-def]
    verdicts = classify_run_dirs(store, state_dir, older_than_s=RETENTION, now=NOW)
    return next(v for v in verdicts if v.run_id == run_id)


class TestPolicy:
    def test_old_terminal_run_is_prunable(self, store: StateStore, state_dir: Path) -> None:
        seed_run(store, state_dir, "raaaaaaa1")
        v = verdict_for(store, state_dir, "raaaaaaa1")
        assert v.prunable
        assert v.size_bytes >= 1024
        assert "older than 14d" in v.reason

    @pytest.mark.parametrize("state", ["completed", "merged", "failed", "blocked", "cancelled"])
    def test_every_terminal_state_qualifies(
        self, store: StateStore, state_dir: Path, state: str
    ) -> None:
        seed_run(store, state_dir, "raaaaaaa2", state=state)
        assert verdict_for(store, state_dir, "raaaaaaa2").prunable

    @pytest.mark.parametrize(
        "state", ["created", "provisioning", "building", "delivering", "awaiting_ci", "landing"]
    )
    def test_in_flight_runs_are_never_pruned(
        self, store: StateStore, state_dir: Path, state: str
    ) -> None:
        # However old: the daemon may still resume it on its next start.
        seed_run(store, state_dir, "raaaaaaa3", state=state, age_days=400)
        v = verdict_for(store, state_dir, "raaaaaaa3")
        assert not v.prunable
        assert "resumable" in v.reason

    def test_within_retention_is_kept(self, store: StateStore, state_dir: Path) -> None:
        seed_run(store, state_dir, "raaaaaaa4", age_days=13.9)
        v = verdict_for(store, state_dir, "raaaaaaa4")
        assert not v.prunable
        assert "within retention" in v.reason

    def test_delivery_failed_is_kept(self, store: StateStore, state_dir: Path) -> None:
        # The workspace is the only copy of delivered-but-not-delivered work
        # (#223 redelivery needs it).
        seed_run(store, state_dir, "raaaaaaa5")
        store.append_event(
            Event.now(HostEventTypes.RUN_DELIVER, "raaaaaaa5", repo="o/r", error="HTTP 409")
        )
        v = verdict_for(store, state_dir, "raaaaaaa5")
        assert not v.prunable
        assert "delivery failed" in v.reason

    def test_later_successful_delivery_clears_the_failure(
        self, store: StateStore, state_dir: Path
    ) -> None:
        seed_run(store, state_dir, "raaaaaaa6")
        store.append_event(
            Event.now(HostEventTypes.RUN_DELIVER, "raaaaaaa6", repo="o/r", error="HTTP 409")
        )
        store.append_event(
            Event.now(HostEventTypes.RUN_DELIVER, "raaaaaaa6", repo="o/r", pr=1, url="u")
        )
        assert verdict_for(store, state_dir, "raaaaaaa6").prunable

    def test_kept_sandboxes_are_kept(self, store: StateStore, state_dir: Path) -> None:
        # A live kept sandbox may still mount the workspace.
        seed_run(store, state_dir, "raaaaaaa7", kept="debug")
        v = verdict_for(store, state_dir, "raaaaaaa7")
        assert not v.prunable
        assert "kept" in v.reason

    def test_unknown_and_foreign_dirs_are_reported_not_pruned(
        self, store: StateStore, state_dir: Path
    ) -> None:
        (state_dir / "runs" / "rzzzzzzz9").mkdir(parents=True)  # valid id, no row
        (state_dir / "runs" / "notes").mkdir()  # not a run id at all
        verdicts = classify_run_dirs(store, state_dir, older_than_s=RETENTION, now=NOW)
        by_id = {v.run_id: v for v in verdicts}
        assert not by_id["rzzzzzzz9"].prunable
        assert "unknown" in by_id["rzzzzzzz9"].reason
        assert not by_id["notes"].prunable
        assert by_id["rzzzzzzz9"].run_state is None

    def test_no_runs_dir_is_empty(self, store: StateStore, state_dir: Path) -> None:
        assert classify_run_dirs(store, state_dir, older_than_s=RETENTION, now=NOW) == []


class TestPrune:
    def test_removes_only_candidates_and_records_event(
        self, store: StateStore, state_dir: Path
    ) -> None:
        old = seed_run(store, state_dir, "rbbbbbbb1")
        young = seed_run(store, state_dir, "rbbbbbbb2", age_days=1)
        live = seed_run(store, state_dir, "rbbbbbbb3", state="building")
        result = prune_run_dirs(store, state_dir, older_than_s=RETENTION, now=NOW, actor="daemon")
        assert result.pruned == ["rbbbbbbb1"]
        assert result.failed == []
        assert result.bytes_freed >= 1024
        assert not old.exists()
        assert young.exists() and live.exists()
        # SQLite row survives (audit trail); the removal is on the record.
        assert store.get_run("rbbbbbbb1").state == "completed"
        events = [e for _s, e in store.events("rbbbbbbb1", type_prefix="daemon.gc")]
        assert len(events) == 1
        assert events[0].data["workspace_removed"] is True
        assert events[0].data["by"] == "daemon"
        assert events[0].data["bytes"] == result.bytes_freed
        assert workspace_pruned(store, "rbbbbbbb1")
        assert not workspace_pruned(store, "rbbbbbbb2")

    def test_workspace_elsewhere_is_not_flagged_removed(
        self, store: StateStore, state_dir: Path, tmp_path: Path
    ) -> None:
        # In-place workspace (user's checkout): only the artifacts dir goes,
        # so resume of such a run would still find its work.
        seed_run(store, state_dir, "rbbbbbbb4", workspace=False)
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        store.set_run_workspace("rbbbbbbb4", checkout, mounted=True)
        store._conn.execute(
            "UPDATE runs SET updated_at = ? WHERE run_id = ?", (NOW - 30 * DAY_S, "rbbbbbbb4")
        )
        store._conn.commit()
        result = prune_run_dirs(store, state_dir, older_than_s=RETENTION, now=NOW)
        assert result.pruned == ["rbbbbbbb4"]
        assert checkout.exists()
        assert not workspace_pruned(store, "rbbbbbbb4")

    def test_dry_run_removes_nothing_but_counts(self, store: StateStore, state_dir: Path) -> None:
        old = seed_run(store, state_dir, "rbbbbbbb5")
        result = prune_run_dirs(store, state_dir, older_than_s=RETENTION, now=NOW, dry_run=True)
        assert result.dry_run
        assert result.pruned == []
        assert [v.run_id for v in result.candidates] == ["rbbbbbbb5"]
        assert result.bytes_freed >= 1024
        assert old.exists()
        assert list(store.events("rbbbbbbb5", type_prefix="daemon.gc")) == []

    def test_second_sweep_is_a_no_op(self, store: StateStore, state_dir: Path) -> None:
        seed_run(store, state_dir, "rbbbbbbb6")
        prune_run_dirs(store, state_dir, older_than_s=RETENTION, now=NOW)
        again = prune_run_dirs(store, state_dir, older_than_s=RETENTION, now=NOW)
        assert again.pruned == [] and again.verdicts == []

    def test_claim_backs_off_when_run_leaves_terminal_after_classification(
        self, store: StateStore, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Another process resumes the failed run between our read of its
        # state and our removal: the marker+re-check is one write
        # transaction, so the sweep must see the new state and keep the dir.
        run_dir = seed_run(store, state_dir, "rbbbbbbb7", state="failed")
        real = StateStore.append_event_if_state

        def resume_first(self_: StateStore, event: Event, states: object) -> bool:
            self_.set_run_state(event.run_id, "provisioning")
            return real(self_, event, states)  # type: ignore[arg-type]

        monkeypatch.setattr(StateStore, "append_event_if_state", resume_first)
        result = prune_run_dirs(store, state_dir, older_than_s=RETENTION, now=NOW)
        assert result.pruned == [] and result.failed == []
        assert run_dir.exists()
        assert not workspace_pruned(store, "rbbbbbbb7")

    def test_marker_precedes_removal_and_survives_a_failed_rmtree(
        self, store: StateStore, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # rmtree can delete half a workspace and then raise; the durable
        # marker must already exist by then so resume refuses the remains.
        run_dir = seed_run(store, state_dir, "rbbbbbbb8", state="failed")

        def half_then_fail(path: object, *a: object, **k: object) -> None:
            (run_dir / "workspace" / "file.bin").unlink(missing_ok=True)
            raise OSError("EBUSY")

        def no_rename(*a: object, **k: object) -> None:
            raise OSError("EXDEV")

        # Force the in-place fallback (rename refused) and make rmtree fail.
        monkeypatch.setattr(Path, "rename", no_rename)
        monkeypatch.setattr("sbxloop.gc.shutil.rmtree", half_then_fail)
        result = prune_run_dirs(store, state_dir, older_than_s=RETENTION, now=NOW)
        assert result.failed == ["rbbbbbbb8"] and result.pruned == []
        assert workspace_pruned(store, "rbbbbbbb8")
        events = [e for _s, e in store.events("rbbbbbbb8", type_prefix="daemon.gc")]
        assert [bool(e.data.get("error")) for e in events] == [False, True]

    def test_staged_leftovers_are_reclaimed_next_sweep(
        self, store: StateStore, state_dir: Path
    ) -> None:
        # A sweep that died after the rename but before the delete leaves
        # the payload under gc-pending/; it was already marked, so the next
        # sweep just reclaims the disk.
        leftover = state_dir / "gc-pending" / "rdeadrun1"
        (leftover / "workspace").mkdir(parents=True)
        prune_run_dirs(store, state_dir, older_than_s=RETENTION, now=NOW)
        assert not leftover.exists()

    def test_dir_size_counts_symlinks_without_following(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "a").write_bytes(b"x" * 100)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "big").write_bytes(b"y" * 10_000)
        (root / "link").symlink_to(outside)
        assert 100 <= dir_size(root) < 10_000

    def test_format_bytes(self) -> None:
        assert format_bytes(0) == "0 B"
        assert format_bytes(1536) == "1.5 KB"
        assert format_bytes(3 * 1024**3) == "3.0 GB"


class TestResumeGuard:
    def test_resume_refuses_pruned_workspace(self, store: StateStore, state_dir: Path) -> None:
        from sbxloop.engine.engine import LoopEngine
        from sbxloop.errors import StateError

        # A failed run is resumable by an operator — until gc took its
        # workspace; then resume must say so instead of re-provisioning empty.
        seed_run(store, state_dir, "rccccccc1", state="failed")
        prune_run_dirs(store, state_dir, older_than_s=RETENTION, now=NOW)
        engine = LoopEngine(Config.model_validate({"state_dir": str(state_dir)}), store=store)
        with pytest.raises(StateError, match="removed by gc"):
            engine.resume("rccccccc1")

    def test_resume_leaves_terminal_before_touching_workspace(
        self, store: StateStore, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.engine.engine import LoopEngine

        # resume moves a failed run out of the terminal set before it
        # provisions, so a sweep racing it backs off: its claim re-checks
        # the state under the write lock.
        run_dir = seed_run(store, state_dir, "rccccccc2", state="failed")
        engine = LoopEngine(Config.model_validate({"state_dir": str(state_dir)}), store=store)
        real = StateStore.set_run_state
        seen: list[str] = []

        def gc_between(self_: StateStore, run_id: str, state: str) -> None:
            real(self_, run_id, state)  # type: ignore[arg-type]
            if state == "provisioning" and not seen:
                seen.append(state)
                # A concurrent sweep now: the run is no longer terminal.
                result = prune_run_dirs(self_, state_dir, older_than_s=RETENTION, now=NOW)
                assert result.pruned == []
                raise RuntimeError("stop before provisioning")

        monkeypatch.setattr(StateStore, "set_run_state", gc_between)
        with pytest.raises(RuntimeError, match="stop before"):
            engine.resume("rccccccc2")
        assert run_dir.exists() and not workspace_pruned(store, "rccccccc2")

    def test_resume_rechecks_after_leaving_terminal_and_restores_state(
        self, store: StateStore, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.engine.engine import LoopEngine
        from sbxloop.errors import StateError

        # The sweep's marker lands right after resume's first guard check:
        # the second check (after the transition) must refuse, and the run
        # goes back to failed rather than dangling in provisioning.
        seed_run(store, state_dir, "rccccccc3", state="failed")
        engine = LoopEngine(Config.model_validate({"state_dir": str(state_dir)}), store=store)
        calls: list[str] = []

        def marker_lands_after_first_check(store_: StateStore, run_id: str) -> bool:
            calls.append(run_id)
            if len(calls) == 1:
                prune_run_dirs(store_, state_dir, older_than_s=RETENTION, now=NOW)
                return False  # what the pre-check saw a moment ago
            return workspace_pruned(store_, run_id)

        monkeypatch.setattr(
            "sbxloop.engine.engine.workspace_pruned", marker_lands_after_first_check
        )
        with pytest.raises(StateError, match="removed by gc"):
            engine.resume("rccccccc3")
        assert store.get_run("rccccccc3").state == "failed"
        assert len(calls) == 2


class _NoWork:
    """A WorkSource that never has anything: the sweep is what is under test."""

    name = "none"

    def poll(self) -> list[Any]:
        return []

    def claim(self, item: Any) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        if name.startswith("report_"):
            return lambda *a, **k: True
        raise AttributeError(name)


class TestDaemonSweep:
    def make_loop(self, state_dir: Path, store: StateStore, **daemon: object) -> DaemonLoop:
        config = Config.model_validate({"state_dir": str(state_dir), "daemon": daemon})
        return DaemonLoop(
            config,
            store=store,
            dstore=DaemonStore(state_dir / "state.db"),
            source=cast(Any, _NoWork()),
            clock=lambda: NOW,
        )

    def test_first_tick_sweeps_then_daily(self, store: StateStore, state_dir: Path) -> None:
        old = seed_run(store, state_dir, "rddddddd1")
        loop = self.make_loop(state_dir, store)
        loop.tick()
        assert not old.exists()
        # A run that ages past the window later is not swept until a day
        # has passed since the last sweep.
        later = seed_run(store, state_dir, "rddddddd2")
        loop.tick()
        assert later.exists()
        loop.clock = lambda: NOW + DAY_S + 1  # type: ignore[assignment]
        loop.tick()
        assert not later.exists()

    def test_config_window_and_disable(self, store: StateStore, state_dir: Path) -> None:
        one_day = seed_run(store, state_dir, "rddddddd3", age_days=2)
        loop = self.make_loop(state_dir, store, prune_runs_after_days=1)
        loop.tick()
        assert not one_day.exists()
        never = seed_run(store, state_dir, "rddddddd4", age_days=400)
        self.make_loop(state_dir, store, prune_runs_after_days=0).tick()
        assert never.exists()

    def test_sweep_notifies_frontend_with_counts(self, store: StateStore, state_dir: Path) -> None:
        seed_run(store, state_dir, "rddddddd5")
        seen: list[Any] = []

        class Front:
            def daemon_notice(self, notice: Any) -> None:
                seen.append(notice)

            def run_started(self, *a: object) -> None: ...
            def run_finished(self, *a: object) -> None: ...

        loop = self.make_loop(state_dir, store)
        loop.frontend = Front()
        loop.tick()
        notice = next(n for n in seen if n.kind == "daemon.gc")
        assert "pruned 1 run dir(s)" in notice.text and "freed" in notice.text

    def test_sweep_failure_does_not_take_daemon_down(
        self, store: StateStore, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sbxloop.daemon.loop as loop_mod

        def boom(*a: object, **k: object) -> None:
            raise OSError("disk on fire")

        monkeypatch.setattr(loop_mod, "prune_run_dirs", boom)
        loop = self.make_loop(state_dir, store)
        assert loop.tick().idle_kind == "no_work"


class TestCli:
    @pytest.fixture
    def workdir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def cli_seed(self, workdir: Path) -> tuple[StateStore, Path, Path]:
        state_dir = workdir / ".sbxloop"
        store = StateStore(state_dir / "state.db")
        old = seed_run(store, state_dir, "reeeeeee1")
        young = seed_run(store, state_dir, "reeeeeee2", age_days=1)
        # seed_run ages against the fixed NOW; the CLI uses wall time, so
        # re-age relative to now.
        import time

        store._conn.execute(
            "UPDATE runs SET updated_at = ? WHERE run_id = ?",
            (time.time() - 30 * DAY_S, "reeeeeee1"),
        )
        store._conn.execute(
            "UPDATE runs SET updated_at = ? WHERE run_id = ?",
            (time.time() - 1 * DAY_S, "reeeeeee2"),
        )
        store._conn.commit()
        return store, old, young

    def test_gc_dry_run_reports_and_keeps(self, workdir: Path) -> None:
        _store, old, young = self.cli_seed(workdir)
        result = runner.invoke(app, ["gc", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "reeeeeee1" in result.output and "prune" in result.output
        assert "dry run: 1 run dir(s)" in result.output
        assert old.exists() and young.exists()

    def test_gc_removes_and_honours_older_than(self, workdir: Path) -> None:
        _store, old, young = self.cli_seed(workdir)
        result = runner.invoke(app, ["gc"])
        assert result.exit_code == 0, result.output
        assert "removed 1 run dir(s)" in result.output
        assert not old.exists() and young.exists()
        result = runner.invoke(app, ["gc", "--older-than", "0"])
        assert result.exit_code == 0, result.output
        assert not young.exists()

    @pytest.mark.parametrize("value", ["-1", "nan", "inf"])
    def test_gc_rejects_negative_and_non_finite_older_than(self, workdir: Path, value: str) -> None:
        # NaN compares false against every age, which would classify every
        # terminal run as past retention; inf/negative are just nonsense.
        _store, old, young = self.cli_seed(workdir)
        result = runner.invoke(app, ["gc", "--older-than", value])
        assert result.exit_code == 2, result.output
        assert "finite" in result.output
        assert old.exists() and young.exists()

    def test_gc_nothing_to_do(self, workdir: Path) -> None:
        StateStore(workdir / ".sbxloop" / "state.db")
        result = runner.invoke(app, ["gc"])
        assert result.exit_code == 0
        assert "no run directories" in result.output

    def test_gc_in_help(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Wide terminal so rich does not wrap the command's help line.
        monkeypatch.setenv("COLUMNS", "300")
        result = runner.invoke(app, ["--help"])
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert re.search(r"\bgc\s+Remove old run directories", plain), plain

    def test_gc_table_shows_foreign_dir_as_unknown(self, workdir: Path) -> None:
        # A runs/<id>/ directory with no state-DB row: the table must
        # render the missing state/age rather than crash, and keep it.
        state_dir = workdir / ".sbxloop"
        StateStore(state_dir / "state.db")
        (state_dir / "runs" / "rforeign1").mkdir(parents=True)
        result = runner.invoke(app, ["gc"])
        assert result.exit_code == 0, result.output
        assert "rforeign1" in result.output
        assert "unknown" in result.output and "keep" in result.output
        assert (state_dir / "runs" / "rforeign1").exists()
