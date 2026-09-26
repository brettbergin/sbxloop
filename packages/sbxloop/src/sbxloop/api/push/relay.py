"""The push relay's HTTP API, as sbxloop calls it.

Two calls. ``POST /v1/enroll`` hands the relay a device token and gets back
an opaque handle that can only ever address that token. ``POST /v1/push``
hands it a handle and a payload of references — the kind, a notification
ref, a thread id, the client's name for this server — never free text: the
relay builds the visible alert itself. Neither the token nor the handle is
logged here or anywhere a caller reports on a result.

Every answer is classified, not raised: a push that failed says whether it
is worth trying again (a 502 the relay does not call final, a 429, a
transport error), whether the device is gone for good (410), or neither
(any 400). ``Retry-After`` travels with a 429.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx

from sbxloop.log import get_logger

log = get_logger(__name__)

PushOutcome = Literal["sent", "retry", "unregistered", "refused", "invalid_handle"]


class RelayError(Exception):
    """Enrollment failed: ``refused`` (the relay said no) or ``unavailable``
    (it could not be reached, or answered with nothing usable)."""

    def __init__(self, kind: Literal["refused", "unavailable"], detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True, slots=True)
class PushResult:
    outcome: PushOutcome
    status: int | None = None
    #: The relay's own error code, when it gave one.
    error: str | None = None
    #: Seconds the relay asked to wait (429 ``Retry-After``).
    retry_after: float | None = None


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class RelayClient:
    """Calls one relay. ``transport`` is for tests; the daemon passes none."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.transport = transport

    def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        with httpx.Client(
            timeout=self.timeout_s, follow_redirects=False, transport=self.transport
        ) as client:
            return client.post(self.base_url + path, json=body)

    def enroll(self, token: str, env: str) -> str:
        """The handle the relay seals ``token`` into; raises
        :class:`RelayError` when there is none to be had."""
        try:
            response = self._post("/v1/enroll", {"token": token, "env": env})
        except httpx.HTTPError as exc:
            raise RelayError(
                "unavailable", f"the push relay could not be reached: {type(exc).__name__}"
            ) from exc
        body = _json(response)
        if response.status_code == 200:
            handle = body.get("handle")
            if isinstance(handle, str) and handle:
                return handle
            raise RelayError("unavailable", "the push relay answered without a handle")
        if 400 <= response.status_code < 500:
            raise RelayError(
                "refused",
                f"the push relay refused the device ({response.status_code} "
                f"{body.get('error') or 'error'})",
            )
        raise RelayError(
            "unavailable",
            f"the push relay could not enroll the device ({response.status_code} "
            f"{body.get('error') or 'error'})",
        )

    def push(self, handle: str, payload: dict[str, str]) -> PushResult:
        """Send one push; the answer classified (see the module docstring)."""
        try:
            response = self._post("/v1/push", {"handle": handle, "payload": payload})
        except httpx.HTTPError as exc:
            return PushResult("retry", error=type(exc).__name__)
        status = response.status_code
        body = _json(response)
        error = body.get("error") if isinstance(body.get("error"), str) else None
        if status == 200:
            return PushResult("sent", status)
        if status == 410:
            return PushResult("unregistered", status, error)
        if status == 429:
            return PushResult("retry", status, error, _retry_after(response))
        if status == 400:
            if error == "invalid_handle":
                return PushResult("invalid_handle", status, error)
            return PushResult("refused", status, error)
        if status == 502:
            if body.get("retryable") is False:
                return PushResult("refused", status, error)
            return PushResult("retry", status, error, _retry_after(response))
        if status >= 500:
            return PushResult("retry", status, error, _retry_after(response))
        return PushResult("refused", status, error)


__all__ = ["PushOutcome", "PushResult", "RelayClient", "RelayError"]
