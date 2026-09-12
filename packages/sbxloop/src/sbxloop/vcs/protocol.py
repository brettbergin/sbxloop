"""What a version-control backend does for a run, as role protocols.

The host never talks to a forge directly: every read and write is a named
operation on a backend object that runs inside the credential's sandbox.
The roles below say which operations exist and what they answer in
(:mod:`sbxloop.vcs.model`); a backend under ``vcs/<name>/`` implements them
against its own API, and the engine, the daemon and the doctor annotate
the role they need — or :class:`VcsOps`, all of them — never a backend.

Six roles plus one for the commit path, because they have different
consumers and different capability profiles across forges:

- :class:`RepoOps` — the repository, its branches and its files.
- :class:`IssueOps` — issues, their comments, labels and search.
- :class:`ChangeOps` — the pull request (merge request) and its landing.
- :class:`ReviewOps` — reviews, inline threads, replies and resolution.
- :class:`ChecksOps` — what CI reports on a commit and what it required.
- :class:`PolicyOps` — the credential and the base branch's rules.
- :class:`ContentOps` — a commit built remotely, with no local checkout.

**Capabilities are three-state, never a boolean.** A backend reports each
of :data:`CAPABILITIES` as :class:`Capability`: ``SUPPORTED`` (it does
this), ``UNSUPPORTED`` (a real answer — a forge with no merge queue lands by
merging directly, which is a design input, not an error) or ``UNKNOWN``
(the backend could not decide; the caller names what it needed and stops,
per the project's fail-closed rule). The distinction between the last two
is the whole point: an absent feature is a design input, an unreadable one
is a halt.

One rule makes the roles hold: **the generic transport is private to the
backend package.** If engine or daemon code needs a path, it needs a named
operation here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from sbxloop.config import MergeMethod
from sbxloop.vcs.model import (
    BaseRequirements,
    ChecksVerdict,
    FailedCheck,
    Identity,
    IssueRef,
    MergeOutcome,
    PostedFinding,
    PrRef,
    QueueEntry,
    QueueState,
    ReviewComment,
    ReviewEvent,
    ReviewThread,
    ReviewVerdict,
    SubmittedReview,
)


class Capability(StrEnum):
    """Whether a backend does one of the things sbxloop relies on."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


# What a run relies on a forge for, beyond the operations themselves. Each
# is answered per backend by :meth:`CapabilityOps.capabilities`; a
# per-repository fact (whether *this* base has a merge queue) is not a
# capability but a :class:`~sbxloop.vcs.model.BaseRequirements` field.
CAPABILITIES: tuple[str, ...] = (
    # The forge has a merge queue (train) a landing can enqueue into.
    "merge_queue",
    # Inline review threads can be replied to and resolved by API.
    "review_threads",
    # A change can be opened as a draft and taken out of draft.
    "draft_changes",
    # A review can request changes in a way the forge enforces at merge.
    "request_changes_review",
    # The host can mint a short-lived token instead of holding a long one.
    "short_lived_token",
    # A commit can be created remotely, with no local checkout.
    "remote_commit",
    # The base's required checks can be read by the run's credential.
    "required_checks_introspection",
    # A reviewer's account carries a signal that it is a bot.
    "bot_identity",
    # Commits created through the API arrive signed.
    "signed_api_commits",
)


@runtime_checkable
class CapabilityOps(Protocol):
    def capabilities(self) -> Mapping[str, Capability]:
        """One :class:`Capability` per name in :data:`CAPABILITIES`."""
        ...


@runtime_checkable
class RepoOps(Protocol):
    """The repository, its branches and its files."""

    def repo_get(self, repo: str) -> dict[str, Any]: ...
    def repo_lookup(self, repo: str) -> dict[str, Any] | None: ...
    def repo_create(
        self, repo: str, *, private: bool = True, for_user: bool = False
    ) -> dict[str, Any]: ...
    def default_branch(self, repo: str) -> str: ...
    def ref_lookup(self, repo: str, ref: str) -> str | None: ...
    def branch_delete(self, repo: str, branch: str) -> None: ...
    def merge_base(self, repo: str, base: str, head: str) -> str | None: ...
    def compare_lookup(self, repo: str, base: str, head: str) -> dict[str, Any] | None: ...
    def contents_read(self, repo: str, path: str, ref: str | None = None) -> str: ...


@runtime_checkable
class IssueOps(Protocol):
    """Issues, their comments, labels and search."""

    def issue_create(
        self,
        repo: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
    ) -> IssueRef: ...
    def issue_get(self, repo: str, number: int | str) -> dict[str, Any]: ...
    def issue_comment(self, repo: str, number: int, body: str) -> str: ...
    def issue_comments(self, repo: str, number: int | str) -> list[Any]: ...
    def issue_comment_delete(self, repo: str, comment_id: int) -> None: ...
    def issue_events(self, repo: str, number: int | str) -> list[Any]: ...
    def issues_list(
        self,
        repo: str,
        *,
        state: str = "open",
        labels: Sequence[str] = (),
        per_page: int = 100,
        page: int = 1,
        sort: str = "",
        direction: str = "",
    ) -> list[Any]: ...
    def issue_search(self, query: str, *, per_page: int) -> dict[str, Any]: ...
    def search_issues(self, query: str, per_page: int = 30) -> list[dict[str, Any]]: ...
    def issue_labels_add(self, repo: str, number: int | str, labels: Sequence[str]) -> None: ...
    def issue_label_remove(self, repo: str, number: int | str, label: str) -> None: ...
    def issue_close(self, repo: str, number: int | str, *, reason: str = "completed") -> None: ...
    def label_lookup(self, repo: str, name: str) -> dict[str, Any] | None: ...
    def label_create(
        self, repo: str, *, name: str, color: str, description: str
    ) -> dict[str, Any]: ...
    def labels_list(self, repo: str) -> list[Any]: ...


@runtime_checkable
class ChangeOps(Protocol):
    """The pull request (merge request) and its landing."""

    def pr_create(
        self,
        repo: str,
        base: str,
        head: str,
        title: str,
        body: str = "",
        *,
        draft: bool = False,
    ) -> PrRef: ...
    def pr_get(self, repo: str, number: int) -> dict[str, Any]: ...
    def pr_list_open(self, repo: str, *, head: str) -> list[Any]: ...
    def pr_update(
        self, repo: str, number: int, *, title: str | None = None, body: str | None = None
    ) -> dict[str, Any]: ...
    def pr_comment(self, repo: str, number: int, body: str) -> str: ...
    def pr_files(self, repo: str, number: int) -> list[Any]: ...
    def pr_request_reviewers(self, repo: str, number: int, reviewers: Sequence[str]) -> None: ...
    def pr_ready_for_review(self, node_id: str) -> bool: ...
    def pr_update_branch(self, repo: str, number: int, *, expected_head_sha: str = "") -> bool: ...
    def pr_merge(
        self,
        repo: str,
        number: int,
        *,
        method: MergeMethod = "squash",
        sha: str = "",
        title: str = "",
        message: str = "",
    ) -> MergeOutcome: ...
    def pr_enqueue(self, node_id: str, *, head: str = "") -> QueueEntry: ...
    def pr_queue_state(self, repo: str, number: int) -> QueueState: ...


@runtime_checkable
class ReviewOps(Protocol):
    """Reviews, inline threads, replies and resolution."""

    def pr_review_create(
        self,
        repo: str,
        number: int,
        event: ReviewEvent,
        body: str,
        comments: Sequence[ReviewComment] = (),
    ) -> SubmittedReview: ...
    def pr_review_comments_create(
        self,
        repo: str,
        number: int,
        comments: Sequence[ReviewComment],
        *,
        commit_id: str,
    ) -> tuple[PostedFinding, ...]: ...
    def pr_review_locations(
        self, repo: str, number: int, *, commit_id: str | None
    ) -> dict[str, tuple[range, ...]]: ...
    def pr_reviews(self, repo: str, number: int) -> list[Any]: ...
    def pr_review_comments(self, repo: str, number: int) -> list[Any]: ...
    def pr_review_verdicts(
        self, repo: str, number: int, *, exclude: Identity | None = None
    ) -> tuple[ReviewVerdict, ...]: ...
    def pr_review_state(self, repo: str, number: int, *, login: str | None = None) -> str: ...
    def pr_review_feedback(
        self,
        repo: str,
        number: int,
        *,
        exclude_login: str | None = None,
        exclude_is_bot: bool | None = None,
        clip: int = 6000,
    ) -> str: ...
    def pr_review_threads(self, repo: str, number: int) -> list[ReviewThread]: ...
    def pr_comment_reply(self, repo: str, number: int, comment_id: int, body: str) -> str: ...
    def pr_issue_comment(self, repo: str, number: int, body: str) -> str: ...
    def resolve_review_thread(self, thread_node_id: str) -> bool: ...


@runtime_checkable
class ChecksOps(Protocol):
    """What CI reports on a commit, and what the change requires of it."""

    def pr_checks(self, repo: str, sha: str) -> ChecksVerdict: ...
    def check_runs(self, repo: str, sha: str) -> list[Any]: ...
    def checks_failed_logs(
        self, repo: str, sha: str, *, max_chars: int = 6000
    ) -> list[FailedCheck]: ...
    def pr_required_checks(self, repo: str, number: int) -> tuple[str, ...]: ...
    def status_create(
        self,
        repo: str,
        sha: str,
        state: str,
        *,
        context: str = "sbxloop",
        description: str = "",
        target_url: str = "",
    ) -> None: ...
    def workflows_list(self, repo: str) -> list[Any]: ...
    def workflow_runs(self, repo: str, *, branch: str, per_page: int = 1) -> list[Any]: ...


@runtime_checkable
class PolicyOps(Protocol):
    """The credential, and what the base branch requires before a merge."""

    def base_requirements(self, repo: str, base: str) -> BaseRequirements: ...
    def rate_limit(self) -> dict[str, Any]: ...
    def authenticated_user(self) -> dict[str, Any]: ...
    def token_scopes(self) -> tuple[str, ...] | None: ...
    def permission_probe(self, permission: str, repo: str, base: str) -> bool | None: ...


@runtime_checkable
class ContentOps(Protocol):
    """A commit built remotely, with no local checkout: blobs, a tree, the
    commit, and the ref that carries it. The least portable role."""

    def blobs_create_many(self, repo: str, files: list[dict[str, str]]) -> dict[str, str]: ...
    def commit_get(self, repo: str, sha: str) -> dict[str, Any]: ...
    def tree_create(
        self, repo: str, *, base_tree: str, entries: list[dict[str, Any]]
    ) -> dict[str, Any]: ...
    def commit_create(
        self, repo: str, *, message: str, tree: str, parents: list[str]
    ) -> dict[str, Any]: ...
    def ref_create(self, repo: str, ref: str, sha: str) -> None: ...
    def ref_force_update(self, repo: str, branch: str, sha: str) -> None: ...
    def contents_put(
        self, repo: str, path: str, *, message: str, content_b64: str, branch: str
    ) -> dict[str, Any]: ...


@runtime_checkable
class VcsOps(
    CapabilityOps,
    RepoOps,
    IssueOps,
    ChangeOps,
    ReviewOps,
    ChecksOps,
    PolicyOps,
    ContentOps,
    Protocol,
):
    """Every role at once — what a backend object is, and what a consumer
    that spans several roles annotates."""


ROLES: tuple[type, ...] = (
    CapabilityOps,
    RepoOps,
    IssueOps,
    ChangeOps,
    ReviewOps,
    ChecksOps,
    PolicyOps,
    ContentOps,
)
