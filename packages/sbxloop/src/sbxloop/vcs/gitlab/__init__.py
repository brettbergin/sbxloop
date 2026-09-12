"""The GitLab backend (#1009): the role protocols answered against the
GitLab REST API v4, from the sandbox that holds the project token."""

from sbxloop.vcs.gitlab.ops import GitlabOps, gitlab_transport

__all__ = ["GitlabOps", "gitlab_transport"]
