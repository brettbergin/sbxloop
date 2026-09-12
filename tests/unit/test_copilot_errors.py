"""The Copilot provider-envelope classifier.

Pure decision logic over SDK-shaped stand-ins, so the polarity that
decides whether a credential is parked is testable without the SDK. The
envelopes are synthetic: no account, credential or run data.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sbxloop_worker.backends.copilot_errors import (
    MAX_RETRY_AFTER_S,
    ProviderFailed,
    failure_from_envelope,
    failure_from_error_event,
    failure_from_exception,
    resume_recovery_failure,
    seconds,
)

CLOCK = 1_000.0


def at(value: float) -> Any:
    return lambda: value


def error_event(message: str, **fields: Any) -> Any:
    base: dict[str, Any] = {
        "error_type": "session_error",
        "message": message,
        "error_code": None,
        "status_code": None,
        "remediation": None,
        "eligible_for_auto_switch": None,
    }
    base.update(fields)
    return SimpleNamespace(**base)


class TestCategories:
    @pytest.mark.parametrize(
        ("message", "status", "category"),
        [
            ("Too many requests", 429, "throttle"),
            ("Rate limit exceeded for this model", None, "throttle"),
            # Provider facts outrank the status a limit was served with.
            ("Monthly usage limit reached", 429, "quota"),
            ("Premium request entitlement exhausted", 429, "quota"),
            ("Your credit balance is too low", 429, "billing"),
            ("Payment method declined", 402, "billing"),
            ("The service is temporarily unavailable", 503, "unavailable"),
        ],
    )
    def test_envelope_classification(self, message, status, category) -> None:
        failure = failure_from_envelope(message=message, status=status)
        assert failure is not None
        assert failure.backend == "copilot"
        assert failure.category == category
        assert failure.http_status == status

    @pytest.mark.parametrize(
        ("message", "status", "remediation"),
        [
            # Auth keeps the backend's own diagnostic path.
            ("Session was not created with authentication info", 401, "sign_in"),
            ("Choose a different account", 403, "switch_account"),
            # A sandbox policy rejection is not provider capacity.
            ("Outbound request blocked by policy", 403, "allow_sandbox_outbound"),
            # An unplaced rejection is never guessed into a category.
            ("Forbidden", 403, None),
            ("The request was malformed", 400, None),
            ("Something went wrong", None, None),
        ],
    )
    def test_what_is_not_a_provider_limit(self, message, status, remediation) -> None:
        assert (
            failure_from_envelope(
                message=message,
                status=status,
                remediation=SimpleNamespace(value=remediation) if remediation else None,
            )
            is None
        )

    def test_auto_switch_eligibility_alone_marks_a_throttle(self) -> None:
        # The SDK asks to switch models only after an eligible rate limit.
        failure = failure_from_envelope(message="model unavailable", auto_switch_eligible=True)
        assert failure is not None
        assert failure.category == "throttle"

    def test_an_auth_remediation_outranks_a_throttle_status(self) -> None:
        assert (
            failure_from_envelope(
                message="Too many requests",
                status=429,
                remediation=SimpleNamespace(value="sign_in"),
            )
            is None
        )


class TestTiming:
    def test_a_throttle_honours_supplied_retry_timing(self) -> None:
        failure = failure_from_envelope(
            message="Too many requests", status=429, retry_after_s=90, now=at(CLOCK)
        )
        assert failure is not None
        assert failure.retry_at == CLOCK + 90

    def test_missing_timing_stays_unknown(self) -> None:
        failure = failure_from_envelope(message="Too many requests", status=429, now=at(CLOCK))
        assert failure is not None
        assert failure.retry_at is None
        assert failure.reset_at is None

    def test_a_quota_rejection_schedules_no_retry_of_its_own(self) -> None:
        failure = failure_from_envelope(
            message="Monthly usage limit reached", retry_after_s=90, now=at(CLOCK)
        )
        assert failure is not None
        assert failure.category == "quota"
        assert failure.retry_at is None

    @pytest.mark.parametrize("value", [None, "60", True, float("nan"), float("inf"), -1])
    def test_unusable_timing_is_dropped(self, value) -> None:
        assert seconds(value) is None

    def test_absurd_timing_is_bounded(self) -> None:
        assert seconds(10 * MAX_RETRY_AFTER_S) == MAX_RETRY_AFTER_S


class TestSanitization:
    def test_the_reason_is_a_fixed_diagnosis(self) -> None:
        failure = failure_from_error_event(
            error_event(
                "Rate limit for account octo-org token ghp_synthetic_value", status_code=429
            )
        )
        assert failure is not None
        assert "octo-org" not in failure.reason
        assert "ghp_synthetic_value" not in failure.reason
        assert failure.reason == "The provider temporarily throttled requests"

    def test_only_an_identifier_shaped_code_travels(self) -> None:
        failure = failure_from_error_event(
            error_event(
                "Too many requests",
                status_code=429,
                error_code="account octo-org exceeded its allowance",
            )
        )
        assert failure is not None
        assert failure.code is None

    def test_a_structured_code_is_preserved(self) -> None:
        failure = failure_from_error_event(
            error_event("Too many requests", status_code=429, error_code="rate_limit_exceeded")
        )
        assert failure is not None
        assert failure.code == "rate_limit_exceeded"

    def test_the_error_type_stands_in_for_a_missing_code(self) -> None:
        failure = failure_from_error_event(error_event("nope", error_type="rate_limit"))
        assert failure is not None
        assert failure.category == "throttle"
        assert failure.code == "rate_limit"


class TestExceptions:
    def test_a_json_rpc_envelope_is_classified(self) -> None:
        exc = SimpleNamespace(code=-32603, message="Too many requests", data={"statusCode": 429})
        failure = failure_from_exception(exc, now=at(CLOCK))  # type: ignore[arg-type]
        assert failure is not None
        assert failure.category == "throttle"
        assert failure.http_status == 429

    def test_envelope_timing_is_read_in_either_spelling(self) -> None:
        for key in ("retryAfterSeconds", "retry_after_seconds"):
            exc = SimpleNamespace(
                code=-32603, message="Too many requests", data={"statusCode": 429, key: 45}
            )
            failure = failure_from_exception(exc, now=at(CLOCK))  # type: ignore[arg-type]
            assert failure is not None
            assert failure.retry_at == CLOCK + 45

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("the CLI runtime exited"),
            TimeoutError("no response"),
            # Message text alone is not an envelope: without the SDK's
            # numeric code there is no provider metadata to trust.
            SimpleNamespace(message="Too many requests"),
        ],
    )
    def test_an_arbitrary_exception_is_not_classified(self, exc) -> None:
        assert failure_from_exception(exc) is None  # type: ignore[arg-type]


def test_a_parked_resume_reports_preserved_work() -> None:
    failure = resume_recovery_failure()
    assert failure.category == "recovery"
    assert failure.partial_progress
    assert failure.retry_at is None


def test_provider_failed_carries_its_failure() -> None:
    failure = resume_recovery_failure()
    held = ProviderFailed(failure)
    assert held.failure is failure
    assert str(held) == failure.reason
