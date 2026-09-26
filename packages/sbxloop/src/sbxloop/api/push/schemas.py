"""Request and response shapes for devices and push notifications.

These are the contract a mobile client is generated from, so each model and
field says what it is for. Nothing here carries a device's push token back
out, or the relay's handle at all.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from sbxloop.api.models import ApiModel

#: A device push token as the platform issues it: hex, 32 to 100 bytes.
TOKEN_PATTERN = r"^[0-9A-Fa-f]{64,200}$"  # nosec B105 - a shape, not a secret
#: An opaque reference a client or the relay may carry: 1 to 64 of
#: letters, digits and ``_ . : -``.
SERVER_REF_PATTERN = r"^[A-Za-z0-9_.:-]{1,64}$"


class DevicePlatform(StrEnum):
    """The push platform a device registers for."""

    IOS = "ios"


class PushEnvironment(StrEnum):
    """Which push gateway issued the token: a development build's
    ``sandbox`` or a distributed build's ``production``."""

    SANDBOX = "sandbox"
    PRODUCTION = "production"


class ChannelNotify(StrEnum):
    """How much of one channel reaches a device: ``all`` of it (the
    default), only ``mentions`` of the device's owner, or ``none``."""

    ALL = "all"
    MENTIONS = "mentions"
    NONE = "none"


class NotificationKind(StrEnum):
    """What a push is about.

    ``mention``: another person named you in a channel. ``gate``: work is
    waiting on a decision you can make. ``work``: work or a reply you asked
    for arrived. ``failure``: work or a reply you asked for could not
    finish. ``test``: a test push you asked for.
    """

    MENTION = "mention"
    GATE = "gate"
    WORK = "work"
    FAILURE = "failure"
    TEST = "test"


class DevicePrefs(ApiModel):
    """Which pushes a device wants. Every kind is on by default; a channel
    absent from ``per_channel`` gets ``all``."""

    mentions: bool = Field(default=True, description="Another person mentioned you.")
    gates: bool = Field(default=True, description="Work is waiting on your decision.")
    work: bool = Field(default=True, description="Work or a reply you asked for arrived.")
    failures: bool = Field(
        default=True, description="Work or a reply you asked for could not finish."
    )
    per_channel: dict[str, ChannelNotify] = Field(
        default_factory=dict,
        max_length=500,
        description=(
            "Channel id to how much of that channel reaches this device. `none` "
            "silences the channel, `mentions` lets only mentions through."
        ),
    )


class DeviceIn(ApiModel):
    """Register a device, or update the one already registered with the same
    token. Registering enrolls the token with the push relay; so does a
    change of `env`."""

    platform: DevicePlatform = Field(description="The push platform.")
    token: str = Field(
        pattern=TOKEN_PATTERN,
        description="The device push token, hex. Stored only as a digest; never returned.",
    )
    env: PushEnvironment = Field(description="The gateway the token was issued for.")
    server_ref: str = Field(
        pattern=SERVER_REF_PATTERN,
        description=(
            "The client's own opaque name for this server, echoed in every push "
            "as `srv` so a client registered with several servers knows which "
            "one to ask."
        ),
    )
    name: str | None = Field(
        default=None, max_length=80, description="A label the person recognises."
    )
    prefs: DevicePrefs | None = Field(
        default=None,
        description=(
            "Which pushes this device wants. Omitted on a new device: everything. "
            "Omitted on an update: unchanged."
        ),
    )


class DeviceOut(ApiModel):
    """A registered device, as its owner sees it."""

    id: str = Field(description="The device id (`dev_…`).")
    platform: DevicePlatform
    env: PushEnvironment
    server_ref: str
    name: str | None
    prefs: DevicePrefs
    token_suffix: str = Field(description="The token's last six characters, to tell devices apart.")
    created_at: str
    updated_at: str
    last_push_at: str | None = Field(description="When a push last reached the relay for it.")


class DevicePage(ApiModel):
    """The caller's devices, oldest first."""

    items: list[DeviceOut]


class PushTestOut(ApiModel):
    """A test push, queued."""

    ref: str = Field(description="The test notification's ref, fetchable once pushed.")


class PushNotificationOut(ApiModel):
    """What a push was about: the text a device shows in place of the
    relay's generic alert."""

    ref: str = Field(description="The ref the push carried.")
    kind: NotificationKind
    channel_id: str | None = Field(description="The channel it happened in, when one did.")
    turn_id: str | None = Field(description="The turn it answers, when it answers one.")
    title: str
    body: str
    created_at: str


__all__ = [
    "SERVER_REF_PATTERN",
    "TOKEN_PATTERN",
    "ChannelNotify",
    "DeviceIn",
    "DeviceOut",
    "DevicePage",
    "DevicePlatform",
    "DevicePrefs",
    "NotificationKind",
    "PushEnvironment",
    "PushNotificationOut",
    "PushTestOut",
]
