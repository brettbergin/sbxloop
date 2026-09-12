"""The github-role box for a repository on another forge (#1017): the
token from the forge's own variable, no sbx service secret, the forge's
host on the allowlist, and the token delivered under the forge's name."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop.config import Config
from sbxloop.errors import ProvisionError
from sbxloop.events import EventBus
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import GhPat, Provisioner, github_policy_allows
from tests.conftest import FakeSbx

GITLAB = {
    "vcs": {"kind": "gitlab", "api_url": "https://gitlab.example.com/api/v4"},
    "github": {"repo": "acme/widgets"},
}


@pytest.fixture(autouse=True)
def _no_ambient_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN", "GITLAB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def make(fake_sbx: FakeSbx, tmp_path: Path, env: dict[str, str], **doc: object) -> Provisioner:
    config = Config.model_validate({"home": str(tmp_path / "state"), **GITLAB, **doc})
    return Provisioner(SbxCLI(binary=str(fake_sbx.binary)), config, bus=EventBus(), env=env)


class TestCredential:
    def test_the_token_comes_from_the_forges_variable(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        provisioner = make(
            fake_sbx, tmp_path, {"GITLAB_TOKEN": "glpat-x", "COPILOT_GITHUB_TOKEN": "c"}
        )
        cred = provisioner.gh_credential("acme/widgets")
        assert isinstance(cred, GhPat) and cred.token() == "glpat-x"

    def test_a_missing_token_is_named_before_any_sandbox(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        provisioner = make(fake_sbx, tmp_path, {"GH_TOKEN": "ghp", "COPILOT_GITHUB_TOKEN": "c"})
        with pytest.raises(ProvisionError, match="GITLAB_TOKEN is not set"):
            provisioner.ensure_github_only("sbxloop-daemon-github", tmp_path / "ws")
        assert fake_sbx.invocations("create") == []

    def test_a_configured_name_wins(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make(
            fake_sbx,
            tmp_path,
            {"GL_MAIN": "glpat-y"},
            vcs={**GITLAB["vcs"], "token_env": "GL_MAIN"},
        )
        assert provisioner.gh_credential("acme/widgets").token() == "glpat-y"


class TestSpec:
    def test_the_box_carries_no_github_secret_and_the_forges_host(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        provisioner = make(
            fake_sbx, tmp_path, {"GITLAB_TOKEN": "glpat-x", "COPILOT_GITHUB_TOKEN": "c"}
        )
        _agent, github = provisioner.build_specs("r1", tmp_path)
        assert github.secrets == []
        assert github.forge_token_envs == ["GITLAB_TOKEN"]
        assert "gitlab.example.com" in github.policy_allows
        assert not any("github.com" in host for host in github.policy_allows)
        assert "GH_HOST" not in github.persistent_env
        assert github.persistent_env["GH_REPO"] == "acme/widgets"
        # The daemon's long-lived box is the same spec.
        daemon = provisioner.github_only_spec("d", tmp_path, "acme/widgets")
        assert daemon.secrets == [] and daemon.forge_token_envs == ["GITLAB_TOKEN"]

    def test_the_allowlist_without_an_api_root_is_empty_not_githubs(self) -> None:
        config = Config.model_validate({"vcs": {"kind": "gitlab"}, "github": {"repo": "a/b"}})
        assert github_policy_allows(config, "gitlab") == []
        assert "api.github.com" in github_policy_allows(config, "github")

    def test_a_github_repository_is_unchanged(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        config = Config.model_validate(
            {"home": str(tmp_path / "state"), "github": {"repo": "acme/widgets"}}
        )
        provisioner = Provisioner(
            SbxCLI(binary=str(fake_sbx.binary)),
            config,
            env={"GH_TOKEN": "ghp", "COPILOT_GITHUB_TOKEN": "c"},
        )
        _, github = provisioner.build_specs("r1", tmp_path)
        assert [s.service for s in github.secrets] == ["github"]
        assert github.forge_token_envs == ["GH_TOKEN", "GITHUB_TOKEN"]


class TestDelivery:
    def test_the_token_rides_the_env_file_under_the_forges_name(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        bus = EventBus()
        seen: list[str] = []
        bus.subscribe(lambda event: seen.append(event.type))
        config = Config.model_validate({"home": str(tmp_path / "state"), **GITLAB})
        provisioner = Provisioner(
            SbxCLI(binary=str(fake_sbx.binary)),
            config,
            bus=bus,
            env={"GITLAB_TOKEN": "glpat-x", "COPILOT_GITHUB_TOKEN": "c"},
        )
        assert provisioner._env_file_reason("github", GhPat("glpat-x"), kind="gitlab") == "forge"
        sandbox = provisioner.ensure_github_only("sbxloop-daemon-github", tmp_path / "ws")
        try:
            assert "sandbox.forge_token" in seen
            # No sbx secret was registered for the box: the token rode the
            # env-file road, under the forge's variable.
            assert not any("sbxloop-daemon-github" in s for s in fake_sbx.secrets())
            env_file = fake_sbx.sandbox_fs(sandbox.name) / "home/agent/.sbxloop/env.sh"
            if env_file.is_file():
                text = env_file.read_text()
                assert "GITLAB_TOKEN=" in text and "GH_TOKEN=" not in text
        finally:
            sandbox.rm()
