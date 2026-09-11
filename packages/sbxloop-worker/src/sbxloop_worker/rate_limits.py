"""Bounded, sanitized provider status shared by the worker and host.

This is evidence for an operator, never input to scheduling or recovery.
Absent fields stay null; a query time is not a provider snapshot time.
"""

from __future__ import annotations

import contextlib
import math
import os
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from sbxloop_worker.protocol import ProtocolModel
from sbxloop_worker.secrets import redact_secrets

QUERY_TIMEOUT_S = 10.0
MAX_LIMITS = 32
MAX_REPORT_BYTES = 32_768
MAX_RESPONSE_BYTES = 65_536
Source = Literal["copilot.account.getQuota", "claude.organization.rate_limits", "none"]
Units = Literal["requests", "tokens", "unknown"]
Status = Literal[
    "ok",
    "partial",
    "unsupported",
    "unavailable",
    "stale",
    "timeout",
    "authentication_failed",
    "throttled",
]
Amount = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Percentage = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]


def clean(value: str, limit: int = 96) -> str:
    # Replace exact values BEFORE clipping, including credentials whose
    # formats are unknown to the general-purpose redactor.
    for name in ("COPILOT_GITHUB_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        secret = os.environ.get(name)
        if secret:
            value = value.replace(secret, "***")
    return " ".join(redact_secrets(value).split())[:limit]


def number(value: Any, *, percentage: bool = False) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    if not math.isfinite(result) or result < 0 or (percentage and result > 100):
        return None
    return result


def boolean(value: Any) -> bool | None:
    return value if type(value) is bool else None


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except ValueError:
        return None


class RateLimit(ProtocolModel):
    name: str = Field(max_length=96)
    group: str | None = Field(default=None, max_length=96)
    category: Literal["rate", "quota"]
    units: Units = "unknown"
    models: list[str] = Field(default_factory=list, max_length=16)
    window_seconds: Amount | None = None
    limit: Amount | None = None
    used: Amount | None = None
    remaining: Amount | None = None
    used_percentage: Percentage | None = None
    remaining_percentage: Percentage | None = None
    unlimited: bool | None = None
    overage: Amount | None = None
    overage_allowed: bool | None = None
    usage_allowed_when_exhausted: bool | None = None
    reset_at: datetime | None = None
    retry_after_seconds: Amount | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: str) -> str:
        return clean(value)

    @field_validator("group", mode="before")
    @classmethod
    def _group(cls, value: str | None) -> str | None:
        return clean(value) if value is not None else None

    @field_validator("models")
    @classmethod
    def _models(cls, values: list[str]) -> list[str]:
        return [clean(value) for value in values]


class RateLimitReport(ProtocolModel):
    backend: str = Field(max_length=32)
    status: Status
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: Source = "none"
    snapshot_at: datetime | None = None
    freshness: Literal["live", "unknown", "stale"] = "unknown"
    scope: Literal["account", "organization", "unknown"] = "unknown"
    rate_limits: Literal["query", "unsupported"] = "unsupported"
    usage_quotas: Literal["query", "unsupported"] = "unsupported"
    reason: str = Field(default="", max_length=512)
    retry_after_seconds: Amount | None = None
    limits: list[RateLimit] = Field(default_factory=list, max_length=MAX_LIMITS)
    truncated: bool = False

    @field_validator("reason", mode="before")
    @classmethod
    def _reason(cls, value: str) -> str:
        return clean(value, 512)

    def bounded(self, max_bytes: int = MAX_REPORT_BYTES) -> RateLimitReport:
        while len(self.model_dump_json().encode()) > max_bytes and self.limits:
            self.limits.pop()
            if self.status == "ok":
                self.status = "partial"
            if not self.truncated:
                self.reason = clean(
                    self.reason + " Response truncated; omitted limits remain unknown.", 512
                )
            self.truncated = True
        return self


def failure(backend: str, exc: Exception, *, source: Source = "none") -> RateLimitReport:
    """Only structured codes cross this seam; never exception prose/body.

    Unknown provider errors are unavailable, not authentication diagnoses.
    No terminal-failure recovery policy is applied to a status query.
    """
    status: Status = "unavailable"
    reason = "Provider status query failed; capacity and reset timing are unknown."
    code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if code is None:
        code = getattr(exc, "code", None)
    if isinstance(exc, TimeoutError):
        status, reason = "timeout", "Provider status query timed out."
    elif isinstance(exc, (ImportError, AttributeError, NotImplementedError)) or code == -32601:
        status, reason = "unsupported", "Installed provider runtime does not expose this query."
    elif code == 401:
        status, reason = "authentication_failed", "Provider rejected the configured credential."
    elif code == 403:
        status, reason = "unsupported", "Configured credential lacks permission for this query."
    elif code == 429:
        status, reason = "throttled", "Provider throttled the status query."
    retry = number(getattr(exc, "retry_after_seconds", None))
    headers = getattr(exc, "headers", None)
    if retry is None and headers is not None:
        raw = headers.get("retry-after")
        if isinstance(raw, str):
            with contextlib.suppress(ValueError):
                retry = number(float(raw))
    return RateLimitReport(
        backend=backend,
        status=status,
        reason=reason,
        source=source,
        retry_after_seconds=retry,
        rate_limits="query" if source == "claude.organization.rate_limits" else "unsupported",
        usage_quotas="query" if source == "copilot.account.getQuota" else "unsupported",
    )
