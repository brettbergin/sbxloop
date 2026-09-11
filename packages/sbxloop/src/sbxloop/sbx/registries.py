"""Registry configuration and agent-side offline dependency preparation.

Open registries receive client files and non-secret environment in the
agent. Credentialed registries produce a service catalogue and credential
environment instead; the service downloads bytes through fixed operations.
All native resolution, extraction, metadata hooks and cache population run
in the agent. Fetch recipes below are only used to verify that preparation
offline. Disabling package-manager scripts alone is not a security boundary.

The agent cache is inside the workspace and linked at DEPS_HOME. It is not
mounted or linked as a cache in the service. The host moves downloaded data
between sandboxes using sbx cp, with no listener or cross-sandbox channel.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from html import escape
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from sbxloop.config import RegistryConfig, RegistryKind
from sbxloop.sbx.sandbox import SANDBOX_HOME

NPMRC = f"{SANDBOX_HOME}/.npmrc"
NETRC = f"{SANDBOX_HOME}/.netrc"
CARGO_CONFIG = f"{SANDBOX_HOME}/.cargo/config.toml"
MAVEN_SETTINGS = f"{SANDBOX_HOME}/.m2/settings.xml"
NUGET_CONFIG = f"{SANDBOX_HOME}/.nuget/NuGet/NuGet.Config"
GEMRC = f"{SANDBOX_HOME}/.gemrc"


# The agent populates this workspace cache from downloaded artifacts.
# Only the agent gets the stable link in its home directory.
DEPS_WORKSPACE_DIR = ".sbxloop/deps"
DEPS_HOME = f"{SANDBOX_HOME}/.sbxloop/deps"

# The toolchain (``[sandbox] languages`` name) a kind's package manager
# comes with — used for agent-side dependency preparation.
KIND_LANGUAGES: Mapping[RegistryKind, str] = {
    "npm": "node",
    "pypi": "python",
    "go": "go",
    "cargo": "rust",
    "maven": "java",
    "nuget": "dotnet",
    "gem": "ruby",
}

# The manifests a workspace fetch is driven by, per kind, in the order
# tried; a kind whose manifest the workspace lacks is not fetched for.
KIND_MANIFESTS: Mapping[RegistryKind, tuple[str, ...]] = {
    "npm": ("package.json",),
    "pypi": ("requirements.txt", "pyproject.toml"),
    "go": ("go.mod",),
    "cargo": ("Cargo.toml",),
    "maven": ("pom.xml",),
    "nuget": ("*.sln", "*.csproj", "*.fsproj"),
    "gem": ("Gemfile",),
}

FETCH_VERBS = ("fetch", "add")
# The kinds whose recipe takes explicit packages (``add``): the others
# resolve from the manifest only, and the agent edits the manifest itself.
ADD_KINDS: frozenset[RegistryKind] = frozenset({"npm", "pypi", "go"})

# A package spec as the ``add`` verb accepts it: a name, optionally with a
# scope, version, extras or ``@version``/``==version`` pin. Never a leading
# ``-`` (a flag), never whitespace or a shell character — the argv is a list
# and no shell sees it, but a flag could still change what the tool does.
_PACKAGE_RE = re.compile(r"^[A-Za-z0-9@_][A-Za-z0-9@_.\-/+~^<>=!,\[\]:]*$")


class ClientFile(NamedTuple):
    path: str
    text: str


class FetchPlan(NamedTuple):
    """One dependency preparation command for the agent: the argv (host-authored,
    never a shell) and the manifest it was chosen for (None for ``add``).
    The engine verifies this in the agent's offline environment."""

    argv: tuple[str, ...]
    manifest: str | None


def domains(registries: Sequence[RegistryConfig]) -> list[str]:
    """The registry hosts, in configuration order, deduped."""
    return list(dict.fromkeys(r.host for r in registries))


def catalogue_entries(registries: Sequence[RegistryConfig]) -> list[dict[str, str]]:
    """Non-secret authority and authentication metadata for fixed artifact reads."""
    return [
        {
            "name": registry_key(r),
            "kind": r.kind,
            "url": r.url or f"https://{r.host}",
            "env": r.auth_env,
            "user": r.auth_user or "",
        }
        for r in registries
        if r.auth_env
    ]


def registry_key(registry: RegistryConfig) -> str:
    suffix = f":{registry.scope or 'default'}" if registry.kind == "npm" else ""
    return f"{registry.kind}:{registry.effective_name}{suffix}"


def kinds(registries: Sequence[RegistryConfig]) -> list[RegistryKind]:
    """The kinds ``registries`` cover, in configuration order, deduped —
    ``generic`` excluded: nothing fetches from it."""
    return [k for k in dict.fromkeys(r.kind for r in registries) if k != "generic"]


def languages(registries: Sequence[RegistryConfig]) -> list[str]:
    """The native toolchains used to prepare these dependencies in the agent."""
    return list(dict.fromkeys(KIND_LANGUAGES[k] for k in kinds(registries)))


def cache_dir(kind: RegistryKind) -> str:
    return f"{DEPS_HOME}/{kind}"


def fetch_env(registries: Sequence[RegistryConfig]) -> dict[str, str]:
    """Native cache locations, retained for callers preparing data in the agent."""
    env: dict[str, str] = {}
    for kind in kinds(registries):
        if kind == "npm":
            env["npm_config_cache"] = cache_dir("npm")
        elif kind == "go":
            env["GOMODCACHE"] = cache_dir("go")
            env["GOFLAGS"] = "-mod=mod"
        elif kind == "cargo":
            env["CARGO_HOME"] = cache_dir("cargo")
    return env


def offline_env(registries: Sequence[RegistryConfig]) -> dict[str, str]:
    """The agent sandbox's environment for the kinds the service sandbox
    fetches: each package manager reads the cache and never asks a
    registry — there is no credential in this sandbox to ask with."""
    env: dict[str, str] = {}
    for kind in kinds(registries):
        cache = cache_dir(kind)
        if kind == "npm":
            env["npm_config_cache"] = cache
            env["npm_config_offline"] = "true"
        elif kind == "pypi":
            env["PIP_NO_INDEX"] = "1"
            env["PIP_FIND_LINKS"] = cache
            env["UV_NO_INDEX"] = "1"
            env["UV_FIND_LINKS"] = cache
        elif kind == "go":
            env["GOMODCACHE"] = cache
            env["GOFLAGS"] = "-mod=mod"
            env["GOPROXY"] = "off"
        elif kind == "cargo":
            env["CARGO_HOME"] = cache
            env["CARGO_NET_OFFLINE"] = "true"
        elif kind == "maven":
            env["MAVEN_ARGS"] = f"-o -Dmaven.repo.local={cache}"
        elif kind == "nuget":
            env["NUGET_PACKAGES"] = cache
        elif kind == "gem":
            env["BUNDLE_LOCAL"] = "true"
    return env


def workspace_manifests(workspace: Path, kind: RegistryKind) -> list[str]:
    """Which of ``kind``'s manifests the host workspace has (glob patterns
    matched, the pattern itself reported), plus npm's lockfile — what
    :func:`fetch_plan` picks the ``fetch`` recipe from."""
    candidates = [*KIND_MANIFESTS.get(kind, ()), *(("package-lock.json",) if kind == "npm" else ())]
    present: list[str] = []
    for candidate in candidates:
        if "*" in candidate:
            if any(workspace.glob(candidate)):
                present.append(candidate)
        elif (workspace / candidate).is_file():
            present.append(candidate)
    return present


def check_packages(packages: Sequence[str]) -> list[str]:
    """``add``'s packages, validated: raises ValueError naming the first
    that is not a package spec."""
    for package in packages:
        if not _PACKAGE_RE.match(package):
            raise ValueError(f"{package!r} is not a package spec")
    return list(packages)


def fetch_plan(
    kind: RegistryKind,
    verb: str,
    packages: Sequence[str] = (),
    *,
    manifests: Sequence[str] = (),
) -> FetchPlan:
    """A native dependency recipe, executed only in the agent sandbox.

    ``fetch`` resolves the workspace's manifest (``manifests`` is what the
    workspace has, from :data:`KIND_MANIFESTS`'s candidates; the first
    present wins); ``add`` fetches the named ``packages`` for the kinds
    that take them. Raises ValueError for a verb, kind or package outside
    the recipe — the host refuses before any job is built.
    """
    if verb not in FETCH_VERBS:
        raise ValueError(f"unknown fetch verb {verb!r}; one of {list(FETCH_VERBS)}")
    if kind not in KIND_MANIFESTS:
        raise ValueError(f"nothing to fetch for a {kind!r} registry")
    cache = cache_dir(kind)
    if verb == "add":
        if kind not in ADD_KINDS:
            raise ValueError(
                f"{kind} takes no package list: add it to the manifest and fetch again"
            )
        if not packages:
            raise ValueError("add needs at least one package")
        pkgs = check_packages(packages)
        if kind == "npm":
            return FetchPlan(("npm", "install", "--ignore-scripts", *pkgs), None)
        if kind == "pypi":
            return FetchPlan(("pip", "download", "-d", cache, *pkgs), None)
        return FetchPlan(("go", "mod", "download", *pkgs), None)
    if packages:
        raise ValueError("fetch takes no packages (use add)")
    manifest = next((m for m in KIND_MANIFESTS[kind] if m in manifests), None)
    if manifest is None:
        raise ValueError(
            f"no {' / '.join(KIND_MANIFESTS[kind])} in the workspace to fetch {kind} from"
        )
    if kind == "npm":
        verb_argv = ("ci",) if "package-lock.json" in manifests else ("install",)
        return FetchPlan(("npm", *verb_argv, "--ignore-scripts"), manifest)
    if kind == "pypi":
        target = ("-r", manifest) if manifest == "requirements.txt" else (".",)
        return FetchPlan(("pip", "download", "-d", cache, *target), manifest)
    if kind == "go":
        return FetchPlan(("go", "mod", "download"), manifest)
    if kind == "cargo":
        return FetchPlan(("cargo", "fetch"), manifest)
    if kind == "maven":
        return FetchPlan(
            ("mvn", "-B", "dependency:go-offline", f"-Dmaven.repo.local={cache}"), manifest
        )
    if kind == "nuget":
        return FetchPlan(("dotnet", "restore", "--packages", cache), manifest)
    return FetchPlan(("bundle", "cache", "--all", "--no-install"), manifest)


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
