"""The remote API is unreachable from every sandbox by construction (#1041):
no allowlist the provisioner hands `sbx policy allow network` ever names
the host, its loopback, or the address the listener binds — whatever the
backend, the toolchains, the registries or the operator's extras — so the
network policy that fails closed on everything else fails closed on the
daemon's own API too. The live half of the claim (a connect from inside
a real sandbox) is the ``api-host-unreachable`` conformance probe."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest

from sbxloop.config import Config
from sbxloop.sbx.provision import (
    agent_policy_allows,
    github_policy_allows,
    service_policy_allows,
)

LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal", "*"}


def _config(tmp_path: Path, backend: str, bind: str) -> Config:
    return Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "github": {"repo": "o/r"},
            "agent": {
                "backend": backend,
                **(
                    {"openai": {"base_url": "https://llm.corp.example/v1"}}
                    if backend == "openai"
                    else {}
                ),
            },
            "api": {"enabled": True, "bind": bind, "port": 8420},
            "sandbox": {"extra_allow_domains": ["nexus.corp.example"]},
            "registries": [
                {"host": "npm.corp.example", "kind": "npm", "url": "https://npm.corp.example/"}
            ],
            "credentials": [{"name": "svc", "host": "api.corp.example", "env": "SVC_TOKEN"}],
            "policy": {"allow": ["*.corp.example"]},
        }
    )


def _names(domains: list[str], bind: str) -> set[str]:
    hostile = LOOPBACK | {bind}
    found = {d for d in domains if d.lower() in hostile}
    for domain in domains:
        try:
            address = ipaddress.ip_address(domain)
        except ValueError:
            continue
        if address.is_loopback or address.is_private or address.is_unspecified:
            found.add(domain)
    return found


@pytest.mark.parametrize("backend", ["copilot", "claude", "codex", "openai"])
@pytest.mark.parametrize("bind", ["127.0.0.1", "0.0.0.0", "10.0.0.12"])
def test_no_sandbox_allowlist_names_the_host_or_the_listener(
    tmp_path: Path, backend: str, bind: str
) -> None:
    config = _config(tmp_path, backend, bind)
    agent = agent_policy_allows(
        config,
        ["python", "javascript", "go", "rust", "java", "dotnet", "ruby"],
        "o/r",
        extra_domains=["git.corp.example"],
        mcp_roles=None,
    )
    github = github_policy_allows(config)
    service = service_policy_allows(
        list(config.credentials),
        list(config.registries),
        ["javascript"],
        list(config.policy.deny),
    )
    for name, domains in (("agent", agent), ("github", github), ("service", service)):
        assert domains, name
        assert _names(domains, bind) == set(), (name, domains)


def test_the_operators_extras_cannot_name_the_host(tmp_path: Path) -> None:
    """An extra allow domain is a domain: a bare address or a loopback name
    is refused where it is written, never granted to a sandbox."""
    for hostile in ("127.0.0.1", "localhost", "0.0.0.0", "*"):
        with pytest.raises(ValueError):
            Config.model_validate(
                {
                    "home": str(tmp_path / "state"),
                    "github": {"repo": "o/r"},
                    "sandbox": {"extra_allow_domains": [hostile]},
                }
            )
