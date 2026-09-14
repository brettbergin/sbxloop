"""Issue IIDs and MR IIDs are separate resources on GitLab."""

from types import SimpleNamespace
from typing import cast

import pytest

from sbxloop.config import RepoConfig
from sbxloop.engine.engine import LoopEngine, Pipeline
from tests.fakes.fake_gitlab import FakeGitlab


@pytest.mark.parametrize("same_issue", [True, False])
def test_code_delivery_labels_the_mr_without_touching_an_issue(same_issue: bool) -> None:
    fake = FakeGitlab()
    fake.seed_mr(1, source_branch="feature", head_sha="source")
    fake.merge_requests[1]["labels"] = ["existing"]
    if same_issue:
        fake.seed_issue(1, "Unrelated issue", labels=["issue-label"])
    pipeline = cast(
        Pipeline,
        SimpleNamespace(
            repo_config=RepoConfig(repo=fake.repo, labels=["automation"]),
            repo=fake.repo,
            ops=fake,
            run_id="r1",
        ),
    )
    LoopEngine._label_pr(cast(LoopEngine, None), pipeline, 1)
    assert fake.merge_requests[1]["labels"] == ["existing", "automation"]
    if same_issue:
        assert fake.issues[1]["labels"] == ["issue-label"]
    assert fake.mr_updates == [(1, {"add_labels": "automation"})]
