"""Unit tests for the typed GitHub resource id helper."""

from __future__ import annotations

import pytest

from sbxloop.ghids import (
    GhId,
    format_gh_id,
    has_gh_prefix,
    is_gh_id,
    issue_item_id,
    normalize_item_id,
    parse_gh_id,
    pr_item_id,
    try_parse_gh_id,
)


def test_format_emits_typed_ids() -> None:
    assert format_gh_id("issue", 1234) == "gh:issue:1234"
    assert format_gh_id("pr", 7) == "gh:pr:7"
    assert issue_item_id(508) == "gh:issue:508"
    assert pr_item_id(509) == "gh:pr:509"


def test_format_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="kind"):
        format_gh_id("tag", 5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        format_gh_id("issue", 0)
    with pytest.raises(ValueError, match="positive"):
        format_gh_id("pr", -1)


def test_parse_typed_forms() -> None:
    assert parse_gh_id("gh:issue:12") == GhId("issue", 12)
    assert parse_gh_id("gh:pr:12") == GhId("pr", 12)


def test_parse_legacy_bare_form_is_an_issue() -> None:
    parsed = parse_gh_id("gh:1234")
    assert parsed == GhId("issue", 1234)
    assert parsed.item_id == "gh:issue:1234"
    assert str(parsed) == "gh:issue:1234"


@pytest.mark.parametrize(
    "value",
    [
        "gh:",
        "gh:abc",
        "gh:issue:",
        "gh:issue:-1",
        "gh:issue:0",
        "gh:tag:5",
        "gh:pr:x",
        "gh:issue:1:2",
    ],
)
def test_parse_rejects_malformed(value: str) -> None:
    with pytest.raises(ValueError):
        parse_gh_id(value)
    assert try_parse_gh_id(value) is None
    assert is_gh_id(value) is False


@pytest.mark.parametrize("value", ["inbox:foo.md", "", "gh", "ghx:12"])
def test_parse_rejects_non_github_ids(value: str) -> None:
    with pytest.raises(ValueError, match="not a GitHub id"):
        parse_gh_id(value)


def test_round_trip() -> None:
    for kind, number in (("issue", 1), ("pr", 99999)):
        rendered = format_gh_id(kind, number)  # type: ignore[arg-type]
        parsed = parse_gh_id(rendered)
        assert (parsed.kind, parsed.number) == (kind, number)
        assert format_gh_id(parsed.kind, parsed.number) == rendered


def test_normalize_item_id() -> None:
    assert normalize_item_id("gh:1234") == "gh:issue:1234"
    assert normalize_item_id("gh:issue:1234") == "gh:issue:1234"
    assert normalize_item_id("gh:pr:12") == "gh:pr:12"
    # Non-GitHub and malformed ids pass through untouched.
    assert normalize_item_id("inbox:x.md") == "inbox:x.md"
    assert normalize_item_id("gh:abc") == "gh:abc"


def test_predicates() -> None:
    assert is_gh_id("gh:12") is True
    assert is_gh_id("gh:pr:12") is True
    assert is_gh_id("inbox:x.md") is False
    assert has_gh_prefix("gh:abc") is True
    assert has_gh_prefix("inbox:x.md") is False
