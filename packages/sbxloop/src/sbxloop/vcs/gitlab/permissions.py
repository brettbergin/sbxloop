"""What a run's GitLab token must be allowed to do (#1017).

GitLab scopes are coarse: ``api`` is every read and write the loop makes
and ``read_api`` only the reads. The doctor compares the token's scopes
(``GET /personal_access_tokens/self``, field-verified in #1016 V6) against
the loop's needs — the same :data:`~sbxloop.vcs.github.permissions.NEEDS`
table, whose feature wording is the loop's, not GitHub's — and, for a
token that cannot read its own scopes, asks each read endpoint directly
through :data:`READ_PROBES`.
"""

from __future__ import annotations

from collections.abc import Iterable

from sbxloop.vcs.github.permissions import NEEDS, Need

# The scope that covers everything a run does, and the one that covers
# only its reads.
FULL_SCOPE = "api"
READ_SCOPE = "read_api"


def missing_from_scopes(scopes: Iterable[str]) -> tuple[Need, ...]:
    """The needs the token's scopes do not cover: nothing under ``api``;
    every write under ``read_api`` alone; everything otherwise. The
    workflows need is GitHub's own and never missing here."""
    held = frozenset(scopes)
    if FULL_SCOPE in held:
        return ()
    if READ_SCOPE in held:
        return tuple(n for n in NEEDS if n.level == "write" and n.permission != "workflows")
    return tuple(n for n in NEEDS if n.permission != "workflows")


# The read a token is asked to prove each permission with, GitLab's own
# paths: ``{project}`` is the encoded project path, ``{base}`` the base
# branch. 401/403 means the permission is not there; anything else means
# it is.
READ_PROBES: dict[str, str] = {
    "contents": "{project}/repository/tree?per_page=1&ref={base}",
    "issues": "{project}/issues?per_page=1",
    "pull_requests": "{project}/merge_requests?per_page=1",
    "checks": "{project}/repository/commits/{base}/statuses?per_page=1",
    "actions": "{project}/pipelines?per_page=1",
}
