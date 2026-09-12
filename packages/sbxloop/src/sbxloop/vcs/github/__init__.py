"""GitHub integration: the typed facade over ``github.op`` jobs."""

from sbxloop.vcs.github.ops import GithubOps, IssueRef, PrRef

__all__ = ["GithubOps", "IssueRef", "PrRef"]
