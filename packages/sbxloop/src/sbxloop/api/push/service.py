"""What the device routes call: registration, the test push, a
notification read back — and the dispatcher that does the pushing.

Registration enrolls the token with the relay synchronously, on the
caller's executor thread, because the answer is the registration: a device
the relay refused is not registered. It happens on a new device, on a
change of ``env``, and for a device whose handle the relay stopped
recognising; an update of name or preferences does not call the relay.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from sbxloop.api.push.dispatcher import PushDispatcher
from sbxloop.api.push.relay import RelayClient, RelayError
from sbxloop.api.push.rules import NoticeRules
from sbxloop.api.push.schemas import DeviceIn
from sbxloop.api.push.store import Device, DeviceStore, Notification, default_prefs
from sbxloop.config import Config
from sbxloop.daemon.store import DaemonStore
from sbxloop.log import get_logger

log = get_logger(__name__)

TEST_TITLE = "Test notification"
TEST_BODY = "Notifications from this server reach this device."


class PushRefusal(Exception):
    """A request the push service will not carry out, as the route renders it."""

    def __init__(self, status: int, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra


def _unavailable() -> PushRefusal:
    return PushRefusal(
        503,
        "push_disabled",
        "push notifications are off on this server: set [push] enabled = true and "
        "[push] relay_url to the push relay's address",
    )


class PushService:
    def __init__(
        self,
        dstore: DaemonStore,
        config: Callable[[], Config],
        *,
        clock: Callable[[], float],
        agent_name: Callable[[str | None], str],
        transport: Callable[[], httpx.BaseTransport | None] = lambda: None,
    ) -> None:
        self.config = config
        self.clock = clock
        self.transport = transport
        self.devices = DeviceStore(dstore)
        self.dispatcher = PushDispatcher(
            dstore,
            self.devices,
            NoticeRules(agent_name),
            config=config,
            relay=self.relay,
            clock=clock,
        )

    def relay(self) -> RelayClient:
        """A client for the relay the config names now."""
        push = self.config().push
        return RelayClient(push.relay_url, timeout_s=push.timeout_s, transport=self.transport())

    @property
    def available(self) -> bool:
        return self.config().push.available

    # -- registration ----------------------------------------------------------------

    def register(self, user_id: str, body: DeviceIn) -> tuple[Device, bool]:
        """Register or update the caller's device; ``(device, created)``."""
        if not self.available:
            raise _unavailable()
        token = body.token.lower()
        env = body.env.value
        prefs = body.prefs.model_dump(mode="json") if body.prefs is not None else None
        existing = self.devices.by_token(user_id, token)
        if existing is None:
            cap = self.config().push.max_devices_per_user
            if self.devices.count(user_id) >= cap:
                raise PushRefusal(
                    409,
                    "device_limit_reached",
                    f"a person may register at most {cap} devices; remove one first",
                    limit=cap,
                )
            enrolled = self._enroll(token, env)
            created = self.devices.insert(
                user_id,
                platform=body.platform.value,
                token=token,
                env=env,
                server_ref=body.server_ref,
                name=body.name,
                prefs=prefs if prefs is not None else default_prefs(),
                handle=enrolled,
                now=self.clock(),
            )
            if created is not None:
                log.info("push.device_registered", device=created.id, user=user_id, env=env)
                return created, True
            # A concurrent registration of the same token won; update it.
            existing = self.devices.by_token(user_id, token)
            if existing is None:  # pragma: no cover - deleted between the two
                raise PushRefusal(409, "device_conflict", "the device changed; register again")
        handle = None
        if existing.env != env or not existing.enrolled:
            handle = self._enroll(token, env)
        updated = self.devices.update(
            existing.id,
            env=env,
            server_ref=body.server_ref,
            name=body.name,
            prefs=prefs if prefs is not None else existing.prefs,
            handle=handle,
            now=self.clock(),
        )
        if updated is None:  # pragma: no cover - deleted while updating
            raise PushRefusal(409, "device_conflict", "the device changed; register again")
        log.info(
            "push.device_updated",
            device=updated.id,
            user=user_id,
            env=env,
            enrolled=handle is not None,
        )
        return updated, False

    def _enroll(self, token: str, env: str) -> str:
        try:
            return self.relay().enroll(token, env)
        except RelayError as exc:
            log.warning("push.enroll_failed", kind=exc.kind, detail=exc.detail)
            raise PushRefusal(
                502,
                "push_relay_refused" if exc.kind == "refused" else "push_relay_unavailable",
                exc.detail,
            ) from exc

    # -- reads and the test push -----------------------------------------------------

    def list_devices(self, user_id: str) -> list[Device]:
        return self.devices.for_user(user_id)

    def remove(self, user_id: str, device_id: str) -> bool:
        removed = self.devices.delete(user_id, device_id)
        if removed:
            log.info("push.device_removed", device=device_id, user=user_id)
        return removed

    def test(self, user_id: str, device_id: str) -> str:
        """Queue a test push to one of the caller's devices; its ref."""
        if not self.available:
            raise _unavailable()
        device = self.devices.get(user_id, device_id)
        target = self.devices.target(device_id) if device is not None else None
        if device is None:
            raise PushRefusal(404, "device_not_found", "device not found")
        if target is None:
            raise PushRefusal(
                409,
                "device_not_enrolled",
                "the push relay no longer recognises this device; register it again",
            )
        ref = self.devices.record(
            user_id=user_id,
            kind="test",
            channel_id=None,
            turn_id=None,
            title=TEST_TITLE,
            body=TEST_BODY,
            event_seq=None,
            now=self.clock(),
        )
        self.dispatcher.enqueue_test(target, ref)
        log.info("push.test_queued", device=device_id, ref=ref)
        return ref

    def notification(self, user_id: str, ref: str) -> Notification | None:
        return self.devices.notification(user_id, ref)

    # -- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        self.dispatcher.start()

    def stop(self) -> None:
        self.dispatcher.stop()


__all__ = ["TEST_BODY", "TEST_TITLE", "PushRefusal", "PushService"]
