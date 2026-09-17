"""The workspace's people: the user directory, member administration and
invites.

Any member reads the directory. Admins and owners manage members and
invites; only an owner grants or removes the owner role or acts on an
owner; nobody deactivates or removes themselves; the workspace always keeps
an active owner. A plain API client with no member acts as an owner when it
holds ``daemon:manage`` and is refused otherwise.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response

from sbxloop.api.auth.deps import Authenticated, get_ctx, require_role, role_of
from sbxloop.api.collaboration import CollaborationError, Invite, Member
from sbxloop.api.collaboration_schemas import (
    WorkspaceInviteCreate,
    WorkspaceInviteCreated,
    WorkspaceInviteOut,
    WorkspaceInvitePage,
    WorkspaceMemberUpdate,
    WorkspaceUserOut,
    WorkspaceUserPage,
)
from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import rfc3339

router = APIRouter(prefix="/v1", tags=["workspace"])

HOUR_S = 3600

_STATUS = {
    "user_not_found": 404,
    "invite_not_found": 404,
    "owner_required": 403,
    "invalid_role": 422,
    "invalid_invite": 422,
}


def _problem(exc: CollaborationError) -> Problem:
    return Problem(_STATUS.get(exc.code, 409), exc.code, exc.message)


def _actor(auth: Authenticated) -> dict[str, Any]:
    """Who made a change, for the audit record: never a token."""
    if auth.member is not None:
        user = auth.member.user
        return {"kind": "user", "id": user.id, "display": user.username, "via": "api"}
    return {"kind": "client", "id": auth.client.id, "display": auth.client.name, "via": "api"}


def _is_self(auth: Authenticated, user_id: str) -> bool:
    return auth.member is not None and auth.member.user.id == user_id


def _self_action() -> Problem:
    return Problem(409, "self_action", "you cannot deactivate or remove yourself")


def _member_out(member: Member) -> WorkspaceUserOut:
    user = member.user
    return WorkspaceUserOut(
        id=user.id,
        username=user.username,
        email=user.email,
        full_name=user.full_name,
        avatar_url=user.avatar_url,
        role=member.role,
        is_active=user.active,
        auth_source="oidc" if user.auth_source == "oidc" else "local",
        last_seen_at=rfc3339(user.last_seen_at),
    )


def _invite_out(invite: Invite) -> WorkspaceInviteOut:
    return WorkspaceInviteOut(
        id=invite.id,
        role=invite.role,
        email=invite.email,
        expires_at=rfc3339(invite.expires_at) or "",
        accepted_at=rfc3339(invite.accepted_at),
        created_by=invite.created_by,
    )


@router.get("/users", response_model=WorkspaceUserPage)
async def list_users(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require_role("member")),  # noqa: B008
) -> WorkspaceUserPage:
    """Everyone in the workspace, active or not, oldest member first."""
    members = await ctx.call(ctx.collaboration.list_members)
    return WorkspaceUserPage(data=[_member_out(member) for member in members])


@router.patch("/workspace/members/{user_id}", response_model=WorkspaceUserOut)
async def update_member(
    user_id: str,
    body: WorkspaceMemberUpdate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require_role("admin")),  # noqa: B008
) -> WorkspaceUserOut:
    """Change a member's role or deactivate (or reactivate) them. A
    deactivated user's tokens stop working and their refresh tokens are
    revoked."""
    if body.is_active is False and _is_self(auth, user_id):
        raise _self_action()
    now = ctx.clock()
    try:
        member = await ctx.call(
            ctx.collaboration.update_member,
            user_id,
            role=body.role,
            active=body.is_active,
            owner_ok=role_of(auth) == "owner",
            actor=_actor(auth),
            now=now,
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    if not member.user.active:
        await ctx.call(ctx.auth.revoke_client_refresh, member.user.client_id, now)
    ctx.hub.notify()
    return _member_out(member)


@router.delete("/workspace/members/{user_id}", status_code=204)
async def remove_member(
    user_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require_role("admin")),  # noqa: B008
) -> Response:
    """End a membership: the user's client keeps no capability and its
    refresh tokens are revoked."""
    if _is_self(auth, user_id):
        raise _self_action()
    target = await ctx.call(ctx.collaboration.member_for_user, user_id)
    if target is None:
        raise Problem(404, "user_not_found", "user not found")
    now = ctx.clock()
    try:
        removed = await ctx.call(
            ctx.collaboration.remove_member,
            user_id,
            owner_ok=role_of(auth) == "owner",
            actor=_actor(auth),
            now=now,
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    if not removed:
        raise Problem(404, "user_not_found", "user not found")
    await ctx.call(ctx.auth.revoke_client_refresh, target.user.client_id, now)
    ctx.hub.notify()
    return Response(status_code=204)


@router.post("/workspace/invites", response_model=WorkspaceInviteCreated, status_code=201)
async def create_invite(
    body: WorkspaceInviteCreate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require_role("admin")),  # noqa: B008
) -> WorkspaceInviteCreated:
    """A new invite. Its token is in this response only; the daemon keeps a
    hash."""
    if body.role == "owner" and role_of(auth) != "owner":
        raise Problem(403, "owner_required", "only an owner may invite an owner")
    created_by = auth.member.user.id if auth.member is not None else auth.client.id
    try:
        invite, token = await ctx.call(
            ctx.collaboration.create_invite,
            body.role,
            body.email,
            created_by=created_by,
            ttl_s=float(body.ttl_hours * HOUR_S),
            now=ctx.clock(),
            actor=_actor(auth),
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    ctx.hub.notify()
    return WorkspaceInviteCreated(
        id=invite.id,
        token=token,
        expires_at=rfc3339(invite.expires_at) or "",
        role=invite.role,
        email=invite.email,
    )


@router.get("/workspace/invites", response_model=WorkspaceInvitePage)
async def list_invites(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require_role("admin")),  # noqa: B008
) -> WorkspaceInvitePage:
    """Pending and spent invites, newest first, without their tokens."""
    invites = await ctx.call(ctx.collaboration.list_invites)
    return WorkspaceInvitePage(data=[_invite_out(invite) for invite in invites])


@router.delete("/workspace/invites/{invite_id}", status_code=204)
async def revoke_invite(
    invite_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require_role("admin")),  # noqa: B008
) -> Response:
    """Withdraw an unspent invite; its token admits nobody afterwards."""
    try:
        revoked = await ctx.call(
            ctx.collaboration.revoke_invite, invite_id, actor=_actor(auth), now=ctx.clock()
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    if not revoked:
        raise Problem(404, "invite_not_found", "invite not found")
    ctx.hub.notify()
    return Response(status_code=204)
