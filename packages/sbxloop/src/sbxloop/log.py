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
  keyword whose name says it is a credential; :func:`redact_text` masks
  credential *shapes* inside free-form text (a command line, captured
  stdout/stderr) where there is no key name to go on. The Discord bridge
  runs every command and output excerpt it renders through
  :func:`redact_text` before publishing it to a thread.

The daemon configures the pipeline once via :func:`configure_logging`
(``[daemon] log_level`` / ``--log-level``, ``log_format`` console|json).
Structlog is wired *through* ``logging`` so third-party stdlib loggers
(discord.py, httpx) render in the same shape and pytest's ``caplog`` sees
every record.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import logging.handlers
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NamedTuple, TextIO

import structlog

from sbxloop.ghids import normalize_item_id

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

#: Credential-ish key vocabulary, shared by the key-name and text scrubbers.
#: NOTE: a worker-side twin lives in ``sbxloop_worker.secrets`` (the worker
#: cannot import sbxloop); when adding a word here, consider whether the twin
#: needs it too — see "Redaction at the render seam" in docs/architecture.md.
_SECRET_WORDS = r"token|secret|password|passwd|api[_-]?key|apikey|pat"  # nosec B105 - regex vocabulary of credential key names, not a credential

_SECRET_KEY = re.compile(rf"(^|_)(authorization|{_SECRET_WORDS})($|_)", re.I)
_HANDLER_MARK = "_sbxloop_log_handler"

#: How many rendered log lines the in-process ring buffer keeps.
LOG_BUFFER_MAXLEN = 2000
#: The daemon's log file under the home: rotated by size, this many kept.
DAEMON_LOG_MAX_BYTES = 20 * 1024 * 1024
DAEMON_LOG_BACKUPS = 5


class LogRecordLine(NamedTuple):
    """One buffered, already-rendered log line."""

    timestamp: str
    level: str
    logger: str
    line: str


class LogBuffer:
    """A bounded, in-process ring of rendered log lines.

    Backed by :class:`collections.deque` with a ``maxlen``, so a long-lived
    daemon cannot grow memory: the oldest line is dropped as a new one
    arrives. Appends are a single atomic deque operation — no locks, no I/O.
    """

    def __init__(self, maxlen: int = LOG_BUFFER_MAXLEN) -> None:
        self._records: collections.deque[LogRecordLine] = collections.deque(maxlen=maxlen)

    def append(self, record: LogRecordLine) -> None:
        self._records.append(record)

    def __len__(self) -> int:
        return len(self._records)

    def clear(self) -> None:
        self._records.clear()

    def tail(
        self, n: int = 50, level: str | None = None, grep: str | None = None
    ) -> list[LogRecordLine]:
        """The most recent ``n`` records that survive the filters.

        ``level`` keeps records at or above that level (case-insensitive;
        ``ValueError`` for an unknown name). ``grep`` is a plain
        case-insensitive substring test against the rendered line — never a
        regular expression. The result is oldest-first (most recent last).
        """
        if n <= 0:
            return []
        records = list(self._records)
        if level is not None:
            threshold = _level_no(level)
            records = [r for r in records if _level_no_safe(r.level) >= threshold]
        if grep:
            needle = grep.lower()
            records = [r for r in records if needle in r.line.lower()]
        return records[-n:]


_LOG_BUFFER = LogBuffer()


def log_buffer() -> LogBuffer:
    """The process-wide ring buffer that :func:`configure_logging` fills."""
    return _LOG_BUFFER


class _RingBufferHandler(logging.Handler):
    """Renders each record into the bounded :class:`LogBuffer`.

    Never raises and never blocks: formatting failures are swallowed so a bad
    record can't take down a logging call on the hot path.
    """

    def __init__(self, buffer: LogBuffer) -> None:
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):  # defensive; never break logging
            line = self.format(record).rstrip("\n")
            self._buffer.append(
                LogRecordLine(
                    timestamp=datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
                    level=record.levelname,
                    logger=record.name,
                    line=line,
                )
            )


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


# Provider token shapes that are secrets wherever they appear, with no key
# name to go on: GitHub's ``ghp_``/``gho_``/``ghu_``/``ghs_``/``ghr_`` and
# the fine-grained ``github_pat_`` prefix, plus Anthropic API keys
# (``sk-ant-``, the claude agent backend, #533).
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"),
)

_AUTH_RE = re.compile(r"(?i)\b(authorization\s*[:=]\s*)((?:bearer|token|basic)\s+)?([^\s'\"]+)")
# The credential word must be a whole delimiter-separated segment of the
# name (`GITHUB_TOKEN`, `--api-key`, `my.secret`), not a substring of an
# ordinary word — otherwise `PATH=`, `--patch`, `compat=1` and `patch: 3`
# all get masked and the rendered command becomes unreadable.
_FLAG_RE = re.compile(
    rf"(?i)(--?(?:[A-Za-z0-9]+-)*(?:{_SECRET_WORDS})(?:-[A-Za-z0-9]+)*)([=\s]+)"
    r"(['\"]?)([^\s'\"]+)"
)
_ASSIGN_RE = re.compile(
    rf"(?i)(?<![\w.-])((?:[A-Za-z0-9]+[_.-])*(?:{_SECRET_WORDS})(?:[_.-][A-Za-z0-9]+)*)"
    r"(\s*[:=]\s*)(['\"]?)([^\s'\"]+)"
)


def _mask_value(match: re.Match[str]) -> str:
    """Keep everything but the credential itself; leave already-masked
    values (anything starting with ``*``) alone so redaction is idempotent."""
    prefix, sep, quote, value = match.group(1), match.group(2), match.group(3), match.group(4)
    if value.startswith("*"):
        return match.group(0)
    return f"{prefix}{sep}{quote}{REDACTED}{quote}"


def _mask_auth(match: re.Match[str]) -> str:
    head, scheme, value = match.group(1), match.group(2) or "", match.group(3)
    if value.startswith("*"):
        return match.group(0)
    return f"{head}{scheme}{REDACTED}"


def redact_text(text: str) -> str:
    """Mask credential *shapes* inside free-form text.

    Where :func:`redact_secrets` masks by key *name* in an event dict, this
    works on text that has no keys: a command line, captured stdout/stderr,
    an excerpt about to be published to a Discord thread. It covers GitHub
    token prefixes (``ghp_``…, ``github_pat_``), ``Authorization: Bearer
    <token>`` headers, ``NAME=value`` / ``NAME: value`` assignments whose
    name reads as a credential, and ``--token=…`` style CLI flags,
    replacing the credential with :data:`REDACTED`.

    Guarantees: ordinary text comes back unchanged, the function is
    idempotent (already-masked text is left alone) and it never raises —
    on unexpected input it returns the input coerced to ``str``."""
    try:
        out = str(text)
    except Exception:  # pragma: no cover - str() on a hostile __str__
        return ""
    if not out:
        return out
    try:
        for pattern in _TOKEN_PATTERNS:
            out = pattern.sub(REDACTED, out)
        out = _AUTH_RE.sub(_mask_auth, out)
        out = _FLAG_RE.sub(_mask_value, out)
        out = _ASSIGN_RE.sub(_mask_value, out)
    except Exception:  # pragma: no cover - defensive: never break a render
        return str(text)
    return out


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
    ``threading.Thread`` — call this *inside* the run thread.

    ``item_id`` is normalised through :func:`sbxloop.ghids.normalize_item_id`,
    so every log line carries the typed ``gh:issue:<n>`` / ``gh:pr:<n>``
    form even when the caller still holds a legacy id."""
    fields: dict[str, Any] = {"run": run_id, **extra}
    if item_id is not None:
        # Canonicalise at the boundary: a legacy ``gh:<n>`` id read from an
        # old checkpoint must not leak the ambiguous form into log lines.
        fields["item"] = normalize_item_id(item_id)
    structlog.contextvars.bind_contextvars(**fields)


def clear_run() -> None:
    structlog.contextvars.clear_contextvars()


def _level_no(level: str) -> int:
    value = logging.getLevelName(level.upper())
    if not isinstance(value, int):
        raise ValueError(f"unknown log level {level!r}")
    return value


def _level_no_safe(level: str) -> int:
    """Numeric level for a buffered record's level name; 0 if unrecognised."""
    value = logging.getLevelName(level.upper())
    return value if isinstance(value, int) else 0


def configure_logging(
    level: str = "INFO",
    *,
    fmt: LogFormat = "console",
    stream: TextIO | None = None,
    file: Path | None = None,
    file_max_bytes: int = DAEMON_LOG_MAX_BYTES,
    file_backups: int = DAEMON_LOG_BACKUPS,
) -> None:
    """Configure structlog + the stdlib root logger. Idempotent: calling it
    again replaces the handlers it installed earlier (and nothing else — a
    test runner's capture handlers stay put) and re-applies the level.
    With no explicit ``stream`` records go to whatever ``sys.stderr`` is at
    emit time.

    ``file`` adds a second copy of every record to that path — the daemon's
    ``logs/daemon.log`` under the home — rotated by size
    (``file_max_bytes`` each, ``file_backups`` kept), plain text or JSON as
    ``fmt`` says, never coloured. stderr keeps flowing to journald under
    systemd; the file is the copy a host without a journal, a ``tail -f``
    over ssh, or the console reads.
    """
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
        buffer_tail: list[Any] = tail
    else:
        target = stream if stream is not None else sys.stderr
        renderer = structlog.dev.ConsoleRenderer(
            colors=bool(getattr(target, "isatty", lambda: False)()),
            exception_formatter=structlog.dev.plain_traceback,
        )
        tail = [renderer]
        # Plain text for the buffer: greppable, and safe to paste into chat.
        buffer_tail = [
            structlog.dev.ConsoleRenderer(
                colors=False, exception_formatter=structlog.dev.plain_traceback
            )
        ]

    def _formatter(tail_processors: list[Any]) -> structlog.stdlib.ProcessorFormatter:
        return structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                *tail_processors,
            ],
            foreign_pre_chain=shared,
        )

    formatter = _formatter(tail)
    handler: logging.Handler = (
        logging.StreamHandler(stream) if stream is not None else _StderrHandler()
    )
    handler.setFormatter(formatter)
    setattr(handler, _HANDLER_MARK, True)

    ring_handler = _RingBufferHandler(_LOG_BUFFER)
    ring_handler.setFormatter(_formatter(buffer_tail))
    ring_handler.setLevel(level_no)
    setattr(ring_handler, _HANDLER_MARK, True)

    file_handler: logging.Handler | None = None
    if file is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            file, maxBytes=file_max_bytes, backupCount=file_backups, encoding="utf-8"
        )
        # The same plain rendering the ring buffer keeps: greppable, no colour.
        file_handler.setFormatter(_formatter(buffer_tail))
        file_handler.setLevel(level_no)
        setattr(file_handler, _HANDLER_MARK, True)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARK, False):
            root.removeHandler(existing)
            existing.close()
    root.addHandler(handler)
    root.addHandler(ring_handler)
    if file_handler is not None:
        root.addHandler(file_handler)
    root.setLevel(level_no)
    third_party = logging.INFO if level_no <= logging.DEBUG else logging.WARNING
    for name in THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(third_party)


__all__ = [
    "DAEMON_LOG_BACKUPS",
    "DAEMON_LOG_MAX_BYTES",
    "LOG_BUFFER_MAXLEN",
    "REDACTED",
    "THIRD_PARTY_LOGGERS",
    "LogBuffer",
    "LogFormat",
    "LogLevel",
    "LogRecordLine",
    "bind_run",
    "clear_run",
    "configure_logging",
    "get_logger",
    "log_buffer",
    "redact_secrets",
    "redact_text",
]
