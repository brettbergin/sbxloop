"""Read-only provider status: synthetic SDK/API responses, never live credentials."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest

from sbxloop_worker.backends import get_backend
from sbxloop_worker.backends.claude_limits import normalize as claude_normalize
from sbxloop_worker.backends.copilot_limits import normalize as copilot_normalize
from sbxloop_worker.protocol import JobRequest
from sbxloop_worker.rate_limits import MAX_REPORT_BYTES, RateLimitReport
from sbxloop_worker.runner import JobRunner

NOW = datetime(2026, 9, 9, tzinfo=UTC)


def test_copilot_quota_semantics_and_unknowns() -> None:
    report = copilot_normalize(
        NS(
            quota_snapshots={
                "premium_interactions": NS(
                    entitlement_requests=300,
                    used_requests=60,
                    remaining_percentage=80,
                    reset_date="2026-10-01T00:00:00Z",
                    is_unlimited_entitlement=False,
                ),
                "chat": NS(remaining_percentage=25, reset_date="2026-09-10T00:00:00+00:00"),
            }
        ),
        now=NOW,
    )
    assert report.backend == "copilot" and report.scope == "account"
    assert report.source == "copilot.account.getQuota"
    assert report.observed_at == NOW and report.snapshot_at is None
    assert report.freshness == "unknown"
    premium, chat = report.limits
    assert premium.category == "quota" and premium.units == "requests"
    assert premium.limit == 300 and premium.used == 60
    assert premium.remaining_percentage == 80 and premium.used_percentage is None
    assert premium.remaining is None and premium.window_seconds is None
    assert premium.reset_at == datetime(2026, 10, 1, tzinfo=UTC)
    assert chat.limit is None and chat.used is None and chat.unlimited is None
    assert chat.remaining_percentage == 25
    assert report.rate_limits == "unsupported" and report.usage_quotas == "query"


@pytest.mark.parametrize("snapshots", [None, {}, [], {"chat": None}, {"chat": NS()}])
def test_missing_copilot_data_is_unavailable(snapshots) -> None:
    report = copilot_normalize(NS(quota_snapshots=snapshots), now=NOW)
    assert report.status == "unavailable"
    assert not report.limits


def test_stale_and_invalid_copilot_values_are_explicit() -> None:
    report = copilot_normalize(
        NS(
            quota_snapshots={
                "chat": NS(
                    entitlement_requests=-1,
                    used_requests=True,
                    remaining_percentage=float("nan"),
                    reset_date="2026-09-08T00:00:00Z",
                )
            }
        ),
        now=NOW,
    )
    assert report.status == "stale" and report.freshness == "stale"
    entry = report.limits[0]
    assert entry.limit is None and entry.unlimited is None
    assert entry.used is None and entry.remaining_percentage is None
    assert entry.reset_at is not None


def test_exhausted_quota_keeps_overage_permissions():
    report = copilot_normalize(
        NS(
            quota_snapshots={
                "premium_interactions": NS(
                    entitlement_requests=300,
                    used_requests=305,
                    remaining_percentage=0,
                    overage=5,
                    overage_allowed_with_exhausted_quota=True,
                    usage_allowed_with_exhausted_quota=True,
                )
            }
        ),
        now=NOW,
    )
    entry = report.limits[0]
    assert entry.remaining_percentage == 0 and entry.overage == 5
    assert entry.overage_allowed is True and entry.usage_allowed_when_exhausted is True
    assert entry.unlimited is None


def test_truncation_preserves_source_limitations():
    from sbxloop_worker.rate_limits import RateLimit

    report = RateLimitReport(
        backend="claude",
        status="partial",
        reason="Organization ceilings only; workspace overrides may be lower.",
        limits=[RateLimit(name=f"limit-{i}", category="rate") for i in range(32)],
    )
    report.bounded(1000)
    assert report.truncated is True
    assert "workspace overrides may be lower" in report.reason
    assert len(report.model_dump_json().encode()) <= 1000


def test_claude_organization_limits_are_not_remaining_quota() -> None:
    report = claude_normalize(
        {
            "data": [
                {
                    "group_type": "model_group",
                    "models": ["model-a", "model-b"],
                    "limits": [
                        {"type": "requests_per_minute", "value": 50},
                        {"type": "input_tokens_per_minute", "value": 10000},
                        {"type": "future_limit", "value": None},
                    ],
                }
            ],
            "next_page": None,
        },
        now=NOW,
    )
    assert report.scope == "organization" and report.backend == "claude"
    assert report.status == "partial" and report.usage_quotas == "unsupported"
    assert "workspace" in report.reason
    rpm, tpm, unknown = report.limits
    assert rpm.window_seconds == 60 and rpm.limit == 50 and rpm.units == "requests"
    assert tpm.window_seconds == 60 and tpm.limit == 10000 and tpm.units == "tokens"
    assert tpm.models == ["model-a", "model-b"]
    assert tpm.group == "model_group"
    for entry in report.limits:
        assert entry.remaining is None and entry.reset_at is None
    assert unknown.limit is None and unknown.units == "unknown"


def test_copilot_query_uses_only_read_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    token = "arbitrary-test-credential"
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", token)

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["github_token"] == token
            assert kwargs["use_logged_in_user"] is False
            calls.append("init")
            self.rpc = NS(account=NS(get_quota=self.quota))

        async def start(self):
            calls.append("start")

        async def quota(self, params, *, timeout):
            assert vars(params) == {} and 0 < timeout <= 10
            calls.append("get_quota")
            return NS(quota_snapshots={"chat": NS(remaining_percentage=42)})

        async def force_stop(self):
            calls.append("stop")

    monkeypatch.setitem(sys.modules, "copilot", NS(CopilotClient=Client))
    monkeypatch.setitem(sys.modules, "copilot.rpc", NS(AccountGetQuotaRequest=NS))
    report = get_backend("copilot").rate_limits(timeout_s=1)
    assert report.limits[0].remaining_percentage == 42
    assert calls == ["init", "start", "get_quota", "stop"]
    assert token not in report.model_dump_json()


@pytest.mark.parametrize(
    "error,status",
    [
        (TimeoutError("secret"), "timeout"),
        (ImportError("secret"), "unsupported"),
        (RuntimeError("secret"), "unavailable"),
    ],
)
def test_provider_failures_are_sanitized(monkeypatch, error, status) -> None:
    from sbxloop_worker.backends import copilot_limits

    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "secret")
    monkeypatch.setattr(copilot_limits, "_query", fail)
    report = get_backend("copilot").rate_limits(timeout_s=0.1)
    assert report.status == status and not report.limits
    assert "secret" not in report.model_dump_json()


@pytest.mark.parametrize(
    "code,status",
    [
        (401, "authentication_failed"),
        (403, "unsupported"),
        (429, "throttled"),
        (-32601, "unsupported"),
        (-32603, "unavailable"),
    ],
)
def test_copilot_structured_query_errors(monkeypatch, code, status):
    from sbxloop_worker.backends import copilot_limits

    class RpcError(Exception):
        pass

    error = RpcError("private provider error: secret")
    error.code = code
    error.retry_after_seconds = 12 if code == 429 else None

    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "secret")
    monkeypatch.setattr(copilot_limits, "_query", fail)
    report = get_backend("copilot").rate_limits(timeout_s=1)
    assert report.status == status and report.source == "copilot.account.getQuota"
    assert report.usage_quotas == "query" and not report.limits
    assert report.retry_after_seconds == (12 if code == 429 else None)
    assert "secret" not in report.model_dump_json()


def test_query_timeout_is_bounded(monkeypatch) -> None:
    from sbxloop_worker.backends import copilot_limits

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "test")
    monkeypatch.setattr(copilot_limits, "_query", blocked)
    assert get_backend("copilot").rate_limits(timeout_s=0.01).status == "timeout"


def test_report_redacts_before_clipping_and_bounds_size(monkeypatch) -> None:
    token = "custom-credential-" + "x" * 80
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", token)
    report = copilot_normalize(
        NS(quota_snapshots={f"{i}-{token}": NS(remaining_percentage=20) for i in range(1000)}),
        now=NOW,
    )
    assert report.status == "partial"
    assert token[:40] not in report.model_dump_json()
    assert len(report.model_dump_json().encode()) <= MAX_REPORT_BYTES


def test_worker_rate_query_routes_backend_without_model_generation(tmp_path, monkeypatch) -> None:
    from sbxloop_worker.backends.claude import ClaudeBackend

    seen = []

    def query(self, *, timeout_s):
        seen.append(timeout_s)
        return RateLimitReport(backend="claude", status="unsupported", reason="test")

    monkeypatch.setattr(ClaudeBackend, "rate_limits", query)
    job = JobRequest(
        job_id="limits",
        run_id="concierge",
        kind="agent.rate_limits",
        params={"backend": "claude"},
        timeout_s=1,
    )
    result = JobRunner(job, tmp_path / "events", tmp_path / "result", heartbeat_s=0).run()
    assert result.status == "ok" and result.usage is None and result.session_id is None
    assert result.output_json["backend"] == "claude"
    assert seen == [1]
    events = (tmp_path / "events").read_text()
    assert "agent.message" not in events and "agent.usage" not in events


@pytest.mark.parametrize(
    "params", [{}, {"backend": "other"}, {"backend": "claude", "token": "must-not-travel"}]
)
def test_status_job_rejects_credential_or_missing_backend(params) -> None:
    with pytest.raises(ValueError):
        JobRequest(job_id="x", run_id="concierge", kind="agent.rate_limits", params=params)


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "authentication_failed"),
        (403, "unsupported"),
        (429, "throttled"),
        (503, "unavailable"),
    ],
)
def test_claude_http_failures_do_not_leak_body_or_credential(monkeypatch, status, expected):
    from urllib.error import HTTPError

    from sbxloop_worker.backends import claude_limits

    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-inference-key")
    calls = []

    def opened(request, *, timeout):
        calls.append(request)
        assert request.full_url == claude_limits.URL and request.get_method() == "GET"
        assert request.data is None and request.get_header("X-api-key") == "secret-inference-key"
        assert timeout <= 10
        raise HTTPError(
            request.full_url, status, "secret-inference-key", {"retry-after": "17"}, None
        )

    monkeypatch.setattr(
        claude_limits.urllib.request, "build_opener", lambda handler: NS(open=opened)
    )
    report = get_backend("claude").rate_limits(timeout_s=1)
    assert report.status == expected and not report.limits
    assert report.retry_after_seconds == 17
    assert "secret-inference-key" not in report.model_dump_json()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "body,headers,expected",
    [
        (b'{"data": []}', {}, "unavailable"),
        (b"not-json secret-inference-key", {}, "unavailable"),
        (b"x" * 65537, {}, "unavailable"),
        (b'{"data": []}', {"Age": "301"}, "stale"),
        (b'{"data": [{"limits": [{"type": "requests_per_minute", "value": 20}]}]}', {}, "partial"),
    ],
)
def test_claude_query_bounds_read_and_marks_cached_data(monkeypatch, body, headers, expected):
    from io import BytesIO

    from sbxloop_worker.backends import claude_limits

    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-inference-key")

    class Response(BytesIO):
        def read(self, size=-1):
            assert size == 65537
            return super().read(size)

    response = Response(body)
    response.headers = headers
    monkeypatch.setattr(
        claude_limits.urllib.request,
        "build_opener",
        lambda handler: NS(open=lambda *a, **kw: response),
    )
    report = get_backend("claude").rate_limits(timeout_s=1)
    assert report.status == expected
    assert "secret-inference-key" not in report.model_dump_json()


@pytest.mark.parametrize(
    "backend,env", [("copilot", "COPILOT_GITHUB_TOKEN"), ("claude", "ANTHROPIC_API_KEY")]
)
def test_missing_credentials_are_explicit(monkeypatch, backend, env):
    monkeypatch.delenv(env, raising=False)
    report = get_backend(backend).rate_limits(timeout_s=1)
    assert report.status == "authentication_failed" and not report.limits


def test_claude_refuses_redirects():
    from sbxloop_worker.backends.claude_limits import _NoRedirect

    assert (
        _NoRedirect().redirect_request(None, None, 302, "moved", {}, "https://other.test") is None
    )


@pytest.mark.parametrize("backend", ["echo", "codex"])
def test_unsupported_backends_do_not_start_runtime(backend, monkeypatch):
    adapter = get_backend(backend)
    monkeypatch.setattr(adapter, "ensure_available", lambda: pytest.fail("must not start SDK"))
    assert adapter.rate_limits(timeout_s=1).status == "unsupported"


def test_worker_sanitizes_unexpected_query_exception(tmp_path, monkeypatch):
    from sbxloop_worker.backends.claude import ClaudeBackend

    def fail(*args, **kwargs):
        raise RuntimeError("provider leaked secret-inference-key")

    monkeypatch.setattr(ClaudeBackend, "rate_limits", fail)
    job = JobRequest(
        job_id="limits",
        run_id="concierge",
        kind="agent.rate_limits",
        params={"backend": "claude"},
        timeout_s=1,
    )
    result = JobRunner(job, tmp_path / "events", tmp_path / "result", heartbeat_s=0).run()
    assert result.status == "ok" and result.output_json["status"] == "unavailable"
    assert "secret-inference-key" not in (tmp_path / "events").read_text()
    assert "secret-inference-key" not in (tmp_path / "result").read_text()
