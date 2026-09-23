"""Upload and retrieve opaque channel input originals.

The API accepts arbitrary bytes. It never parses document or executable
formats in the credential-bearing daemon process.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from sbxloop.api.auth.deps import Authenticated, current_member, get_ctx, require
from sbxloop.api.channel_files import MAX_FILE_BYTES, ChannelInputFile
from sbxloop.api.collaboration import CollaborationError, Member
from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import ApiModel, rfc3339
from sbxloop.hostfiles import make_private

router = APIRouter(prefix="/v1/channels/{channel_id}/files", tags=["collaboration"])
CHUNK = 1 << 20


class FileReserve(ApiModel):
    client_upload_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=1024)
    size: int | None = Field(default=None, ge=0)


class ChannelFileOut(ApiModel):
    id: str
    channel_id: str
    name: str
    size: int | None
    sha256: str | None
    status: str
    message_id: str | None
    created_at: str
    uploaded_at: str | None


class ChannelFilePage(ApiModel):
    data: list[ChannelFileOut]


def _out(file: ChannelInputFile) -> ChannelFileOut:
    return ChannelFileOut(
        id=file.id,
        channel_id=file.channel_id,
        name=file.display_name,
        size=file.size,
        sha256=file.sha256,
        status=file.status,
        message_id=file.message_id,
        created_at=rfc3339(file.created_at) or "",
        uploaded_at=rfc3339(file.uploaded_at),
    )


def _problem(exc: CollaborationError) -> Problem:
    status = 404 if exc.code.endswith("not_found") else 409
    if exc.code in {"invalid_file_name", "invalid_upload_id"}:
        status = 422
    elif exc.code in {"file_too_large", "storage_limit"}:
        status = 413
    elif exc.code == "channel_forbidden":
        status = 403
    return Problem(status, exc.code, exc.message)


@router.post("", response_model=ChannelFileOut, status_code=201)
async def reserve_file(
    channel_id: str,
    body: FileReserve,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("collaboration:delegate")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> ChannelFileOut:
    if not ctx.ready.is_set() or ctx.stopping.is_set():
        raise Problem(503, "daemon_not_ready", "the daemon is not ready for uploads")
    try:
        file = await ctx.call(
            ctx.channel_files.reserve,
            member,
            channel_id,
            client_upload_id=body.client_upload_id,
            display_name=body.name,
            declared_size=body.size,
            now=ctx.clock(),
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    return _out(file)


@router.put("/{file_id}/content", response_model=ChannelFileOut)
async def upload_file(
    channel_id: str,
    file_id: str,
    request: Request,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("collaboration:delegate")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> ChannelFileOut:
    if not ctx.ready.is_set() or ctx.stopping.is_set():
        raise Problem(503, "daemon_not_ready", "the daemon is not ready for uploads")
    try:
        existing = await ctx.call(ctx.channel_files.get, member, channel_id, file_id)
    except CollaborationError as exc:
        raise _problem(exc) from exc
    if existing.uploader_id != member.user.id:
        raise Problem(404, "input_file_not_found", "file not found")
    root = await ctx.call(ctx.channel_files.directory)
    fd, raw_path = tempfile.mkstemp(prefix="input-", suffix=".part", dir=root)
    temporary = Path(raw_path)
    try:
        make_private(temporary, os_name=ctx.config.paths.os_name)
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise Problem(
                        413, "file_too_large", f"files are limited to {MAX_FILE_BYTES} bytes"
                    )
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            file = await ctx.call(
                ctx.channel_files.complete,
                member,
                channel_id,
                file_id,
                temporary,
                size=size,
                sha256=digest.hexdigest(),
                now=ctx.clock(),
            )
        except CollaborationError as exc:
            raise _problem(exc) from exc
        return _out(file)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


@router.get("", response_model=ChannelFilePage)
async def list_files(
    channel_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> ChannelFilePage:
    try:
        files = await ctx.call(ctx.channel_files.list_committed, member, channel_id)
    except CollaborationError as exc:
        raise _problem(exc) from exc
    return ChannelFilePage(data=[_out(file) for file in files])


@router.get("/{file_id}", response_model=ChannelFileOut)
async def get_file(
    channel_id: str,
    file_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> ChannelFileOut:
    try:
        file = await ctx.call(ctx.channel_files.get, member, channel_id, file_id)
    except CollaborationError as exc:
        raise _problem(exc) from exc
    return _out(file)


@router.get("/{file_id}/content")
async def download_file(
    channel_id: str,
    file_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> StreamingResponse:
    try:
        file = await ctx.call(ctx.channel_files.get, member, channel_id, file_id)
    except CollaborationError as exc:
        raise _problem(exc) from exc
    if file.status not in {"uploaded", "attached"} or file.size is None or file.sha256 is None:
        raise Problem(409, "file_not_uploaded", "file bytes have not been uploaded")
    try:
        handle = await ctx.call(ctx.channel_files.open_original, file_id, file.size)
    except OSError as exc:
        raise Problem(410, "file_gone", "file bytes are no longer available") from exc

    async def body() -> AsyncIterator[bytes]:
        try:
            while chunk := await ctx.call(handle.read, CHUNK):
                yield chunk
        finally:
            handle.close()

    return StreamingResponse(
        body(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": (
                "attachment; filename=\"download\"; filename*=UTF-8''"
                + quote(file.display_name, safe="")
            ),
            "Content-Length": str(file.size),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "X-Checksum-Sha256": file.sha256,
        },
    )


@router.delete("/{file_id}", status_code=204)
async def cancel_file(
    channel_id: str,
    file_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("collaboration:delegate")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> None:
    try:
        await ctx.call(ctx.channel_files.cancel, member, channel_id, file_id, ctx.clock())
    except CollaborationError as exc:
        raise _problem(exc) from exc
