"""Bounded, redacted diagnostics: the daemon's recent log records and the
allowlisted effective configuration (#1040)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from sbxloop.api.auth.deps import Authenticated, get_ctx, require
from sbxloop.api.context import ApiContext
from sbxloop.api.diagnostics import CONFIGURATION_SECTIONS, configuration, redact
from sbxloop.api.models import Configuration, ConfigurationEntry, LogRecord, LogTail, rfc3339
from sbxloop.daemon.control import LOG_TAIL_MAX

router = APIRouter(prefix="/v1", tags=["diagnostics"])


@router.get("/logs", response_model=LogTail)
async def get_logs(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("diagnostics:read")),  # noqa: B008
    tail: Annotated[int, Query(ge=1, le=LOG_TAIL_MAX)] = 50,
    level: Annotated[str | None, Query(max_length=16)] = None,
    grep: Annotated[str | None, Query(max_length=200)] = None,
) -> LogTail:
    """The most recent records of the daemon's in-process log ring —
    at most ``LOG_TAIL_MAX`` — at or above ``level``, containing ``grep``
    (a plain substring, never a pattern); credential-shaped text is
    masked before it leaves the host."""
    service = ctx.service()
    outcome = await ctx.call(
        lambda: service.log_records(auth.principal, tail=tail, level=level, grep=grep)
    )
    return LogTail(
        records=[
            LogRecord(
                timestamp=str(r["timestamp"]),
                level=str(r["level"]),
                logger=str(r["logger"]),
                message=redact(str(r["message"])),
            )
            for r in outcome.records
        ],
        buffer_size=outcome.buffer_size,
        tail=tail,
        level=(level or "").strip().upper() or None,
        grep=grep or None,
        observed_at=rfc3339(ctx.clock()) or "",
    )


@router.get("/configuration", response_model=Configuration)
async def get_configuration(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("diagnostics:read")),  # noqa: B008
) -> Configuration:
    """The configuration this daemon runs on, restricted to the sections a
    remote operator may read, with the layer each key comes from, whether
    a change applies live, whether the file on disk now says otherwise,
    and what keeps the daemon's own tools from changing it. Never a secret
    value, never a host path; the listener's own settings are not here."""
    rows = await ctx.call(configuration, ctx.config)
    return Configuration(
        observed_at=rfc3339(ctx.clock()) or "",
        sections=list(CONFIGURATION_SECTIONS),
        entries=[ConfigurationEntry.model_validate(row) for row in rows],
    )
