"""The backend registry (#1017): a kind becomes a backend object over a
worker client with the right transport, and a kind no backend answers
fails closed by name."""

from __future__ import annotations

import pytest

from sbxloop.config import Config
from sbxloop.daemon.github import DaemonGithub
from sbxloop.errors import GithubOpsError
from sbxloop.events import EventBus
from sbxloop.sbx.cli import SbxCLI
from sbxloop.vcs.backends import (
    BackendNotImplemented,
    backend_for,
    capabilities_for,
    sandbox_token_envs,
    transport_for,
    unimplemented_operations,
    unimplemented_roles,
)
from sbxloop.vcs.github.ops import GithubOps
from sbxloop.vcs.gitlab.ops import GitlabOps
from sbxloop.vcs.protocol import Capability
from tests.conftest import FakeSbx
from tests.unit.test_gh_ops import StubWorkerClient


class TestFactory:
    def test_github_is_built_with_its_descriptor(self) -> None:
        ops = backend_for("github", StubWorkerClient({}), "r1", api_url="https://api.github.com")  # type: ignore[arg-type]
        assert isinstance(ops, GithubOps)
        assert ops.transport is not None and ops.transport.gh_cli is True

    def test_gitlab_is_built_with_its_descriptor(self) -> None:
        ops = backend_for("gitlab", StubWorkerClient({}), "r1", api_url="https://gl.example/api/v4")  # type: ignore[arg-type]
        assert isinstance(ops, GitlabOps)
        assert ops.transport is not None and ops.transport.auth == "private-token"
        assert ops.transport.api_url == "https://gl.example/api/v4"

    def test_a_forge_without_an_api_root_is_refused_by_name(self) -> None:
        with pytest.raises(GithubOpsError, match=r"\[vcs\] api_url is not set"):
            transport_for("gitlab", None)

    def test_a_kind_no_backend_answers_fails_closed(self) -> None:
        with pytest.raises(BackendNotImplemented, match='"svn"'):
            backend_for("svn", StubWorkerClient({}), "r1", api_url="https://svn.example/api")  # type: ignore[arg-type]
        assert capabilities_for("svn") is None and unimplemented_roles("svn") == ()

    def test_gitea_takes_the_operators_bot_list(self) -> None:
        from sbxloop.vcs.gitea.ops import GiteaOps

        ops = backend_for(
            "gitea",
            StubWorkerClient({}),  # type: ignore[arg-type]
            "r1",
            api_url="https://gt.example/api/v1",
            bot_logins=["ci-bot"],
        )
        assert isinstance(ops, GiteaOps) and ops.bot_logins == frozenset({"ci-bot"})
        assert ops.transport is not None and ops.transport.auth == "token"
        gitlab = backend_for(
            "gitlab",
            StubWorkerClient({}),
            "r1",
            api_url="https://gl.example/api/v4",
            bot_logins=["x"],  # type: ignore[arg-type]
        )
        assert not hasattr(gitlab, "bot_logins"), "GitLab reads its own flag"

    def test_the_registry_reports_what_each_kind_can_do(self) -> None:
        github = capabilities_for("github")
        gitlab = capabilities_for("gitlab")
        assert github is not None and github["merge_queue"] is Capability.SUPPORTED
        assert gitlab is not None and gitlab["review_threads"] is Capability.SUPPORTED
        assert unimplemented_roles("github") == () and unimplemented_operations("github") == ()
        assert unimplemented_roles("gitlab") == () and unimplemented_operations("gitlab") == ()
        gitea = capabilities_for("gitea")
        assert gitea is not None and gitea["review_threads"] is Capability.UNSUPPORTED
        assert unimplemented_roles("gitea") == ()

    def test_each_kinds_sandbox_token_variables(self) -> None:
        assert sandbox_token_envs("github") == ("GH_TOKEN", "GITHUB_TOKEN")
        assert sandbox_token_envs("gitlab") == ("GITLAB_TOKEN",)
        assert sandbox_token_envs("gitea") == ("GITEA_TOKEN",)


class TestTheDaemonsBox:
    def test_the_daemon_box_follows_the_configured_forge(
        self, fake_sbx: FakeSbx, tmp_path: str
    ) -> None:
        config = Config.model_validate(
            {
                "home": f"{tmp_path}/home",
                "vcs": {"kind": "gitlab", "api_url": "https://gl.example/api/v4"},
                "github": {"repo": "acme/widgets"},
            }
        )
        box = DaemonGithub(
            config, SbxCLI(binary=str(fake_sbx.binary)), EventBus(), worker_python="python"
        )
        assert box.kind == "gitlab"
        ops = box.backend(StubWorkerClient({}))  # type: ignore[arg-type]
        assert isinstance(ops, GitlabOps)
        assert ops.transport is not None and ops.transport.api_url == "https://gl.example/api/v4"

    def test_a_repository_on_its_own_forge(self, fake_sbx: FakeSbx, tmp_path: str) -> None:
        config = Config.model_validate(
            {
                "home": f"{tmp_path}/home",
                "vcs": {"api_url": "https://gl.example/api/v4"},
                "github": {"repos": [{"repo": "o/a"}, {"repo": "acme/widgets", "kind": "gitlab"}]},
            }
        )
        cli = SbxCLI(binary=str(fake_sbx.binary))
        assert DaemonGithub(config, cli, EventBus(), worker_python="python").kind == "github"
        scoped = DaemonGithub(config, cli, EventBus(), worker_python="python", repo="acme/widgets")
        assert scoped.kind == "gitlab"
