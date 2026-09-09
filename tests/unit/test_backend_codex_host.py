"""The Codex backend's host configuration, credential and runtime wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sbxloop.backends import backend_for, backend_named
from sbxloop.cli import doctor
from sbxloop.cli.app import app
from sbxloop.config import Config
from sbxloop.daemon.discord_format import agent_model_label
from sbxloop.errors import ProvisionError
from sbxloop.log import redact_text
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner, agent_policy_allows
from sbxloop.sbx.secretstate import tracked_custom_secrets
from tests.conftest import FakeSbx


def codex_config(tmp_path: Path) -> Config:
    return Config.model_validate({"home": str(tmp_path / "state"), "agent": {"backend": "codex"}})


@pytest.fixture
def configured_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", "300")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / "sbxloop.toml").write_text('[agent]\nbackend = "codex"\n')
    return tmp_path


def test_codex_can_be_selected_without_changing_the_default() -> None:
    assert Config().agent.backend == "copilot"
    config = Config.model_validate({"agent": {"backend": "codex"}, "model": "auto"})
    assert config.model == "auto"
    assert backend_for(config) is backend_named("codex")


def test_codex_attribution_preserves_the_backend() -> None:
    assert agent_model_label("codex", "example-model") == "codex · example-model"


def test_codex_credential_registration_follows_the_descriptor(tmp_path: Path) -> None:
    config = codex_config(tmp_path)
    backend = backend_for(config)
    assert backend.secret == ("OPENAI_API_KEY", "api.openai.com")
    assert backend.token_hosts == ("api.openai.com",)
    assert backend.doctor_check_name == "OPENAI_API_KEY (agent backend: codex)"
    assert tracked_custom_secrets(config) == [backend.secret]
    assert backend.has_token({"OPENAI_API_KEY": "sk-test-key"})
    assert not backend.has_token({"COPILOT_GITHUB_TOKEN": "other"})


def test_codex_spec_keeps_the_inference_key_on_the_agent(tmp_path: Path) -> None:
    config = codex_config(tmp_path)
    provisioner = Provisioner(SbxCLI(binary="unused"), config, env={"OPENAI_API_KEY": "key"})
    agent, github = provisioner.build_specs("r1", tmp_path)
    assert [(secret.env, secret.host) for secret in agent.secrets] == [
        ("OPENAI_API_KEY", "api.openai.com")
    ]
    assert "api.openai.com" in agent.policy_allows
    assert "api.anthropic.com" not in agent.policy_allows
    assert len(agent.policy_allows) == len(set(agent.policy_allows))
    assert agent.persistent_env == {"SBXLOOP_WORKER_BACKEND": "codex"}
    assert "api.openai.com" not in github.policy_allows
    assert "OPENAI_API_KEY" not in github.persistent_env
    assert all(secret.env != "OPENAI_API_KEY" for secret in github.secrets)


def test_codex_missing_key_fails_before_provisioning(tmp_path: Path) -> None:
    provisioner = Provisioner(
        SbxCLI(binary="unused"), codex_config(tmp_path), env={"COPILOT_GITHUB_TOKEN": "other"}
    )
    with pytest.raises(ProvisionError, match="OPENAI_API_KEY is not set"):
        provisioner.agent_token()


def test_openai_egress_is_only_added_for_codex(tmp_path: Path) -> None:
    assert "api.openai.com" in agent_policy_allows(codex_config(tmp_path), ["python"])
    assert "api.openai.com" not in agent_policy_allows(Config(), ["python"])


@pytest.mark.parametrize(
    "data",
    [
        {"sandbox": {"env": {"OPENAI_API_KEY": "plain"}}},
        {"github": {"repos": [{"repo": "owner/repo", "env": {"OPENAI_API_KEY": "plain"}}]}},
        {
            "registries": [
                {
                    "kind": "npm",
                    "host": "npm.example.com",
                    "url": "https://npm.example.com",
                    "auth_env": "OPENAI_API_KEY",
                }
            ]
        },
        {"credentials": [{"name": "inference", "env": "OPENAI_API_KEY", "host": "api.openai.com"}]},
    ],
)
def test_openai_key_cannot_be_overridden_by_sandbox_or_registry_config(
    data: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY is delivered by sbxloop itself"):
        Config.model_validate(data)


def test_codex_doctor_checks_its_key_and_inference_host(
    configured_directory: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        doctor, "installed_sdk_permission_kinds", lambda: pytest.fail("Copilot SDK probed")
    )
    checks = doctor.collect_checks(
        {"OPENAI_API_KEY": "key"}, cli=SbxCLI(binary=str(fake_sbx.binary))
    )
    rows = {check.name: check for check in checks}
    assert rows["OPENAI_API_KEY (agent backend: codex)"].ok
    assert "policy: api.openai.com" in rows
    assert not any("copilot" in name.lower() for name in rows)


def test_codex_secret_rotation_selects_the_openai_registration(
    configured_directory: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-rotation-key")
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "another-provider")
    runner = CliRunner()
    listed = runner.invoke(app, ["secrets", "list"])
    assert listed.exit_code == 0, listed.output
    assert "OPENAI_API_KEY" in listed.output and "api.openai.com" in listed.output
    assert "COPILOT_GITHUB_TOKEN" not in listed.output
    rotated = runner.invoke(app, ["secrets", "rotate", "--no-verify"])
    assert rotated.exit_code == 0, rotated.output
    custom = json.loads((fake_sbx.state / "secrets-state.json").read_text())["custom"]
    assert custom["OPENAI_API_KEY"] == {
        "scope": "-g",
        "host": "api.openai.com",
        "value": "sk-test-rotation-key",
    }
    assert "COPILOT_GITHUB_TOKEN" not in custom


@pytest.mark.parametrize("stdin", [False, True])
def test_codex_key_and_selector_reach_the_worker_without_credentials_in_argv(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdin: bool
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if stdin:
        monkeypatch.setenv("SBX_FAKE_EXEC_STDIN", "1")
    else:
        monkeypatch.delenv("SBX_FAKE_EXEC_STDIN", raising=False)
    token = "sk-test-inference-credential"
    config = codex_config(tmp_path).model_copy(update={"secret_strategy": "plain-env"})
    provisioner = Provisioner(
        SbxCLI(binary=str(fake_sbx.binary)), config, env={"OPENAI_API_KEY": token}
    )
    provisioner.ensure_pair("r1", kind="workload")
    if stdin:
        provider = provisioner.job_env("agent")
        assert provider is not None
        exports = provider()
        assert exports["OPENAI_API_KEY"] == token
        assert exports["SBXLOOP_WORKER_BACKEND"] == "codex"
    else:
        env_file = fake_sbx.sandbox_fs("sbxloop-r1-agent") / "home/agent/.sbxloop/env.sh"
        content = env_file.read_text()
        assert f"OPENAI_API_KEY={token}" in content
        assert "SBXLOOP_WORKER_BACKEND=codex" in content
        assert "COPILOT_GITHUB_TOKEN" not in content
    assert token not in json.dumps(fake_sbx.invocations())


@pytest.mark.parametrize("prefix", ["sk-", "sk-proj-", "sk-svcacct-"])
def test_openai_keys_are_redacted_from_unlabelled_host_text(prefix: str) -> None:
    token = prefix + "A1b2C3d4E5f6G7h8I9j0K1l2"
    assert redact_text(f"response included {token} here") == "response included *** here"
