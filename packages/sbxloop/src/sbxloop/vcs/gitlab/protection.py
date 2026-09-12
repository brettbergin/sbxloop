"""What a GitLab base branch requires of a merge request before it may
merge, as :class:`~sbxloop.vcs.model.BaseRequirements` (#1017).

Field-verified on GitLab CE 19.3.2 (#1016, V2), with the credential a run
holds — a Developer:

- the protected branch (``GET /projects/:id/protected_branches/:name``)
  is readable, and a 404 is "not protected": an answer;
- the project's merge settings (``GET /projects/:id``) are readable:
  ``only_allow_merge_if_pipeline_succeeds`` means the *whole* pipeline
  must be green — CE names no required context, so the requirements
  carry ``all_checks_required`` with an empty ``required_contexts`` —
  and ``only_allow_merge_if_all_discussions_are_resolved`` is the
  conversation-resolution rule;
- ``GET /metadata`` says whether the instance is an enterprise edition.
  On the free tier required approvals do not exist (the approval
  endpoints are 404, not 403), so ``approvals_required`` is ``0``: a
  real answer. On an enterprise instance the approval rules are read
  (**field-unverified**, no Premium instance was available) and an
  unreadable rule set leaves the count ``None``.

The reading is advisory and never raises: an unreadable source is
reported as *unknown* with ``unread`` naming it, so the landing gates on
everything and the doctor says "unverifiable" rather than "fine".
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from sbxloop.errors import GithubOpsError
from sbxloop.log import get_logger
from sbxloop.vcs.model import BaseRequirements

log = get_logger(__name__)

FORGE = "gitlab"
UNKNOWN = BaseRequirements(None, None, "unknown", forge=FORGE)


def read_base_requirements(ops: Any, repo: str, base: str) -> BaseRequirements:
    """Read ``base``'s requirements. ``ops`` needs ``raw`` and
    ``raw_lookup``; the project path is spelt here."""
    project_path = f"/projects/{quote(repo, safe='')}"
    project = _read(ops, project_path)
    protected = _read(
        ops, f"{project_path}/protected_branches/{quote(base, safe='')}", missing=(404,)
    )
    enterprise = _enterprise(ops)

    unread = tuple(
        name
        for name, reading in (("project", project), ("protected_branch", protected))
        if reading is _UNREAD
    )
    settings = project if isinstance(project, dict) else {}
    rule = protected if isinstance(protected, dict) else {}
    all_checks = settings.get("only_allow_merge_if_pipeline_succeeds") is True
    conversation = settings.get("only_allow_merge_if_all_discussions_are_resolved") is True
    code_owners = rule.get("code_owner_approval_required") is True

    approvals: int | None
    if enterprise is False:
        approvals = 0
    elif enterprise is True:
        approvals = _approvals_required(ops, project_path, base)
    else:
        approvals = None
    if unread:
        return BaseRequirements(
            None,
            approvals,
            "unknown",
            code_owner_review=code_owners,
            conversation_resolution=conversation,
            unread=unread,
            forge=FORGE,
            all_checks_required=all_checks,
        )
    return BaseRequirements(
        (),
        approvals,
        "protected_branch+project" if protected is not None else "project",
        code_owner_review=code_owners,
        conversation_resolution=conversation,
        forge=FORGE,
        all_checks_required=all_checks,
    )


_UNREAD = object()


def _read(ops: Any, path: str, *, missing: tuple[int, ...] = ()) -> Any:
    """One read: the payload, ``None`` for a status in ``missing`` (an
    answer), or :data:`_UNREAD` for any other failure."""
    try:
        if missing:
            return ops.raw_lookup("GET", path, missing=missing)
        return ops.raw("GET", path)
    except GithubOpsError as exc:
        log.info("gitlab.protection_unreadable", path=path, error=str(exc))
        return _UNREAD
    except Exception as exc:  # nosec B110 - advisory probe; the fallback is "gate on all"
        log.info("gitlab.protection_unreadable", path=path, error=str(exc))
        return _UNREAD


def _enterprise(ops: Any) -> bool | None:
    """Whether the instance is an enterprise edition, from ``GET /metadata``
    (``"enterprise": false`` on CE, field-verified); ``None`` when the
    token cannot read it."""
    data = _read(ops, "/metadata")
    if not isinstance(data, dict):
        return None
    flag = data.get("enterprise")
    return flag if isinstance(flag, bool) else None


def _approvals_required(ops: Any, project_path: str, base: str) -> int | None:
    """The approvals an enterprise project requires for ``base``: the
    largest ``approvals_required`` among the rules that apply to it —
    every rule with no branch restriction, plus the ones naming the
    branch. **Field-unverified** (no Premium instance in #1016)."""
    rules = _read(ops, f"{project_path}/approval_rules")
    if not isinstance(rules, list):
        return None
    required = 0
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        branches = rule.get("protected_branches")
        applies = not branches or any(
            isinstance(b, dict) and b.get("name") == base for b in branches
        )
        if applies:
            try:
                required = max(required, int(rule.get("approvals_required") or 0))
            except (TypeError, ValueError):
                continue
    return required
