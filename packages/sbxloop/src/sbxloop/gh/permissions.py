"""What a run's GitHub credential must be allowed to do, and the one place
that says so (#696).

The loop reads the tree, opens and reviews and merges pull requests,
drives issue labels and comments, waits on check runs, and reads failed
Actions job logs. Each of those is a GitHub permission, and a token missing
one fails at the stage that first needs it — mid-run, after the sandboxes
were built and the agent spent its turns. `sbxloop doctor` compares the
credential against :data:`NEEDS` up front, from whichever source describes
the token:

- a **classic PAT** answers every request with an ``X-OAuth-Scopes``
  header (:func:`missing_from_scopes`);
- a **GitHub App** installation token's mint carries the installation's
  ``permissions`` map (:func:`missing_from_app`);
- a **fine-grained PAT** reports neither, so doctor asks each read
  endpoint directly (``sbxloop.cli.doctor``).

``docs/permissions.md`` renders the same table for the person creating the
token; the README and ``.env.example`` point there rather than restating it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

Level = Literal["read", "write"]


@dataclass(frozen=True)
class Need:
    """One permission a run uses, and the feature that first needs it."""

    permission: str  # the App / fine-grained key: ``contents``, ``pull_requests``, ...
    level: Level
    feature: str  # what the loop does with it, in a doctor row's words
    # Whether a run without it fails (a FAIL row) or only loses something
    # a repository may never need (a WARN row).
    required: bool = True

    @property
    def label(self) -> str:
        """``contents:write`` — the spelling doctor rows and docs use."""
        return f"{self.permission}:{self.level}"

    def describe(self) -> str:
        return f"{self.label} ({self.feature})"


NEEDS: tuple[Need, ...] = (
    Need("metadata", "read", "looking the repository up"),
    Need("contents", "write", "delivering the run's branch and commits"),
    Need("pull_requests", "write", "opening, reviewing and merging the pull request"),
    Need("issues", "write", "polling issues for work and driving the lifecycle labels"),
    Need("checks", "read", "waiting for check runs at the CI and landing stages"),
    Need("actions", "read", "reading workflow runs and failed-job logs at the CI stage"),
    Need(
        "workflows",
        "write",
        "delivering changes under .github/workflows — without it GitHub refuses "
        "a delivery that touches a workflow file",
        required=False,
    ),
)

# A classic PAT's scopes are coarse: ``repo`` (or ``public_repo`` on a
# public repository) covers everything a run does except editing workflow
# files, which is the separate ``workflow`` scope.
_REPO_SCOPES = frozenset({"repo"})
_PUBLIC_REPO_SCOPES = frozenset({"repo", "public_repo"})
_WORKFLOW_SCOPES = frozenset({"workflow"})


def missing_from_scopes(scopes: Iterable[str], *, private: bool = True) -> tuple[Need, ...]:
    """The needs a classic PAT's ``X-OAuth-Scopes`` do not cover.

    ``private`` is whether the repository is private: ``public_repo`` is
    enough for a public one and nothing for a private one.
    """
    held = frozenset(scopes)
    covering = _REPO_SCOPES if private else _PUBLIC_REPO_SCOPES
    missing: list[Need] = []
    for need in NEEDS:
        if need.permission == "workflows":
            if not (held & _WORKFLOW_SCOPES):
                missing.append(need)
        elif not (held & covering):
            missing.append(need)
    return tuple(missing)


def missing_from_app(permissions: Mapping[str, str]) -> tuple[Need, ...]:
    """The needs an App installation's ``permissions`` map does not grant.

    A write need wants ``write``; a read need is satisfied by ``read`` or
    ``write``. An absent key is "not granted".
    """
    missing: list[Need] = []
    for need in NEEDS:
        granted = permissions.get(need.permission)
        if granted == "write" or (need.level == "read" and granted == "read"):
            continue
        missing.append(need)
    return tuple(missing)


def split_required(needs: Iterable[Need]) -> tuple[tuple[Need, ...], tuple[Need, ...]]:
    """``(required, optional)`` — the FAIL and WARN halves of a missing set."""
    listed = tuple(needs)
    return (
        tuple(n for n in listed if n.required),
        tuple(n for n in listed if not n.required),
    )
