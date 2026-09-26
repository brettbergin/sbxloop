"""A person's devices and the notifications pushed to them.

Feature ``push.apns_relay``. Every route acts for the signed-in person
only: a device or a notification that is somebody else's is
``404``, the same as one that does not exist.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from sbxloop.api.auth.deps import Authenticated, current_member, get_ctx, require
from sbxloop.api.collaboration import Member
from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import rfc3339
from sbxloop.api.push import PushRefusal
from sbxloop.api.push.schemas import (
    DeviceIn,
    DeviceOut,
    DevicePage,
    DevicePrefs,
    PushNotificationOut,
    PushTestOut,
)
from sbxloop.api.push.store import Device, Notification

router = APIRouter(prefix="/v1/users/me", tags=["push"])

_PROBLEM = {"description": "A refusal, as `application/problem+json`."}


def _problem(exc: PushRefusal) -> Problem:
    return Problem(exc.status, exc.code, exc.detail, **exc.extra)


def _device_out(device: Device) -> DeviceOut:
    return DeviceOut.model_validate(
        {
            "id": device.id,
            "platform": device.platform,
            "env": device.env,
            "server_ref": device.server_ref,
            "name": device.name,
            "prefs": DevicePrefs.model_validate(device.prefs),
            "token_suffix": device.token_suffix,
            "created_at": rfc3339(device.created_at),
            "updated_at": rfc3339(device.updated_at),
            "last_push_at": rfc3339(device.last_push_at),
        }
    )


def _notification_out(notification: Notification) -> PushNotificationOut:
    return PushNotificationOut.model_validate(
        {
            "ref": notification.ref,
            "kind": notification.kind,
            "channel_id": notification.channel_id,
            "turn_id": notification.turn_id,
            "title": notification.title,
            "body": notification.body,
            "created_at": rfc3339(notification.created_at),
        }
    )


@router.post(
    "/devices",
    response_model=DeviceOut,
    status_code=201,
    summary="Register a device for push notifications",
    responses={
        200: {"model": DeviceOut, "description": "The device was already registered; updated."},
        409: _PROBLEM,
        502: _PROBLEM,
        503: _PROBLEM,
    },
)
async def register_device(
    body: DeviceIn,
    response: Response,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> DeviceOut:
    """Register the device, or update the one already registered with this
    token (`201` created, `200` updated). A new device, or a change of
    `env`, is enrolled with the push relay before this answers; a relay that
    refuses or cannot be reached is `502`, push switched off `503`."""
    try:
        device, created = await ctx.call(ctx.push.register, member.user.id, body)
    except PushRefusal as exc:
        raise _problem(exc) from exc
    response.status_code = 201 if created else 200
    return _device_out(device)


@router.get("/devices", response_model=DevicePage, summary="List your devices")
async def list_devices(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> DevicePage:
    devices = await ctx.call(ctx.push.list_devices, member.user.id)
    return DevicePage(items=[_device_out(device) for device in devices])


@router.delete(
    "/devices/{device_id}",
    status_code=204,
    summary="Remove a device",
    responses={404: _PROBLEM},
)
async def delete_device(
    device_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> Response:
    if not await ctx.call(ctx.push.remove, member.user.id, device_id):
        raise Problem(404, "device_not_found", "device not found")
    return Response(status_code=204)


@router.post(
    "/devices/{device_id}/test",
    response_model=PushTestOut,
    status_code=202,
    summary="Send a test push to one device",
    responses={404: _PROBLEM, 409: _PROBLEM, 503: _PROBLEM},
)
async def send_test_push(
    device_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> PushTestOut:
    """Queue a `test` push to this device, whatever its preferences; its
    notification reads "Test notification"."""
    try:
        ref = await ctx.call(ctx.push.test, member.user.id, device_id)
    except PushRefusal as exc:
        raise _problem(exc) from exc
    ctx.push.dispatcher.wake()
    return PushTestOut(ref=ref)


@router.get(
    "/notifications/{ref}",
    response_model=PushNotificationOut,
    summary="Read what a push was about",
    responses={404: _PROBLEM},
)
async def get_notification(
    ref: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> PushNotificationOut:
    """The text a device shows for the push that carried `ref`."""
    notification = await ctx.call(ctx.push.notification, member.user.id, ref)
    if notification is None:
        raise Problem(404, "notification_not_found", "notification not found")
    return _notification_out(notification)
