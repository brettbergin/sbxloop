"""What a run's Gitea token must be allowed to do (#1021).

A Gitea access token cannot read its own record or scopes (#1016 V6:
``GET /users/{name}/tokens`` takes basic auth only), so the doctor asks
each read endpoint directly through :data:`READ_PROBES` and reads the
write level off the repository's ``permissions.push`` bit, the same road
a fine-grained GitHub PAT takes. The needs table is the loop's own
(:data:`~sbxloop.vcs.github.permissions.NEEDS`).
"""

from __future__ import annotations

# The read a token is asked to prove each permission with, Gitea's own
# paths: ``{repo}`` is ``owner/name``, ``{base}`` the base branch.
# 401/403 means the permission is not there; anything else means it is.
READ_PROBES: dict[str, str] = {
    "contents": "/repos/{repo}/contents?ref={base}",
    "issues": "/repos/{repo}/issues?limit=1",
    "pull_requests": "/repos/{repo}/pulls?limit=1",
    "checks": "/repos/{repo}/commits/{base}/statuses?limit=1",
    "actions": "/repos/{repo}/actions/tasks?limit=1",
}
