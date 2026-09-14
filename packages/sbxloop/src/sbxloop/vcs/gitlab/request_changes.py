"""Licensed blocking reviews through GitLab's GraphQL API.

The changeRequesters resolver returns null when the project's licensed
feature is unavailable. A reviewer state alone is not evidence of a gate.
These shapes follow GitLab's documented schema; paid tiers are field-unverified.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sbxloop.errors import GithubOpsError
from sbxloop.vcs.jobs import MAX_PAGES

Raw = Callable[[str, str, dict[str, Any]], Any]

_REQUESTERS = """
query($projectPath: ID!, $iid: String!, $after: String) {
  project(fullPath: $projectPath) {
    mergeRequest(iid: $iid) {
      changeRequesters(first: 100, after: $after) {
        nodes { username }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
_REQUEST = """
mutation($projectPath: ID!, $iid: String!) {
  mergeRequestRequestChanges(input: {projectPath: $projectPath, iid: $iid}) {
    errors
    mergeRequest { iid }
  }
}
"""


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GithubOpsError("GitLab blocking review response is incomplete")
    return value


def _execute(raw: Raw, url: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    response = _object(raw("POST", url, {"query": query, "variables": variables}))
    if response.get("errors"):
        raise GithubOpsError("GitLab blocking review GraphQL request was refused")
    return _object(response.get("data"))


def requesters(raw: Raw, url: str, repo: str, number: int) -> set[str] | None:
    """Licensed requesters, None for a disabled feature, or an unread error."""
    variables: dict[str, Any] = {"projectPath": repo, "iid": str(number), "after": None}
    names: set[str] = set()
    cursors: set[str] = set()
    for _ in range(MAX_PAGES):
        data = _execute(raw, url, _REQUESTERS, variables)
        mr = _object(_object(data.get("project")).get("mergeRequest"))
        if "changeRequesters" not in mr:
            raise GithubOpsError("GitLab did not report blocking review support")
        connection = mr["changeRequesters"]
        if connection is None:
            return None
        connection = _object(connection)
        nodes = connection.get("nodes")
        if not isinstance(nodes, list):
            raise GithubOpsError("GitLab did not list blocking reviewers")
        for node in nodes:
            name = _object(node).get("username")
            if not isinstance(name, str) or not name:
                raise GithubOpsError("GitLab returned an unnamed blocking reviewer")
            names.add(name)
        page = _object(connection.get("pageInfo"))
        if page.get("hasNextPage") is False:
            return names
        cursor = page.get("endCursor")
        if (
            page.get("hasNextPage") is not True
            or not isinstance(cursor, str)
            or not cursor
            or cursor in cursors
        ):
            raise GithubOpsError("GitLab blocking reviewer pagination is incomplete")
        cursors.add(cursor)
        variables["after"] = cursor
    raise GithubOpsError("GitLab blocking reviewer pagination limit reached")


def submit(raw: Raw, url: str, repo: str, number: int) -> None:
    data = _execute(raw, url, _REQUEST, {"projectPath": repo, "iid": str(number)})
    result = _object(data.get("mergeRequestRequestChanges"))
    if result.get("errors") != [] or str(_object(result.get("mergeRequest")).get("iid")) != str(
        number
    ):
        raise GithubOpsError("GitLab did not accept the blocking review")
