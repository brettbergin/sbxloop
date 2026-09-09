"""Read organization limits with the configured Claude credential.

This endpoint requires admin scope (workspace API keys cannot use it).
It reports configured ceilings, not remaining capacity or spend quotas.
The Agent SDK itself exposes no equivalent read-only capacity query.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any, Final

from sbxloop_worker.rate_limits import (
    MAX_LIMITS,
    MAX_RESPONSE_BYTES,
    QUERY_TIMEOUT_S,
    RateLimit,
    RateLimitReport,
    Units,
    failure,
    number,
)

SOURCE: Final = "claude.organization.rate_limits"
URL = "https://api.anthropic.com/v1/organizations/rate_limits"
# Only documented limiter types have known units/windows. Future fields
# remain visible with unknown units rather than deriving semantics by name.
LIMIT_TYPES: dict[str, tuple[Units, int | None]] = {
    "requests_per_minute": ("requests", 60),
    "input_tokens_per_minute": ("tokens", 60),
    "output_tokens_per_minute": ("tokens", 60),
    "tokens_per_minute": ("tokens", 60),
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        # Never forward the API credential to a redirect destination.
        return None


def normalize(payload: Any, *, now: datetime | None = None) -> RateLimitReport:
    report = RateLimitReport(
        backend="claude",
        status="unavailable",
        observed_at=now or datetime.now(UTC),
        source=SOURCE,
        scope="organization",
        rate_limits="query",
        reason="No organization limits supplied; remaining capacity and usage quotas are unknown.",
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return report
    truncated = bool(payload.get("next_page"))
    for group in payload["data"][:MAX_LIMITS]:
        if not isinstance(group, dict) or not isinstance(group.get("limits"), list):
            continue
        models = group.get("models")
        truncated |= len(group["limits"]) > MAX_LIMITS
        truncated |= isinstance(models, list) and len(models) > 16
        models = [m for m in models[:16] if isinstance(m, str)] if isinstance(models, list) else []
        for item in group["limits"][:MAX_LIMITS]:
            if len(report.limits) == MAX_LIMITS:
                truncated = True
                break
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                continue
            name = item["type"]
            units, window = LIMIT_TYPES.get(name, ("unknown", None))
            report.limits.append(
                RateLimit(
                    name=name,
                    group=group.get("group_type")
                    if isinstance(group.get("group_type"), str)
                    else None,
                    category="rate",
                    units=units,
                    window_seconds=window,
                    limit=number(item.get("value")),
                    models=models,
                )
            )
    if report.limits:
        report.status = "partial"
        report.reason = (
            "Organization ceilings only; workspace overrides may be lower. Remaining capacity, "
            "reset times and longer-term usage quotas are not exposed by this endpoint."
        )
    if truncated or len(payload["data"]) > MAX_LIMITS:
        report.truncated = True
        report.status = "partial"
        report.reason += " Response truncated; omitted limits remain unknown."
    return report.bounded()


def query(*, timeout_s: float) -> RateLimitReport:
    token = os.environ.get("ANTHROPIC_API_KEY")
    if not token:
        return RateLimitReport(
            backend="claude",
            status="authentication_failed",
            source=SOURCE,
            reason="Configured Claude credential is missing in the agent sandbox.",
        )
    request = urllib.request.Request(
        URL,
        method="GET",
        headers={
            "x-api-key": token,
            "anthropic-version": "2023-06-01",
            "accept": "application/json",
        },
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=min(timeout_s, QUERY_TIMEOUT_S)) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return RateLimitReport(
                    backend="claude",
                    status="unavailable",
                    source=SOURCE,
                    reason="Provider status response exceeded the size bound.",
                )
            report = normalize(json.loads(raw))
            age = response.headers.get("Age")
            if age is not None and number(float(age)) is not None and float(age) > 300:
                report.status, report.freshness = "stale", "stale"
                report.reason = "Provider returned cached limits older than five minutes."
            return report
    except urllib.error.HTTPError as exc:
        report = failure("claude", exc, source=SOURCE)
        if exc.code == 403:
            report.reason = (
                "Configured credential cannot read organization limits; admin scope is required."
            )
        return report
    except Exception as exc:
        return failure("claude", exc, source=SOURCE)
