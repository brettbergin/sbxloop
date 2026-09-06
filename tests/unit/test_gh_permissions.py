"""The permission matrix a run needs (#696): one table, three readers."""

from __future__ import annotations

from typing import ClassVar

from sbxloop.gh.permissions import (
    NEEDS,
    WORKFLOWS_DIR,
    missing_from_app,
    missing_from_scopes,
    split_required,
    workflow_paths,
    workflows_write_granted,
)

LABELS = [n.label for n in NEEDS]


class TestMatrix:
    def test_every_stage_is_named(self) -> None:
        assert LABELS == [
            "metadata:read",
            "contents:write",
            "pull_requests:write",
            "issues:write",
            "checks:read",
            "actions:read",
            "workflows:write",
        ]
        required, optional = split_required(NEEDS)
        assert [n.label for n in optional] == ["workflows:write"]
        assert len(required) == 6
        assert all(n.feature for n in NEEDS)


class TestClassicScopes:
    def test_repo_covers_everything_but_workflow_files(self) -> None:
        assert [n.label for n in missing_from_scopes(["repo"])] == ["workflows:write"]
        assert missing_from_scopes(["repo", "workflow"]) == ()

    def test_no_scopes_misses_everything(self) -> None:
        assert [n.label for n in missing_from_scopes([])] == LABELS
        assert [n.label for n in missing_from_scopes(["gist", "read:org"])] == LABELS

    def test_public_repo_is_enough_for_a_public_repository_only(self) -> None:
        assert [n.label for n in missing_from_scopes(["public_repo"], private=False)] == [
            "workflows:write"
        ]
        assert [n.label for n in missing_from_scopes(["public_repo"], private=True)] == LABELS


class TestAppPermissions:
    FULL: ClassVar[dict[str, str]] = {
        "metadata": "read",
        "contents": "write",
        "pull_requests": "write",
        "issues": "write",
        "checks": "read",
        "actions": "read",
        "workflows": "write",
    }

    def test_a_full_grant_misses_nothing(self) -> None:
        assert missing_from_app(self.FULL) == ()

    def test_write_on_a_read_need_is_fine(self) -> None:
        assert missing_from_app({**self.FULL, "checks": "write", "actions": "write"}) == ()

    def test_read_on_a_write_need_is_not(self) -> None:
        missing = missing_from_app({**self.FULL, "contents": "read"})
        assert [n.label for n in missing] == ["contents:write"]
        assert "delivering" in missing[0].feature

    def test_an_absent_key_is_missing(self) -> None:
        granted = dict(self.FULL)
        del granted["checks"]
        del granted["workflows"]
        required, optional = split_required(missing_from_app(granted))
        assert [n.label for n in required] == ["checks:read"]
        assert [n.label for n in optional] == ["workflows:write"]
        assert "CI and landing" in required[0].feature


class TestWorkflowsWrite:
    """The one delivery GitHub holds to a permission of its own (#752):
    a tree entry under ``.github/workflows/``."""

    def test_workflow_paths_are_the_entries_under_the_directory(self) -> None:
        assert WORKFLOWS_DIR == ".github/workflows/"
        paths = [
            "README.md",
            ".github/workflows/ci.yml",
            ".github/dependabot.yml",
            ".github/workflows/nested/x.yml",
            "docs/.github/workflows/not-really.yml",
        ]
        assert workflow_paths(paths) == (
            ".github/workflows/ci.yml",
            ".github/workflows/nested/x.yml",
        )

    def test_an_app_grant_map_decides(self) -> None:
        assert workflows_write_granted(app_permissions={"workflows": "write"}, scopes=None) is True
        assert workflows_write_granted(app_permissions={"contents": "write"}, scopes=None) is False
        # read is not write
        assert workflows_write_granted(app_permissions={"workflows": "read"}, scopes=None) is False
        # the map wins over scopes when both are given: an App is not a user
        assert (
            workflows_write_granted(app_permissions={"contents": "write"}, scopes=["workflow"])
            is False
        )

    def test_classic_scopes_decide(self) -> None:
        assert workflows_write_granted(app_permissions=None, scopes=["repo", "workflow"]) is True
        assert workflows_write_granted(app_permissions=None, scopes=["repo"]) is False
        assert workflows_write_granted(app_permissions=None, scopes=()) is False

    def test_nothing_reported_is_unknown_not_refused(self) -> None:
        # A fine-grained PAT reports neither: GitHub's answer at the tree is
        # the only one, and the delivery must not refuse on a guess.
        assert workflows_write_granted(app_permissions=None, scopes=None) is None
