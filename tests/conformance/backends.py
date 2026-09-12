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
from typing import Protocol

from sbxloop.vcs.model import ChecksVerdict, FailedCheck
from sbxloop.vcs.protocol import VcsOps
from tests.fakes.fake_github import FakeGithub
from tests.live.env import live_forge


class Seeds(Protocol):
    """Arrange state in the backend, in its own terms."""

    def failed_check(self, name: str, excerpt: str) -> None:
        """The change's head has one red check called ``name`` whose log
        (or description) says ``excerpt``."""
        ...

    def pending_check(self, name: str) -> None:
        """The change's head has one check called ``name`` still running."""
        ...

    def base_rules(self, *, required: Sequence[str], approvals: int) -> None:
        """The base branch requires the checks ``required`` green and
        ``approvals`` approving reviews before a merge."""
        ...

    def existing_issue(self, number: int, title: str, labels: Sequence[str]) -> None:
        """The repository carries issue ``number`` with ``labels``."""
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

    def base_rules(self, *, required: Sequence[str], approvals: int) -> None:
        self.fake.protection = {
            "required_status_checks": {"contexts": list(required)},
            "required_pull_request_reviews": {"required_approving_review_count": approvals},
        }
        self.fake.rules = []

    def existing_issue(self, number: int, title: str, labels: Sequence[str]) -> None:
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


def _github() -> VcsOps:
    return FakeGithub(repo="o/r", number=7)


def _github_seeds(ops: VcsOps) -> Seeds:
    assert isinstance(ops, FakeGithub)
    return _GithubSeeds(ops)


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


BACKENDS: dict[str, Backend] = {
    "github": Backend(kind="github", repo="o/r", base="main", make=_github, seeds=_github_seeds),
    "gitlab-live": Backend(
        kind="gitlab",
        repo="acme/widgets",
        base="main",
        make=_no_backend("gitlab"),
        seeds=_no_seeds,
        unavailable=_live_without_backend("gitlab"),
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
