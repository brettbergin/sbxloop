"""Push notifications to people's mobile devices, through a push relay.

The relay holds the push provider's key and nothing else: sbxloop enrolls a
device token with it for an opaque handle, and pings through that handle
with references only — a kind, a notification ref, a thread id. What the
ping is about stays on this server; the device fetches it here. See
:mod:`~sbxloop.api.push.dispatcher` for what is pushed and when.
"""

from __future__ import annotations

from sbxloop.api.push.service import PushRefusal, PushService

__all__ = ["PushRefusal", "PushService"]
