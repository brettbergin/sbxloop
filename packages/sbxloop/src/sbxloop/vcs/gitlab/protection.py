"""What a GitLab base branch requires of a merge request before it may
merge, as :class:`~sbxloop.vcs.model.BaseRequirements` (#1017).

Field-verified on GitLab CE 19.3.2 (#1016, V2), with the credential a run
holds — a Developer:

- protected branches are readable. The paginated collection is matched
  against the base, including wildcard and inherited rules; a literal
  branch's 404 alone does not prove that the base is unprotected;
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

from collections.abc import Mapping
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import quote, urlencode

from sbxloop.errors import GithubOpsError
from sbxloop.log import get_logger
from sbxloop.vcs.jobs import MAX_PAGES, PAGE_SIZE
from sbxloop.vcs.model import ApprovalRule, BaseRequirements

log = get_logger(__name__)

FORGE = "gitlab"
UNKNOWN = BaseRequirements(None, None, "unknown", forge=FORGE)


def read_base_requirements(ops: Any, repo: str, base: str) -> BaseRequirements:
    """Read ``base``'s requirements. ``ops`` needs ``raw`` and
    ``raw_lookup``; the project path is spelt here."""
    project_path = f"/projects/{quote(repo, safe='')}"
    project = _read(ops, project_path)
    protected = _read_pages(ops, f"{project_path}/protected_branches")
    matches = []
    if isinstance(protected, list):
        if any(not isinstance(rule.get("name"), str) for rule in protected):
            protected = _UNREAD
        else:
            matches = [rule for rule in protected if fnmatchcase(base, rule["name"])]
    enterprise = _enterprise(ops)

    unread = tuple(
        name
        for name, reading in (("project", project), ("protected_branch", protected))
        if reading is _UNREAD
    )
    settings = project if isinstance(project, dict) else {}
    rule = {
        "merge_access_levels": [
            level
            for match in matches
            for field in ("merge_access_levels", "push_access_levels")
            for level in match.get(field, [])
        ]
    }
    all_checks = settings.get("only_allow_merge_if_pipeline_succeeds") is True
    conversation = settings.get("only_allow_merge_if_all_discussions_are_resolved") is True
    code_owners = any(match.get("code_owner_approval_required") is True for match in matches)
    # Merge trains are a paid tier and a per-project setting (#1019):
    # ``true`` is a train the landing enters; ``false`` and the free tier's
    # ``null`` (field-verified) are a direct merge.
    trains = settings.get("merge_trains_enabled") is True
    extra = _merge_access_blockers(settings, rule)

    approvals: int | None
    if enterprise is False:
        approvals = 0
    elif enterprise is True:
        approvals = _approvals_required(ops, project_path, base, protected=bool(matches))
    else:
        approvals = None
    if approvals is None:
        unread = (*unread, "approval_rules")
    if unread:
        return BaseRequirements(
            None,
            approvals,
            "unknown",
            code_owner_review=code_owners,
            conversation_resolution=conversation,
            merge_queue=trains,
            unread=unread,
            forge=FORGE,
            all_checks_required=all_checks,
            extra_blockers=(*extra, f"GitLab requirements could not be read: {', '.join(unread)}"),
        )
    return BaseRequirements(
        (),
        approvals,
        "protected_branch+project" if matches else "project",
        code_owner_review=code_owners,
        conversation_resolution=conversation,
        merge_queue=trains,
        forge=FORGE,
        all_checks_required=all_checks,
        extra_blockers=extra,
    )


# GitLab's access levels by name, for the merge-access reason.
_LEVEL_NAMES = {
    0: "no one",
    10: "Guests",
    20: "Reporters",
    30: "Developers",
    40: "Maintainers",
    50: "Owners",
}


def _merge_access_blockers(settings: Mapping[str, Any], rule: Mapping[str, Any]) -> tuple[str, ...]:
    """The one rule GitLab has that GitHub does not (#1019): who may merge
    into the protected branch. GitLab grants that through either
    ``merge_access_levels`` or ``push_access_levels``; a token below the
    lowest entry across both can never land the change, whatever else is
    green. The reason is phrased with the branch's own description (field-verified shape:
    ``[{"access_level": 30, "access_level_description": "Developers + Maintainers"}]``)."""
    levels = rule.get("merge_access_levels")
    if not isinstance(levels, list) or not levels:
        return ()
    numeric = [
        int(entry["access_level"])
        for entry in levels
        if isinstance(entry, dict) and isinstance(entry.get("access_level"), int)
    ]
    if not numeric:
        return ()
    positive = [level for level in numeric if level > 0]
    if not positive:
        if any(
            entry.get("user_id") or entry.get("group_id")
            for entry in levels
            if isinstance(entry, dict)
        ):
            return ()
        return ("the base permits no direct merges through its protected branch rules",)
    required = min(positive)
    permissions = settings.get("permissions")
    held = 0
    if isinstance(permissions, dict):
        for key in ("project_access", "group_access"):
            access = permissions.get(key)
            if isinstance(access, dict) and isinstance(access.get("access_level"), int):
                held = max(held, int(access["access_level"]))
    if held >= required:
        return ()
    described = next(
        (
            str(entry.get("access_level_description"))
            for entry in levels
            if isinstance(entry, dict)
            and entry.get("access_level") == required
            and entry.get("access_level_description")
        ),
        _LEVEL_NAMES.get(required, f"access level {required}"),
    )
    return (
        f"the base allows merges by {described} only, and this token's access is "
        f"{_LEVEL_NAMES.get(held, f'access level {held}')}; give the token a higher role "
        "on the project or lower the branch's merge access",
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


def _approvals_required(ops: Any, project_path: str, base: str, *, protected: bool) -> int | None:
    """The approvals an enterprise project requires for ``base``: the
    largest ``approvals_required`` among the rules that apply to it —
    every rule with no branch restriction, plus the ones naming the
    branch. **Field-unverified** (no Premium instance in #1016)."""
    rules = _read_pages(ops, f"{project_path}/approval_rules")
    if not isinstance(rules, list):
        return None
    required = 0
    for rule in rules:
        branches = rule.get("protected_branches")
        if branches is not None and (
            not isinstance(branches, list)
            or any(not isinstance(b, dict) or not isinstance(b.get("name"), str) for b in branches)
        ):
            return None
        applies = (
            protected if rule.get("applies_to_all_protected_branches") else not branches
        ) or any(fnmatchcase(base, b["name"]) for b in branches or [])
        if applies:
            count = rule.get("approvals_required")
            if type(count) is not int or count < 0:
                return None
            required = max(required, count)
    return required


def _read_pages(ops: Any, path: str) -> list[dict[str, Any]] | object:
    rows: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        query = urlencode({"per_page": PAGE_SIZE, "page": page})
        data = _read(ops, f"{path}?{query}")
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            return _UNREAD
        rows.extend(data)
        if len(data) < PAGE_SIZE:
            return rows
    return _UNREAD


def read_change_requirements(
    ops: Any, repo: str, number: int, requirements: BaseRequirements
) -> BaseRequirements:
    """MR rules include overrides, code owners, and policy-generated requirements."""
    if _enterprise(ops) is False:
        return requirements
    path = f"/projects/{quote(repo, safe='')}/merge_requests/{number}/approval_state"
    data = _read(ops, path)
    rows = data.get("rules") if isinstance(data, dict) else None
    rules: list[ApprovalRule] = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                break
            count, approved = row.get("approvals_required"), row.get("approved")
            if type(count) is not int or count < 0 or not isinstance(approved, bool):
                break
            approvers = row.get("approved_by")
            if not isinstance(approvers, list) or any(
                not isinstance(user, dict) or not user.get("id") for user in approvers
            ):
                break
            have = len({user["id"] for user in approvers})
            rules.append(
                ApprovalRule(str(row.get("name") or "Unnamed approval rule"), count, have, approved)
            )
        else:
            unread = tuple(name for name in requirements.unread if name != "approval_rules")
            extra = tuple(
                b
                for b in requirements.extra_blockers
                if not b.startswith("GitLab requirements could not be read:")
            )
            if unread:
                extra = (*extra, f"GitLab requirements could not be read: {', '.join(unread)}")
            return requirements._replace(
                approvals_required=max((rule.required for rule in rules), default=0),
                code_owner_review=any(
                    row.get("rule_type") == "code_owner" and row["approvals_required"] > 0
                    for row in rows
                ),
                approval_rules=tuple(rules),
                unread=unread,
                extra_blockers=extra,
            )
    return requirements._replace(
        approvals_required=None,
        approval_rules=None,
        unread=tuple(dict.fromkeys((*requirements.unread, "merge_request_approval_state"))),
        extra_blockers=(
            *requirements.extra_blockers,
            "GitLab merge request approval rules could not be read",
        ),
    )
