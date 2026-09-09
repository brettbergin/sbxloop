"""Opt-in GlitchTip reports from the host, isolated from the SDK's global scope.

Reports include exception messages, chains, groups and stack source context.
Recognizable credentials are redacted; locals, argv and log fields stay local.
The worker never imports this module or receives the reporting credential.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    import sentry_sdk
    from sentry_sdk._types import Event
    from structlog.typing import EventDict

    from sbxloop.config import TelemetryConfig

_client: sentry_sdk.Client | None = None
_EVENT_NAME = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
_MAX_VALUE_LENGTH = 100_000


def _before_send(event: Event, _hint: dict[str, Any]) -> Event:
    """Discard ambient context the SDK may add before it queues the envelope."""
    allowed = {
        "event_id",
        "timestamp",
        "platform",
        "release",
        "environment",
        "level",
        "message",
        "exception",
    }
    return cast("Event", {key: value for key, value in event.items() if key in allowed})


def configure_telemetry(config: TelemetryConfig) -> None:
    """Enable reporting only when the named DSN exists; never break startup."""
    global _client

    shutdown_telemetry()
    dsn = os.environ.get(config.dsn_env, "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk

        from sbxloop import __version__

        # A dedicated client with no global binding or integrations: no SDK
        # monkey-patching, request interception or implicit library logging.
        _client = sentry_sdk.Client(
            dsn=dsn,
            release=f"sbxloop@{__version__}",
            environment=config.environment,
            debug=False,
            spotlight=False,
            default_integrations=False,
            auto_enabling_integrations=False,
            auto_session_tracking=False,
            send_default_pii=False,
            include_local_variables=False,
            include_source_context=True,
            max_value_length=_MAX_VALUE_LENGTH,
            max_breadcrumbs=0,
            traces_sample_rate=0.0,
            profiles_sample_rate=0.0,
            enable_logs=False,
            enable_metrics=False,
            send_client_reports=False,
            enable_backpressure_handling=False,
            before_send=_before_send,
            shutdown_timeout=2.0,
        )
    except Exception:
        # Neither the DSN nor the SDK's exception text is safe to log.
        from sbxloop.log import get_logger

        get_logger(__name__).warning("telemetry.init_failed")


def _exception_event(error: BaseException) -> dict[Literal["values"], list[dict[str, Any]]]:
    from sentry_sdk.utils import event_from_exception

    # The SDK handles chained exceptions, groups, and traceback ordering. Keep
    # frame locals disabled: a trace needs source locations, not process memory.
    event, _ = event_from_exception(
        error,
        client_options={
            "include_local_variables": False,
            "include_source_context": True,
            "max_value_length": _MAX_VALUE_LENGTH,
        },
    )
    return cast(
        'dict[Literal["values"], list[dict[str, Any]]]', _redact_diagnostics(event["exception"])
    )


def _redact_diagnostics(value: Any) -> Any:
    """Keep diagnostic text while masking the credential shapes used by logs."""
    from sbxloop.log import redact_text

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: _redact_diagnostics(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_diagnostics(item) for item in value]
    return value


def capture_exception(error: BaseException) -> None:
    """Report an unhandled CLI exception without changing its exit behavior."""
    if _client is not None:
        with contextlib.suppress(Exception):
            _client.capture_event({"level": "error", "exception": _exception_event(error)})


def capture_log(_logger: Any, method: str, event: EventDict) -> EventDict:
    """Structlog processor: ERROR events, and WARNING events with exceptions.

    Runs once before rendering, never in a handler shared by stderr/file/ring
    outputs. Log fields stay local; they are not a reporting payload.
    """
    if _client is None or method not in {"warning", "error", "exception", "critical"}:
        return event
    with contextlib.suppress(Exception):
        if not str(event.get("logger", "")).startswith("sbxloop."):
            return event
        name = event.get("event")
        if not isinstance(name, str) or not _EVENT_NAME.fullmatch(name):
            return event
        exc_info = event.get("exc_info")
        error = (
            exc_info
            if isinstance(exc_info, BaseException)
            else exc_info[1]
            if isinstance(exc_info, tuple)
            else sys.exc_info()[1]
            if exc_info
            else None
        )
        if method == "warning" and error is None:
            return event
        payload: Event = {
            "message": name,
            "level": "warning" if method == "warning" else "error",
        }
        if isinstance(error, BaseException):
            payload["exception"] = _exception_event(error)
        _client.capture_event(payload)
    return event


def shutdown_telemetry() -> None:
    """Flush queued reports with a bounded wait and release the transport."""
    global _client

    client, _client = _client, None
    if client is not None:
        with contextlib.suppress(Exception):
            client.close(timeout=2.0)
