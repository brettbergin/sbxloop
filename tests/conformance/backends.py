"""The backends the conformance suite runs against, one entry per kind.

An entry names the kind, builds a fresh backend object that answers every
role in :mod:`sbxloop.vcs.protocol`, and — because a scenario cannot reach
into a fake's knobs without knowing the fake — a :class:`Seeds` object
that arranges the state a scenario needs in that backend's own terms: a
red check with a log, a base branch's rules, an existing issue. A second
backend registers itself here with its own fake and its own seeds; the
scenarios do not change.

A live forge (``tests/live``) registers the same way, under its own name,
and says through ``unavailable`` why it cannot run here: not configured,
configured but not answering, or answering with no backend yet to drive
it. The suite skips with that reason; nothing about the gate changes.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import NamedTuple, Protocol

from sbxloop.vcs.model import ChecksVerdict, FailedCheck
from sbxloop.vcs.protocol import VcsOps
from tests.fakes.fake_gitea import FakeGitea
from tests.fakes.fake_github import FakeGithub
from tests.fakes.fake_gitlab import FakeGitlab
from tests.live.env import LiveForge, live_forge


class SeededRules(NamedTuple):
    """What the base requires after :meth:`Seeds.base_rules`, as the forge
    can express it: the contexts it names, the approvals it counts, and
    whether it gates on the whole pipeline instead of naming any (#1016
    V2). A scenario asserts against this, so a forge that cannot name a
    check is held to what it *can* say rather than to GitHub's shape."""

    required: tuple[str, ...]
    approvals: int
    all_checks_required: bool = False


class Seeds(Protocol):
    """Arrange state in the backend, in its own terms."""

    def failed_check(self, name: str, excerpt: str) -> None:
        """The change's head has one red check called ``name`` whose log
        (or description) says ``excerpt``."""
        ...

    def pending_check(self, name: str) -> None:
        """The change's head has one check called ``name`` still running."""
        ...

    def base_rules(self, *, required: Sequence[str], approvals: int) -> SeededRules:
        """The base branch requires the checks ``required`` green and
        ``approvals`` approving reviews before a merge — or the nearest
        the forge can express, which is what comes back."""
        ...

    def existing_issue(self, title: str, labels: Sequence[str]) -> int:
        """The repository carries an issue titled ``title`` with ``labels``;
        its number."""
        ...

    def standing_rules(self) -> SeededRules:
        """What the base requires before any scenario seeds a rule: nothing
        on a fake, whatever the harness seeded on a live forge (a
        protected base is the shape #1016 verified against)."""
        ...

    def approve(self, number: int) -> None:
        """A second person approves change ``number``, where the base needs
        an approval before a merge; nothing where it does not."""
        ...


def _always_available() -> str | None:
    return None


@dataclass(frozen=True)
class Backend:
    kind: str
    repo: str
    base: str
    make: Callable[[], VcsOps]
    seeds: Callable[[VcsOps], Seeds]
    # Why this entry cannot run here, or None when it can.
    unavailable: Callable[[], str | None] = _always_available


class _GithubSeeds:
    def __init__(self, fake: FakeGithub) -> None:
        self.fake = fake

    def failed_check(self, name: str, excerpt: str) -> None:
        self.fake.checks = [ChecksVerdict("red", 1, (), (name,))]
        self.fake.failed_logs = [FailedCheck(name, "failure", excerpt, "https://ci.example/1")]

    def pending_check(self, name: str) -> None:
        self.fake.checks = [ChecksVerdict("pending", 1, (name,), ())]

    def base_rules(self, *, required: Sequence[str], approvals: int) -> SeededRules:
        self.fake.protection = {
            "required_status_checks": {"contexts": list(required)},
            "required_pull_request_reviews": {"required_approving_review_count": approvals},
        }
        self.fake.rules = []
        return SeededRules(tuple(required), approvals)

    def standing_rules(self) -> SeededRules:
        return SeededRules((), 0)

    def approve(self, number: int) -> None:
        return None

    def existing_issue(self, title: str, labels: Sequence[str]) -> int:
        number = 41
        self.fake.existing_issues.append(
            {
                "number": number,
                "title": title,
                "body": "",
                "state": "open",
                "html_url": f"https://github.com/{self.fake.repo}/issues/{number}",
                "labels": [{"name": label} for label in labels],
            }
        )
        return number


def _github() -> VcsOps:
    return FakeGithub(repo="o/r", number=7)


def _github_seeds(ops: VcsOps) -> Seeds:
    assert isinstance(ops, FakeGithub)
    return _GithubSeeds(ops)


class _GitlabSeeds:
    """The fake GitLab is CE (#1016): required checks cannot be named, so
    ``base_rules`` turns on "pipeline must succeed" and answers with the
    whole pipeline required and no approvals."""

    def __init__(self, fake: FakeGitlab) -> None:
        self.fake = fake

    def failed_check(self, name: str, excerpt: str) -> None:
        self.fake.seed_verdict(
            self.fake.head_sha,
            ChecksVerdict("red", 1, (), (name,)),
            logs=[FailedCheck(name, "failure", excerpt, "")],
        )

    def pending_check(self, name: str) -> None:
        self.fake.seed_verdict(self.fake.head_sha, ChecksVerdict("pending", 1, (name,), ()))

    def base_rules(self, *, required: Sequence[str], approvals: int) -> SeededRules:
        self.fake.settings["only_allow_merge_if_pipeline_succeeds"] = True
        self.fake.protected = {
            "name": "main",
            "push_access_levels": [{"access_level": 0}],
            "merge_access_levels": [{"access_level": 30}],
            "allow_force_push": False,
        }
        self.fake.enterprise = False
        return SeededRules((), 0, all_checks_required=True)

    def standing_rules(self) -> SeededRules:
        return SeededRules((), 0)

    def approve(self, number: int) -> None:
        return None

    def existing_issue(self, title: str, labels: Sequence[str]) -> int:
        self.fake.seed_issue(41, title, labels)
        return 41


def _gitlab() -> VcsOps:
    return FakeGitlab(repo="acme/widgets")


def _gitlab_seeds(ops: VcsOps) -> Seeds:
    assert isinstance(ops, FakeGitlab)
    return _GitlabSeeds(ops)


class _GiteaSeeds:
    """The fake Gitea (#1021): required contexts and an approval count are
    readable from the branch (#1016 V4), so ``base_rules`` is exactly what
    was asked; an approval is a second user's review, because the author's
    own does not count."""

    def __init__(self, fake: FakeGitea) -> None:
        self.fake = fake

    def failed_check(self, name: str, excerpt: str) -> None:
        self.fake.seed_verdict(
            self.fake.branches[CHANGE_BRANCH],
            ChecksVerdict("red", 1, (), (name,)),
            logs=[FailedCheck(name, "failure", excerpt, "")],
        )

    def pending_check(self, name: str) -> None:
        self.fake.seed_verdict(
            self.fake.branches[CHANGE_BRANCH], ChecksVerdict("pending", 1, (name,), ())
        )

    def base_rules(self, *, required: Sequence[str], approvals: int) -> SeededRules:
        self.fake.branch_rules = {
            "required_approvals": approvals,
            "enable_status_check": bool(required),
            "status_check_contexts": list(required),
            "user_can_merge": True,
        }
        return SeededRules(tuple(required), approvals)

    def standing_rules(self) -> SeededRules:
        return SeededRules((), 0)

    def approve(self, number: int) -> None:
        self.fake.seed_review(number, "APPROVED", author="rev-bob")

    def existing_issue(self, title: str, labels: Sequence[str]) -> int:
        self.fake.seed_issue(41, title, labels)
        return 41


def _gitea() -> VcsOps:
    """The fake Gitea with the change branch cut and the loop's labels
    present, the shape the live reset gives the harness repository."""
    fake = FakeGitea(repo="acme/widgets")
    fake.trees["commit0"] = {**fake.trees["base123"], "a.py": ("100644", b"x = 1\ny = 2\nz = 3\n")}
    fake.commits["commit0"] = {"sha": "commit0", "parents": ["base123"], "message": "one change"}
    fake.branches[CHANGE_BRANCH] = "commit0"
    for name in ("sbxloop:run", "sbxloop:in-progress"):
        fake._ensure_label(name)
    return fake


def _gitea_seeds(ops: VcsOps) -> Seeds:
    assert isinstance(ops, FakeGitea)
    return _GiteaSeeds(ops)


# -- live forges ----------------------------------------------------------------


def _live_unavailable(kind: str) -> Callable[[], str | None]:
    def unavailable() -> str | None:
        forge = live_forge(kind)
        return forge if isinstance(forge, str) else None

    return unavailable


def _live_gitlab_forge() -> LiveForge:
    forge = live_forge("gitlab")
    assert not isinstance(forge, str), forge
    return forge


def _live_gitlab() -> VcsOps:
    """The real GitLab backend over the worker's own transport, in this
    process (``tests/live/localclient.py``), against the harness forge."""
    from sbxloop.vcs.gitlab.ops import GitlabOps, gitlab_transport
    from tests.live.localclient import LocalWorkerClient

    forge = _live_gitlab_forge()
    spec = gitlab_transport(forge.api_url)
    client = LocalWorkerClient(spec, forge.get("GITLAB_TOKEN"), ca_file=forge.ca_file)
    _reset_live_branch(forge, CHANGE_BRANCH)
    return GitlabOps(client, "live", transport=spec)  # type: ignore[arg-type]


# The branch every change scenario opens its change from; the live entry
# cuts it fresh per backend build and the live seeds put statuses on its
# head, the change's head.
CHANGE_BRANCH = "sbxloop/r1"
# The branch the remote-commit scenario creates and rewrites; never the
# change branch, which the live reset already cut.
CONTENT_BRANCH = "sbxloop/r2"


def _reset_live_branch(forge: LiveForge, branch: str) -> None:
    """The change scenarios open a merge request from ``branch`` and never
    close it (the fake forgets everything per test). On a live forge the
    last run's request would refuse the next (409), so the branch starts
    every backend build fresh: its open requests closed, the branch cut
    again from the base with one commit to diff. The commit's third line
    is new every build, so it diffs against a base the last merge scenario
    already landed ``a.py`` on (the review scenario anchors line 3), and
    its head carries a green ``ci`` status: the seeded base only merges a
    head whose pipeline succeeded, and the checks scenarios overwrite the
    context with their own failure or pending status."""
    from time import time_ns
    from urllib.parse import quote

    dev = forge.client("GITLAB_TOKEN", "Developer")
    project = f"/projects/{quote(forge.repo, safe='')}"
    for change in (
        dev.get(
            f"{project}/merge_requests", query={"state": "opened", "source_branch": branch}
        ).data
        or []
    ):
        dev.put(f"{project}/merge_requests/{change['iid']}", {"state_event": "close"})
    dev.delete(f"{project}/repository/branches/{quote(branch, safe='')}", check=False)
    # The content scenario's branch and any pending branch a delivery that
    # died mid-way left behind (the GitLab backend commits on one first).
    dev.delete(f"{project}/repository/branches/{quote(CONTENT_BRANCH, safe='')}", check=False)
    for stale in (
        dev.get(f"{project}/repository/branches", query={"search": "^sbxloop/pending/"}).data or []
    ):
        dev.delete(f"{project}/repository/branches/{quote(stale['name'], safe='')}", check=False)
    on_base = dev.get(f"{project}/repository/files/a.py", query={"ref": "main"}, check=False)
    commit = dev.post(
        f"{project}/repository/commits",
        {
            "branch": branch,
            "start_branch": "main",
            "commit_message": "sbxloop conformance: one change to review",
            "actions": [
                {
                    "action": "update" if on_base.ok else "create",
                    "file_path": "a.py",
                    "content": f"x = 1\ny = 2\nz = {time_ns()}\n",
                }
            ],
        },
    )
    dev.post(
        f"{project}/statuses/{commit.data['id']}",
        {"state": "success", "name": "ci", "description": "sbxloop conformance: green head"},
    )


class _LiveGitlabSeeds:
    """Seeds against the harness's ``acme/widgets``: the seed script
    already protected ``main`` and set "pipeline must succeed" as the
    administrator (``tests/live/seed_gitlab.py``); a Developer cannot
    change those, so ``base_rules`` reports what stands. Statuses go on
    the change branch's head, the head of the change the scenario opens."""

    def __init__(self, ops: VcsOps, forge: LiveForge) -> None:
        self.ops = ops
        self.forge = forge

    def _head(self) -> str:
        sha = self.ops.ref_lookup(self.forge.repo, f"heads/{CHANGE_BRANCH}")
        assert sha
        return sha

    def failed_check(self, name: str, excerpt: str) -> None:
        self.ops.status_create(
            self.forge.repo, self._head(), "failure", context=name, description=excerpt
        )

    def pending_check(self, name: str) -> None:
        self.ops.status_create(self.forge.repo, self._head(), "pending", context=name)

    def base_rules(self, *, required: Sequence[str], approvals: int) -> SeededRules:
        return SeededRules((), 0, all_checks_required=True)

    def standing_rules(self) -> SeededRules:
        return SeededRules((), 0, all_checks_required=True)

    def approve(self, number: int) -> None:
        return None

    def existing_issue(self, title: str, labels: Sequence[str]) -> int:
        return self.ops.issue_create(self.forge.repo, title, "", labels=list(labels)).number


def _live_gitlab_seeds(ops: VcsOps) -> Seeds:
    return _LiveGitlabSeeds(ops, _live_gitlab_forge())


# -- live Gitea -------------------------------------------------------------------


def _live_gitea_forge() -> LiveForge:
    forge = live_forge("gitea")
    assert not isinstance(forge, str), forge
    return forge


def _live_gitea() -> VcsOps:
    """The real Gitea backend over the worker's own transport, in this
    process, against the harness forge."""
    from sbxloop.vcs.gitea.ops import GiteaOps, gitea_transport
    from tests.live.localclient import LocalWorkerClient

    forge = _live_gitea_forge()
    spec = gitea_transport(forge.api_url)
    client = LocalWorkerClient(spec, forge.get("GITEA_TOKEN"), ca_file=forge.ca_file)
    _reset_live_gitea(forge, CHANGE_BRANCH)
    return GiteaOps(client, "live", transport=spec)  # type: ignore[arg-type]


def _reset_live_gitea(forge: LiveForge, branch: str) -> None:
    """The Gitea twin of :func:`_reset_live_branch`: the last run's open
    pull requests closed, the change and content branches (and any
    pending branch a dead delivery left) deleted, the change branch cut
    again from the base with one commit whose third line is new, both
    required contexts green on its head (the seeded base requires ``ci``
    and ``lint``), and the loop's labels present."""
    from time import time_ns
    from urllib.parse import quote

    dev = forge.client("GITEA_TOKEN", "Developer")
    repo = f"/repos/{forge.repo}"
    for pull in dev.get(f"{repo}/pulls", query={"state": "open", "limit": 50}).data or []:
        if pull["head"]["ref"] in (branch, CONTENT_BRANCH):
            dev.patch(f"{repo}/pulls/{pull['number']}", {"state": "closed"})
    stale = [
        b["name"]
        for b in dev.get(f"{repo}/branches", query={"limit": 50}).data or []
        if b["name"] in (branch, CONTENT_BRANCH) or b["name"].startswith("sbxloop/pending/")
    ]
    for name in stale:
        dev.delete(f"{repo}/branches/{quote(name, safe='')}", check=False)
    dev.post(f"{repo}/branches", {"new_branch_name": branch, "old_branch_name": "main"})
    on_base = dev.get(f"{repo}/contents/a.py", query={"ref": "main"}, check=False)
    commit = dev.post(
        f"{repo}/contents",
        {
            "branch": branch,
            "message": "sbxloop conformance: one change to review",
            "files": [
                {
                    "operation": "update" if on_base.ok else "create",
                    "path": "a.py",
                    "content": base64.b64encode(
                        f"x = 1\ny = 2\nz = {time_ns()}\n".encode()
                    ).decode(),
                }
            ],
        },
    )
    sha = commit.data["commit"]["sha"]
    for context in ("ci", "lint"):
        dev.post(
            f"{repo}/statuses/{sha}",
            {
                "state": "success",
                "context": context,
                "description": "sbxloop conformance: green head",
            },
        )
    present = {lb["name"] for lb in dev.get(f"{repo}/labels", query={"limit": 50}).data or []}
    for name in ("sbxloop:run", "sbxloop:in-progress"):
        if name not in present:
            dev.post(f"{repo}/labels", {"name": name, "color": "#cccccc"})


class _LiveGiteaSeeds:
    """Seeds against the harness's ``acme/widgets`` on Gitea: ``main`` is
    protected by the administrator (one approval, the contexts ``ci`` and
    ``lint``, #1016 V4), which a write collaborator reads and cannot
    change, so ``base_rules`` reports what stands; statuses go on the change
    branch's head; an approval is the reviewer's token's review."""

    def __init__(self, ops: VcsOps, forge: LiveForge) -> None:
        self.ops = ops
        self.forge = forge

    def _head(self) -> str:
        sha = self.ops.ref_lookup(self.forge.repo, f"heads/{CHANGE_BRANCH}")
        assert sha
        return sha

    def failed_check(self, name: str, excerpt: str) -> None:
        self.ops.status_create(
            self.forge.repo, self._head(), "failure", context=name, description=excerpt
        )

    def pending_check(self, name: str) -> None:
        self.ops.status_create(self.forge.repo, self._head(), "pending", context=name)

    def base_rules(self, *, required: Sequence[str], approvals: int) -> SeededRules:
        return SeededRules(("ci", "lint"), 1)

    def standing_rules(self) -> SeededRules:
        return SeededRules(("ci", "lint"), 1)

    def approve(self, number: int) -> None:
        reviewer = self.forge.client("GITEA_REVIEWER_TOKEN", "Reviewer")
        reviewer.post(
            f"/repos/{self.forge.repo}/pulls/{number}/reviews",
            {"event": "APPROVED", "body": "sbxloop conformance: approved"},
        )

    def existing_issue(self, title: str, labels: Sequence[str]) -> int:
        return self.ops.issue_create(self.forge.repo, title, "", labels=list(labels)).number


def _live_gitea_seeds(ops: VcsOps) -> Seeds:
    return _LiveGiteaSeeds(ops, _live_gitea_forge())


BACKENDS: dict[str, Backend] = {
    "github": Backend(kind="github", repo="o/r", base="main", make=_github, seeds=_github_seeds),
    "gitlab": Backend(
        kind="gitlab", repo="acme/widgets", base="main", make=_gitlab, seeds=_gitlab_seeds
    ),
    "gitlab-live": Backend(
        kind="gitlab",
        repo="acme/widgets",
        base="main",
        make=_live_gitlab,
        seeds=_live_gitlab_seeds,
        unavailable=_live_unavailable("gitlab"),
    ),
    "gitea": Backend(
        kind="gitea", repo="acme/widgets", base="main", make=_gitea, seeds=_gitea_seeds
    ),
    "gitea-live": Backend(
        kind="gitea",
        repo="acme/widgets",
        base="main",
        make=_live_gitea,
        seeds=_live_gitea_seeds,
        unavailable=_live_unavailable("gitea"),
    ),
}


def registered() -> list[str]:
    return sorted(BACKENDS)
