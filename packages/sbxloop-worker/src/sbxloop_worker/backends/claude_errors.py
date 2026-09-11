"""Claude provider envelopes, isolated from ordinary task/tool content.

Checked against the SDK's types and parser at 6bbd309 (2026-09-09):
AssistantMessage.error, ResultMessage.is_error/api_error_status and
RateLimitEvent.rate_limit_info. Older SDKs omit timing/status; unknown
values stay unknown. Informational rate events alone are not terminal.
"""

from __future__ import annotations

import math
from typing import Any

from sbxloop_worker.protocol import ProviderFailure


def timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= 253402300799 else None


def failure_from_envelope(
    code: str | None, text: str, *, status: int | None = None, limit: Any = None
) -> ProviderFailure:
    """Normalize only text explicitly marked as a provider error.

    Emit a fixed diagnosis instead of echoing a provider's arbitrary prose
    (which can include account data, prompts or credentials). No dates are
    inferred from prose, and HTTP 429 does not outrank quota/billing facts.
    """
    prose = text.lower()
    rejected = getattr(limit, "status", None) == "rejected"
    window = getattr(limit, "rate_limit_type", None) if rejected else None
    reset = timestamp(getattr(limit, "resets_at", None)) if rejected else None
    category: Any = "unknown"
    reason = "The provider rejected the session; operator recovery is required"
    if code == "billing_error" or any(
        word in prose
        for word in ("insufficient credit", "credit balance", "billing", "out of credit")
    ):
        category, reason = "billing", "The provider requires credit or billing recovery"
    elif window or any(
        word in prose
        for word in (
            "usage limit",
            "usage cap",
            "quota",
            "weekly limit",
            "spend limit",
            "hit your limit",
            "reached your limit",
        )
    ):
        category, reason = "quota", "The provider usage quota is exhausted"
    elif code == "rate_limit" or status == 429 or "too many requests" in prose:
        category, reason = "throttle", "The provider temporarily throttled requests"
    elif code == "server_error" or status in (500, 502, 503, 504, 529):
        category, reason = "unavailable", "The provider is temporarily unavailable"
    known_codes = {
        "authentication_failed",
        "billing_error",
        "rate_limit",
        "invalid_request",
        "server_error",
        "unknown",
    }
    return ProviderFailure(
        backend="claude",
        category=category,
        reason=reason,
        code=code if code in known_codes else None,
        http_status=status,
        reset_at=reset,
    )
