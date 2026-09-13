"""What Gitea reports a branch requires before a merge (#1021).

Field-verified on Gitea 1.24.7 (#1016 V4): a write collaborator reads
``GET /repos/{repo}/branches/{base}`` and gets the required contexts
(``status_check_contexts`` when ``enable_status_check``), the approval
count (``required_approvals``) and whether the token may merge
(``user_can_merge``), while ``GET .../branch_protections/{base}`` is a 403
for anyone below admin. So the branch is the source for a run's token,
``source = "branch"``, and the flags only an admin reads (dismiss stale
approvals, signed commits, rejected reviews blocking the merge) are left
**unknown** with ``unread = ("protection",)`` rather than read as off.
With an admin token the rule is read too, ``source = "branch+protection"``.
"""

from __future__ import annotations

from typing import Any

from sbxloop.errors import GithubOpsError
from sbxloop.vcs.model import BaseRequirements

FORGE = "gitea"


def read_base_requirements(ops: Any, repo: str, base: str) -> BaseRequirements:
    """``base``'s requirements from the branch view and, for an admin
    token, the protection rule; never raises."""
    path = f"/repos/{repo}/branches/{base}"
    try:
        branch = ops.raw_lookup("GET", path)
    except GithubOpsError:
        branch = None
    if not isinstance(branch, dict):
        return BaseRequirements(None, None, "unknown", unread=("branch",), forge=FORGE)
    if not branch.get("protected"):
        return BaseRequirements((), 0, "branch", forge=FORGE)
    contexts = branch.get("status_check_contexts")
    required = (
        tuple(str(c) for c in contexts if c)
        if branch.get("enable_status_check") and isinstance(contexts, list)
        else ()
    )
    approvals = branch.get("required_approvals")
    extra: tuple[str, ...] = ()
    if branch.get("user_can_merge") is False:
        extra = (
            f"Gitea reports that this token may not merge into {base} (user_can_merge is "
            "false); give the token's account merge access on the branch's protection rule",
        )
    rule = _protection_rule(ops, repo, base)
    if rule is None:
        return BaseRequirements(
            required,
            int(approvals) if isinstance(approvals, int) else 0,
            "branch",
            unread=("protection",),
            forge=FORGE,
            extra_blockers=extra,
        )
    return BaseRequirements(
        required,
        int(approvals) if isinstance(approvals, int) else 0,
        "branch+protection",
        dismiss_stale_reviews=rule.get("dismiss_stale_approvals") is True,
        signed_commits=rule.get("require_signed_commits") is True,
        forge=FORGE,
        extra_blockers=extra,
    )


def _protection_rule(ops: Any, repo: str, base: str) -> dict[str, Any] | None:
    """The protection rule when the token may read it (admin), else
    ``None``: a 403 is the field-verified answer for a write
    collaborator, a 404 a rule that is not there."""
    try:
        rule = ops.raw_lookup("GET", f"/repos/{repo}/branch_protections/{base}", missing=(403, 404))
    except GithubOpsError:
        return None
    return rule if isinstance(rule, dict) else None
