"""Copilot's documented account.getQuota RPC, without opening a session.

The generated public RPC surface exists in github-copilot-sdk 1.0.8. It is
experimental, so an older/mismatched runtime explicitly reports unsupported.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from datetime import UTC, datetime
from itertools import islice
from typing import Any, Final

from sbxloop_worker.rate_limits import (
    MAX_LIMITS,
    QUERY_TIMEOUT_S,
    RateLimit,
    RateLimitReport,
    boolean,
    failure,
    number,
    timestamp,
)
from sbxloop_worker.secrets import is_sbx_sentinel

SOURCE: Final = "copilot.account.getQuota"


def normalize(payload: Any, *, now: datetime | None = None) -> RateLimitReport:
    now = now or datetime.now(UTC)
    report = RateLimitReport(
        backend="copilot",
        status="unavailable",
        observed_at=now,
        source=SOURCE,
        scope="account",
        usage_quotas="query",
        reason="No quota snapshot supplied; short-term request/token limits are unsupported.",
    )
    snapshots = getattr(payload, "quota_snapshots", None)
    if not isinstance(snapshots, dict):
        return report
    for name, data in islice(snapshots.items(), MAX_LIMITS):
        if not isinstance(name, str) or data is None:
            continue
        entry = RateLimit(
            name=name,
            category="quota",
            units="requests",
            limit=number(getattr(data, "entitlement_requests", None)),
            used=number(getattr(data, "used_requests", None)),
            remaining_percentage=number(
                getattr(data, "remaining_percentage", None), percentage=True
            ),
            unlimited=boolean(getattr(data, "is_unlimited_entitlement", None)),
            overage=number(getattr(data, "overage", None)),
            overage_allowed=boolean(getattr(data, "overage_allowed_with_exhausted_quota", None)),
            usage_allowed_when_exhausted=boolean(
                getattr(data, "usage_allowed_with_exhausted_quota", None)
            ),
            reset_at=timestamp(getattr(data, "reset_date", None)),
        )
        if any(
            value is not None
            for value in (
                entry.limit,
                entry.used,
                entry.remaining_percentage,
                entry.unlimited,
                entry.reset_at,
            )
        ):
            report.limits.append(entry)
    if report.limits:
        report.status = "ok"
        report.reason = (
            "Shared account quota snapshots; snapshot time and window lengths are unknown. "
            "Short-term request/token limits are unsupported."
        )
    if len(snapshots) > MAX_LIMITS:
        report.truncated = True
        report.status = "partial"
        report.reason = "Quota response truncated; omitted limits remain unknown."
    if any(item.reset_at is not None and item.reset_at <= now for item in report.limits):
        report.status = "stale"
        report.freshness = "stale"
        report.reason = (
            "A reported reset has elapsed; these snapshots cannot establish current capacity."
        )
    return report.bounded()


async def _query(token: str, timeout_s: float) -> Any:
    from copilot import CopilotClient
    from copilot.rpc import AccountGetQuotaRequest

    # A separate runtime and scratch directory cannot restart, resume or
    # wait on the concierge's existing SDK session. Credentials stay in the
    # worker; the SDK forwards its token via an env name, never CLI argv.
    with tempfile.TemporaryDirectory(prefix="sbxloop-quota-") as scratch:
        client = CopilotClient(
            github_token=token, use_logged_in_user=False, base_directory=scratch, log_level="error"
        )
        try:
            await client.start()
            return await client.rpc.account.get_quota(AccountGetQuotaRequest(), timeout=timeout_s)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.force_stop(), timeout=1.0)


def query(*, timeout_s: float) -> RateLimitReport:
    token = os.environ.get("COPILOT_GITHUB_TOKEN")
    if not token or is_sbx_sentinel(token):
        return RateLimitReport(
            backend="copilot",
            status="authentication_failed",
            source=SOURCE,
            reason="A usable configured Copilot credential is not available in the agent sandbox.",
        )
    timeout_s = min(timeout_s, QUERY_TIMEOUT_S)
    try:

        async def bounded() -> Any:
            return await asyncio.wait_for(_query(token, timeout_s), timeout_s)

        return normalize(asyncio.run(bounded()))
    except Exception as exc:
        return failure("copilot", exc, source=SOURCE)
