"""Opt-in GlitchTip reports from the host, isolated from the SDK's global scope.

Reports include exception messages, chains, groups and stack source context.
Recognizable credentials are redacted; locals and argv stay local, and how
much of a log record's structured fields travel is the operator's choice
(``[telemetry] log_fields``). The worker never imports this module or
receives the reporting credential.

An ERROR event logged without an exception — a circuit breaker opening, a
work item abandoned, a sandbox that could not be provisioned — has no
traceback to explain it, so the report carries the three things that do:
the call site (``culprit``), the event's static operator ``hint`` as the
report's title line, and the record's reportable fields.
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
_log_fields: Literal["none", "diagnostic", "all"] = "diagnostic"
_EVENT_NAME = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
_MAX_VALUE_LENGTH = 100_000

#: Where this module parks the fields it chose, until :func:`_before_send`
#: promotes them into ``extra``. Fields never sit in ``extra`` before that,
#: so an ``extra`` the SDK itself put on an event is unambiguously not ours
#: and is still discarded.
_FIELDS_KEY = "sbxloop_log_fields"

#: Record keys that are the log line's own scaffolding, not facts about what
#: happened; they are already the report's message, level and stack.
_NON_FIELDS = frozenset(
    {"event", "level", "logger", "timestamp", "exc_info", "stack_info", "stack", "exception"}
)

#: String-valued keys whose vocabulary this codebase writes — a backend name,
#: a sandbox role, a run kind. Unlike a reason or an error they cannot come to
#: hold a target repository's text, so ``diagnostic`` reports them.
_ENUM_FIELDS = frozenset(
    {"backend", "kind", "mode", "outcome", "phase", "role", "source", "stage", "state", "status"}
)

#: The operator-facing sentence a call site writes for a human reading the
#: journal. Static prose from this repository, so every level above ``none``
#: reports it: it is what makes an exception-less event mean something.
_HINT_FIELD = "hint"

#: Module prefixes between a log call and this processor; skipped when
#: naming the call site.
_PLUMBING = ("structlog", "logging", "sbxloop.log", "sbxloop.telemetry")

#: A single free-text field is worth a paragraph of context, not a payload.
_MAX_FIELD_LENGTH = 2_000


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
        "culprit",
    }
    kept = {key: value for key, value in event.items() if key in allowed}
    fields = event.get(_FIELDS_KEY)
    if fields:
        kept["extra"] = fields
    return cast("Event", kept)


def configure_telemetry(config: TelemetryConfig) -> None:
    """Enable reporting only when the named DSN exists; never break startup."""
    global _client, _log_fields

    shutdown_telemetry()
    _log_fields = config.log_fields
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


def _call_site() -> str | None:
    """``module in function`` for the frame that logged, or ``None``.

    An event logged without an exception reaches the server with no stack at
    all: two call sites emit ``breaker.opened`` and the report cannot say
    which. This is the same class of fact a traceback frame already carries —
    a module, a function — never a value.
    """
    frame: Any = sys._getframe(1)
    while frame is not None:
        module = str(frame.f_globals.get("__name__", ""))
        plumbing = any(module == name or module.startswith(f"{name}.") for name in _PLUMBING)
        if module and not plumbing:
            return f"{module} in {frame.f_code.co_name}"
        frame = frame.f_back
    return None


def _reportable(key: str, value: Any) -> bool:
    """Whether one log field may travel, under the operator's policy.

    ``diagnostic`` keeps what describes the *failure* and cannot describe the
    *work*: numbers and flags (an attempt count, a cooldown, a duration), the
    static ``hint``, and the enum-valued keys this codebase writes. A free-text
    field — a reason, an error string, an item id, a url, a branch — can carry
    a target repository's content, so it travels only under ``all``.
    """
    if key in _NON_FIELDS or key.startswith("_"):
        return False
    if _log_fields == "all":
        return True
    if key in (_HINT_FIELD, *_ENUM_FIELDS):
        return True
    return isinstance(value, bool | int | float)


def _fields(event: EventDict) -> dict[str, Any]:
    """The record's reportable fields, redacted and bounded."""
    from sbxloop.log import redact_text

    if _log_fields == "none":
        return {}
    fields: dict[str, Any] = {}
    for key, value in event.items():
        if not _reportable(key, value):
            continue
        if isinstance(value, bool | int | float):
            fields[key] = value
            continue
        text = redact_text(str(value))
        fields[key] = text[:_MAX_FIELD_LENGTH] if len(text) > _MAX_FIELD_LENGTH else text
    return fields


def capture_exception(error: BaseException) -> None:
    """Report an unhandled CLI exception without changing its exit behavior."""
    if _client is not None:
        with contextlib.suppress(Exception):
            _client.capture_event({"level": "error", "exception": _exception_event(error)})


def capture_log(_logger: Any, method: str, event: EventDict) -> EventDict:
    """Structlog processor: ERROR events, and WARNING events with exceptions.

    Runs once before rendering, never in a handler shared by stderr/file/ring
    outputs. What of the record travels is :func:`_reportable`'s decision.
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
        fields = _fields(event)
        # The event name stays the grouping key; ``formatted`` is what a
        # human reads. The hint is static per call site, so a report that
        # explains itself still groups with every other one of its kind.
        hint = fields.get(_HINT_FIELD)
        payload: dict[str, Any] = {
            "message": {
                "message": name,
                "formatted": f"{name}: {hint}" if isinstance(hint, str) and hint else name,
            },
            "level": "warning" if method == "warning" else "error",
        }
        if isinstance(error, BaseException):
            payload["exception"] = _exception_event(error)
        else:
            # No traceback to locate this one: name the frame that logged it.
            culprit = _call_site()
            if culprit is not None:
                payload["culprit"] = culprit
        if fields:
            payload[_FIELDS_KEY] = fields
        _client.capture_event(cast("Event", payload))
    return event


def shutdown_telemetry() -> None:
    """Flush queued reports with a bounded wait and release the transport."""
    global _client

    client, _client = _client, None
    if client is not None:
        with contextlib.suppress(Exception):
            client.close(timeout=2.0)
