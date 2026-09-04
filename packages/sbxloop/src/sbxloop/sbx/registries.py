"""What each ``[[registries]]`` entry writes into the agent sandbox (#680).

A private registry needs three things before a dependency install can
succeed: the host must be reachable (it joins the sandbox's allowlist in
``sbx.provision.agent_policy_allows``), the ecosystem's client must be told
to use it, and the credential must be where that client looks. This module
answers the second and third per kind, as data the provisioner delivers:

* :func:`plain_env` — non-secret environment (``PIP_INDEX_URL``,
  ``GOPRIVATE``), folded into the agent's persistent env.
* :func:`secret_env` — environment carrying the credential
  (``CARGO_REGISTRIES_<NAME>_TOKEN``, ``BUNDLE_<HOST>``, and the
  ``auth_env`` variable itself), delivered the way ``[sandbox] secret_env``
  is: per-job stdin or the 0600 env file, never an ``sbx`` argument.
* :func:`client_files` — the client files, written with ``sbx cp`` and
  chmod 600. Wherever the ecosystem expands environment variables in its
  config (npm ``${VAR}``, Maven ``${env.VAR}``, NuGet ``%VAR%``) the file
  names the variable and holds no secret; the ``.netrc`` kinds (pypi, go,
  generic) have no such form, so ``~/.netrc`` holds the value at rest.

Per kind:

==========  =================================================================
npm         ``~/.npmrc``: ``@scope:registry=URL`` (or ``registry=URL`` when
            unscoped) and ``//host/path/:_authToken=${AUTH_ENV}``. Read by
            npm, pnpm and yarn classic.
pypi        ``PIP_INDEX_URL`` and ``UV_DEFAULT_INDEX`` set to the URL — the
            registry IS the index (point it at a virtual/group repository
            that proxies PyPI); credential in ``~/.netrc``.
go          ``GOPRIVATE`` names the host (which also disables the checksum
            database and proxy for it); credential in ``~/.netrc`` for the
            git fetch.
cargo       ``~/.cargo/config.toml`` ``[registries.NAME] index = "sparse+URL"``;
            token in ``CARGO_REGISTRIES_<NAME>_TOKEN``.
maven       ``~/.m2/settings.xml``: a ``<mirror>`` of ``*`` (the registry
            stands in for every remote repository — a virtual repository
            that proxies Central) and a ``<server>`` whose password is
            ``${env.AUTH_ENV}``.
nuget       ``~/.nuget/NuGet/NuGet.Config``: a package source plus
            ``<packageSourceCredentials>`` with ``%AUTH_ENV%``; nuget.org
            stays unless the repository's own NuGet.Config clears it.
gem         ``BUNDLE_<HOST>=user:token`` for bundler; with ``url``, also
            ``~/.gemrc`` listing it as the gem source.
generic     host only, plus a ``~/.netrc`` entry when ``auth_env`` is set.
==========  =================================================================
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from html import escape
from typing import NamedTuple
from urllib.parse import urlsplit

from sbxloop.config import RegistryConfig
from sbxloop.sbx.sandbox import SANDBOX_HOME

NPMRC = f"{SANDBOX_HOME}/.npmrc"
NETRC = f"{SANDBOX_HOME}/.netrc"
CARGO_CONFIG = f"{SANDBOX_HOME}/.cargo/config.toml"
MAVEN_SETTINGS = f"{SANDBOX_HOME}/.m2/settings.xml"
NUGET_CONFIG = f"{SANDBOX_HOME}/.nuget/NuGet/NuGet.Config"
GEMRC = f"{SANDBOX_HOME}/.gemrc"


class ClientFile(NamedTuple):
    path: str
    text: str


def domains(registries: Sequence[RegistryConfig]) -> list[str]:
    """The registry hosts, in configuration order, deduped."""
    return list(dict.fromkeys(r.host for r in registries))


def plain_env(registries: Sequence[RegistryConfig]) -> dict[str, str]:
    env: dict[str, str] = {}
    go_hosts = [r.host for r in registries if r.kind == "go"]
    if go_hosts:
        env["GOPRIVATE"] = ",".join(dict.fromkeys(go_hosts))
    for r in registries:
        if r.kind == "pypi":
            assert r.url is not None
            env["PIP_INDEX_URL"] = r.url
            env["UV_DEFAULT_INDEX"] = r.url
    return env


def secret_env(registries: Sequence[RegistryConfig], values: Mapping[str, str]) -> dict[str, str]:
    """The credential-bearing environment, given each ``auth_env``'s value.

    The ``auth_env`` variable itself rides along under its own name — the
    npm/Maven/NuGet client files reference it by name — plus each kind's
    derived variable.
    """
    env: dict[str, str] = {}
    for r in registries:
        if r.auth_env is None:
            continue
        token = values[r.auth_env]
        env[r.auth_env] = token
        if r.kind == "cargo":
            env[_cargo_token_var(r)] = token
        elif r.kind == "gem":
            env[_bundle_var(r.host)] = f"{r.auth_user}:{token}"
    return env


def client_files(
    registries: Sequence[RegistryConfig], values: Mapping[str, str]
) -> list[ClientFile]:
    """The client files, one per path (several npm scopes share one
    ``.npmrc``; every netrc kind shares one ``.netrc``)."""
    files: list[ClientFile] = []
    npm = [r for r in registries if r.kind == "npm"]
    if npm:
        files.append(ClientFile(NPMRC, "".join(_npmrc_lines(r) for r in npm)))
    netrc = [r for r in registries if r.kind in ("pypi", "go", "generic") and r.auth_env]
    if netrc:
        files.append(
            ClientFile(
                NETRC,
                "".join(
                    f"machine {r.host} login {r.auth_user} password {values[r.auth_env]}\n"
                    for r in netrc
                    if r.auth_env is not None
                ),
            )
        )
    cargo = [r for r in registries if r.kind == "cargo"]
    if cargo:
        files.append(ClientFile(CARGO_CONFIG, "".join(_cargo_section(r) for r in cargo)))
    maven = [r for r in registries if r.kind == "maven"]
    if maven:
        files.append(ClientFile(MAVEN_SETTINGS, _maven_settings(maven)))
    nuget = [r for r in registries if r.kind == "nuget"]
    if nuget:
        files.append(ClientFile(NUGET_CONFIG, _nuget_config(nuget)))
    gem = [r for r in registries if r.kind == "gem" and r.url]
    if gem:
        files.append(ClientFile(GEMRC, ":sources:\n" + "".join(f"- {r.url}\n" for r in gem)))
    return files


def _npmrc_lines(r: RegistryConfig) -> str:
    assert r.url is not None
    lines = f"{r.scope}:registry={r.url}\n" if r.scope else f"registry={r.url}\n"
    if r.auth_env:
        # npm keys auth by the registry URL minus its scheme, with a
        # trailing slash, and expands `${VAR}` from the environment.
        parts = urlsplit(r.url)
        path = parts.path if parts.path.endswith("/") else parts.path + "/"
        lines += f"//{parts.netloc}{path}:_authToken=${{{r.auth_env}}}\n"
    return lines


def _cargo_token_var(r: RegistryConfig) -> str:
    return f"CARGO_REGISTRIES_{re.sub(r'[^A-Za-z0-9]', '_', r.effective_name).upper()}_TOKEN"


def _cargo_section(r: RegistryConfig) -> str:
    assert r.url is not None
    index = r.url if r.url.startswith("sparse+") else f"sparse+{r.url}"
    return f'[registries.{r.effective_name}]\nindex = "{index}"\n\n'


def _bundle_var(host: str) -> str:
    # Bundler's env form of a host: `.` → `__`, `-` → `___`, upper-cased.
    return "BUNDLE_" + host.replace("-", "___").replace(".", "__").upper()


def _maven_settings(registries: Sequence[RegistryConfig]) -> str:
    mirrors = "".join(
        "    <mirror>\n"
        f"      <id>{escape(r.effective_name)}</id>\n"
        "      <mirrorOf>*</mirrorOf>\n"
        f"      <url>{escape(r.url or '')}</url>\n"
        "    </mirror>\n"
        for r in registries
    )
    servers = "".join(
        "    <server>\n"
        f"      <id>{escape(r.effective_name)}</id>\n"
        f"      <username>{escape(r.auth_user or '')}</username>\n"
        f"      <password>${{env.{r.auth_env}}}</password>\n"
        "    </server>\n"
        for r in registries
        if r.auth_env
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0">\n'
        f"  <mirrors>\n{mirrors}  </mirrors>\n"
        + (f"  <servers>\n{servers}  </servers>\n" if servers else "")
        + "</settings>\n"
    )


def _nuget_config(registries: Sequence[RegistryConfig]) -> str:
    sources = "".join(
        f'    <add key="{escape(r.effective_name)}" value="{escape(r.url or "")}" />\n'
        for r in registries
    )
    creds = "".join(
        f"    <{r.effective_name}>\n"
        f'      <add key="Username" value="{escape(r.auth_user or "")}" />\n'
        f'      <add key="ClearTextPassword" value="%{r.auth_env}%" />\n'
        f"    </{r.effective_name}>\n"
        for r in registries
        if r.auth_env
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<configuration>\n"
        f"  <packageSources>\n{sources}  </packageSources>\n"
        + (f"  <packageSourceCredentials>\n{creds}  </packageSourceCredentials>\n" if creds else "")
        + "</configuration>\n"
    )
