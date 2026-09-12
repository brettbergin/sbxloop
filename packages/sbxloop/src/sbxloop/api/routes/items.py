"""Work items: what the daemon has been asked to do, the queue in dispatch
order, and admitting or settling an item (#1036)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, get_args

from fastapi import APIRouter, Depends, Query, Request, Response

from sbxloop.api.auth.deps import Authenticated, get_ctx, ready_daemon, require
from sbxloop.api.commands import Replayed, idempotency, replayed_problem, run_command
from sbxloop.api.context import PAGE_DEFAULT, PAGE_MAX, ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import (
    Admitted,
    IntakeRequest,
    IssueIntake,
    Item,
    ItemCommand,
    ItemCommandResult,
    ItemDetail,
    OperationOut,
    QueuePage,
    ToolIntake,
    WorkloadIntake,
    rfc3339,
)
from sbxloop.api.pagination import Page, decode_cursor, encode_cursor
from sbxloop.api.projections import Views, not_found
from sbxloop.daemon.controls.intake import (
    AdmitRequest,
    IssueAdmission,
    ToolAdmission,
    WorkloadAdmission,
)
from sbxloop.daemon.controls.operations import Operation
from sbxloop.daemon.controls.results import AdmitOutcome, ItemOutcome
from sbxloop.daemon.model import ItemState, WorkItem

router = APIRouter(prefix="/v1", tags=["items"])

ITEM_STATES: tuple[str, ...] = get_args(ItemState)
RUN_KINDS: tuple[str, ...] = ("code", "workload", "tool")


def _operation(ctx: ApiContext, op_id: str | None) -> Operation:
    store = getattr(ctx.loop, "operations", None)
    op: Operation | None = store.get(op_id) if store is not None and op_id else None
    if op is None:
        raise Problem(503, "daemon_not_ready", "the daemon keeps no operation record")
    return op


@router.get("/items", response_model=Page[Item])
async def list_items(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
    state: Annotated[list[str] | None, Query()] = None,
    kind: Annotated[list[str] | None, Query()] = None,
    repository_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
    cursor: Annotated[str | None, Query()] = None,
) -> Page[Item]:
    """Work items, newest first, filterable by state, kind and repository."""
    states = [s for s in state or [] if s in ITEM_STATES]
    if state and len(states) != len(state):
        raise Problem(422, "invalid_request", f"state must be one of {', '.join(ITEM_STATES)}")
    kinds = [k for k in kind or [] if k in RUN_KINDS]
    if kind and len(kinds) != len(kind):
        raise Problem(422, "invalid_request", f"kind must be one of {', '.join(RUN_KINDS)}")
    filters: dict[str, Any] = {
        "state": sorted(states),
        "kind": sorted(kinds),
        "repository_id": repository_id,
    }
    after: tuple[float, str] | None = None
    if cursor is not None:
        key = decode_cursor(cursor, filters)
        try:
            after = (float(key["c"]), str(key["i"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise Problem(400, "invalid_cursor", "the cursor is malformed") from exc

    def read() -> tuple[list[Item], list[WorkItem], bool]:
        views = Views(ctx)
        repo = views.repository_by_public_id(repository_id).repo if repository_id else None
        rows: list[WorkItem] = views.dstore.page_items(
            states=states or None, kinds=kinds or None, repo=repo, after=after, limit=limit + 1
        )
        more = len(rows) > limit
        rows = rows[:limit]
        return views.items(rows), rows, more

    data, rows, more = await ctx.call(read)
    next_cursor = (
        encode_cursor({"c": rows[-1].created_at, "i": rows[-1].item_id}, filters)
        if more and rows
        else None
    )
    return Page(data=data, next_cursor=next_cursor, has_more=more)


@router.get("/items/{item_id}", response_model=ItemDetail)
async def get_item(
    item_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> ItemDetail:
    """The request, its origin, its state, and every run it had."""

    def read() -> ItemDetail:
        views = Views(ctx)
        return views.item_detail(views.item_by_public_id(item_id))

    return await ctx.call(read)


@router.get("/queue", response_model=QueuePage)
async def queue(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
) -> QueuePage:
    """The queue in the order dispatch considers it — interrupted runs
    awaiting resume first, then oldest first — with each entry's own
    eligibility and what holds the whole queue."""

    def read() -> QueuePage:
        views = Views(ctx)
        entries, more = views.queue(limit)
        status = views.status()
        return QueuePage(
            data=entries,
            has_more=more,
            observed_at=rfc3339(views.now) or "",
            paused=bool(status.get("paused")),
            breaker_open=bool(status.get("breaker_open")),
        )

    return await ctx.call(read)


# -- intake ----------------------------------------------------------------------


def _admission(ctx: ApiContext, body: IssueIntake | WorkloadIntake | ToolIntake) -> AdmitRequest:
    """The service's request for the route's body; a public repository id
    is resolved here, on the executor."""
    if isinstance(body, IssueIntake):
        if (body.repository_id is None) == (body.repository is None):
            raise Problem(
                422, "invalid_request", "name the repository by `repository_id` or `repository`"
            )
        repo = body.repository
        if body.repository_id is not None:
            repo = Views(ctx).repository_by_public_id(body.repository_id).repo
        assert repo is not None  # nosec B101 - one of the two was given
        run_kind: Literal["code", "workload"] = body.run_kind
        return IssueAdmission(repository=repo, number=body.number, run_kind=run_kind)
    if isinstance(body, WorkloadIntake):
        return WorkloadAdmission(ask=body.ask, profile=body.profile, sink=body.sink)
    return ToolAdmission(recipe=body.recipe, parameters=dict(body.parameters))


@router.post("/items", response_model=Admitted, status_code=201)
async def admit_item(
    body: IntakeRequest,
    request: Request,
    response: Response,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("items:create")),  # noqa: B008
) -> Admitted:
    """Admit work through its source's rules: an existing issue in a
    configured repository (labelled as a person would, so polling
    converges on the same item), an inline workload under an allowed
    profile, or a registered tool recipe with validated parameters. An
    ``Idempotency-Key`` is required; a replay answers with the same item
    and operation. ``201`` when the item was created, ``200`` when it was
    already queued."""
    principal = auth.principal
    pair = idempotency(request, principal, "/v1/items", required=True)
    service = ctx.service()

    def apply() -> AdmitOutcome:
        return service.admit(principal, _admission(ctx, body), idempotency=pair)

    try:
        outcome = await run_command(ctx, apply)
    except Replayed as replay:
        existing = replay.operation
        problem = replayed_problem(existing)
        if problem is not None:
            raise problem from replay
        result = existing.result or {}
        item_id = str((result.get("item") or {}).get("item_id") or "")

        def reread() -> tuple[Item, Operation]:
            views = Views(ctx)
            item = views.dstore.get(item_id) if item_id else None
            if item is None:
                raise not_found()
            return views.item(item), existing

        view, op = await ctx.call(reread)
        response.status_code = 200
        response.headers["Location"] = f"/v1/items/{view.id}"
        return Admitted(item=view, operation=OperationOut.from_operation(op), created=False)

    def project() -> tuple[Item, Operation]:
        return Views(ctx).item(outcome.item), _operation(ctx, outcome.operation_id)

    view, op = await ctx.call(project)
    response.status_code = 201 if outcome.fresh else 200
    response.headers["Location"] = f"/v1/items/{view.id}"
    return Admitted(item=view, operation=OperationOut.from_operation(op), created=outcome.fresh)


# -- item commands ---------------------------------------------------------------


async def _item_command(
    verb: Literal["retry", "requeue", "abandon"],
    public_id: str,
    body: ItemCommand | None,
    request: Request,
    ctx: ApiContext,
    auth: Authenticated,
) -> ItemCommandResult:
    principal = auth.principal
    pair = idempotency(request, principal, f"/v1/items/{public_id}/{verb}", required=False)
    command = body or ItemCommand()
    service = ctx.service()

    def apply() -> ItemOutcome:
        views = Views(ctx)
        item = views.item_by_public_id(public_id)
        if verb == "abandon":
            return service.abandon(
                principal,
                item.item_id,
                command.reason,
                expected_revision=command.expected_revision,
                idempotency=pair,
            )
        if verb == "retry":
            return service.retry(
                principal,
                item.item_id,
                expected_revision=command.expected_revision,
                idempotency=pair,
            )
        return service.requeue(
            principal, item.item_id, expected_revision=command.expected_revision, idempotency=pair
        )

    try:
        outcome = await run_command(ctx, apply)
    except Replayed as replay:
        existing = replay.operation
        problem = replayed_problem(existing)
        if problem is not None:
            raise problem from replay

        def reread() -> ItemCommandResult:
            views = Views(ctx)
            return ItemCommandResult(
                item=views.item(views.item_by_public_id(public_id)),
                operation=OperationOut.from_operation(existing),
            )

        return await ctx.call(reread)

    def project() -> ItemCommandResult:
        return ItemCommandResult(
            item=Views(ctx).item(outcome.item),
            operation=OperationOut.from_operation(_operation(ctx, outcome.operation_id)),
        )

    return await ctx.call(project)


@router.post("/items/{item_id}/retry", response_model=ItemCommandResult)
async def retry_item(
    item_id: str,
    request: Request,
    body: ItemCommand | None = None,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("runs:control")),  # noqa: B008
) -> ItemCommandResult:
    """A fresh attempt for a settled item: attempts reset, the run
    unpinned, the source told who asked."""
    return await _item_command("retry", item_id, body, request, ctx, auth)


@router.post("/items/{item_id}/requeue", response_model=ItemCommandResult)
async def requeue_item(
    item_id: str,
    request: Request,
    body: ItemCommand | None = None,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("runs:control")),  # noqa: B008
) -> ItemCommandResult:
    """Unpin the item's run so its next dispatch starts fresh, attempts
    intact; the run in flight is cancelled."""
    return await _item_command("requeue", item_id, body, request, ctx, auth)


@router.post("/items/{item_id}/abandon", response_model=ItemCommandResult)
async def abandon_item(
    item_id: str,
    request: Request,
    body: ItemCommand | None = None,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("runs:control")),  # noqa: B008
) -> ItemCommandResult:
    """Give the item up with an attributed reason; the source hears the
    ordinary abandon report."""
    return await _item_command("abandon", item_id, body, request, ctx, auth)
