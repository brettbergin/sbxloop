"""Provisioner tests: specs, token split, policy, secrets, rollback."""

from pathlib import Path

import pytest

from sdxloop.config import Config
from sdxloop.errors import ProvisionError
from sdxloop.events import Event, EventBus
from sdxloop.sbx.cli import SbxCLI
from sdxloop.sbx.provision import Provisioner, sandbox_name
from tests.conftest import FakeSbx

TOKENS = {"COPILOT_GITHUB_TOKEN": "github_pat_copilot", "GH_TOKEN": "github_pat_user"}


def make_provisioner(
    fake_sbx: FakeSbx,
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    config: Config | None = None,
    bus: EventBus | None = None,
) -> Provisioner:
    config = config or Config.model_validate({"state_dir": str(tmp_path / "state")})
    return Provisioner(
        SbxCLI(binary=str(fake_sbx.binary)),
        config,
        bus=bus,
        env=TOKENS if env is None else env,
    )


class TestSpecs:
    def test_build_specs_roles_and_domains(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path)
        agent, github = provisioner.build_specs("r1", tmp_path)
        assert agent.name == "sdxloop-r1-agent"
        assert github.name == "sdxloop-r1-github"
        assert "api.githubcopilot.com" in agent.policy_allows
        assert "uploads.github.com" in github.policy_allows
        assert {s.kind for s in agent.secrets} == {"custom"}
        assert [s.service for s in github.secrets] == ["github"]
        # the copilot token must be bound to both the copilot API and the
        # token-exchange host
        assert {s.host for s in agent.secrets} == {"api.githubcopilot.com", "api.github.com"}
        assert all(s.env == "COPILOT_GITHUB_TOKEN" for s in agent.secrets)

    def test_extra_allow_domains_added(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        config = Config.model_validate(
            {"sandbox": {"extra_allow_domains": ["internal.example.com"]}}
        )
        provisioner = make_provisioner(fake_sbx, tmp_path, config=config)
        agent, github = provisioner.build_specs("r1", tmp_path)
        assert "internal.example.com" in agent.policy_allows
        assert "internal.example.com" in github.policy_allows


class TestTokens:
    def test_missing_copilot_token(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path, env={"GH_TOKEN": "x"})
        with pytest.raises(ProvisionError, match="COPILOT_GITHUB_TOKEN is not set"):
            provisioner.ensure_pair("r1")

    def test_missing_gh_token(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path, env={"COPILOT_GITHUB_TOKEN": "x"})
        with pytest.raises(ProvisionError, match="GH_TOKEN, GITHUB_TOKEN"):
            provisioner.ensure_pair("r1")

    def test_github_token_fallback(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(
            fake_sbx, tmp_path, env={"COPILOT_GITHUB_TOKEN": "a", "GITHUB_TOKEN": "b"}
        )
        assert provisioner.gh_token() == "b"

    def test_no_sandbox_created_when_tokens_missing(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path, env={})
        with pytest.raises(ProvisionError):
            provisioner.ensure_pair("r1")
        assert fake_sbx.invocations("create") == []


class TestEnsurePair:
    def test_full_provision_proxy_strategy(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, bus=bus)

        pair = provisioner.ensure_pair("r1")
        try:
            assert pair.agent.name == sandbox_name("r1", "agent")
            assert pair.github.name == sandbox_name("r1", "github")

            # both sandboxes exist and are running
            assert fake_sbx.meta(pair.agent.name)["status"] == "running"
            assert fake_sbx.meta(pair.github.name)["status"] == "running"

            # per-sandbox policy allows applied
            policies = fake_sbx.policies()
            assert [
                "allow",
                "network",
                "api.githubcopilot.com",
                "--sandbox",
                pair.agent.name,
            ] in policies
            assert [
                "allow",
                "network",
                "uploads.github.com",
                "--sandbox",
                pair.github.name,
            ] in policies

            # secrets: custom copilot token on agent (both hosts), github
            # service on github sandbox; values via the right channels
            secrets = fake_sbx.secrets()
            custom = [s for s in secrets if s["args"][0] == "set-custom"]
            service = [s for s in secrets if s["args"][0] == "set"]
            assert {s["args"][1] for s in custom} == {pair.agent.name}
            assert {s["args"][s["args"].index("--host") + 1] for s in custom} == {
                "api.githubcopilot.com",
                "api.github.com",
            }
            assert all("github_pat_copilot" in s["args"] for s in custom)
            assert service == [
                {
                    "args": ["set", pair.github.name, "github"],
                    "stdin": "github_pat_user",
                    "ts": service[0]["ts"],
                }
            ]

            # worker dirs created in both sandboxes
            for name in (pair.agent.name, pair.github.name):
                fs = fake_sbx.sandbox_fs(name)
                assert (fs / "home/agent/.sdxloop/jobs").is_dir()
                assert (fs / "home/agent/.sdxloop/results").is_dir()
                assert (fs / "home/agent/.sdxloop/events").is_dir()

            # workspace created under state dir
            assert (tmp_path / "state/runs/r1/workspace").is_dir()

            # events emitted
            types = [e.type for e in events]
            assert types.count("sandbox.provision_start") == 2
            assert types.count("sandbox.ready") == 2
        finally:
            pair.cleanup()

    def test_plain_env_strategy_writes_env_file(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        config = Config.model_validate(
            {"secret_strategy": "plain-env", "state_dir": str(tmp_path / "state")}
        )
        provisioner = make_provisioner(fake_sbx, tmp_path, config=config)
        pair = provisioner.ensure_pair("r1")
        try:
            agent_env = (
                fake_sbx.sandbox_fs(pair.agent.name) / "home/agent/.sdxloop/env.sh"
            ).read_text()
            github_env = (
                fake_sbx.sandbox_fs(pair.github.name) / "home/agent/.sdxloop/env.sh"
            ).read_text()
            assert "export COPILOT_GITHUB_TOKEN=github_pat_copilot" in agent_env
            assert "GH_TOKEN" not in agent_env
            assert "export GH_TOKEN=github_pat_user" in github_env
            assert "export GITHUB_TOKEN=github_pat_user" in github_env
            assert "COPILOT_GITHUB_TOKEN" not in github_env
            # no sbx secret invocations under plain-env
            assert fake_sbx.secrets() == []
        finally:
            pair.cleanup()

    def test_keep_sandboxes_from_config(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        config = Config.model_validate(
            {"keep_sandboxes": True, "state_dir": str(tmp_path / "state")}
        )
        provisioner = make_provisioner(fake_sbx, tmp_path, config=config)
        pair = provisioner.ensure_pair("r1")
        assert pair.keep is True
        pair.cleanup()

    def test_post_create_hook_runs_per_sandbox(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        seen: list[tuple[str, str]] = []
        provisioner = make_provisioner(fake_sbx, tmp_path)
        provisioner.post_create = lambda sandbox, role: seen.append((sandbox.name, role))
        pair = provisioner.ensure_pair("r1")
        pair.cleanup()
        assert seen == [
            ("sdxloop-r1-agent", "agent"),
            ("sdxloop-r1-github", "github"),
        ]

    def test_rollback_on_secret_failure(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path)
        # the github sandbox's secret application fails after both creates
        fake_sbx.fail_next("secret set sdxloop-r1-github", returncode=1, stderr="keychain locked")
        with pytest.raises(ProvisionError, match="provisioning run r1 failed"):
            provisioner.ensure_pair("r1")
        # everything created so far was rolled back
        assert not (fake_sbx.state / "sandboxes" / "sdxloop-r1-agent").exists()
        assert not (fake_sbx.state / "sandboxes" / "sdxloop-r1-github").exists()

    def test_explicit_workspace_wins(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path)
        workspace = tmp_path / "custom-ws"
        pair = provisioner.ensure_pair("r1", workspace=workspace)
        try:
            assert workspace.is_dir()
            assert fake_sbx.meta(pair.agent.name)["workspace"] == str(workspace.resolve())
        finally:
            pair.cleanup()


class TestSecretIdempotency:
    def secret_state(self, fake_sbx: FakeSbx) -> dict[str, dict[str, str]]:
        import json

        path = fake_sbx.state / "secrets-state.json"
        return json.loads(path.read_text()) if path.is_file() else {"service": {}, "custom": {}}

    def test_reprovision_replaces_existing_secrets(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        """Second run: custom secrets are keyed globally by host+env, so a
        naive set fails with 'secret exists' — the provisioner must remove
        and re-set instead of dying (the field-reported bug)."""
        provisioner = make_provisioner(fake_sbx, tmp_path)
        provisioner.ensure_pair("r1").cleanup()
        pair = provisioner.ensure_pair("r2")  # must not raise
        try:
            state = self.secret_state(fake_sbx)
            # replaced, not duplicated: still one entry per host+env
            assert set(state["custom"]) == {
                "api.githubcopilot.com|COPILOT_GITHUB_TOKEN",
                "api.github.com|COPILOT_GITHUB_TOKEN",
            }
            # rm invocations happened for the collisions
            rms = [s["args"] for s in fake_sbx.secrets() if s["args"][0] == "rm"]
            assert any("--host" in a for a in rms)
        finally:
            pair.cleanup()

    def test_resume_same_run_id_replaces_service_secret(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        """resume() reuses run ids -> same sandbox names -> the github
        service secret collides too; it must be replaced with the current
        token value."""
        provisioner = make_provisioner(fake_sbx, tmp_path)
        provisioner.ensure_pair("r1").cleanup()
        rotated = dict(TOKENS, GH_TOKEN="github_pat_rotated")
        provisioner2 = make_provisioner(fake_sbx, tmp_path, env=rotated)
        pair = provisioner2.ensure_pair("r1")
        try:
            state = self.secret_state(fake_sbx)
            assert state["service"]["sdxloop-r1-github|github"] == "github_pat_rotated"
        finally:
            pair.cleanup()

    def test_unremovable_secret_keeps_existing_value_with_warning(
        self, fake_sbx: FakeSbx, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path)
        provisioner.ensure_pair("r1").cleanup()
        # every rm is rejected (simulating an sbx build without custom rm)
        fake_sbx.script("secret rm", returncode=1, stderr="unknown command")
        import logging

        with caplog.at_level(logging.WARNING):
            pair = provisioner.ensure_pair("r2")  # still must not raise
        pair.cleanup()
        assert any("could not be removed" in r.message for r in caplog.records)

    def test_non_exists_secret_error_still_raises(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path)
        fake_sbx.script("secret set-custom", returncode=1, stderr="keychain locked")
        with pytest.raises(ProvisionError):
            provisioner.ensure_pair("r1")
