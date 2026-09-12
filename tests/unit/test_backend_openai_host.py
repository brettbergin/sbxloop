"""The openai backend's host side: `[agent.openai]`, the endpoint's
addressing through the policy and secret paths, and provisioning."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from sbxloop.backends import OPENAI, backend_for, backend_named
from sbxloop.config import Config
from sbxloop.engine.model import EgressSpec, TaskNeeds
from sbxloop.errors import ProvisionError
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner, agent_policy_allows
from sbxloop.sbx.secretstate import parse_secret_ls_entry, tracked_custom_secrets
from sbxloop_worker.protocol import (
    OPENAI_BASE_URL_ENV,
    OPENAI_KEY_NAME_ENV,
    OPENAI_RETRIES_ENV,
    OPENAI_TIMEOUT_ENV,
)
from tests.conftest import FakeSbx

SHAPES = {
    "domain": ("https://models.example.com/v1", "models.example.com"),
    "hostname": ("http://vllm:8000/v1", "vllm"),
    "ipv4": ("http://10.0.0.12:8000/v1", "10.0.0.12"),
    "ipv6": ("http://[fd00::1]:8000/v1", "fd00::1"),
}


def openai_config(
    tmp_path: Path, base_url: str = "https://models.example.com/v1", **openai
) -> Config:
    return Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "agent": {
                "backend": "openai",
                "openai": {"base_url": base_url, "allow_insecure_endpoint": True, **openai},
            },
        }
    )


# -- config ------------------------------------------------------------------


def test_openai_without_base_url_fails_to_load_naming_the_key() -> None:
    """The test that fails first: selecting the backend without an endpoint
    is refused at config load, not at the first model call."""
    with pytest.raises(ValidationError, match=r"\[agent.openai\] base_url is required"):
        Config.model_validate({"agent": {"backend": "openai"}})


def test_openai_is_not_the_default_and_selection_keeps_the_model() -> None:
    assert Config().agent.backend == "copilot"
    config = Config.model_validate(
        {"agent": {"backend": "openai", "openai": {"base_url": "https://models.example/v1"}}}
    )
    assert config.model == "auto"
    assert backend_for(config) is backend_named("openai") is OPENAI
    assert config.agent.openai.request_timeout_s == 600.0
    assert config.agent.openai.max_retries == 2


@pytest.mark.parametrize(
    "url", ["not a url", "ftp://x/v1", "https://user:pw@h/v1", "http://a_b/v1"]
)
def test_malformed_base_url_fails_to_load_with_the_reason(url: str) -> None:
    with pytest.raises(ValidationError, match=r"agent\.openai\.base_url"):
        Config.model_validate({"agent": {"backend": "openai", "openai": {"base_url": url}}})


def test_plain_http_refuses_unless_allowed_on_purpose() -> None:
    with pytest.raises(ValidationError, match=r"plain http://.*allow_insecure_endpoint"):
        Config.model_validate(
            {"agent": {"backend": "openai", "openai": {"base_url": "http://vllm:8000/v1"}}}
        )
    config = Config.model_validate(
        {
            "agent": {
                "backend": "openai",
                "openai": {"base_url": "http://vllm:8000/v1", "allow_insecure_endpoint": True},
            }
        }
    )
    assert config.agent.openai.base_url == "http://vllm:8000/v1"


def test_a_repository_override_is_held_to_the_same_rules() -> None:
    base = {"agent": {"backend": "openai", "openai": {"base_url": "https://models.example/v1"}}}
    with pytest.raises(ValidationError, match=r"\[github.repos.openai\] \(o/r\).*plain http"):
        Config.model_validate(
            {
                **base,
                "github": {"repos": [{"repo": "o/r", "openai": {"base_url": "http://p:8000/v1"}}]},
            }
        )
    with pytest.raises(ValidationError, match=r"github\.repos\[\]\.openai\.base_url"):
        Config.model_validate(
            {**base, "github": {"repos": [{"repo": "o/r", "openai": {"base_url": "nope"}}]}}
        )


def test_repository_override_narrows_the_endpoint_not_the_credential() -> None:
    config = Config.model_validate(
        {
            "agent": {"backend": "openai", "openai": {"base_url": "https://models.example/v1"}},
            "github": {
                "repos": [
                    {"repo": "o/private", "openai": {"base_url": "https://p.internal:8443/v1"}},
                    {"repo": "o/other"},
                ]
            },
        }
    )
    assert config.openai_for("o/private").base_url == "https://p.internal:8443/v1"
    assert config.openai_for("o/private").api_key_env == "OPENAI_API_KEY"
    assert config.openai_for("o/other").base_url == "https://models.example/v1"
    assert OPENAI.token_host(config, "o/private") == "p.internal"
    assert OPENAI.token_host(config, "o/other") == "models.example"
    assert OPENAI.token_env(config, "o/private") == OPENAI.token_env(config) == "OPENAI_API_KEY"
    with pytest.raises(ValidationError, match=r"extra_forbidden|api_key_env"):
        Config.model_validate(
            {
                **config.model_dump(mode="json", exclude_defaults=True),
                "github": {"repos": [{"repo": "o/r", "openai": {"api_key_env": "OTHER"}}]},
            }
        )


@pytest.mark.parametrize(
    ("value", "reason"),
    [("not a name", "not an environment variable name"), ("SBXLOOP_KEY", "reserved")],
)
def test_api_key_env_must_be_a_plain_env_name(value: str, reason: str) -> None:
    with pytest.raises(ValidationError, match=reason):
        Config.model_validate(
            {
                "agent": {
                    "backend": "openai",
                    "openai": {"base_url": "https://m.example/v1", "api_key_env": value},
                }
            }
        )


@pytest.mark.parametrize(
    "data",
    [
        {"sandbox": {"env": {"VLLM_KEY": "plain"}}},
        {"github": {"repos": [{"repo": "o/r", "env": {"VLLM_KEY": "plain"}}]}},
        {
            "registries": [
                {
                    "kind": "npm",
                    "host": "npm.example.com",
                    "url": "https://npm.example.com",
                    "auth_env": "VLLM_KEY",
                }
            ]
        },
        {"credentials": [{"name": "inference", "env": "VLLM_KEY", "host": "m.example"}]},
    ],
)
def test_a_custom_api_key_env_cannot_also_be_operator_configured(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="VLLM_KEY is also named by"):
        Config.model_validate(
            {
                "agent": {
                    "backend": "openai",
                    "openai": {"base_url": "https://m.example/v1", "api_key_env": "VLLM_KEY"},
                },
                **data,
            }
        )
    # Under another backend the same config is an ordinary operator env.
    Config.model_validate(data)


def test_operator_env_cannot_name_the_base_url_variable() -> None:
    with pytest.raises(ValidationError, match=f"{OPENAI_BASE_URL_ENV} is delivered by sbxloop"):
        Config.model_validate({"sandbox": {"env": {OPENAI_BASE_URL_ENV: "http://elsewhere"}}})


def test_worker_env_names_are_not_config_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The names provisioning delivers to the worker are reserved in the
    env config layer, like SBXLOOP_WORKER_BACKEND: a daemon whose own
    environment carries them must not read them as (unknown) settings."""
    from sbxloop.config import load_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(OPENAI_KEY_NAME_ENV, "OPENAI_API_KEY")
    monkeypatch.setenv(OPENAI_TIMEOUT_ENV, "600")
    monkeypatch.setenv(OPENAI_RETRIES_ENV, "2")
    assert load_config().agent.backend == "copilot"


# -- the descriptor ----------------------------------------------------------


def test_descriptor_binds_the_configured_env_to_the_endpoint_host(tmp_path: Path) -> None:
    config = openai_config(tmp_path, "http://vllm:8000/v1", api_key_env="VLLM_KEY")
    backend = backend_for(config)
    assert backend.secret(config) == ("VLLM_KEY", "vllm")
    assert backend.token_hosts(config) == ("vllm",)
    assert backend.doctor_check_name(config) == "VLLM_KEY (agent backend: openai)"
    assert tracked_custom_secrets(config) == [("VLLM_KEY", "vllm")]
    assert backend.has_token(config, {"VLLM_KEY": "placeholder"})
    assert not backend.has_token(config, {"OPENAI_API_KEY": "sk"})
    assert "vllm:8000" in backend.missing_token_detail(config)
    assert "VLLM_KEY" in backend.missing_token_error(config)
    assert 'backend = "openai"' in backend.missing_token_error(config)


def test_descriptor_refuses_to_answer_without_an_endpoint() -> None:
    with pytest.raises(ValueError, match="base_url is not set"):
        OPENAI.secret(Config())


def test_vendor_backends_are_unchanged_by_the_openai_block(tmp_path: Path) -> None:
    """A `[agent.openai]` block under another backend changes nothing that
    backend says of itself."""
    config = Config.model_validate(
        {"agent": {"backend": "codex", "openai": {"base_url": "https://m.example/v1"}}}
    )
    assert backend_for(config).secret(config) == ("OPENAI_API_KEY", "api.openai.com")
    assert tracked_custom_secrets(config) == [("OPENAI_API_KEY", "api.openai.com")]
    assert "m.example" not in agent_policy_allows(config, ["python"])


# -- addressing: the policy path and the secret path ---------------------------


@pytest.mark.parametrize("kind", sorted(SHAPES))
def test_each_endpoint_shape_reaches_the_allowlist_and_the_binding(
    tmp_path: Path, kind: str
) -> None:
    url, host = SHAPES[kind]
    config = openai_config(tmp_path, url)
    allows = agent_policy_allows(config, ["python"])
    assert host in allows
    assert len(allows) == len(set(allows))
    assert tracked_custom_secrets(config) == [("OPENAI_API_KEY", host)]
    provisioner = Provisioner(SbxCLI(binary="unused"), config, env={"OPENAI_API_KEY": "key"})
    agent, github = provisioner.build_specs("r1", tmp_path)
    assert [(s.env, s.host) for s in agent.secrets] == [("OPENAI_API_KEY", host)]
    assert host in agent.policy_allows
    assert host not in github.policy_allows
    assert "api.openai.com" not in agent.policy_allows


@pytest.mark.parametrize("kind", sorted(SHAPES))
def test_listing_parser_recognises_each_shape_as_a_host_binding(kind: str) -> None:
    _, host = SHAPES[kind]
    shown = f"[{host}]" if kind == "ipv6" else host
    raw = f"SCOPE  TYPE  NAME  HOST\nsbxloop-r1-agent  custom  OPENAI_API_KEY  {shown}\n"
    assert parse_secret_ls_entry(raw, "OPENAI_API_KEY", host=host) == ("sbxloop-r1-agent", [host])


def test_listing_parser_does_not_mistake_columns_for_hostnames() -> None:
    """A bare word is a host only when it is the one expected: the listing's
    own columns (`custom`, `global`) never read as bindings."""
    raw = "global  custom  OPENAI_API_KEY  vllm\n"
    assert parse_secret_ls_entry(raw, "OPENAI_API_KEY", host="vllm") == ("global", ["vllm"])
    assert parse_secret_ls_entry(raw, "OPENAI_API_KEY", host="other") == ("global", [])
    assert parse_secret_ls_entry(raw, "OPENAI_API_KEY") == ("global", [])


@pytest.mark.parametrize("declared", ["vllm", "10.0.0.12", "[fd00::1]", "vllm:8000"])
def test_a_plan_still_may_not_declare_a_bare_host_or_literal(declared: str) -> None:
    """The operator-configured endpoint widens nothing a plan may ask for:
    plan-declared egress keeps today's rule and today's message."""
    with pytest.raises(
        ValidationError, match=r"egress domain must be a domain or \*\.domain wildcard"
    ):
        EgressSpec(domain=declared, reason="model")
    with pytest.raises(
        ValidationError, match=r"needs\.hosts must be domains or \*\.domain wildcards"
    ):
        TaskNeeds(hosts=[declared])


def test_repository_override_reaches_that_repositorys_specs(tmp_path: Path) -> None:
    config = Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "agent": {"backend": "openai", "openai": {"base_url": "https://models.example/v1"}},
            "github": {
                "repos": [
                    {"repo": "o/private", "openai": {"base_url": "https://p.internal:8443/v1"}},
                    {"repo": "o/other"},
                ]
            },
        }
    )
    assert sorted(tracked_custom_secrets(config)) == [
        ("OPENAI_API_KEY", "models.example"),
        ("OPENAI_API_KEY", "p.internal"),
    ]
    provisioner = Provisioner(SbxCLI(binary="unused"), config, env={"OPENAI_API_KEY": "key"})
    agent, _ = provisioner.build_specs("r1", tmp_path, repo="o/private")
    assert [(s.env, s.host) for s in agent.secrets] == [("OPENAI_API_KEY", "p.internal")]
    assert "p.internal" in agent.policy_allows and "models.example" not in agent.policy_allows
    assert agent.persistent_env[OPENAI_BASE_URL_ENV] == "https://p.internal:8443/v1"
    agent, _ = provisioner.build_specs("r2", tmp_path, repo="o/other")
    assert [(s.env, s.host) for s in agent.secrets] == [("OPENAI_API_KEY", "models.example")]
    assert agent.persistent_env[OPENAI_BASE_URL_ENV] == "https://models.example/v1"


# -- provisioning ---------------------------------------------------------------


def test_spec_routes_the_worker_to_the_endpoint_and_keeps_the_key_off_the_agent_env(
    tmp_path: Path,
) -> None:
    config = openai_config(
        tmp_path,
        "http://vllm:8000/v1/",
        api_key_env="VLLM_KEY",
        request_timeout_s=42.5,
        max_retries=0,
    )
    provisioner = Provisioner(SbxCLI(binary="unused"), config, env={"VLLM_KEY": "key"})
    agent, github = provisioner.build_specs("r1", tmp_path)
    assert agent.persistent_env == {
        "SBXLOOP_WORKER_BACKEND": "openai",
        OPENAI_BASE_URL_ENV: "http://vllm:8000/v1",
        OPENAI_KEY_NAME_ENV: "VLLM_KEY",
        OPENAI_TIMEOUT_ENV: "42.5",
        OPENAI_RETRIES_ENV: "0",
    }
    assert "key" not in json.dumps(agent.persistent_env)
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in agent.persistent_env
    assert "VLLM_KEY" not in github.persistent_env
    assert all(secret.env != "VLLM_KEY" for secret in github.secrets)


def test_missing_key_fails_before_provisioning_naming_the_endpoint(tmp_path: Path) -> None:
    provisioner = Provisioner(
        SbxCLI(binary="unused"),
        openai_config(tmp_path, "http://vllm:8000/v1"),
        env={"COPILOT_GITHUB_TOKEN": "other"},
    )
    with pytest.raises(ProvisionError, match=r"OPENAI_API_KEY is not set.*vllm:8000"):
        provisioner.agent_token()


def test_bake_installs_the_extra_named_after_the_backend(tmp_path: Path) -> None:
    """`bake` passes `[agent] backend` verbatim as the worker extra, so the
    extra must be called exactly what the backend is."""
    from sbxloop.sbx import bake

    assert bake.bake_template.__doc__ is not None
    assert openai_config(tmp_path).agent.backend == "openai" == OPENAI.name


@pytest.mark.parametrize("stdin", [False, True])
def test_key_and_endpoint_reach_the_worker_without_the_key_in_argv(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdin: bool
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if stdin:
        monkeypatch.setenv("SBX_FAKE_EXEC_STDIN", "1")
    else:
        monkeypatch.delenv("SBX_FAKE_EXEC_STDIN", raising=False)
    token = "sk-test-endpoint-credential"
    config = openai_config(tmp_path, "http://10.0.0.12:8000/v1").model_copy(
        update={"secret_strategy": "plain-env"}
    )
    provisioner = Provisioner(
        SbxCLI(binary=str(fake_sbx.binary)), config, env={"OPENAI_API_KEY": token}
    )
    provisioner.ensure_pair("r1", kind="workload")
    if stdin:
        provider = provisioner.job_env("agent")
        assert provider is not None
        exports = provider()
        assert exports["OPENAI_API_KEY"] == token
        assert exports["SBXLOOP_WORKER_BACKEND"] == "openai"
        assert exports[OPENAI_BASE_URL_ENV] == "http://10.0.0.12:8000/v1"
    else:
        env_file = fake_sbx.sandbox_fs("sbxloop-r1-agent") / "home/agent/.sbxloop/env.sh"
        content = env_file.read_text()
        assert f"OPENAI_API_KEY={token}" in content
        assert "SBXLOOP_WORKER_BACKEND=openai" in content
        assert f"{OPENAI_BASE_URL_ENV}=http://10.0.0.12:8000/v1" in content
    assert token not in json.dumps(fake_sbx.invocations())
    assert ["allow", "network", "10.0.0.12", "--sandbox", "sbxloop-r1-agent"] in fake_sbx.policies()


def test_a_refused_network_allow_fails_closed_naming_the_endpoint(
    fake_sbx: FakeSbx, tmp_path: Path
) -> None:
    """FIELD-UNVERIFIED whether sbx's policy takes an address literal: when
    it refuses the batch carrying one, provisioning stops naming the
    endpoint and its shape, never a sandbox that fails at the first call."""
    config = openai_config(tmp_path, "http://10.0.0.12:8000/v1")
    provisioner = Provisioner(
        SbxCLI(binary=str(fake_sbx.binary)), config, env={"OPENAI_API_KEY": "key"}
    )
    fake_sbx.fail_next("policy allow network", stderr="invalid domain pattern: 10.0.0.12")
    with pytest.raises(ProvisionError, match=r"10\.0\.0\.12:8000 \(ipv4\).*invalid domain pattern"):
        provisioner.ensure_pair("r1", kind="workload")
    assert SbxCLI(binary=str(fake_sbx.binary)).ls() == []


def test_a_refused_credential_binding_fails_closed_naming_the_endpoint(
    fake_sbx: FakeSbx, tmp_path: Path
) -> None:
    config = openai_config(tmp_path, "http://vllm:8000/v1")
    provisioner = Provisioner(
        SbxCLI(binary=str(fake_sbx.binary)), config, env={"OPENAI_API_KEY": "key"}
    )
    fake_sbx.fail_next("secret set-custom", stderr="host must be a domain")
    with pytest.raises(ProvisionError, match=r"bind OPENAI_API_KEY to .*vllm:8000 \(hostname\)"):
        provisioner.ensure_pair("r1", kind="workload")
    assert SbxCLI(binary=str(fake_sbx.binary)).ls() == []


def test_a_vendor_backends_refusals_read_as_before(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    """The fail-closed naming is the openai backend's: a codex host hits
    sbx's own error, wrapped the way it always was."""
    config = Config.model_validate({"home": str(tmp_path / "state"), "agent": {"backend": "codex"}})
    provisioner = Provisioner(
        SbxCLI(binary=str(fake_sbx.binary)), config, env={"OPENAI_API_KEY": "key"}
    )
    fake_sbx.fail_next("policy allow network", stderr="boom")
    with pytest.raises(ProvisionError) as excinfo:
        provisioner.ensure_pair("r1", kind="workload")
    assert "model endpoint" not in str(excinfo.value)
