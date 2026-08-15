"""Agent-filed backlog collection: dedup, caps, mount requirement, filing mode."""

from __future__ import annotations

from pathlib import Path

from sbxloop.daemon.backlog import BACKLOG_SUBDIR, collect_backlog
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import RunRecord


class RecordingFiler:
    name = "github"

    def __init__(self) -> None:
        self.filed: list[tuple[str, bool]] = []

    def file_backlog(self, title: str, body: str, origin_run_id: str, *, trigger: bool) -> str:
        self.filed.append((title, trigger))
        return f"gh:{len(self.filed)}"

    # unused protocol members
    def poll(self):  # type: ignore[no-untyped-def]
        return []

    def claim(self, item):  # type: ignore[no-untyped-def]
        return True

    def report_started(self, item, run_id):  # type: ignore[no-untyped-def]
        pass

    def report_success(self, item, report):  # type: ignore[no-untyped-def]
        pass

    def report_delivery_failed(self, item, report):  # type: ignore[no-untyped-def]
        pass

    def report_retry(self, item, error, attempts_left):  # type: ignore[no-untyped-def]
        pass

    def report_abandoned(self, item, error):  # type: ignore[no-untyped-def]
        pass


def run_record(workspace: Path, *, mounted: bool = True) -> RunRecord:
    return RunRecord(
        run_id="r1",
        outcome="x",
        state="completed",
        created_at=1.0,
        updated_at=2.0,
        workspace=workspace,
        mounted=mounted,
    )


def write_backlog(workspace: Path, name: str, title: str, body: str = "why") -> None:
    folder = workspace / BACKLOG_SUBDIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(f"# {title}\n\n{body}\n")


class TestCollectBacklog:
    def test_collects_dedups_and_caps(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        for i in range(7):
            write_backlog(ws, f"item{i}", f"Task {i}")
        write_backlog(ws, "dup", "Task 0")  # same title+body as item0 → same fingerprint
        dstore = DaemonStore(tmp_path / "state.db")
        filer = RecordingFiler()
        filed = collect_backlog(
            run_record(ws), dstore=dstore, source=filer, max_items=5, trigger=False, now=1.0
        )
        assert len(filed) == 5
        assert len(filer.filed) == 5
        assert all(trigger is False for _, trigger in filer.filed)
        # re-collection (e.g. after resume): the 5 already-filed are deduped
        # by fingerprint; only the 2 the cap deferred get filed now — a cap
        # defers, it never silently drops.
        again = collect_backlog(
            run_record(ws), dstore=dstore, source=filer, max_items=5, trigger=False, now=2.0
        )
        assert len(again) == 2
        assert len(filer.filed) == 7
        # and a third pass files nothing
        assert (
            collect_backlog(
                run_record(ws), dstore=dstore, source=filer, max_items=5, trigger=False, now=3.0
            )
            == []
        )

    def test_unmounted_run_skips(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        write_backlog(ws, "a", "A")
        filer = RecordingFiler()
        filed = collect_backlog(
            run_record(ws, mounted=False),
            dstore=DaemonStore(tmp_path / "state.db"),
            source=filer,
            max_items=5,
            trigger=False,
            now=1.0,
        )
        assert filed == [] and filer.filed == []

    def test_no_backlog_dir_is_fine(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        assert (
            collect_backlog(
                run_record(ws),
                dstore=DaemonStore(tmp_path / "state.db"),
                source=RecordingFiler(),
                max_items=5,
                trigger=False,
                now=1.0,
            )
            == []
        )

    def test_auto_trigger_flag_reaches_filer(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        write_backlog(ws, "a", "A")
        filer = RecordingFiler()
        collect_backlog(
            run_record(ws),
            dstore=DaemonStore(tmp_path / "state.db"),
            source=filer,
            max_items=5,
            trigger=True,
            now=1.0,
        )
        assert filer.filed == [("A", True)]
