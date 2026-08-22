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
