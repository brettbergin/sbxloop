"""The artifact catalog: what a finished run left behind, by identity.

A remote client never learns a host path. Each file a run produced gets
an ``art_…`` id, a digest, a size and a media type in ``api_artifacts``;
the listing says which run and task it came from and whether the bytes
are still on the host. The catalog is built once per finished run from
the same scan ``sbxloop artifacts`` shows (``scan_artifacts`` under the
run's artifact directory, the operator's excludes applied), each file
opened through :func:`sbxloop.repofiles.open_file` — relative to the
run's directory, never following a link out of it — so a symlink that
points outside the run is refused at catalog time, not discovered at
download time. Downloads take the same road.

A run the retention sweep pruned keeps its rows: ``available`` goes off
the first time a read finds the bytes gone, so history still says what
was delivered. Publication is a separate fact: a catalog entry is a file
on the host; whether a sink took it is on the run's ``published`` list.
"""

from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import insert, select, update

from sbxloop import repofiles
from sbxloop.daemon.store import DaemonStore
from sbxloop.db.api_models import ArtifactRow
from sbxloop.engine.model import RunRecord, artifacts_dir, scan_artifacts
from sbxloop.engine.store import StateStore
from sbxloop.ids import _token
from sbxloop.log import get_logger
from sbxloop.paths import SbxloopHome

log = get_logger(__name__)

#: Files past this many in one run are not catalogued individually: the
#: listing says how many were left out, and the run's own listing on the
#: host still has them.
CATALOG_MAX_FILES = 2000
#: Types a browser would execute or render if served as themselves: every
#: download of one is `application/octet-stream`, as an attachment.
INLINE_UNSAFE: frozenset[str] = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
        "application/javascript",
        "text/javascript",
        "application/x-javascript",
        "text/xml",
        "application/xml",
    }
)
OCTET = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class Artifact:
    id: str
    run_id: str
    task_id: str | None
    relpath: str
    size: int
    sha256: str
    media_type: str
    origin: str
    recorded_at: float
    available: bool
    tombstoned_at: float | None


def new_artifact_id() -> str:
    return "art_" + _token(16)


def guess_media_type(relpath: str) -> str:
    guessed, _ = mimetypes.guess_type(relpath, strict=False)
    return guessed or OCTET


def content_type_for(media_type: str) -> str:
    """What a download is served as: the catalogued type, unless a browser
    could run or render it — then bytes, as an attachment."""
    return OCTET if media_type in INLINE_UNSAFE else media_type


def _row(row: ArtifactRow) -> Artifact:
    return Artifact(
        id=str(row.id),
        run_id=str(row.run_id),
        task_id=row.task_id,
        relpath=str(row.relpath),
        size=int(row.size),
        sha256=str(row.sha256),
        media_type=str(row.media_type),
        origin=str(row.origin),
        recorded_at=float(row.recorded_at),
        available=bool(row.available),
        tombstoned_at=row.tombstoned_at,
    )


class ArtifactCatalog:
    def __init__(
        self,
        dstore: DaemonStore,
        store: StateStore,
        home: SbxloopHome,
        *,
        exclude: Sequence[str],
        clock: Callable[[], float],
    ) -> None:
        self.dstore = dstore
        self.store = store
        self.home = home
        self.exclude = tuple(exclude)
        self.clock = clock

    # -- building --------------------------------------------------------------

    def root_for(self, record: RunRecord) -> Path | None:
        return artifacts_dir(record, self.home)

    def catalogued(self, run_id: str) -> bool:
        with self.dstore.read() as session:
            return (
                session.scalars(
                    select(ArtifactRow.id).where(ArtifactRow.run_id == run_id).limit(1)
                ).first()
                is not None
            )

    def catalog_run(self, record: RunRecord) -> int:
        """Catalog a finished run's files once; returns rows written (0 when
        already catalogued, or when the run left nothing to catalog)."""
        if self.catalogued(record.run_id):
            return 0
        root = self.root_for(record)
        if root is None or not root.is_dir():
            return 0
        origin = "sink" if record.kind in ("workload", "tool") else "workspace"
        if record.kind == "code" and not record.mounted:
            origin = "harvest"
        task_ids = {
            rel: task.spec.id
            for task in self.store.get_tasks(record.run_id)
            if task.output is not None
            for rel in task.output.files
        }
        scan = scan_artifacts(root, self.exclude)
        now = self.clock()
        rows: list[dict[str, Any]] = []
        skipped = 0
        for path in scan.files[:CATALOG_MAX_FILES]:
            rel = path.relative_to(root).as_posix()
            try:
                with repofiles.open_file(root, rel) as handle:
                    digest, size = _digest(handle)
            except OSError as exc:
                # A link out of the run, a special file, a vanished one:
                # not an artifact a client may have.
                log.info("api.artifact_skipped", run=record.run_id, path=rel, reason=str(exc))
                skipped += 1
                continue
            rows.append(
                {
                    "id": new_artifact_id(),
                    "run_id": record.run_id,
                    "task_id": task_ids.get(rel),
                    "relpath": rel,
                    "size": size,
                    "sha256": digest,
                    "media_type": guess_media_type(rel),
                    "origin": origin,
                    "recorded_at": now,
                    "available": 1,
                    "tombstoned_at": None,
                }
            )
        if not rows:
            return 0
        with self.dstore.transaction() as session:
            session.execute(insert(ArtifactRow).prefix_with("OR IGNORE"), rows)
        log.info(
            "api.artifacts_catalogued",
            run=record.run_id,
            files=len(rows),
            skipped=skipped,
            beyond_cap=max(0, len(scan.files) - CATALOG_MAX_FILES),
        )
        return len(rows)

    # -- reads -----------------------------------------------------------------

    def for_run(self, run_id: str) -> list[Artifact]:
        with self.dstore.read() as session:
            rows = session.scalars(
                select(ArtifactRow)
                .where(ArtifactRow.run_id == run_id)
                .order_by(ArtifactRow.relpath)
            )
            return [_row(row) for row in rows]

    def get(self, artifact_id: str) -> Artifact | None:
        with self.dstore.read() as session:
            row = session.get(ArtifactRow, artifact_id)
            return None if row is None else _row(row)

    def tombstone(self, artifact_id: str) -> None:
        with self.dstore.transaction() as session:
            session.execute(
                update(ArtifactRow)
                .where(ArtifactRow.id == artifact_id, ArtifactRow.available == 1)
                .values(available=0, tombstoned_at=self.clock())
            )

    def tombstone_run(self, run_id: str) -> int:
        with self.dstore.transaction() as session:
            result = session.execute(
                update(ArtifactRow)
                .where(ArtifactRow.run_id == run_id, ArtifactRow.available == 1)
                .values(available=0, tombstoned_at=self.clock())
            )
            return int(getattr(result, "rowcount", 0) or 0)

    def present(self, record: RunRecord, artifact: Artifact) -> bool:
        """Whether the bytes are still where the catalog says; a missing
        file tombstones the entry on the spot."""
        root = self.root_for(record)
        if root is None:
            self.tombstone(artifact.id)
            return False
        try:
            with repofiles.open_file(root, artifact.relpath):
                return True
        except OSError:
            self.tombstone(artifact.id)
            return False

    @contextmanager
    def open(self, record: RunRecord, artifact: Artifact) -> Iterator[BinaryIO]:
        """The bytes, opened relative to the run's directory without
        following a link out of it. ``OSError`` when they are gone."""
        root = self.root_for(record)
        if root is None:
            raise FileNotFoundError(artifact.relpath)
        with repofiles.open_file(root, artifact.relpath) as handle:
            yield handle


def _digest(handle: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = handle.read(1 << 20)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size
