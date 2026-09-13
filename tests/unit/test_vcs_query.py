"""The issue-search grammar (#1017): the two queries the loop spells,
parsed into what a backend can answer from its own endpoint."""

from __future__ import annotations

import pytest

from sbxloop.vcs.query import IssueQuery, UnsupportedQuery, parse_issue_query


def test_the_follow_up_lookups_query() -> None:
    asked = parse_issue_query("repo:o/r is:issue in:title,body flaky release gate")
    assert asked == IssueQuery(
        repo="o/r",
        state="all",
        labels=(),
        terms=("flaky", "release", "gate"),
        kind="issue",
        fields=("title", "body"),
    )


def test_the_daemons_labelled_poll() -> None:
    asked = parse_issue_query('repo:o/r is:issue is:open label:"sbxloop:run"')
    assert asked.repo == "o/r" and asked.state == "open"
    assert asked.labels == ("sbxloop:run",) and asked.terms == ()


def test_a_qualifier_the_grammar_lacks_is_refused_not_widened() -> None:
    with pytest.raises(UnsupportedQuery, match="author:me"):
        parse_issue_query("repo:o/r author:me is:issue")
    with pytest.raises(UnsupportedQuery, match="is:merged"):
        parse_issue_query("repo:o/r is:merged")
