"""Only locations established by the PR diff may become inline findings."""

from __future__ import annotations

from typing import Any

import pytest

from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import MAX_PAGES, PAGE_SIZE, PaginationError
from sbxloop.gh.review_locations import right_side_ranges
from tests.fakes.fake_github import FakeGithub


@pytest.mark.parametrize(
    "patch,lines",
    [
        ("@@ -1 +8 @@\n-old\n+new", [8]),
        ("@@ -4,3 +8,4 @@ scope\n context\n-old\n+new\n+added\n tail", [8, 9, 10, 11]),
        ("@@ -0,0 +1,2 @@\n+one\n+two", [1, 2]),
        ("@@ -2,2 +1,0 @@\n-one\n-two", []),
        ("@@ -1 +1 @@\n-old\n+new\n@@ -20 +25 @@\n-old\n+new", [1, 25]),
        (
            "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n",
            [1],
        ),
        ("@@ -1 +1 @@\n-old\r\n+new\r\n", [1]),
        (None, []),
        (42, []),
        ("", []),
        ("Binary files differ", []),
        ("@@@ -1,2 -1,2 +1,2 @@@\n++combined", []),
        ("@@ -1 +1,2 @@\n-old\n+truncated", []),
        ("@@ -1 +1 @@\n-old\n+extra\n+extra", []),
        ("@@ -1 +1 @@\n-old\n+new\n@@ -4 +4 @@\n-old", []),
        ("@@ -1 +1 @@\n-old\n+new\n@@ -1 +1 @@\n-old\n+overlap", []),
        ("@@ -0 +1 @@\n-old\n+new", []),
        ("@@ -1 +0 @@\n-old\n+new", []),
        ("@@ -1 +1 @@\nunprefixed", []),
        ("@@ -1 +1,999999999999 @@\n-old\n+short", []),
    ],
)
def test_patch_locations(patch: object, lines: list[int]) -> None:
    assert [line for hunk in right_side_ranges(patch) for line in hunk] == lines


def locations(gh: FakeGithub) -> dict[str, tuple[range, ...]]:
    return gh.pr_review_locations(gh.repo, gh.number, commit_id="commit0")


def test_files_use_exact_current_names_and_do_not_guess_missing_patches() -> None:
    gh = FakeGithub()
    gh.files_payload = [
        {
            "filename": "new name/é.txt",
            "previous_filename": "old.txt",
            "patch": "@@ -1 +8 @@\n-old\n+new",
        },
        {"filename": "binary.png"},
        {"filename": "truncated.txt", "patch": "@@ -1 +1,2 @@\n-old\n+short"},
    ]
    assert locations(gh) == {
        "new name/é.txt": (range(8, 9),),
        "binary.png": (),
        "truncated.txt": (),
    }
    gh.assert_no_failed_jobs()


def test_reads_later_file_pages() -> None:
    gh = FakeGithub()
    gh.files_payload = [
        {"filename": f"file-{i}", "patch": "@@ -0,0 +1 @@\n+new"} for i in range(PAGE_SIZE + 1)
    ]
    found = locations(gh)
    assert len(found) == PAGE_SIZE + 1
    assert found[f"file-{PAGE_SIZE}"] == (range(1, 2),)
    assert [path.rsplit("=", 1)[-1] for _, path, _ in gh.raw_calls] == ["1", "2"]


@pytest.mark.parametrize("field", ["head", "base"])
def test_moving_refs_cannot_authorize_locations(field: str) -> None:
    gh = FakeGithub()
    gh.files_after_read = {field: {"sha": "moved"}}
    with pytest.raises(GithubOpsError, match=r"head no longer matches|base changed"):
        locations(gh)


@pytest.mark.parametrize("field,value", [("head", None), ("head", {"sha": "other"}), ("base", {})])
def test_missing_or_stale_refs_do_not_read_the_files(field: str, value: Any) -> None:
    gh = FakeGithub()
    gh.pr[field] = value
    with pytest.raises(GithubOpsError, match=r"SHAs|reviewed commit"):
        locations(gh)
    assert gh.raw_calls == []


@pytest.mark.parametrize("payload", [{}, [None], [{"filename": ""}], [{"filename": "x"}] * 2])
def test_malformed_file_lists_are_not_trusted(payload: Any) -> None:
    gh = FakeGithub()
    gh.files_payload = payload
    with pytest.raises(GithubOpsError, match=r"list of changed files|filenames"):
        locations(gh)


def test_an_unfinished_list_is_not_used() -> None:
    gh = FakeGithub()
    gh.files_payload = [{"filename": str(i)} for i in range(PAGE_SIZE * MAX_PAGES)]
    with pytest.raises(PaginationError, match="page limit"):
        locations(gh)


def test_read_errors_remain_visible() -> None:
    gh = FakeGithub()
    gh.fail_once["pr_files"] = GithubOpsError("service unavailable", http_status=503)
    with pytest.raises(GithubOpsError, match="service unavailable"):
        locations(gh)
    assert gh.failed_jobs
