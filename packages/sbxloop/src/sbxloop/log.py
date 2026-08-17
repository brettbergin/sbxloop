"""Logging: structlog routed through the standard library.

Every module obtains its logger with ``log = get_logger(__name__)`` and
emits **events, not prose**::

    log.info("run.dispatch", item=item.item_id, run=run_id, attempt=2)
    log.warning("github.claim_failed", issue=12, exc_info=True)

House style
-----------
- The first argument is a stable, dotted event name — ``subsystem.verb_object``
  (``daemon.starting``, ``sbx.invoke``, ``discord.steer``). Everything that
  varies goes into keyword arguments; never format values into the event
  string and never pass f-strings.
- Levels: DEBUG is per-call chatter (tool calls, sbx invocations, polls that
  found nothing); INFO is lifecycle (start/finish, claims, operator commands,
  the startup summary); WARNING is degraded-but-continuing (retry, fallback,
  a swallowed error, breaker open); ERROR means a human has to look (crash,
  delivery failed for good, an item abandoned).
- Catching an exception and carrying on? Log it with ``exc_info=True``.
- Correlation ids are keyword arguments — ``run=``, ``item=``, ``job=``,
  ``task=`` — so they can be grepped/filtered whatever the renderer. Inside
  the per-run engine thread :func:`bind_run` stamps them on every record.
- Never log a secret: subprocess argv goes through
  :func:`sbxloop.sbx.cli.redacted_argv`; :func:`redact_secrets` masks any
  keyword whose name says it is a credential.

The daemon configures the pipeline once via :func:`configure_logging`
(``[daemon] log_level`` / ``--log-level``, ``log_format`` console|json).
Structlog is wired *through* ``logging`` so third-party stdlib loggers
(discord.py, httpx) render in the same shape and pytest's ``caplog`` sees
every record.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any, Literal, TextIO

import structlog

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LogFormat = Literal["console", "json"]

REDACTED = "***"

# Loggers whose INFO output is noise in a daemon journal (gateway heartbeats,
# every HTTP request). Kept at WARNING unless the operator asks for DEBUG,
# and even then only raised to INFO — never their own DEBUG firehose.
THIRD_PARTY_LOGGERS: tuple[str, ...] = (
    "discord",
    "httpx",
    "httpcore",
    "asyncio",
    "urllib3",
    "git",
)

_SECRET_KEY = re.compile(
    r"(^|_)(token|secret|password|passwd|authorization|api_key|apikey|pat)($|_)", re.I
)
_HANDLER_MARK = "_sbxloop_log_handler"


class _StderrHandler(logging.StreamHandler):  # type: ignore[type-arg,unused-ignore]
    """A StreamHandler that looks ``sys.stderr`` up per record, so a stream
    swapped after configuration (test runners, redirects) is honoured."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, _value: Any) -> None:  # pragma: no cover - base __init__ assigns
        pass


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """The module logger: ``log = get_logger(__name__)``."""
    return structlog.stdlib.get_logger(name)


def redact_secrets(
    _logger: Any, _method: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    """Mask the value of any key that names a credential (``token``,
    ``github_pat``, ``api_key`` …) — a belt to ``redacted_argv``'s braces."""
    for key, value in event_dict.items():
        if value not in (None, "") and _SECRET_KEY.search(key):
            event_dict[key] = REDACTED
    return event_dict


def drop_none_fields(
    _logger: Any, _method: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    """Drop keys whose value is ``None``.

    Call sites pass optional facts straight through (``error=result.error``,
    ``cwd=job.cwd``, ``template=spec.template``); an absent fact rendered
    as ``error=None cwd=None template=None`` on every line is noise that
    hides the fields that do carry information. Absence is the record."""
    for key in [k for k, v in event_dict.items() if v is None]:
        del event_dict[key]
    return event_dict


def bind_run(run_id: str, item_id: str | None = None, **extra: Any) -> None:
    """Stamp ``run=`` (and ``item=``) on every record logged from this
    thread until :func:`clear_run`. Context vars do not cross
    ``threading.Thread`` — call this *inside* the run thread."""
    fields: dict[str, Any] = {"run": run_id, **extra}
    if item_id is not None:
        fields["item"] = item_id
    structlog.contextvars.bind_contextvars(**fields)


def clear_run() -> None:
    structlog.contextvars.clear_contextvars()


def _level_no(level: str) -> int:
    value = logging.getLevelName(level.upper())
    if not isinstance(value, int):
        raise ValueError(f"unknown log level {level!r}")
    return value


def configure_logging(
    level: str = "INFO", *, fmt: LogFormat = "console", stream: TextIO | None = None
) -> None:
    """Configure structlog + the stdlib root logger. Idempotent: calling it
    again replaces the handler it installed earlier (and nothing else — a
    test runner's capture handlers stay put) and re-applies the level.
    With no explicit ``stream`` records go to whatever ``sys.stderr`` is at
    emit time."""
    level_no = _level_no(level)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        redact_secrets,
        drop_none_fields,
    ]
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    renderer: Any
    if fmt == "json":
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
        tail: list[Any] = [
            structlog.processors.dict_tracebacks,
            structlog.processors.UnicodeDecoder(),
            renderer,
        ]
    else:
        target = stream if stream is not None else sys.stderr
        renderer = structlog.dev.ConsoleRenderer(
            colors=bool(getattr(target, "isatty", lambda: False)()),
            exception_formatter=structlog.dev.plain_traceback,
        )
        tail = [renderer]
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *tail],
        foreign_pre_chain=shared,
    )
    handler: logging.Handler = (
        logging.StreamHandler(stream) if stream is not None else _StderrHandler()
    )
    handler.setFormatter(formatter)
    setattr(handler, _HANDLER_MARK, True)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARK, False):
            root.removeHandler(existing)
            existing.close()
    root.addHandler(handler)
    root.setLevel(level_no)
    third_party = logging.INFO if level_no <= logging.DEBUG else logging.WARNING
    for name in THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(third_party)


__all__ = [
    "REDACTED",
    "THIRD_PARTY_LOGGERS",
    "LogFormat",
    "LogLevel",
    "bind_run",
    "clear_run",
    "configure_logging",
    "get_logger",
    "redact_secrets",
]
