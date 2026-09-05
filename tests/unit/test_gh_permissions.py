"""The permission matrix a run needs (#696): one table, three readers."""

from __future__ import annotations

from typing import ClassVar

from sbxloop.gh.permissions import (
    NEEDS,
    missing_from_app,
    missing_from_scopes,
    split_required,
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
