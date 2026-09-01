"""A restart continues the previous attempt's pushed work (#600).

Re-applying the trigger label starts a *new* run for the same work item, and
the daemon offers that run whatever the last attempt left on the GitHub
origin: its branch and its pull request. The engine takes the offer only
when GitHub still has the branch and it is still related to the base branch;
anything else is a fresh start with one structured reason line and no
exception.

The scripted-echo harness from ``test_engine`` drives whole runs here, with
:class:`tests.fakes.fake_github.FakeGithub` answering delivery, the ref
lookup, the compare and the PR reads.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tests.conftest import FakeSbx
from tests.fakes.fake_github import FakeGithub
from tests.unit.test_engine import (
    FILES_BUILD,
    REVIEW_OK,
    Harness,
    task,
    taskgraph,
)

PRIOR_BRANCH = "sbxloop/rprev0001"


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


def unusable_reasons(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if "engine.prior_branch_unusable" in record.getMessage()
    ]


def script_one_round(harness: Harness) -> None:
    harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])


class TestBranchReuse:
    def test_reuses_the_prior_branch_and_reattaches_to_its_pr(self, harness: Harness) -> None:
        """The branch is on origin and shares a merge base with the base
        branch: the run delivers onto it, parented on its head, and refreshes
        the open PR instead of opening a second one."""
        fake = FakeGithub()
        # The previous attempt's state: a branch on origin behind an open PR.
        fake.branches.add(PRIOR_BRANCH)
        fake.pr_created = True
        fake.pr["head"] = {"sha": "priorhead"}
        script_one_round(harness)
        engine = harness.pipeline(fake)
        result = engine.start("restart me", prior_branch=PRIOR_BRANCH, prior_pr=fake.number)

        assert result.state == "merged"
        run = engine.store.get_run(result.run_id)
        assert run.branch == PRIOR_BRANCH  # no new branch name was generated
        assert run.pr_number == fake.number
        # The PR was reattached to, never re-opened.
        assert fake.pr_create_calls == 0
        # The delivered commit descends from the prior head, so the earlier
        # commits are still in the branch's history.
        commits = [
            body
            for method, path, body in fake.raw_calls
            if method == "POST" and path.endswith("/git/commits") and body
        ]
        assert commits and commits[0]["parents"] == ["priorhead"]

    def test_a_fresh_run_without_a_prior_branch_is_unchanged(self, harness: Harness) -> None:
        fake = FakeGithub()
        script_one_round(harness)
        engine = harness.pipeline(fake)
        result = engine.start("no prior attempt")
        assert result.state == "merged"
        assert engine.store.get_run(result.run_id).branch == f"sbxloop/{result.run_id}"
        assert fake.pr_create_calls == 1


class TestFallback:
    def test_missing_branch_starts_fresh_with_a_reason(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The previous attempt's branch was deleted: a fresh branch, a
        logged reason, and no exception."""
        fake = FakeGithub()  # branches empty: the ref lookup misses
        script_one_round(harness)
        engine = harness.pipeline(fake)
        with caplog.at_level(logging.INFO, logger="sbxloop.engine.engine"):
            result = engine.start("restart me", prior_branch=PRIOR_BRANCH, prior_pr=7)

        assert result.state == "merged"
        assert engine.store.get_run(result.run_id).branch == f"sbxloop/{result.run_id}"
        reasons = unusable_reasons(caplog)
        assert len(reasons) == 1
        assert "no longer on origin" in reasons[0]
        assert PRIOR_BRANCH in reasons[0]

    def test_unrelated_branch_starts_fresh_with_a_reason(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The branch exists but shares no history with the base branch
        (force-diverged, or a leftover from another line of work)."""
        fake = FakeGithub()
        fake.branches.add(PRIOR_BRANCH)
        fake.unrelated_branches.add(PRIOR_BRANCH)
        script_one_round(harness)
        engine = harness.pipeline(fake)
        with caplog.at_level(logging.INFO, logger="sbxloop.engine.engine"):
            result = engine.start("restart me", prior_branch=PRIOR_BRANCH)

        assert result.state == "merged"
        assert engine.store.get_run(result.run_id).branch == f"sbxloop/{result.run_id}"
        reasons = unusable_reasons(caplog)
        assert len(reasons) == 1
        assert "merge base" in reasons[0]

    def test_a_gone_pr_keeps_the_branch_and_opens_a_new_pr(self, harness: Harness) -> None:
        """The branch is still good even when its PR is gone: continue the
        commits, open a fresh pull request for them."""
        fake = FakeGithub()
        fake.branches.add(PRIOR_BRANCH)  # pushed, but no PR was ever opened
        script_one_round(harness)
        engine = harness.pipeline(fake)
        result = engine.start("restart me", prior_branch=PRIOR_BRANCH)

        assert result.state == "merged"
        assert engine.store.get_run(result.run_id).branch == PRIOR_BRANCH
        assert fake.pr_create_calls == 1


def git(*argv: str, cwd: Path) -> str:
    import subprocess

    return subprocess.run(  # nosec B603 B607
        ["git", *argv], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def checkout_with_prior_branch(tmp_path: Path, branch: str) -> Path:
    """A source checkout that has ``main`` plus a fetched ``origin/<branch>``
    carrying a commit the previous attempt pushed — the shape the daemon's
    workspace has when it restarts an item."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-b", "main", cwd=origin)
    git("config", "user.email", "t@e.st", cwd=origin)
    git("config", "user.name", "t", cwd=origin)
    (origin / "keep.txt").write_text("keep\n")
    git("add", ".", cwd=origin)
    git("commit", "-m", "init", cwd=origin)
    git("checkout", "-b", branch, cwd=origin)
    (origin / "from_prior_attempt.txt").write_text("earlier work\n")
    git("add", ".", cwd=origin)
    git("commit", "-m", "prior attempt", cwd=origin)
    git("checkout", "main", cwd=origin)

    source = tmp_path / "checkout"
    git("clone", str(origin), str(source), cwd=tmp_path)
    git("config", "user.email", "t@e.st", cwd=source)
    git("config", "user.name", "t", cwd=source)
    git("fetch", "origin", f"{branch}:refs/remotes/origin/{branch}", cwd=source)
    return source


class TestWorkspacePinning:
    """The restart's *workspace* — not just its delivery — continues the
    previous attempt's branch (#600). Cutting the clone from base would
    make the agent rebuild the work from zero, and the delivered tree (a
    diff against base layered on the prior head) would then be a union
    neither the agent built nor the reviewer diffed."""

    def test_the_run_clone_is_cut_from_the_prior_branch(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        source = checkout_with_prior_branch(tmp_path, PRIOR_BRANCH)
        fake = FakeGithub()
        fake.branches.add(PRIOR_BRANCH)
        fake.pr_created = True
        fake.pr["head"] = {"sha": "priorhead"}
        script_one_round(harness)
        engine = harness.pipeline(
            fake,
            sandbox={"workspace": str(source), "workspace_isolation": "clone"},
        )
        result = engine.start("restart me", prior_branch=PRIOR_BRANCH, prior_pr=fake.number)

        assert result.state == "merged"
        workspace = engine.store.get_run(result.run_id).workspace
        assert workspace is not None
        clone = Path(workspace)
        # The agent saw the previous attempt's file, on the previous
        # attempt's branch — it did not start from an empty base cut.
        assert (clone / "from_prior_attempt.txt").is_file()
        assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone) == PRIOR_BRANCH

    def test_a_branch_the_checkout_cannot_fetch_starts_fresh(
        self, harness: Harness, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No usable artifact on the host side: the run starts fresh from
        base with a reason rather than erroring out, and it does NOT then
        deliver that base-cut tree onto the prior branch."""
        source = checkout_with_prior_branch(tmp_path, PRIOR_BRANCH)
        fake = FakeGithub()
        fake.branches.add(PRIOR_BRANCH)  # GitHub still has it; the checkout does not
        script_one_round(harness)
        engine = harness.pipeline(
            fake,
            sandbox={"workspace": str(source), "workspace_isolation": "clone"},
        )
        with caplog.at_level(logging.INFO):
            result = engine.start("restart me", prior_branch="sbxloop/never-fetched")

        assert result.state == "merged"
        run = engine.store.get_run(result.run_id)
        assert run.branch == f"sbxloop/{result.run_id}"
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "continue_branch_unusable" in messages
