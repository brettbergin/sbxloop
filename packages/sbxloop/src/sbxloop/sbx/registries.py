"""What each ``[[registries]]`` entry writes into a sandbox (#680, #766).

A private registry needs three things before a dependency install can
succeed: the host must be reachable (it joins a sandbox's allowlist), the
ecosystem's client must be told to use it, and the credential must be where
that client looks. This module answers the second and third per kind, as
data the provisioner delivers — and, since #766, says WHICH sandbox:

* A registry without ``auth_env`` is the agent sandbox's own: there is no
  secret to keep from the agent, so :func:`plain_env` and
  :func:`client_files` land there as they always did.
* A registry with ``auth_env`` belongs to the SERVICE sandbox. The
  credential (:func:`secret_env`), the client files and the host go there;
  the agent sandbox — the one running the model's commands — gets none of
  them. Instead the service sandbox FETCHES the dependencies into a cache
  inside the shared workspace (:func:`fetch_argv`, one fixed recipe per
  kind, package code disabled) and the agent sandbox builds offline from
  that cache (:func:`offline_env`).

* :func:`plain_env` — non-secret environment (``PIP_INDEX_URL``,
  ``GOPRIVATE``), folded into the persistent env of whichever sandbox
  owns the registry.
* :func:`secret_env` — environment carrying the credential
  (``CARGO_REGISTRIES_<NAME>_TOKEN``, ``BUNDLE_<HOST>``, and the
  ``auth_env`` variable itself), delivered the way the loop delivers its
  own credentials: per-job stdin or the 0600 env file, never an ``sbx``
  argument.
* :func:`client_files` — the client files, written with ``sbx cp`` and
  chmod 600. Wherever the ecosystem expands environment variables in its
  config (npm ``${VAR}``, Maven ``${env.VAR}``, NuGet ``%VAR%``) the file
  names the variable and holds no secret; the ``.netrc`` kinds (pypi, go,
  generic) have no such form, so ``~/.netrc`` holds the value at rest —
  in the service sandbox, where nothing the model writes runs.

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

The fetch recipes (service sandbox → cache → agent sandbox), all with the
package manager's own hooks off so a private package's install script never
runs beside the token that fetched it:

==========  ======================================  ==========================
kind        service sandbox (``fetch`` / ``add``)   agent sandbox (offline)
==========  ======================================  ==========================
npm         ``npm ci|install --ignore-scripts``     ``npm_config_offline=true``
            into ``npm_config_cache``               (``npm rebuild`` runs the
                                                    scripts)
pypi        ``pip download -d <cache>``             ``PIP_NO_INDEX`` +
            (``-r requirements.txt`` / ``.``)       ``PIP_FIND_LINKS``, the
                                                    ``UV_*`` twins
go          ``go mod download``                     ``GOPROXY=off``
            (``GOMODCACHE=<cache>``)
cargo       ``cargo fetch`` (``CARGO_HOME=<cache>``)  ``CARGO_NET_OFFLINE=true``
maven       ``mvn dependency:go-offline``           ``MAVEN_ARGS=-o``
            (``-Dmaven.repo.local=<cache>``)
nuget       ``dotnet restore --packages <cache>``   ``NUGET_PACKAGES=<cache>``
gem         ``bundle cache --all --no-install``     ``BUNDLE_LOCAL=true``
            (into ``vendor/cache``)
generic     no fetch — host only
==========  ======================================  ==========================

The cache lives at ``<workspace>/.sbxloop/deps`` — the one directory both
sandboxes see — reached through the stable link :data:`DEPS_HOME` in each
sandbox's ``$HOME`` (the mount path differs per VM and is discovered after
the environment is written). Every flag here is from the tool's own
documentation and exercised only against the fake sandbox: field-unverified
until the first run with a private registry.
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


# The dependency cache the service sandbox fills and the agent sandbox reads:
# a directory inside the shared workspace, reached through a link at a fixed
# path in each sandbox's $HOME (see the module docstring).
DEPS_WORKSPACE_DIR = ".sbxloop/deps"
DEPS_HOME = f"{SANDBOX_HOME}/.sbxloop/deps"

# The toolchain (``[sandbox] languages`` name) a kind's package manager
# comes with — what the service sandbox must have installed to fetch.
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
    """One fetch as the service sandbox runs it: the argv (host-authored,
    never a shell) and the manifest it was chosen for (None for ``add``).
    The environment is the sandbox's own (:func:`fetch_env`)."""

    argv: tuple[str, ...]
    manifest: str | None


def domains(registries: Sequence[RegistryConfig]) -> list[str]:
    """The registry hosts, in configuration order, deduped."""
    return list(dict.fromkeys(r.host for r in registries))


def kinds(registries: Sequence[RegistryConfig]) -> list[RegistryKind]:
    """The kinds ``registries`` cover, in configuration order, deduped —
    ``generic`` excluded: nothing fetches from it."""
    return [k for k in dict.fromkeys(r.kind for r in registries) if k != "generic"]


def languages(registries: Sequence[RegistryConfig]) -> list[str]:
    """The toolchains the service sandbox needs to fetch for ``registries``."""
    return list(dict.fromkeys(KIND_LANGUAGES[k] for k in kinds(registries)))


def cache_dir(kind: RegistryKind) -> str:
    return f"{DEPS_HOME}/{kind}"


def fetch_env(registries: Sequence[RegistryConfig]) -> dict[str, str]:
    """The service sandbox's fetch environment: each kind's package manager
    pointed at its cache under :data:`DEPS_HOME`, online."""
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
    """The recipe for one fetch in the service sandbox.

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
