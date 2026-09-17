"""The backend registry (#1017): a kind becomes a backend object over a
worker client with the right transport, and a kind no backend answers
fails closed by name."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from sbxloop.config import Config
from sbxloop.daemon.github import DaemonGithub, sandbox_name_for
from sbxloop.errors import DaemonError, GithubOpsError
from sbxloop.events import EventBus
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxSpec
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
        with pytest.raises(BackendNotImplemented, match='"gitea"'):
            backend_for("gitea", StubWorkerClient({}), "r1", api_url="https://gt.example/api/v1")  # type: ignore[arg-type]
        assert capabilities_for("gitea") is None and unimplemented_roles("gitea") == ()

    def test_the_registry_reports_what_each_kind_can_do(self) -> None:
        github = capabilities_for("github")
        gitlab = capabilities_for("gitlab")
        assert github is not None and github["merge_queue"] is Capability.SUPPORTED
        assert gitlab is not None and gitlab["review_threads"] is Capability.SUPPORTED
        assert unimplemented_roles("github") == () and unimplemented_operations("github") == ()
        assert unimplemented_roles("gitlab") == () and unimplemented_operations("gitlab") == ()

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
        assert box.name == sandbox_name_for(config.paths, "gitlab")
        assert box.name.startswith("sbxloop-daemon-gitlab-")
        ops = box.backend(StubWorkerClient({}))  # type: ignore[arg-type]
        assert isinstance(ops, GitlabOps)
        assert ops.transport is not None and ops.transport.api_url == "https://gl.example/api/v4"

    @staticmethod
    def _switched_box(
        fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
    ) -> DaemonGithub:
        monkeypatch.setenv("GH_TOKEN", "github_pat_test")
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
        vcs = (
            {"kind": kind}
            if kind == "github"
            else {"kind": kind, "api_url": "https://gl.example/api/v4"}
        )
        config = Config.model_validate(
            {"home": str(tmp_path / "home"), "vcs": vcs, "github": {"repo": "acme/widgets"}}
        )
        return DaemonGithub(
            config,
            SbxCLI(binary=str(fake_sbx.binary)),
            EventBus(),
            worker_python=sys.executable,
            install_workers=False,
        )

    @pytest.mark.parametrize(
        ("configured", "previous"), [("gitlab", "github"), ("github", "gitlab")]
    )
    def test_a_forge_switch_clears_the_previous_forges_box(
        self,
        fake_sbx: FakeSbx,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        configured: str,
        previous: str,
    ) -> None:
        """Switching `[vcs] kind` in either direction leaves this home's box
        under the old forge behind; it used to be cleared only on a switch
        away from GitHub, and a switch back leaked the GitLab box for good
        (prune leaves daemon-owned boxes alone)."""
        box = self._switched_box(fake_sbx, tmp_path, monkeypatch, configured)
        old = sandbox_name_for(box.config.paths, previous)  # type: ignore[arg-type]
        # A later generation of the old box goes too; another home's box stays.
        for name in (old, f"{old}-g2", "sbxloop-daemon-gitlab-0badf00d"):
            box.sbx.create(SandboxSpec(name=name, role="github", workspace=tmp_path))

        box.ops()

        listed = {info.name for info in box.sbx.ls()}
        assert box.name in listed
        assert old not in listed and f"{old}-g2" not in listed
        assert "sbxloop-daemon-gitlab-0badf00d" in listed

    def test_a_wedged_previous_forge_box_does_not_keep_the_new_forge_down(
        self,
        fake_sbx: FakeSbx,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Field failure: after a switch to GitLab, `sbx rm` of the old
        GitHub box timed out on every provision, and because that removal ran
        before the GitLab box was created, polling stayed down for hours. The
        old box has its own name; the new one never waits on it."""
        box = self._switched_box(fake_sbx, tmp_path, monkeypatch, "gitlab")
        old = sandbox_name_for(box.config.paths, "github")
        box.sbx.create(SandboxSpec(name=old, role="github", workspace=tmp_path))
        fake_sbx.fail_next(f"rm --force {old}", stderr="ERROR: context deadline exceeded")

        with caplog.at_level(logging.INFO, logger="sbxloop.daemon.github"):
            box.ops()

        assert box.name in {info.name for info in box.sbx.ls()}
        failed = [
            r
            for r in caplog.records
            if "github_sandbox.previous_forge_remove_failed" in r.getMessage()
        ]
        assert len(failed) == 1 and failed[0].levelno == logging.WARNING
        # Once per daemon process: a re-provision does not pay for it again.
        box.close()
        box.ops()
        assert len([c for c in fake_sbx.invocations("rm") if old in c]) == 1

    def test_a_provisioning_failure_names_the_configured_forge(
        self,
        fake_sbx: FakeSbx,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The report an operator reads under GitLab said "GitHub": the
        DaemonError and the provision_failed hint name the forge the box
        actually serves."""
        box = self._switched_box(fake_sbx, tmp_path, monkeypatch, "gitlab")
        # Every create, not just the first: a refused create is retried once
        # under the next generation of the name (#1165).
        fake_sbx.script("create", returncode=1, stderr="ERROR: failed to run sandbox container")

        with (
            caplog.at_level(logging.ERROR, logger="sbxloop.daemon.github"),
            pytest.raises(DaemonError, match="cannot provision the daemon GitLab sandbox"),
        ):
            box.ops()

        (failed,) = [
            r for r in caplog.records if "github_sandbox.provision_failed" in r.getMessage()
        ]
        assert "GitLab calls" in failed.getMessage()
        assert "GitHub" not in failed.getMessage()

    def test_a_missing_docker_session_names_sbx_login(
        self,
        fake_sbx: FakeSbx,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Field failure: a Docker session that expired made every sbx call
        fail with 401 for two hours, and each report told the operator to
        check the image and the disk."""
        box = self._switched_box(fake_sbx, tmp_path, monkeypatch, "gitlab")
        fake_sbx.fail_next(
            "ls",
            stderr="ERROR: list sandboxes: request failed: 401 Unauthorized: user is not "
            "authenticated to Docker: secret not found\nno valid user session found, "
            "please sign in to Docker to proceed",
        )

        with (
            caplog.at_level(logging.ERROR, logger="sbxloop.daemon.github"),
            pytest.raises(DaemonError, match="not authenticated to Docker"),
        ):
            box.ops()

        (failed,) = [
            r for r in caplog.records if "github_sandbox.provision_failed" in r.getMessage()
        ]
        assert "`sbx login`" in failed.getMessage()
        assert "disk" not in failed.getMessage()

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
