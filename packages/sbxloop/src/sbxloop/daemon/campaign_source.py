"""GitHub checks and label transitions for already-authorized campaign steps.

The coordinator persists membership/readiness before using these helpers and
serializes them against its own dispatch. Normal source claim/recovery remains
the owner of claim tokens. GitHub label writes have no compare-and-swap: a live
foreign claimant can still race a read, so these checks are not a distributed
transaction and never claim to cancel work another daemon has already claimed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.sources import (
    CLAIM_MARKER,
    CompositeSource,
    GitHubIssueSource,
    MultiRepoIssueSource,
    WorkSource,
)
from sbxloop.engine.model import RunRecord
from sbxloop.errors import SbxloopError
from sbxloop.gh.ops import MAX_PAGES, PAGE_SIZE, GithubOps
from sbxloop.ghids import has_gh_prefix, is_local_id, try_parse_gh_id


class CampaignSourceError(ValueError):
    """A campaign source transition needs evidence it could not obtain."""


@dataclass(frozen=True)
class CodeDeliveryEvidence:
    run_id: str
    repo: str
    base: str
    pr_number: int
    pr_url: str
    merge_commit_sha: str


def source_for_campaign(source: WorkSource, item: WorkItem) -> GitHubIssueSource | None:
    """Resolve the exact GitHub source, with no fallback to another repository.

    Local workloads return None without reading GitHub. A repo-less legacy
    issue can resolve only when the source has exactly one repository.
    """
    if is_local_id(item.item_id) or not has_gh_prefix(item.item_id):
        return None
    parsed = try_parse_gh_id(item.item_id)
    if parsed is None or parsed.kind != "issue":
        raise CampaignSourceError("campaign source needs an issue identity")
    if item.repo and parsed.repo and item.repo.casefold() != parsed.repo.casefold():
        raise CampaignSourceError("campaign item repository conflicts with its identity")
    repo = item.repo or parsed.repo
    if isinstance(source, CompositeSource):
        if source.github is None:
            raise CampaignSourceError("no GitHub source is configured for this repository")
        return source_for_campaign(source.github, item)
    candidates = source.sources if isinstance(source, MultiRepoIssueSource) else [source]
    matches = [
        candidate
        for candidate in candidates
        if isinstance(candidate, GitHubIssueSource)
        and (repo is None or candidate.repo.casefold() == repo.casefold())
    ]
    if len(matches) != 1:
        raise CampaignSourceError("campaign item needs one explicitly configured repository")
    return matches[0]


def _timestamp(value: Any, context: str) -> float:
    if not isinstance(value, str):
        raise CampaignSourceError(f"{context} has no readable timestamp")
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            raise ValueError("timezone is missing")
        return stamp.timestamp()
    except ValueError as exc:
        raise CampaignSourceError(f"{context} has an unreadable timestamp") from exc


def _strict_pages(ops: GithubOps, path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        data = ops.raw("GET", f"{path}?per_page={PAGE_SIZE}&page={page}")
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise CampaignSourceError(f"cannot read complete {path.rsplit('/', 1)[-1]}")
        rows.extend(data)
        if len(data) < PAGE_SIZE:
            return rows
    raise CampaignSourceError(f"cannot read complete {path.rsplit('/', 1)[-1]}: too many pages")


class CampaignSource:
    def __init__(self, source: GitHubIssueSource) -> None:
        self.source = source

    @contextmanager
    def _ops(self) -> Iterator[GithubOps]:
        try:
            yield self.source._ops()
        except SbxloopError as exc:
            self.source._failed(exc)
            raise CampaignSourceError(f"campaign GitHub operation failed: {exc}") from exc

    def _pending_identity(self, item: WorkItem) -> None:
        if item.claimed or item.claim_token or item.state != "queued":
            raise CampaignSourceError("existing claim or run must use the normal recovery path")
        if source_for_campaign(self.source, item) is not self.source:
            raise CampaignSourceError("campaign GitHub transition needs an issue identity")
        parsed = try_parse_gh_id(item.item_id)
        if parsed is None or item.source_key != str(parsed.number):
            raise CampaignSourceError("campaign source key conflicts with its issue identity")

    def _open_labels(self, ops: GithubOps, item: WorkItem) -> set[str]:
        row = ops.raw("GET", self.source._issue_path(item.source_key))
        if (
            not isinstance(row, dict)
            or type(row.get("number")) is not int
            or row["number"] != int(item.source_key)
        ):
            raise CampaignSourceError("GitHub returned an unreadable or different issue identity")
        if "pull_request" in row:
            raise CampaignSourceError("campaign member is a pull request")
        if row.get("state") != "open":
            raise CampaignSourceError("campaign member must still be open")
        labels = row.get("labels")
        if not isinstance(labels, list) or any(
            not isinstance(label, dict) or not isinstance(label.get("name"), str)
            for label in labels
        ):
            raise CampaignSourceError("campaign member has unreadable labels")
        names = {label["name"] for label in labels}
        lifecycle = self.source.labels
        if names & {lifecycle.in_progress, lifecycle.gated}:
            raise CampaignSourceError("campaign member is already owned or awaiting a gate")
        opposite = lifecycle.trigger if item.kind == "workload" else lifecycle.workload
        if opposite in names:
            raise CampaignSourceError("campaign member carries a conflicting run kind label")
        return names

    def _require_no_claim(self, ops: GithubOps, item: WorkItem) -> None:
        path = self.source._issue_path(item.source_key)
        comments = _strict_pages(ops, f"{path}/comments")
        claims: list[float] = []
        for comment in comments:
            body = comment.get("body")
            if not isinstance(body, str):
                raise CampaignSourceError("cannot read issue comment while checking ownership")
            if CLAIM_MARKER in body:
                # Even a malformed lock marker is unknown ownership, never
                # permission to rotate the trigger epoch underneath its writer.
                claims.append(_timestamp(comment.get("created_at"), "claim comment"))
        if not claims:
            return
        trigger = self.source.labels.trigger_for(item)
        epoch: float | None = None
        for event in _strict_pages(ops, f"{path}/events"):
            if not isinstance(event.get("event"), str):
                raise CampaignSourceError("cannot read issue event while checking ownership")
            if event["event"] != "labeled":
                continue
            label = event.get("label")
            if not isinstance(label, dict) or not isinstance(label.get("name"), str):
                raise CampaignSourceError("cannot read label event while checking ownership")
            if label["name"] == trigger:
                created = _timestamp(event.get("created_at"), "trigger event")
                epoch = created if epoch is None else max(epoch, created)
        if epoch is None or any(created >= epoch for created in claims):
            raise CampaignSourceError("campaign member has a current or unresolved claim")

    def _unowned(self, ops: GithubOps, item: WorkItem) -> set[str]:
        self._open_labels(ops, item)
        self._require_no_claim(ops, item)
        # Ownership may have become visible while paginating comments. Never
        # remove or add the trigger after observing that transition.
        return self._open_labels(ops, item)

    def validate_campaign_item(self, item: WorkItem) -> None:
        self._pending_identity(item)
        with self._ops() as ops:
            self._unowned(ops, item)

    def park_campaign_item(self, item: WorkItem) -> None:
        self._pending_identity(item)
        with self._ops() as ops:
            names = self._unowned(ops, item)
            trigger = self.source.labels.trigger_for(item)
            if trigger in names:
                self.source._remove_label(ops, item.source_key, trigger)

    def prepare_campaign_item(self, item: WorkItem) -> None:
        self._pending_identity(item)
        with self._ops() as ops:
            names = self._unowned(ops, item)
            trigger = self.source.labels.trigger_for(item)
            if trigger not in names:
                self.source._add_label(ops, item.source_key, trigger)

    def verify_code_delivery(
        self, repo: str, expected_base: str, run: RunRecord
    ) -> CodeDeliveryEvidence:
        if repo.casefold() != self.source.repo.casefold():
            raise CampaignSourceError("delivery repository does not match its GitHub source")
        if not expected_base or expected_base != expected_base.strip():
            raise CampaignSourceError("delivery needs the campaign's pinned base")
        if (
            run.kind != "code"
            or run.state != "merged"
            or run.pr_number is None
            or run.pr_number < 1
        ):
            raise CampaignSourceError("code delivery requires a merged run with its pull request")
        with self._ops() as ops:
            pr: Any = ops.pr_get(repo, run.pr_number)
        if (
            not isinstance(pr, dict)
            or type(pr.get("number")) is not int
            or pr["number"] != run.pr_number
        ):
            raise CampaignSourceError("GitHub returned a different pull request identity")
        if pr.get("merged") is not True or pr.get("state") != "closed":
            raise CampaignSourceError("the pull request is not verifiably merged")
        base = pr.get("base")
        if not isinstance(base, dict) or base.get("ref") != expected_base:
            raise CampaignSourceError("the pull request did not land on the pinned base")
        target = base.get("repo")
        if (
            not isinstance(target, dict)
            or not isinstance(target.get("full_name"), str)
            or target["full_name"].casefold() != repo.casefold()
        ):
            raise CampaignSourceError("the pull request's target repository is not verified")
        sha = pr.get("merge_commit_sha")
        if not isinstance(sha, str) or not sha or any(char.isspace() for char in sha):
            raise CampaignSourceError("the pull request has no readable merge commit")
        url = pr.get("html_url")
        if (
            not isinstance(url, str)
            or urlsplit(url).path.casefold().rstrip("/")
            != f"/{repo}/pull/{run.pr_number}".casefold()
        ):
            raise CampaignSourceError("the pull request URL has a different identity")
        return CodeDeliveryEvidence(run.run_id, repo, expected_base, run.pr_number, url, sha)
