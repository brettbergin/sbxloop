"""The issue-search grammar the loop speaks, parsed once for every backend.

Two callers search for issues and both spell their query the way the
first backend's search API took it: the follow-up lookup asks
``repo:<owner/name> is:issue in:title,body <terms>`` and the daemon's
label poll asks ``repo:<owner/name> is:issue is:open label:"<name>"``.
GitHub takes that string as it is; another forge takes structured
parameters. :func:`parse_issue_query` turns the grammar into
:class:`IssueQuery`, so a backend answers the same question from its own
endpoint rather than guessing at the string, and a qualifier no backend
can honour is a refusal, never a wider answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sbxloop.errors import GithubOpsError

_TOKEN = re.compile(r'(?P<key>[a-z_]+):(?:"(?P<quoted>[^"]*)"|(?P<bare>\S+))|(?P<term>\S+)')


class UnsupportedQuery(GithubOpsError):
    """A qualifier this grammar does not carry: the backend would have to
    guess what the caller meant, so it refuses instead."""


@dataclass(frozen=True)
class IssueQuery:
    repo: str | None = None
    #: ``open``, ``closed`` or ``all``.
    state: str = "all"
    labels: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    #: ``issue`` (the only kind the callers ask for) or ``pr``.
    kind: str = "issue"
    #: Where free terms are matched; ``title,body`` is the loop's ask.
    fields: tuple[str, ...] = ("title", "body")


def parse_issue_query(query: str) -> IssueQuery:
    repo: str | None = None
    state = "all"
    labels: list[str] = []
    terms: list[str] = []
    kind = "issue"
    fields: tuple[str, ...] = ("title", "body")
    for match in _TOKEN.finditer(query):
        if match.group("term") is not None:
            terms.append(match.group("term"))
            continue
        key = match.group("key")
        value = match.group("quoted") if match.group("quoted") is not None else match.group("bare")
        value = value or ""
        if key == "repo":
            repo = value
        elif key == "is":
            if value in ("open", "closed"):
                state = value
            elif value in ("issue", "pr"):
                kind = value
            else:
                raise UnsupportedQuery(f"issue search does not understand is:{value}")
        elif key == "label":
            labels.append(value)
        elif key == "in":
            fields = tuple(part for part in value.split(",") if part)
        elif key == "state":
            if value not in ("open", "closed"):
                raise UnsupportedQuery(f"issue search does not understand state:{value}")
            state = value
        else:
            raise UnsupportedQuery(f"issue search does not understand {key}:{value}")
    return IssueQuery(
        repo=repo,
        state=state,
        labels=tuple(labels),
        terms=tuple(terms),
        kind=kind,
        fields=fields,
    )
