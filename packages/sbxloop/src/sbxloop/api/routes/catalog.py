"""What work may be admitted against: the configured repositories, the
workload profiles, and the registered tool recipes — and, for the owner
registering a repository, what the host's forge credential could see."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from sbxloop.api.auth.deps import Authenticated, get_ctx, require, require_role
from sbxloop.api.context import ApiContext
from sbxloop.api.discovery import configured_repositories, discover
from sbxloop.api.errors import Problem
from sbxloop.api.models import (
    OpenIssue,
    OpenIssuePage,
    Profile,
    Recipe,
    Repository,
    RepositoryDiscovery,
)
from sbxloop.api.pagination import Page
from sbxloop.api.projections import Views
from sbxloop.api.routes.connections import credential_snapshot
from sbxloop.config import VcsKind
from sbxloop.errors import GithubOpsError, SbxError, WorkerError

router = APIRouter(prefix="/v1", tags=["catalog"])


@router.get("/repositories", response_model=Page[Repository])
async def list_repositories(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> Page[Repository]:
    """Every configured repository with its polling health."""
    data = await ctx.call(lambda: Views(ctx).repositories())
    return Page(data=data)


@router.get("/repositories/available", response_model=RepositoryDiscovery)
async def available_repositories(
    forge: Annotated[VcsKind | None, Query()] = None,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require_role("owner")),  # noqa: B008
) -> RepositoryDiscovery:
    """The repositories the host's forge credential can see, each marked
    with whether it is configured here already: what an owner picks from
    when registering one. Read from the forge now, on the host, with the
    same credential snapshot the connection check uses; ``forge`` defaults
    to ``[vcs] kind``."""
    config, secrets = await ctx.call(credential_snapshot, ctx)
    known = configured_repositories(ctx.config)
    return await ctx.call(discover, config, secrets, forge or config.vcs.kind, configured=known)


@router.get("/repositories/{repository_id}/issues", response_model=OpenIssuePage)
async def list_open_issues(
    repository_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
    page: Annotated[int, Query(ge=1)] = 1,
) -> OpenIssuePage:
    """Open forge issues for a configured repository, without pull requests."""

    def read() -> OpenIssuePage:
        entry = Views(ctx).repository_by_public_id(repository_id)
        if not entry.enabled:
            raise Problem(409, "repository_disabled", "This repository is disabled.")
        forge = ctx.loop.github
        if forge is None:
            raise Problem(503, "source_unavailable", "Repository issues are unavailable.")
        try:
            rows = forge.call(
                lambda ops: ops.issues_list(
                    entry.repo,
                    state="open",
                    per_page=100,
                    page=page,
                    sort="updated",
                    direction="desc",
                )
            )
        except (GithubOpsError, SbxError, WorkerError) as exc:
            raise Problem(503, "source_unavailable", "Could not load repository issues.") from exc
        data = [
            OpenIssue(number=row["number"], title=row["title"])
            for row in rows
            if isinstance(row, dict)
            and "pull_request" not in row
            and row.get("state", "open") == "open"
            and isinstance(row.get("number"), int)
            and row["number"] > 0
            and isinstance(row.get("title"), str)
        ]
        return OpenIssuePage(data=data, has_more=len(rows) == 100)

    return await ctx.call(read)


@router.get("/profiles", response_model=Page[Profile])
async def list_profiles(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> Page[Profile]:
    """The ``[[workloads]]`` profiles an inline workload may be admitted under."""
    data = await ctx.call(lambda: Views(ctx).profiles())
    return Page(data=data)


@router.get("/recipes", response_model=Page[Recipe])
async def list_recipes(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> Page[Recipe]:
    """The registered tool recipes and the parameters each takes."""
    data = await ctx.call(lambda: Views(ctx).recipes())
    return Page(data=data)
