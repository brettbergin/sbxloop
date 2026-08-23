"""What a usable credential looks like from inside a sandbox.

sbx's secret proxy does not put the real value in the sandbox: it exports a
*sentinel* (``sbx-cs-…``) and swaps in the secret on the way out to the bound
host. That works for anything that just puts the value in a header, and not at
all for a client that inspects it — the Copilot SDK validates the token format
before it will open a session, and `gh` uses the token to fetch its own user.

So "is the secret env set?" is the wrong question; ``test -n`` cannot tell a
token from a sentinel. Both the provisioner (deciding whether the proxy path
works) and the worker (deciding whether its env file should win) need the same
answer, so the predicate lives here rather than being spelled twice.
"""

from __future__ import annotations

import re

# gho_/ghu_ user-to-server, ghp_ classic PAT, ghs_ server-to-server,
# github_pat_ fine-grained.
GITHUB_TOKEN_PREFIXES = ("gho_", "ghu_", "ghp_", "ghs_", "github_pat_")

# sbx's proxy placeholder. Narrow on purpose: it is the one value we will
# override an existing environment variable for.
SBX_SENTINEL_PREFIX = "sbx-cs-"


def looks_like_github_token(value: str) -> bool:
    """Would a GitHub client accept this as a credential?"""
    return value.startswith(GITHUB_TOKEN_PREFIXES)


def is_sbx_sentinel(value: str) -> bool:
    """Is this sbx's proxy placeholder rather than a real secret?"""
    return value.startswith(SBX_SENTINEL_PREFIX)


def shell_token_case() -> str:
    """The `case` pattern a probe uses to classify a value inside the VM,
    kept in step with :data:`GITHUB_TOKEN_PREFIXES`."""
    return "|".join(f"{prefix}*" for prefix in GITHUB_TOKEN_PREFIXES)


# --- Redaction of free text -------------------------------------------------
#
# Mirrors :func:`sbxloop.log.redact_secrets` in intent, but operates on *text*
# rather than a structlog event dict: tool output is published to run threads,
# so anything credential-shaped must be masked inside the worker, before it
# leaves as an event. The worker cannot import sbxloop (it depends only on
# pydantic), so the patterns are spelled here.

REDACTED = "***"

_TOKEN_CHARS = r"[A-Za-z0-9_\-]"  # nosec B105 - regex character class, not a credential

_REDACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # GitHub tokens, keyed off the same prefixes used above.
    re.compile(r"(?:gho_|ghu_|ghp_|ghs_|github_pat_)[A-Za-z0-9_]{8,}"),
    # sbx secret-proxy sentinels.
    re.compile(rf"{SBX_SENTINEL_PREFIX}{_TOKEN_CHARS}{{4,}}"),
    # AWS access key ids.
    re.compile(r"AKIA[0-9A-Z]{16}"),
)

# `Bearer <token>` headers: keep the scheme, mask the credential.
_BEARER = re.compile(r"\b(Bearer|Basic|token)\s+([A-Za-z0-9._\-+/=]{8,})")

# NOTE: this vocabulary has a host-side twin in ``sbxloop.log`` (used by
# ``redact_text``); the two are spelled separately because the worker cannot
# import sbxloop. When adding a word here, consider whether the twin needs it
# too — see "Redaction at the render seam" in docs/architecture.md.
_SECRET_WORDS = r"token|secret|password|passwd|api[-_]?key|credentials?|authorization"  # nosec B105 - regex vocabulary of credential key names, not a credential
# The credential word must be a whole delimiter-separated segment of the name
# (`GITHUB_TOKEN`, `aws.credentials`), not a substring of an ordinary word —
# otherwise a pytest `tokens: 5` summary or `compat=1` gets masked and the
# published excerpt lies about what the tool printed.
_SECRET_NAME = rf"(?:[A-Za-z0-9]+[_.\-])*(?:{_SECRET_WORDS})(?:[_.\-][A-Za-z0-9]+)*"

# KEY=VALUE (env/CLI style) where the key names a credential.
_ASSIGNMENT = re.compile(rf"\b({_SECRET_NAME})(\s*=\s*)(\"[^\"]*\"|'[^']*'|\S+)", re.IGNORECASE)

# "key": "value" / key: value (JSON/YAML style).
# The value is left alone when it is already a masked ``Bearer ***`` header,
# so an Authorization line keeps its scheme visible.
_MAPPING = re.compile(
    rf"(\"?{_SECRET_NAME}\"?\s*:\s*)"
    rf"(?!(?:Bearer|Basic|token)\b)"
    rf"(\"[^\"]*\"|'[^']*'|[^,;\s}}]+)",
    re.IGNORECASE,
)


def redact_secrets(text: str) -> str:
    """Mask credential-looking substrings in free text.

    Total by construction: never raises, and returns its input unchanged
    when nothing matches.
    """
    if not text:
        return text
    try:
        for pattern in _REDACTION_PATTERNS:
            text = pattern.sub(REDACTED, text)
        text = _BEARER.sub(rf"\1 {REDACTED}", text)
        text = _ASSIGNMENT.sub(rf"\1\2{REDACTED}", text)
        text = _MAPPING.sub(rf"\1{REDACTED}", text)
    except (re.error, RuntimeError):  # pragma: no cover - defensive
        return REDACTED
    return text
