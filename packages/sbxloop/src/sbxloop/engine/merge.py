"""Workspace merge for parallel waves: attribute, enforce, apply.

A parallel wave runs each task in an isolated in-VM workdir seeded from the
run's merged host tree. Afterwards each task's workdir is harvested to a
per-task staging directory and diffed against the pre-wave baseline — with
one sandbox per task, "who wrote what" is exact. Enforcement is the issue's
per-task subtree ownership: every change must fall inside the task's
declared ``owns``; violations fail the task loudly and its changes are
discarded, never merged. Because a wave only ever contains tasks with
pairwise-disjoint ``owns`` (see ``pack_parallel_batch``), the surviving
change sets are disjoint by construction and merging is conflict-free.

Hidden paths (any component starting with ``.``) are excluded from
snapshots, seeding, and merging — mirroring ``artifact_files`` semantics,
and guaranteeing a mounted host workspace's ``.git`` can never be touched
by a parallel merge.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path, PurePosixPath

from sbxloop.engine.model import TaskSpec

Change = str  # "added" | "modified" | "deleted"


def _hidden(relative: PurePosixPath) -> bool:
    return any(part.startswith(".") for part in relative.parts)


def snapshot_tree(root: Path) -> dict[str, str]:
    """Relative path → content digest for every non-hidden file under root."""
    if not root.is_dir():
        return {}
    snapshot: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if _hidden(relative) or not path.is_file():
            continue
        snapshot[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def tree_changes(baseline: dict[str, str], current: dict[str, str]) -> dict[str, Change]:
    """What changed relative to the baseline snapshot."""
    changes: dict[str, Change] = {}
    for path, digest in current.items():
        if path not in baseline:
            changes[path] = "added"
        elif baseline[path] != digest:
            changes[path] = "modified"
    for path in baseline:
        if path not in current:
            changes[path] = "deleted"
    return changes


def _within(path: str, owns: str) -> bool:
    path_parts = PurePosixPath(path).parts
    owns_parts = PurePosixPath(owns).parts
    return path_parts[: len(owns_parts)] == owns_parts


def owns_violations(changes: dict[str, Change], spec: TaskSpec) -> list[str]:
    """Changed paths outside the task's declared ownership, sorted."""
    return sorted(path for path in changes if not any(_within(path, owns) for owns in spec.owns))


def build_seed_dir(merged: Path, seed: Path) -> Path:
    """Materialize the non-hidden view of the merged tree for cp-in seeding."""
    if seed.exists():
        shutil.rmtree(seed)
    seed.mkdir(parents=True)
    for path in snapshot_tree(merged):
        target = seed / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(merged / path, target)
    return seed


def apply_changes(staging: Path, merged: Path, changes: dict[str, Change]) -> None:
    """Apply one task's verified changes from its staging dir to the merged
    tree. Deletions remove the file; empty parent dirs are left in place."""
    for path, change in sorted(changes.items()):
        target = merged / path
        if change == "deleted":
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staging / path, target)
