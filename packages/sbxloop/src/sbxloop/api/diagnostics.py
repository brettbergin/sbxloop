"""Bounded diagnostics for a remote reader: the daemon's recent log
records, redacted, and the allowlisted effective configuration with its
provenance — never a secret value, never a host path (#1040).

The log lines come from the same in-process ring buffer ``ctl log`` and
the concierge read; nothing is read from disk. The principle that secrets
never appear in logs holds upstream, and this layer still masks the shapes
a credential takes in text before a line leaves the host, so a library's
debug line or a pasted header cannot undo it.

The configuration is the one this daemon runs on (its loaded ``Config``),
restricted to the sections a remote operator has business reading and
with every key that names a host location or could carry a credential
left out by name. Provenance says which layer answers for the key now
(``home config``, ``env``, ``default``…), whether a change applies live
or at the next start, and what keeps the daemon's own tools from changing
it — so a reader can tell a setting from the file it came from, and a
value the running daemon has from one an edit has changed on disk.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from sbxloop.config import Config, load_config_with_sources
from sbxloop.configedit import keys as configkeys
from sbxloop.configedit.docs import doc_for
from sbxloop.configedit.editor import applies_for
from sbxloop.daemon import configpolicy
from sbxloop.log import get_logger

log = get_logger(__name__)

REDACTED = "[redacted]"

#: The shapes a credential takes in text. Each is replaced whole; a
#: pattern with a group keeps the label before the value.
_SECRET_SHAPES: tuple[re.Pattern[str], ...] = (
    # A bearer credential in a header or a log line.
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    # A JSON web token: three base64url segments.
    re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b"),
    # The API's own client secrets and refresh tokens.
    re.compile(r"\b(?:sk|rt)_[A-Za-z0-9_-]{8,}\b"),
    # GitHub, GitLab and Slack tokens by prefix.
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}\b"),
    # ``token=…``, ``secret: …``, ``password="…"``, ``authorization: …``.
    re.compile(
        r"(?i)\b((?:token|secret|password|passwd|api[_-]?key|authorization|client_secret)"
        r"\s*[=:]\s*[\"']?(?:(?:bearer|basic)\s+)?)([^\s\"',;]+)"
    ),
)


def redact(text: str) -> str:
    """``text`` with every credential-shaped run replaced by ``[redacted]``."""
    for shape in _SECRET_SHAPES:
        if shape.groups:
            text = shape.sub(lambda m: m.group(1) + REDACTED, text)
        else:
            text = shape.sub(REDACTED, text)
    return text


# -- configuration ---------------------------------------------------------------

#: The top-level sections a remote operator may read. Not here, by
#: design: the listener's own ``api`` (its bind and who it trusts), the
#: chat backends (channel ids and hooks), the console, and the root keys
#: that name the host (``home``, the worker interpreter, the transports).
CONFIGURATION_SECTIONS: tuple[str, ...] = (
    "agent",
    "artifacts",
    "budgets",
    "concierge",
    "daemon",
    "entrygraph",
    "github",
    "landing",
    "limits",
    "model",
    "policy",
    "telemetry",
    "vcs",
    "workload",
    "workloads",
    "schedules",
)

#: A key segment that names a host location or could carry a credential
#: keeps the key out, whatever section it is in. A segment ending in
#: ``_env`` names an environment variable, never its value, and stays.
_HIDDEN_SEGMENT = re.compile(
    r"(?i)(?:^|_)(?:path|paths|dir|dirs|directory|file|files|root|home|workspace|"
    r"secret|secrets|token|tokens|password|passwd|key|keys|cookie|webhook|dsn|url|urls|"
    r"command|commands|args|argv)(?:$|_)"
)
_INDEX = re.compile(r"\[\d+\]")


def _visible(key: str) -> bool:
    section = key.split(".", 1)[0].split("[", 1)[0]
    if section not in CONFIGURATION_SECTIONS:
        return False
    for segment in _INDEX.sub("", key).split("."):
        if segment.endswith("_env"):
            continue
        if _HIDDEN_SEGMENT.search(segment):
            return False
    return True


def _resolved_now(config: Config) -> tuple[dict[str, Any], dict[str, str]] | None:
    """What the loader resolves for this home right now, with the layer
    each key came from — ``None`` when it cannot be loaded (a broken edit
    on disk), which is itself a fact the reader should see as unknown."""
    env: dict[str, str] = {**os.environ, "SBXLOOP_HOME": str(config.home)}
    try:
        loaded, sources = load_config_with_sources(cwd=config.home, env=env)
    except Exception:
        log.debug("api.configuration_reload_failed", exc_info=True)
        return None
    return configkeys.flatten(loaded.model_dump(mode="json")), sources


def configuration(config: Config, *, sources: bool = True) -> list[dict[str, Any]]:
    """Every visible key of the running configuration, with provenance."""
    live = configkeys.flatten(config.model_dump(mode="json"))
    resolved = _resolved_now(config) if sources else None
    locks = list(config.concierge.config_locked)
    rows: list[dict[str, Any]] = []
    for key in sorted(live):
        if not _visible(key):
            continue
        value = live[key]
        bare = _INDEX.sub("", key)
        source: str | None = None
        pending = False
        if resolved is not None:
            flat_now, layers = resolved
            source = configkeys.source_for(key, layers) if key in flat_now else "unset"
            pending = key in flat_now and flat_now[key] != value
        locked = configpolicy.never_from_chat(bare) or configpolicy.locked_by(bare, locks)
        rows.append(
            {
                "key": key,
                "value": _scrub(value),
                "source": source,
                "applies": applies_for(bare),
                "pending": pending,
                "locked": locked,
                "doc": doc_for(bare),
            }
        )
    return rows


def _scrub(value: Any) -> Any:
    """A value as text goes through the same masking the log lines do;
    a scalar that is not text is what it is."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _scrub(v) for k, v in value.items()}
    return value


__all__ = ["CONFIGURATION_SECTIONS", "REDACTED", "configuration", "redact"]
