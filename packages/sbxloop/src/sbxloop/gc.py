"""Run-directory retention: the policy behind ``sbxloop gc`` and the
daemon's periodic sweep.

Every run leaves ``<state_dir>/runs/<run_id>/`` behind — a full clone of
the target checkout under workspace isolation (plus whatever the agent
built in it) and, for unmounted runs, the harvested artifacts. Nothing else
ever removes them, and an always-on daemon at the default 12 runs/day
accretes clones without bound (the #227 field session left ~8 of them in
one afternoon on a small project). This module removes only that
filesystem payload; the SQLite rows are small and are the audit trail, so
they stay.

The policy is deliberately conservative — a run directory is the ONLY copy
of an agent's work until it is fetched or delivered:

* only runs the state DB knows and whose state is terminal
  (completed / failed / cancelled) — never one still in flight, which the
  daemon may resume on its next start;
* never a run whose delivery failed: the workspace is what a later
  redelivery (#223) needs, and a human has yet to look at it;
* never a run whose sandboxes were deliberately kept — a live sandbox may
  still have the workspace mounted;
* only past the retention window, measured from the run's ``updated_at``.

Directories that do not belong to a run this state DB knows are reported
but left alone (another working copy's state_dir, a hand-made directory).
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from sbxloop.engine.model import TERMINAL_RUN_STATES
from sbxloop.engine.store import StateStore
from sbxloop.errors import StateError
from sbxloop.events import HostEventTypes
from sbxloop.ids import is_run_id
from sbxloop.log import get_logger
from sbxloop_worker.protocol import Event

log = get_logger(__name__)

DAY_S = 86400.0


class RunDirVerdict(BaseModel):
    """One ``runs/<run_id>/`` directory: what we know and whether it goes."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    path: Path
    run_state: str | None = None  # None → unknown to this state DB
    age_s: float | None = None  # since the run's updated_at
    size_bytes: int = 0
    prunable: bool = False
    reason: str


class GcResult(BaseModel):
    """What one sweep did (or, in dry-run, would do)."""

    model_config = ConfigDict(extra="forbid")

    verdicts: list[RunDirVerdict]
    pruned: list[str]  # run ids whose directory was removed
    failed: list[str]  # run ids whose removal raised
    bytes_freed: int
    dry_run: bool

    @property
    def candidates(self) -> list[RunDirVerdict]:
        return [v for v in self.verdicts if v.prunable]


def dir_size(path: Path) -> int:
    """Bytes under ``path``. Symlinks are counted, not followed: a clone can
    carry links into the source checkout and following them would both
    inflate the number and — worse — tempt a future "remove what we
    counted"."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                st = entry.lstat()
            except OSError:
                continue
            total += st.st_size
            if entry.is_dir() and not entry.is_symlink():
                stack.append(entry)
    return total


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def delivery_failed(store: StateStore, run_id: str) -> bool:
    """Whether the run's LAST delivery attempt failed — the same reading the
    daemon's report uses: a later successful ``run.deliver`` clears an
    earlier error."""
    failed = False
    for _seq, event in store.events(run_id, type_prefix=HostEventTypes.RUN_DELIVER):
        if event.data.get("error"):
            failed = True
        elif event.data.get("url"):
            failed = False
    return failed


def classify_run_dirs(
    store: StateStore,
    state_dir: Path,
    *,
    older_than_s: float,
    now: float | None = None,
) -> list[RunDirVerdict]:
    """Classify every directory under ``<state_dir>/runs`` against the state
    DB. Only the directory's presence on disk matters — a run whose payload
    is already gone has nothing to prune and is not listed."""
    now = time.time() if now is None else now
    runs_root = state_dir / "runs"
    if not runs_root.is_dir():
        return []
    verdicts: list[RunDirVerdict] = []
    for path in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        run_id = path.name
        if not is_run_id(run_id):
            verdicts.append(RunDirVerdict(run_id=run_id, path=path, reason="not a run directory"))
            continue
        try:
            record = store.get_run(run_id)
        except StateError:
            verdicts.append(
                RunDirVerdict(run_id=run_id, path=path, reason="unknown to this state DB")
            )
            continue
        age_s = max(0.0, now - record.updated_at)
        window = f"{older_than_s / DAY_S:g}d"
        keep: str | None
        if record.state not in TERMINAL_RUN_STATES:
            keep = f"run is {record.state} (resumable)"
        elif record.kept_reason is not None:
            keep = f"sandboxes kept ({record.kept_reason}); workspace may be mounted"
        elif delivery_failed(store, run_id):
            keep = "delivery failed; workspace is the only copy"
        elif age_s < older_than_s:
            keep = f"within retention ({window})"
        else:
            keep = None
        verdicts.append(
            RunDirVerdict(
                run_id=run_id,
                path=path,
                run_state=record.state,
                age_s=age_s,
                size_bytes=0 if keep else dir_size(path),
                prunable=keep is None,
                reason=keep or f"{record.state}, older than {window}",
            )
        )
    return verdicts


def prune_run_dirs(
    store: StateStore,
    state_dir: Path,
    *,
    older_than_s: float,
    dry_run: bool = False,
    now: float | None = None,
    actor: str = "cli",
) -> GcResult:
    """Remove the prunable run directories (see :func:`classify_run_dirs`).

    Each removal is recorded as a ``daemon.gc`` event on the run so
    ``sbxloop logs`` shows why the payload is gone, and so ``resume`` can
    refuse a run whose workspace no longer exists instead of silently
    re-provisioning an empty one.

    Removal is ordered so a crash or a concurrent ``resume`` at any point
    leaves the run either intact or durably marked, never half-gone and
    unmarked:

    1. the marker is written first, in one write transaction with a re-check
       that the run is still terminal (``resume`` moves it out of the
       terminal set before it touches the workspace, so whichever of the two
       commits first wins and the other backs off);
    2. the directory is renamed out of ``runs/`` in one atomic step
       (``rmtree`` can fail half-way; a rename cannot), then deleted.

    A marker with no removal behind it (crash between the two steps, or a
    removal that failed) only costs a refused resume of a run that was
    already past retention; the next sweep finishes the job.
    """
    now = time.time() if now is None else now
    _remove_staged(state_dir)
    verdicts = classify_run_dirs(store, state_dir, older_than_s=older_than_s, now=now)
    pruned: list[str] = []
    failed: list[str] = []
    freed = 0
    for verdict in verdicts:
        if not verdict.prunable:
            continue
        if dry_run:
            freed += verdict.size_bytes
            continue
        record = store.get_run(verdict.run_id)
        workspace_removed = record.workspace is not None and _is_within(
            record.workspace, verdict.path
        )
        data = {
            "path": str(verdict.path),
            "bytes": verdict.size_bytes,
            "age_s": verdict.age_s,
            "workspace_removed": workspace_removed,
            "by": actor,
        }
        if not store.append_event_if_state(
            _gc_event(verdict.run_id, now, data), TERMINAL_RUN_STATES
        ):
            log.info(
                "gc.kept",
                run=verdict.run_id,
                reason="left the terminal states since classification",
            )
            continue
        if not _remove(verdict.path, state_dir):
            failed.append(verdict.run_id)
            store.append_event(
                _gc_event(verdict.run_id, now, {**data, "error": "could not remove directory"})
            )
            continue
        pruned.append(verdict.run_id)
        freed += verdict.size_bytes
    return GcResult(
        verdicts=verdicts, pruned=pruned, failed=failed, bytes_freed=freed, dry_run=dry_run
    )


def workspace_pruned(store: StateStore, run_id: str) -> bool:
    """Whether a gc sweep removed this run's workspace (the resume guard)."""
    return any(
        bool(event.data.get("workspace_removed"))
        for _seq, event in store.events(run_id, type_prefix=HostEventTypes.DAEMON_GC)
    )


def _gc_event(run_id: str, ts: float, data: dict[str, object]) -> Event:
    return Event(ts=ts, run_id=run_id, job_id=None, type=HostEventTypes.DAEMON_GC, data=data)


def _staging_root(state_dir: Path) -> Path:
    return state_dir / "gc-pending"


def _remove(path: Path, state_dir: Path) -> bool:
    """Remove a run directory: rename it out of ``runs/`` first, then delete.

    A rename is all-or-nothing where ``rmtree`` is not, so once it succeeds
    the run directory is wholly gone from its address and a failure while
    deleting the staged copy is only wasted disk (reclaimed next sweep).
    Falls back to deleting in place when the rename is impossible (a
    ``runs/`` on another filesystem); the marker is already durable by then,
    so even a half-deletion can never be resumed into.
    """
    staging = _staging_root(state_dir)
    try:
        staging.mkdir(parents=True, exist_ok=True)
        staged = staging / path.name
        if staged.exists():
            _rmtree_quiet(staged)
        path.rename(staged)
    except OSError:
        log.debug("gc.stage_failed", path=str(path), action="removing in place", exc_info=True)
        try:
            shutil.rmtree(path)
        except OSError:
            log.warning("gc.remove_failed", path=str(path), exc_info=True)
            return False
        return True
    _rmtree_quiet(staged)
    return True


def _remove_staged(state_dir: Path) -> None:
    """Finish what an interrupted sweep started: anything under gc-pending/
    was already marked and renamed away, so it is just disk to reclaim."""
    staging = _staging_root(state_dir)
    if not staging.is_dir():
        return
    for leftover in staging.iterdir():
        _rmtree_quiet(leftover)


def _rmtree_quiet(path: Path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError:
        log.warning("gc.remove_failed", path=str(path), staged=True, exc_info=True)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
