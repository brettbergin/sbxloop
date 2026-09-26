"""The push relay every push test runs against, spoken to over HTTP.

It implements the relay's two routes the way the relay does: ``POST
/v1/enroll`` seals a device token into an opaque handle, and ``POST
/v1/push`` opens the handle, checks the payload's shape and records what
it would have sent to the push provider. The handle here is an HMAC-keyed
envelope rather than the real AES-GCM one — what matters to sbxloop is
that it is opaque, bound to one token, and refused when tampered with.

A test scripts failures with :meth:`FakeRelay.fail_next` (a status, an
error body, optional headers) or :meth:`FakeRelay.unreachable`; every
request it saw is kept, parsed, in ``requests``. It is mounted as an
``httpx.MockTransport`` so the code under test uses its real HTTP client.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

_SECRET = b"fake-relay-secret-for-tests-only-32b"
_TOKEN = re.compile(r"^[0-9a-fA-F]{64,200}$")
_FIELD = re.compile(r"^[A-Za-z0-9_.:-]{0,128}$")
KINDS = frozenset({"mention", "gate", "work", "failure", "test"})


@dataclass
class Scripted:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)
    route: str | None = None


@dataclass
class Sent:
    """One push the relay accepted: the token it opened and the payload."""

    token: str
    env: str
    payload: dict[str, Any]


class FakeRelay:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.sent: list[Sent] = []
        self._script: list[Scripted] = []
        self._unreachable = 0
        #: Tokens the "push provider" reports as no longer registered.
        self.unregistered: set[str] = set()
        self.transport = httpx.MockTransport(self._handle)

    # -- scripting ---------------------------------------------------------------

    def fail_next(
        self,
        status: int,
        body: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        route: str | None = None,
        times: int = 1,
    ) -> None:
        for _ in range(times):
            self._script.append(Scripted(status, body, dict(headers or {}), route))

    def unreachable(self, times: int = 1) -> None:
        self._unreachable += times

    # -- the handle --------------------------------------------------------------

    @staticmethod
    def seal(token: str, env: str) -> str:
        body = json.dumps({"t": token.lower(), "e": env}).encode()
        tag = hmac.new(_SECRET, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(b"\x01" + tag + body).decode().rstrip("=")

    @staticmethod
    def open(handle: str) -> tuple[str, str] | None:
        try:
            raw = base64.urlsafe_b64decode(handle + "=" * (-len(handle) % 4))
        except (ValueError, TypeError):
            return None
        if len(raw) < 34 or raw[0] != 1:
            return None
        tag, body = raw[1:33], raw[33:]
        if not hmac.compare_digest(tag, hmac.new(_SECRET, body, hashlib.sha256).digest()):
            return None
        data = json.loads(body)
        return str(data["t"]), str(data["e"])

    # -- the routes --------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        route = request.url.path
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            body = {}
        self.requests.append((route, body))
        if self._unreachable:
            self._unreachable -= 1
            raise httpx.ConnectError("relay unreachable", request=request)
        for index, scripted in enumerate(self._script):
            if scripted.route in (None, route):
                del self._script[index]
                return httpx.Response(scripted.status, json=scripted.body, headers=scripted.headers)
        if request.method == "POST" and route.endswith("/v1/enroll"):
            return self._enroll(body)
        if request.method == "POST" and route.endswith("/v1/push"):
            return self._push(body)
        return httpx.Response(404, json={"error": "not_found"})

    def _enroll(self, body: dict[str, Any]) -> httpx.Response:
        token, env = body.get("token"), body.get("env")
        if not isinstance(token, str) or not _TOKEN.match(token):
            return httpx.Response(400, json={"error": "invalid_request", "detail": "token"})
        if env not in ("sandbox", "production"):
            return httpx.Response(400, json={"error": "invalid_request", "detail": "env"})
        return httpx.Response(200, json={"handle": self.seal(token, str(env))})

    def _push(self, body: dict[str, Any]) -> httpx.Response:
        handle, payload = body.get("handle"), body.get("payload")
        if not isinstance(handle, str) or not isinstance(payload, dict):
            return httpx.Response(400, json={"error": "invalid_request"})
        if set(payload) != {"srv", "k", "ref", "thread"} or payload["k"] not in KINDS:
            return httpx.Response(400, json={"error": "invalid_request"})
        for name in ("srv", "ref", "thread"):
            value = payload[name]
            if not isinstance(value, str) or not _FIELD.match(value):
                return httpx.Response(400, json={"error": "invalid_request"})
        if not payload["srv"] or not payload["ref"]:
            return httpx.Response(400, json={"error": "invalid_request"})
        opened = self.open(handle)
        if opened is None:
            return httpx.Response(400, json={"error": "invalid_handle"})
        token, env = opened
        if token in self.unregistered:
            return httpx.Response(410, json={"error": "unregistered"})
        self.sent.append(Sent(token=token, env=env, payload=dict(payload)))
        return httpx.Response(200, json={"status": "sent", "apns_id": f"apns-{len(self.sent)}"})

    # -- reading -----------------------------------------------------------------

    def pushes(self) -> list[dict[str, Any]]:
        """The payloads of every push accepted, oldest first."""
        return [sent.payload for sent in self.sent]
