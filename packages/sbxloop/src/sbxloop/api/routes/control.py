"""Steering a run, deciding a gate, and the run-level controls: cancel,
resume, round grants, the review wait (#1038)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from sbxloop.api.auth.deps import Authenticated, get_ctx, ready_daemon, require
from sbxloop.api.commands import approve_gate, idempotency, run_verb, steer
from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import (
    Gate,
    GateApproval,
    GateResult,
    RoundGrant,
    RunCommand,
    RunCommandResult,
    Steering,
    SteerRequest,
    SteerResult,
)
from sbxloop.api.pagination import Page
from sbxloop.api.projections import Views
from sbxloop.daemon.controls.steering import SteeringStore

router = APIRouter(prefix="/v1", tags=["control"])

GATE_STATES = ("open", "approving", "merged", "released", "dismissed")


# -- runs ------------------------------------------------------------------------


@router.post("/runs/{run_id}/cancel", response_model=RunCommandResult, status_code=202)
async def cancel_run(
    run_id: str,
    request: Request,
    response: Response,
    body: RunCommand | None = None,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("runs:control")),  # noqa: B008
) -> RunCommandResult:
    """Cancel this run wherever the daemon holds it — never another run
    that happens to be current. A run in flight stops at its next
    boundary (``202``: the operation finishes when it does); a parked one
    is settled now. ``expected_revision`` refuses a cancel meant for an
    earlier state."""
    pair = idempotency(request, auth.principal, f"/v1/runs/{run_id}/cancel", required=False)
    result = await run_verb(ctx, auth, "cancel", run_id, body, pair)
    response.headers["Location"] = f"/v1/operations/{result.operation.id}"
    if result.operation.state != "running":
        response.status_code = 200
    return result


@router.post("/runs/{run_id}/resume", response_model=RunCommandResult)
async def resume_run(
    run_id: str,
    request: Request,
    body: RunCommand | None = None,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("runs:control")),  # noqa: B008
) -> RunCommandResult:
    """Admit a persisted run to the daemon's own queue for resume: the
    next tick continues it through the same gates a fresh dispatch faces.
    Never a second engine."""
    pair = idempotency(request, auth.principal, f"/v1/runs/{run_id}/resume", required=False)
    return await run_verb(ctx, auth, "resume", run_id, body, pair)


@router.post("/runs/{run_id}/round-grants", response_model=RunCommandResult)
async def grant_rounds(
    run_id: str,
    body: RoundGrant,
    request: Request,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("budgets:grant")),  # noqa: B008
) -> RunCommandResult:
    """Grant a run that exhausted its fix rounds a bounded number more,
    and re-admit it on its own pull request."""
    pair = idempotency(request, auth.principal, f"/v1/runs/{run_id}/round-grants", required=False)
    return await run_verb(ctx, auth, "grant_rounds", run_id, body, pair)


@router.post("/runs/{run_id}/review-wait/resume", response_model=RunCommandResult)
async def resume_review_wait(
    run_id: str,
    request: Request,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("runs:control")),  # noqa: B008
) -> RunCommandResult:
    """Re-arm the wait for an external review and check the pull request
    now; the review itself stays a person's on the forge."""
    pair = idempotency(
        request, auth.principal, f"/v1/runs/{run_id}/review-wait/resume", required=False
    )
    return await run_verb(ctx, auth, "review_resume", run_id, None, pair)


# -- steering --------------------------------------------------------------------


@router.post("/runs/{run_id}/steering", response_model=SteerResult, status_code=202)
async def submit_steering(
    run_id: str,
    body: SteerRequest,
    request: Request,
    response: Response,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("runs:steer")),  # noqa: B008
) -> SteerResult:
    """Submit explicit direction to the run in flight. Acceptance means
    the instruction was durably handed to the run's input path; the
    agent's reply and any course change follow on the record and in the
    chronology. Refused for a run that is not in flight or a tool run."""
    pair = idempotency(request, auth.principal, f"/v1/runs/{run_id}/steering", required=False)
    result = await steer(ctx, auth, run_id, body, pair)
    response.headers["Location"] = f"/v1/runs/{run_id}/steering"
    return result


@router.get("/runs/{run_id}/steering", response_model=Page[Steering])
async def list_steering(
    run_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> Page[Steering]:
    """Every instruction submitted to the run, oldest first, with its
    delivery and handling status."""

    def read() -> Page[Steering]:
        views = Views(ctx)
        record = views.run_by_public_id(run_id)
        # The agent's replies settle the records as they are projected:
        # project first, so the listing is as current as the chronology.
        ctx.chronology.project(views.now)
        rows = SteeringStore(views.dstore).for_run(record.run_id)
        return Page(data=[views.steering(r) for r in rows])

    return await ctx.call(read)


# -- gates -----------------------------------------------------------------------


@router.get("/gates", response_model=Page[Gate])
async def list_gates(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
    state: Annotated[list[str] | None, Query()] = None,
) -> Page[Gate]:
    """Outstanding and resolved merge and publication gates, newest first."""
    states = [s for s in state or [] if s in GATE_STATES]
    if state and len(states) != len(state):
        raise Problem(422, "invalid_request", f"state must be one of {', '.join(GATE_STATES)}")

    def read() -> Page[Gate]:
        views = Views(ctx)
        return Page(data=views.gates(views.dstore.merge_gates(states or None)))

    return await ctx.call(read)


@router.get("/gates/{gate_id}", response_model=Gate)
async def get_gate(
    gate_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> Gate:
    """The gate's revision, subject, required authority and available actions."""

    def read() -> Gate:
        views = Views(ctx)
        return views.gate(views.gate_by_public_id(gate_id))

    return await ctx.call(read)


@router.post("/gates/{gate_id}/approve", response_model=GateResult, status_code=202)
async def approve(
    gate_id: str,
    body: GateApproval,
    request: Request,
    response: Response,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("gates:approve")),  # noqa: B008
) -> GateResult:
    """Endorse and release the gate at exactly the revision the person
    saw (``expected_revision`` is required; a moved gate is
    ``409 stale_revision``, a second approval ``409 already_in_progress``).
    Success is the recorded approval and the committed release; the merge
    or publication completes afterwards and is a separate event."""
    pair = idempotency(request, auth.principal, f"/v1/gates/{gate_id}/approve", required=False)
    result = await approve_gate(ctx, auth, gate_id, body, pair)
    response.headers["Location"] = f"/v1/operations/{result.operation.id}"
    return result
