"""The base branch is what the repository reports, never a guess (#672).

A repository that lives on ``develop`` or ``master`` used to be delivered
against ``main`` wherever the GitHub payload lacked ``default_branch``.
Every stage that needs the base now asks :meth:`GithubOps.default_branch`,
which raises instead of guessing; the scripted-echo harness from
``test_engine`` drives whole runs here with :class:`FakeGithub` answering.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop import hostgit
from sbxloop.errors import GithubOpsError
from sbxloop.events import Event, HostEventTypes
from tests.conftest import FakeSbx
from tests.fakes.fake_github import FakeGithub
from tests.unit.test_engine import (
    BUILD,
    FAST_LANDING,
    FILES_BUILD,
    REVIEW_OK,
    Harness,
    task,
    taskgraph,
)


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


def develop_repo() -> FakeGithub:
    fake = FakeGithub()
    fake.repo_payload["default_branch"] = "develop"
    return fake


class TestDevelopRepository:
    def test_delivery_conflict_round_and_landing_all_target_develop(self, harness: Harness) -> None:
        fake = develop_repo()
        fake.pr["mergeable"] = False
        fake.pr["mergeable_state"] = "dirty"
        merges: list[tuple[Path, str]] = []

        def merge_from_base(
            repo_path: Path, base_branch: str, *, remote: str = "origin"
        ) -> hostgit.MergeResult:
            merges.append((repo_path, base_branch))
            return hostgit.MergeResult(True, (), f"merged origin/{base_branch}: clean")

        harness.monkeypatch.setattr(hostgit, "merge_from_base", merge_from_base)
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK, BUILD, REVIEW_OK])
        engine = harness.pipeline(fake)

        def resolved(event: Event) -> None:
            if event.type == HostEventTypes.FIX_ROUND:
                fake.pr["mergeable"] = True
                fake.pr["mergeable_state"] = "clean"

        engine.bus.subscribe(resolved)
        result = engine.start("land it")

        assert result.state == "merged"
        assert result.workspace is not None
        # The PR opened against develop, the conflict round merged develop
        # into the clone, and the landing read develop's protection.
        assert fake.pr_kwargs["base"] == "develop"
        assert merges == [(result.workspace, "develop")]
        assert any(p.endswith("/branches/develop/protection") for _, p, _ in fake.raw_calls)
        assert not any("/branches/main/" in p for _, p, _ in fake.raw_calls)

    def test_a_restart_compares_the_prior_branch_against_develop(self, harness: Harness) -> None:
        fake = develop_repo()
        fake.branches.add("sbxloop/rprev0001")
        fake.pr_created = True
        fake.pr["head"] = {"sha": "priorhead"}
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        engine = harness.pipeline(fake)
        result = engine.start("restart me", prior_branch="sbxloop/rprev0001", prior_pr=fake.number)

        assert result.state == "merged"
        compares = [p for _, p, _ in fake.raw_calls if "/compare/" in p]
        assert compares[0] == "/repos/o/r/compare/develop...sbxloop/rprev0001"
        assert all("/compare/develop..." in p for p in compares)

    def test_configured_deliver_base_still_wins(self, harness: Harness) -> None:
        fake = develop_repo()
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        engine = harness.engine(
            ops=fake, github={"repo": fake.repo, "deliver_base": "release/2"}, landing=FAST_LANDING
        )
        result = engine.start("land it")
        assert result.state == "merged"
        assert fake.pr_kwargs["base"] == "release/2"


class TestNoDefaultBranch:
    def test_the_run_stops_before_delivering_on_a_guess(self, harness: Harness) -> None:
        """No `default_branch` in the payload: the error names the fix and
        nothing is pushed — no branch, no PR — rather than a delivery
        against a `main` that may not exist."""
        fake = FakeGithub()
        del fake.repo_payload["default_branch"]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        engine = harness.pipeline(fake)
        with pytest.raises(GithubOpsError, match="did not report a default branch for o/r"):
            engine.start("land it")

        assert fake.pr_create_calls == 0
        assert not any(p.endswith("/git/refs") for _, p, _ in fake.raw_calls)
        assert not fake.blob_batches
