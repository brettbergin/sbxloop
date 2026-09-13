"""What work may be admitted against: the configured repositories, the
workload profiles, and the registered tool recipes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from sbxloop.api.auth.deps import Authenticated, get_ctx, require
from sbxloop.api.context import ApiContext
from sbxloop.api.models import Profile, Recipe, Repository
from sbxloop.api.pagination import Page
from sbxloop.api.projections import Views

router = APIRouter(prefix="/v1", tags=["catalog"])


@router.get("/repositories", response_model=Page[Repository])
async def list_repositories(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> Page[Repository]:
    """Every configured repository with its polling health."""
    data = await ctx.call(lambda: Views(ctx).repositories())
    return Page(data=data)


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
