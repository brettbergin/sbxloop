"""Copilot provider envelopes, isolated from ordinary task/tool content.

Field-verified against github-copilot-sdk 1.0.13 (the declared 1.0.8 floor
carries the same fields): ``SessionErrorData`` has ``error_type``,
``message``, ``error_code``, ``status_code``, ``remediation`` and
``eligible_for_auto_switch``; ``AutoModeSwitchRequestedData`` carries
``retry_after_seconds``; ``ModelCallFailureData`` reports one model call,
not a session outcome. ``CopilotSession.send_and_wait`` collapses a
terminal ``SessionErrorData`` into ``Exception(f"Session error: ...")``,
so the adapter must read the event before the SDK loses it.

Classification is deliberately narrow. A rejection this cannot place
returns ``None`` and keeps the caller's ordinary failure path: the run
stops either way, and a misfiled category would park a credential that is
not actually throttled. Provider facts outrank HTTP status, so a quota or
billing rejection served as 429 is not treated as a transient throttle.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from typing import Any, Final

from sbxloop_worker.protocol import ProviderFailure

BACKEND_NAME: Final = "copilot"

#: ``RemediationAction`` values that name the credential rather than
#: capacity. These keep the backend's existing auth-diagnostic path (which
#: reports what the token environment looks like from inside the sandbox);
#: parking a credential that was never throttled helps nobody.
AUTH_REMEDIATIONS: Final = frozenset({"sign_in", "switch_account", "show_account"})

#: Provider-defined error codes are arbitrary strings, so only an
#: identifier-shaped one travels; anything else could carry account prose.
_CODE_RE: Final = re.compile(r"[A-Za-z0-9_.:-]{1,64}")

_BILLING_WORDS: Final = (
    "billing",
    "payment",
    "insufficient credit",
    "credit balance",
    "out of credit",
)
#: "credit"/"credits" are deliberately absent: an app-set session credit cap
#: (``SessionLimitsConfig.max_ai_credits``) is not an account limit.
_QUOTA_WORDS: Final = (
    "quota",
    "usage limit",
    "usage cap",
    "monthly limit",
    "spend limit",
    "premium request",
    "entitlement",
)
_THROTTLE_WORDS: Final = ("too many requests", "rate limit", "rate_limit")
_UNAVAILABLE_STATUS: Final = frozenset({500, 502, 503, 504, 529})

#: Bound on provider-supplied retry timing. Honour what the provider says,
#: but a nonsense value must not park a credential for a year.
MAX_RETRY_AFTER_S: Final = 24 * 60 * 60


class ProviderFailed(Exception):
    """A classified provider rejection, raised out of session setup.

    Control flow, not a diagnostic: the backend turns it into a
    :class:`ProviderFailure` result rather than letting a fresh session,
    an auth diagnosis or a task failure stand in for provider capacity.
    """

    def __init__(self, failure: ProviderFailure) -> None:
        super().__init__(failure.reason)
        self.failure = failure


def text_of(value: Any) -> str:
    """One SDK field as lowercase text — enums carry their wire ``value``."""
    if value is None:
        return ""
    raw = getattr(value, "value", value)
    return raw.lower() if isinstance(raw, str) else ""


def seconds(value: Any) -> float | None:
    """A non-negative, finite, bounded retry delay, or None when unusable."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(min(value, MAX_RETRY_AFTER_S))


def _status(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def failure_from_envelope(
    *,
    code: Any = None,
    message: str = "",
    status: Any = None,
    remediation: Any = None,
    auto_switch_eligible: Any = None,
    retry_after_s: float | None = None,
    now: Callable[[], float] | None = None,
) -> ProviderFailure | None:
    """Classify one provider error envelope, or None when it is not a limit.

    Only fields the provider marked as error metadata are read — never
    assistant text, tool output or task content, which can discuss rate
    limits without the provider having imposed one. The reason is a fixed
    diagnosis, so provider account prose never reaches the chronology.
    """
    prose = message.lower()
    code_text = text_of(code)
    http_status = _status(status)
    if text_of(remediation) in AUTH_REMEDIATIONS:
        return None
    category: Any
    if any(word in code_text or word in prose for word in _BILLING_WORDS):
        category, reason = "billing", "The provider requires credit or billing recovery"
    elif any(word in code_text or word in prose for word in _QUOTA_WORDS):
        category, reason = "quota", "The provider usage quota is exhausted"
    elif (
        http_status == 429
        or auto_switch_eligible is True
        or any(word in code_text or word in prose for word in _THROTTLE_WORDS)
    ):
        category, reason = "throttle", "The provider temporarily throttled requests"
    elif http_status in _UNAVAILABLE_STATUS:
        category, reason = "unavailable", "The provider is temporarily unavailable"
    else:
        # Fails to the caller's ordinary error path rather than guessing:
        # an unplaced 403, a sandbox-policy rejection or a bug in the CLI
        # is not provider capacity, and holding a credential over one would
        # stall every repository sharing it.
        return None
    delay = seconds(retry_after_s)
    clock = now or time.time
    return ProviderFailure(
        backend=BACKEND_NAME,
        category=category,
        reason=reason,
        # Only a temporary throttle may schedule its own retry; a quota or
        # billing rejection waits for a provider reset or an operator, and
        # retry timing supplied alongside one is not a capacity promise.
        retry_at=clock() + delay if delay is not None and category == "throttle" else None,
        code=code_text if _CODE_RE.fullmatch(code_text) else None,
        http_status=http_status,
    )


def failure_from_error_event(
    data: Any, *, retry_after_s: float | None = None, now: Callable[[], float] | None = None
) -> ProviderFailure | None:
    """Classify a terminal ``SessionErrorData``, or None when not a limit."""
    message = getattr(data, "message", None)
    return failure_from_envelope(
        code=getattr(data, "error_code", None) or getattr(data, "error_type", None),
        message=message if isinstance(message, str) else "",
        status=getattr(data, "status_code", None),
        remediation=getattr(data, "remediation", None),
        auto_switch_eligible=getattr(data, "eligible_for_auto_switch", None),
        retry_after_s=retry_after_s,
        now=now,
    )


def failure_from_exception(
    exc: BaseException,
    *,
    retry_after_s: float | None = None,
    now: Callable[[], float] | None = None,
) -> ProviderFailure | None:
    """Classify a create/resume rejection, or None when it is not a limit.

    Confined to the SDK's own error envelope: ``JsonRpcError`` carries a
    numeric JSON-RPC ``code``, a ``message`` and the runtime's ``data``.
    An arbitrary exception has no provider metadata and is not classified,
    so a CLI crash or a bad kwarg never reads as a throttle. The service
    payload inside ``data`` is **field-unverified**, so each key is read
    defensively in both spellings.
    """
    message = getattr(exc, "message", None)
    if not isinstance(message, str) or not isinstance(getattr(exc, "code", None), int):
        return None
    data = getattr(exc, "data", None)
    if not isinstance(data, dict):
        data = {}

    def field(*names: str) -> Any:
        for name in names:
            for holder in (data, exc):
                value = (
                    holder.get(name) if isinstance(holder, dict) else getattr(holder, name, None)
                )
                if value is not None:
                    return value
        return None

    return failure_from_envelope(
        code=field("errorCode", "error_code"),
        message=message,
        status=field("statusCode", "status_code"),
        remediation=field("remediation"),
        auto_switch_eligible=field("eligibleForAutoSwitch", "eligible_for_auto_switch"),
        retry_after_s=retry_after_s
        if retry_after_s is not None
        else seconds(field("retryAfterSeconds", "retry_after_seconds")),
        now=now,
    )


def resume_recovery_failure() -> ProviderFailure:
    """The parked outcome for a ``require_resume`` job that cannot resume.

    Partial work that only the original session holds must be inspected,
    never re-derived by a fresh session replaying its side effects.
    """
    return ProviderFailure(
        backend=BACKEND_NAME,
        category="recovery",
        partial_progress=True,
        reason=("The interrupted session could not resume; inspect preserved work before recovery"),
    )
