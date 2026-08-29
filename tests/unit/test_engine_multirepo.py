"""A run routes every GitHub side effect to its own repository.

With several ``[[github.repos]]`` configured, the repository a run acts on
is the one its work item came from — not the first configured entry. These
tests drive the whole pipeline against a FakeGithub that records the ``repo``
argument of every call, and assert nothing ever touched repo A.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config, GithubConfig
from sbxloop.errors import StateError
from sbxloop.sbx.provision import Provisioner
from tests.conftest import FakeSbx
from tests.fakes.fake_github import GREEN, FakeGithub
from tests.unit.test_engine import (
    FAST_LANDING,
    FILES_BUILD,
    REVIEW_OK,
    Harness,
    task,
    taskgraph,
)

REPO_A = "o/a"
REPO_B = "o/b"


class RepoRecordingGithub(FakeGithub):
    """A FakeGithub that remembers which repository each call named."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.repo_args: list[tuple[str, str]] = []

    def _seen(self, op: str, repo: str) -> None:
        self.repo_args.append((op, repo))

    def repo_get(self, repo: str) -> dict[str, Any]:
        self._seen("repo_get", repo)
        return super().repo_get(repo)

    def repo_lookup(self, repo: str) -> dict[str, Any] | None:
        self._seen("repo_lookup", repo)
        return super().repo_lookup(repo)

    def ref_lookup(self, repo: str, ref: str) -> str | None:
        self._seen("ref_lookup", repo)
        return super().ref_lookup(repo, ref)

    def blobs_create_many(self, repo: str, files: list[dict[str, str]]) -> dict[str, str]:
        self._seen("blobs_create_many", repo)
        return super().blobs_create_many(repo, files)

    def pr_create(self, repo: str, *args: Any, **kwargs: Any) -> Any:
        self._seen("pr_create", repo)
        return super().pr_create(repo, *args, **kwargs)

    def pr_get(self, repo: str, number: int) -> dict[str, Any]:
        self._seen("pr_get", repo)
        return super().pr_get(repo, number)

    def pr_checks(self, repo: str, sha: str) -> Any:
        self._seen("pr_checks", repo)
        return super().pr_checks(repo, sha)

    def pr_review_create(self, repo: str, *args: Any, **kwargs: Any) -> Any:
        self._seen("pr_review_create", repo)
        return super().pr_review_create(repo, *args, **kwargs)

    def pr_merge(self, repo: str, number: int, **kwargs: Any) -> Any:
        self._seen("pr_merge", repo)
        return super().pr_merge(repo, number, **kwargs)

    def pr_update_branch(self, repo: str, number: int, **kwargs: Any) -> bool:
        self._seen("pr_update_branch", repo)
        return super().pr_update_branch(repo, number, **kwargs)

    def branch_delete(self, repo: str, branch: str) -> None:
        self._seen("branch_delete", repo)
        super().branch_delete(repo, branch)

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if path.startswith("/repos/"):
            owner, name = path.split("/")[2:4]
            self._seen(f"raw {method}", f"{owner}/{name}")
        return super().raw(method, path, body)


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


def two_repos(**overrides: Any) -> dict[str, Any]:
    b: dict[str, Any] = {"repo": REPO_B}
    b.update(overrides)
    return {"repos": [{"repo": REPO_A}, b]}


class TestRunRoutesToItsRepo:
    def test_pipeline_targets_the_items_repository(self, harness: Harness) -> None:
        fake = RepoRecordingGithub(repo=REPO_B)
        fake.checks = [GREEN]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        engine = harness.engine(ops=fake, github=two_repos(), landing=FAST_LANDING)

        result = engine.start("write hello.txt", repo=REPO_B)

        assert result.state == "merged"
        repos = {repo for _, repo in fake.repo_args}
        assert repos == {REPO_B}, f"expected only {REPO_B}, saw {sorted(repos)}"
        ops = {op for op, _ in fake.repo_args}
        # Delivery, PR, review, CI and merge all happened — against repo B.
        assert {"pr_create", "pr_get", "pr_checks", "pr_review_create", "pr_merge"} <= ops
        assert fake.deleted_branches == [f"sbxloop/{result.run_id}"]

    def test_the_runs_config_is_narrowed_and_persisted(self, harness: Harness) -> None:
        fake = RepoRecordingGithub(repo=REPO_B)
        fake.checks = [GREEN]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        engine = harness.engine(ops=fake, github=two_repos(), landing=FAST_LANDING)

        result = engine.start("write hello.txt", repo=REPO_B)

        stored = Config.model_validate_json(engine.store.get_run_config(result.run_id))
        assert stored.github.repo == REPO_B
        assert [r.repo for r in stored.github.repo_list()] == [REPO_B]

    def test_per_repo_deliver_base_wins_over_the_global_default(self, harness: Harness) -> None:
        fake = RepoRecordingGithub(repo=REPO_B)
        fake.checks = [GREEN]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        github = two_repos(deliver_base="develop") | {"deliver_base": "trunk"}
        engine = harness.engine(ops=fake, github=github, landing=FAST_LANDING)

        result = engine.start("write hello.txt", repo=REPO_B)

        assert result.state == "merged"
        assert fake.pr_kwargs["base"] == "develop"
        assert fake.pr_kwargs["repo"] == REPO_B

    def test_a_repo_without_its_own_base_inherits_the_global_one(self, harness: Harness) -> None:
        fake = RepoRecordingGithub(repo=REPO_B)
        fake.checks = [GREEN]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        github = two_repos() | {"deliver_base": "trunk"}
        engine = harness.engine(ops=fake, github=github, landing=FAST_LANDING)

        engine.start("write hello.txt", repo=REPO_B)

        assert fake.pr_kwargs["base"] == "trunk"

    def test_single_repo_runs_are_unchanged(self, harness: Harness) -> None:
        fake = RepoRecordingGithub()
        fake.checks = [GREEN]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        engine = harness.engine(ops=fake, github={"repo": fake.repo}, landing=FAST_LANDING)

        result = engine.start("write hello.txt")

        assert result.state == "merged"
        assert {repo for _, repo in fake.repo_args} == {"o/r"}

    def test_an_unconfigured_repository_is_refused(self, harness: Harness) -> None:
        engine = harness.engine(ops=RepoRecordingGithub(), github=two_repos())
        with pytest.raises(StateError, match="not configured"):
            engine.start("write hello.txt", repo="o/nope")


class TestTokenScoping:
    """The github sandbox gets the run repository's own token when it names
    one; otherwise the daemon-wide GH_TOKEN."""

    def _provisioner(self, github: dict[str, Any], env: dict[str, str]) -> Provisioner:
        config = Config.model_validate({"github": github})
        return Provisioner(None, config, env=env)  # type: ignore[arg-type]

    def test_per_repo_token_env_wins(self) -> None:
        github = GithubConfig.model_validate(two_repos(token_env="GH_TOKEN_B")).for_repo(REPO_B)
        prov = self._provisioner(github.model_dump(), {"GH_TOKEN": "global", "GH_TOKEN_B": "btok"})
        assert prov.gh_token() == "btok"

    def test_without_token_env_the_global_token_is_used(self) -> None:
        github = GithubConfig.model_validate(two_repos()).for_repo(REPO_B)
        prov = self._provisioner(github.model_dump(), {"GH_TOKEN": "global"})
        assert prov.gh_token() == "global"
