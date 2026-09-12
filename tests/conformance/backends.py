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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import NamedTuple, Protocol

from sbxloop.vcs.model import ChecksVerdict, FailedCheck
from sbxloop.vcs.protocol import VcsOps
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

    def existing_issue(self, title: str, labels: Sequence[str]) -> int:
        self.fake.seed_issue(41, title, labels)
        return 41


def _gitlab() -> VcsOps:
    return FakeGitlab(repo="acme/widgets")


def _gitlab_seeds(ops: VcsOps) -> Seeds:
    assert isinstance(ops, FakeGitlab)
    return _GitlabSeeds(ops)


# -- live forges ----------------------------------------------------------------


def _no_backend(kind: str) -> Callable[[], VcsOps]:
    def make() -> VcsOps:
        raise AssertionError(f"no backend implements kind {kind!r}")

    return make


def _no_seeds(ops: VcsOps) -> Seeds:
    raise AssertionError("a live forge without a backend has no seeds")


def _live_without_backend(kind: str) -> Callable[[], str | None]:
    """A live forge the harness can reach but no backend can drive yet:
    skip naming both facts, so the skip says the forge was there."""

    def unavailable() -> str | None:
        forge = live_forge(kind)
        if isinstance(forge, str):
            return forge
        return f"live {kind} {forge.version} answers, but no backend implements kind {kind!r} yet"

    return unavailable


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


def _reset_live_branch(forge: LiveForge, branch: str) -> None:
    """The change scenarios open a merge request from ``branch`` and never
    close it (the fake forgets everything per test). On a live forge the
    last run's request would refuse the next (409), so the branch starts
    every backend build fresh: its open requests closed, the branch cut
    again from the base with one commit to diff."""
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
    dev.post(
        f"{project}/repository/commits",
        {
            "branch": branch,
            "start_branch": "main",
            "commit_message": "sbxloop conformance: one change to review",
            "actions": [
                {"action": "create", "file_path": "a.py", "content": "x = 1\ny = 2\nz = 3\n"}
            ],
        },
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

    def existing_issue(self, title: str, labels: Sequence[str]) -> int:
        return self.ops.issue_create(self.forge.repo, title, "", labels=list(labels)).number


def _live_gitlab_seeds(ops: VcsOps) -> Seeds:
    return _LiveGitlabSeeds(ops, _live_gitlab_forge())


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
    "gitea-live": Backend(
        kind="gitea",
        repo="acme/widgets",
        base="main",
        make=_no_backend("gitea"),
        seeds=_no_seeds,
        unavailable=_live_without_backend("gitea"),
    ),
}


def registered() -> list[str]:
    return sorted(BACKENDS)
