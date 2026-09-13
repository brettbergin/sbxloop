"""A run's artifacts by identity, and their bytes — bounded, as
attachments, never by a path the client chose."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from sbxloop.api.artifacts import Artifact, content_type_for
from sbxloop.api.auth.deps import Authenticated, get_ctx, require
from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import ArtifactOut, ArtifactPage, PublishedOut, rfc3339
from sbxloop.api.projections import Views, not_found
from sbxloop.api.publicids import run_public_id
from sbxloop.engine.model import TERMINAL_RUN_STATES, RunRecord

router = APIRouter(prefix="/v1", tags=["artifacts"])

#: Downloads in flight at once; the rest wait their turn.
DOWNLOADS_AT_ONCE = 4
CHUNK = 256 * 1024
_downloads: asyncio.Semaphore | None = None
_downloads_loop: asyncio.AbstractEventLoop | None = None


def _semaphore() -> asyncio.Semaphore:
    global _downloads, _downloads_loop
    running = asyncio.get_running_loop()
    if _downloads is None or _downloads_loop is not running:
        _downloads = asyncio.Semaphore(DOWNLOADS_AT_ONCE)
        _downloads_loop = running
    return _downloads


def _out(artifact: Artifact) -> ArtifactOut:
    return ArtifactOut(
        id=artifact.id,
        run_id=run_public_id(artifact.run_id),
        task_id=artifact.task_id,
        path=artifact.relpath,
        size=artifact.size,
        sha256=artifact.sha256,
        media_type=artifact.media_type,
        origin=artifact.origin,
        recorded_at=rfc3339(artifact.recorded_at) or "",
        available=artifact.available,
        tombstoned_at=rfc3339(artifact.tombstoned_at),
    )


def _ensure_catalogued(ctx: ApiContext, record: RunRecord) -> None:
    """A finished run is catalogued the first time anyone asks, when the
    listener's own pass has not got to it yet."""
    if record.state in TERMINAL_RUN_STATES:
        ctx.artifacts.catalog_run(record)


@router.get("/runs/{run_id}/artifacts", response_model=ArtifactPage)
async def list_artifacts(
    run_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("artifacts:read")),  # noqa: B008
) -> ArtifactPage:
    """The run's catalog, and separately where the run published."""

    def read() -> ArtifactPage:
        views = Views(ctx)
        record = views.run_by_public_id(run_id)
        _ensure_catalogued(ctx, record)
        rows = ctx.artifacts.for_run(record.run_id)
        return ArtifactPage(
            data=[_out(a) for a in rows],
            published=[
                PublishedOut(sink=p.sink, location=p.location, tasks=list(p.tasks), files=p.files)
                for p in record.published
            ],
        )

    return await ctx.call(read)


def _lookup(ctx: ApiContext, artifact_id: str) -> tuple[Artifact, RunRecord]:
    artifact = ctx.artifacts.get(artifact_id)
    if artifact is None:
        raise not_found()
    record = Views(ctx).run_record(artifact.run_id)
    if record is None:
        raise not_found()
    return artifact, record


@router.get("/artifacts/{artifact_id}", response_model=ArtifactOut)
async def get_artifact(
    artifact_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("artifacts:read")),  # noqa: B008
) -> ArtifactOut:
    """Digest, media type, size, origin and availability."""

    def read() -> ArtifactOut:
        artifact, record = _lookup(ctx, artifact_id)
        if artifact.available and not ctx.artifacts.present(record, artifact):
            refreshed = ctx.artifacts.get(artifact_id)
            artifact = refreshed or artifact
        return _out(artifact)

    return await ctx.call(read)


@router.get("/artifacts/{artifact_id}/content")
async def download_artifact(
    artifact_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("artifacts:read")),  # noqa: B008
) -> StreamingResponse:
    """The bytes, as an attachment: never rendered, never a path the
    client chose, opened relative to the run's own directory without
    following a link out of it. ``410`` once the run was pruned."""

    def prepare() -> tuple[Artifact, RunRecord, Any]:
        artifact, record = _lookup(ctx, artifact_id)
        if not artifact.available:
            raise Problem(
                410, "artifact_gone", "the run's payload was pruned", artifact_id=artifact.id
            )
        try:
            opened = ctx.artifacts.open(record, artifact)
            handle = opened.__enter__()
        except OSError as exc:
            ctx.artifacts.tombstone(artifact.id)
            raise Problem(
                410, "artifact_gone", "the run's payload is gone", artifact_id=artifact.id
            ) from exc
        return artifact, record, (opened, handle)

    async with _semaphore():
        artifact, _record, (opened, handle) = await ctx.call(prepare)

        async def body() -> AsyncIterator[bytes]:
            try:
                while True:
                    chunk = await ctx.call(handle.read, CHUNK)
                    if not chunk:
                        break
                    yield chunk
            finally:
                opened.__exit__(None, None, None)

        filename = artifact.relpath.rsplit("/", 1)[-1].replace('"', "")
        return StreamingResponse(
            body(),
            media_type=content_type_for(artifact.media_type),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(artifact.size),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
                "X-Checksum-Sha256": artifact.sha256,
            },
        )
