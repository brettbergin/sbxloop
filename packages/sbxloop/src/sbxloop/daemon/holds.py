"""Named pause holds (#534).

``pause`` used to be one boolean, which made the deploy pipeline's
snapshot-at-start / restore-at-end choreography racy: an operator ``pause``
issued while a deploy was in flight was overwritten by the deploy's restore
step (seen twice on 2026-08-29). A pause is now a *set of named holds*: the
operator's bare ``pause`` takes :data:`OPERATOR_HOLD`, a deploy takes
``deploy-<run id>``, and each side releases only its own. The daemon idles
while any hold stands.

Kept in its own module so the control dispatcher can name the default hold
without importing the loop.
"""

from __future__ import annotations

import re

# The hold a bare `pause` / `resume` acts on.
OPERATOR_HOLD = "operator"
_HOLD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def hold_name(hold: str) -> str:
    """Validate a hold name. It is echoed in status lines a shell script
    greps and in Discord, so it is an identifier-ish token: no whitespace,
    no markup."""
    if not _HOLD_RE.match(hold):
        raise ValueError(
            f"invalid hold name {hold!r}: letters, digits, '.', '_' and '-' only (max 64)"
        )
    return hold
