"""Repo-qualified GitHub ids alongside the legacy bare and typed forms."""

from __future__ import annotations

import pytest

from sbxloop.daemon.model import WorkItem
from sbxloop.ghids import (
    GhId,
    format_gh_id,
    is_repo_slug,
    issue_item_id,
    normalize_item_id,
    parse_gh_id,
    pr_item_id,
    try_parse_gh_id,
)

REPO = "brettbergin/sbxloop"


def test_render_repo_qualified() -> None:
    assert format_gh_id("issue", 511, repo=REPO) == f"gh:{REPO}:issue:511"
    assert issue_item_id(511, repo=REPO) == f"gh:{REPO}:issue:511"
    assert pr_item_id(7, repo=REPO) == f"gh:{REPO}:pr:7"


def test_render_without_repo_unchanged() -> None:
    assert format_gh_id("issue", 511) == "gh:issue:511"
    assert issue_item_id(511) == "gh:issue:511"
    assert pr_item_id(7) == "gh:pr:7"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("gh:511", GhId("issue", 511)),
        ("gh:issue:511", GhId("issue", 511)),
        ("gh:pr:7", GhId("pr", 7)),
        (f"gh:{REPO}:issue:511", GhId("issue", 511, REPO)),
        (f"gh:{REPO}:pr:7", GhId("pr", 7, REPO)),
    ],
)
def test_parse_all_forms(text: str, expected: GhId) -> None:
    assert parse_gh_id(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        f"gh:{REPO}:issue:511",
        f"gh:{REPO}:pr:7",
        "gh:issue:511",
        "gh:pr:7",
    ],
)
def test_round_trip(text: str) -> None:
    assert parse_gh_id(text).item_id == text
    assert str(parse_gh_id(text)) == text
    assert normalize_item_id(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "gh:owner/name",
        "gh:owner/name:issue",
        "gh:owner/name:issue:0",
        "gh:owner/name:bug:3",
        "gh:owner/name:issue:x",
        "gh:own er/name:issue:3",
        "gh:/name:issue:3",
        "gh:owner/:issue:3",
        "gh:owner/name/extra:issue:3",
    ],
)
def test_malformed_repo_qualified_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        parse_gh_id(text)
    assert try_parse_gh_id(text) is None
    # Unparseable ids pass through the normaliser untouched.
    assert normalize_item_id(text) == text


def test_legacy_id_still_normalises() -> None:
    assert normalize_item_id("gh:511") == "gh:issue:511"
    assert normalize_item_id("gh:issue:511") == "gh:issue:511"
    assert normalize_item_id("inbox:foo.md") == "inbox:foo.md"


def test_repo_slug_validation() -> None:
    assert is_repo_slug(REPO)
    assert not is_repo_slug("nope")
    assert not is_repo_slug("a/b/c")
    with pytest.raises(ValueError):
        format_gh_id("issue", 1, repo="nope")


def test_work_item_normalises_and_carries_repo() -> None:
    item = WorkItem(
        item_id=f"gh:{REPO}:issue:511",
        source_key="511",
        title="t",
        repo=REPO,
    )
    assert item.item_id == f"gh:{REPO}:issue:511"
    assert item.repo == REPO


def test_work_item_legacy_load_without_repo() -> None:
    item = WorkItem.model_validate({"item_id": "gh:511", "source_key": "511", "title": "t"})
    assert item.item_id == "gh:issue:511"
    assert item.repo is None
