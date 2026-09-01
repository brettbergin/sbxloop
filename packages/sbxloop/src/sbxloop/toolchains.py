"""Language toolchains the agent sandbox can be provisioned with (issue #140).

This registry is the answer to "which ecosystem gets a head start?" — the
honest answer being "whichever ones the operator selected", rather than
Python by accident of implementation. ``[sandbox] languages`` picks entries
from here; ``worker.client._ensure_dev_tools`` runs them before the agent's
first turn so a Node task does not spend revision budget discovering that
``node`` is absent while a Python task never pays that cost.

Each entry carries the three things the ensure needs and nothing else:

- ``probe`` — a ``sh -c`` expression that exits 0 when the toolchain is
  already usable. Probing first means a template that already ships the
  toolchain costs no apt call and no network at all, matching the existing
  ``_ensure_dev_tools``/``_ensure_search_fallback`` pattern.
- ``apt_packages`` — packages for the distro path. These are pooled across
  all selected toolchains into ONE ``apt-get update && apt-get install``,
  so selecting three apt languages is one round trip, not three.
- ``install_script`` — a ``sh -c`` script for the direct/official-installer
  path, run after the apt batch (installers frequently need curl/ca-certs
  that the batch pulls in).

Per the #140 decision the convention is **apt where viable, direct/official
installer otherwise** — "viable" meaning the distro package is complete and
current enough for ordinary project work. An entry may use both: apt for the
interpreter, an installer for a tool the distro carries badly.

Every install path is best-effort. A failure warns loudly and the run
continues — the agent keeps passwordless ``sudo apt-get`` as its escape
hatch, so a failed pre-install degrades to the pre-#140 behavior rather than
failing the run.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "BASELINE_TOOLS",
    "DEFAULT_LANGUAGES",
    "GIT",
    "TOOLCHAINS",
    "Toolchain",
    "normalize_language",
    "resolve",
    "supported_languages",
]


@dataclass(frozen=True)
class Toolchain:
    """One selectable language toolchain.

    ``name`` is the canonical ``[sandbox] languages`` value; ``aliases`` are
    the other spellings operators reach for (``"js"`` for ``"javascript"``,
    ``"c++"`` for ``"cpp"``) and are accepted on input but normalized away.
    ``wanted`` names what should land on PATH and is quoted verbatim in the
    warning when provisioning fails, so the log says what is now missing
    rather than only that something went wrong.
    """

    name: str
    wanted: str
    probe: str
    apt_packages: tuple[str, ...] = ()
    install_script: str | None = None
    aliases: tuple[str, ...] = ()
    # Canonical names of toolchains this one is built on. Selecting an entry
    # selects its requirements too, and registry order guarantees they are
    # probed and installed first — TypeScript needs the Node runtime present
    # before `npm i -g typescript` can mean anything.
    requires: tuple[str, ...] = ()


def _arch_dispatch(cases: dict[str, tuple[str, str]]) -> str:
    """POSIX ``case`` on the Debian arch, setting ``$arch`` and ``$sum``.

    Upstream tarballs are per-architecture and so are their digests, and
    the fleet is genuinely mixed (Apple-silicon microVMs are arm64, CI
    runners are amd64). Hardcoding one would work on half the hosts and
    fail the checksum on the other half, so the arch is resolved in-sandbox
    and an unrecognized one fails loudly rather than downloading something
    that cannot run.
    """
    branches = " ".join(
        f"{deb}) arch={upstream}; sum={digest};;" for deb, (upstream, digest) in cases.items()
    )
    return (
        f'case "$(dpkg --print-architecture)" in {branches} '
        '*) echo "unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1;; esac'
    )


# Python was the one entry left on "whatever the template ships" while every
# other runtime here is pinned (#250): no `uv`, no interpreter version pin,
# no probe of the version. Modern pyproject-driven projects — sbxloop's own
# repo included — are uv workspaces declaring `requires-python >= 3.13`, and
# a distro python3 several minors behind fails them at `uv sync` before any
# work happens. So: uv from its pinned GitHub release (checksum-verified,
# same shape as the Node/Go entries — `pip install uv` would need a bootstrap
# venv first because the system Python is externally managed), and the
# interpreter series installed *through* uv as a managed Python. Both
# downloads are GitHub release assets: `github.com` answers with a redirect
# to `release-assets.githubusercontent.com`, so that host is part of the
# provision-time allowlist (``provision.AGENT_ALLOW_DOMAINS``). uv 0.12
# would by default try Astral's own CDN (`releases.astral.sh`) first for
# the managed interpreter and only fall back to GitHub; the install pins
# `UV_PYTHON_INSTALL_MIRROR` to the canonical GitHub prefix so provisioning
# reaches one vendor's hosts rather than needing a second in the baseline.
UV_VERSION = "0.12.5"
PYTHON_SERIES = "3.13"
# The canonical python-build-standalone release prefix, given to uv as its
# install mirror so the download stays on GitHub hosts (see above).
UV_PYTHON_INSTALL_MIRROR = "https://github.com/astral-sh/python-build-standalone/releases/download"
_PYTHON_SERIES_PATTERN = PYTHON_SERIES.replace(".", "\\.")
_UV_TARBALL = "/tmp/uv.tar.gz"  # nosec B108 - path inside the sandbox VM, not host tmp
_UV_DIGESTS = {
    "amd64": (
        "x86_64-unknown-linux-gnu",
        "68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2",
    ),
    "arm64": (
        "aarch64-unknown-linux-gnu",
        "9bf43b4d1a07665bf64d4c4e710930b382321a785e0eb10aac07f46471f86a31",
    ),
}

PYTHON = Toolchain(
    name="python",
    wanted=f"python3, pip, venv, uv, python{PYTHON_SERIES}",
    # The historical ensurepip probe stays: templates ship a system python3
    # but Debian/Ubuntu split ensurepip into python3-venv, and it is exactly
    # that split that made the agent's `python3 -m venv` die with
    # "ensurepip is not available" on every revision (field failure, 0.4.0).
    # Added to it: uv on PATH and the pinned series answering to its
    # versioned name — like Node and Go, checking the version rather than
    # mere presence, so a template with an older python3.x cannot satisfy
    # the probe and leave the agent with the interpreter #250 says fails.
    probe=(
        "python3 -c 'import ensurepip, pip' && command -v uv >/dev/null "
        f"&& python{PYTHON_SERIES} --version 2>/dev/null "
        f'| grep -q "^Python {_PYTHON_SERIES_PATTERN}\\."'
    ),
    apt_packages=("python3-venv", "python3-pip", "curl", "ca-certificates"),
    # uv and uvx go straight into /usr/local/bin (no profile edits to
    # source). The managed interpreter lives under the agent's home so
    # `uv run`/`uv sync` find it without sudo; its versioned name is linked
    # onto PATH so `python3.13 -m venv` and the probe work without uv. The
    # system `python3` is deliberately left alone — the worker runs on it.
    install_script=(
        "set -e; " + _arch_dispatch(_UV_DIGESTS) + "; "
        f'curl -fsSL -o {_UV_TARBALL} "https://github.com/astral-sh/uv/releases/download'
        f'/{UV_VERSION}/uv-$arch.tar.gz"; '
        f"printf '%s  {_UV_TARBALL}\\n' \"$sum\" | sha256sum -c - >/dev/null; "
        f"sudo -n tar -xzf {_UV_TARBALL} -C /usr/local/bin --strip-components=1 "
        '"uv-$arch/uv" "uv-$arch/uvx"; '
        f"rm -f {_UV_TARBALL}; "
        f'UV_PYTHON_INSTALL_MIRROR="{UV_PYTHON_INSTALL_MIRROR}" uv python install {PYTHON_SERIES}; '
        f'sudo -n ln -sf "$(uv python find {PYTHON_SERIES})" /usr/local/bin/python{PYTHON_SERIES}'
    ),
    aliases=("py", "python3"),
)


CPP = Toolchain(
    name="cpp",
    wanted="gcc, g++, make, cmake, ninja, pkg-config",
    # Probe the tools a build actually needs, not everything installed:
    # ninja is optional (cmake falls back to make) and clang is a second
    # compiler, so requiring them here would reinstall on templates that
    # are already perfectly usable.
    probe=(
        "command -v gcc >/dev/null && command -v g++ >/dev/null "
        "&& command -v make >/dev/null && command -v cmake >/dev/null "
        "&& command -v pkg-config >/dev/null"
    ),
    # The cleanest case in the set: all current and complete in
    # Debian/Ubuntu, so no installer and no egress beyond the apt mirrors
    # that are already in the always-reachable baseline. build-essential
    # brings gcc, g++, make, and libc headers.
    apt_packages=("build-essential", "cmake", "ninja-build", "pkg-config"),
    aliases=("c", "c++", "cxx", "c-cpp"),
)


RUBY = Toolchain(
    name="ruby",
    wanted="ruby, gem, bundle",
    probe=(
        "command -v ruby >/dev/null && command -v gem >/dev/null && command -v bundle >/dev/null"
    ),
    # `ruby-dev` and `build-essential` are not optional in practice: gems
    # with native extensions (nokogiri, pg, ...) fail to build without
    # headers and a compiler, which is the common failure mode when only
    # `ruby` is installed. build-essential is shared with cpp and installs
    # once thanks to the pooled apt call.
    apt_packages=("ruby-full", "ruby-dev", "bundler", "build-essential"),
    aliases=("rb",),
)


# sbx documents /etc/sandbox-persistent.sh as where persistent sandbox env
# lives, and the worker loads it at startup (see
# ``sbxloop_worker.__main__.PERSISTENT_ENV_FILE``) so the agent session and
# its shell commands inherit it. That makes it the one place a toolchain can
# export a variable the agent will actually see — `sbx exec sh -c` does not
# source login profiles, so /etc/profile.d would be invisible here.
PERSISTENT_ENV = "/etc/sandbox-persistent.sh"


def _persist_env(variable: str, value_expr: str) -> str:
    """Shell that appends ``export VAR=...`` to the persistent env, once.

    ``value_expr`` is substituted into the shell unquoted, so it may be a
    command substitution. The grep guard keeps a re-run from stacking
    duplicate exports.
    """
    return (
        f"grep -qs '^export {variable}=' {PERSISTENT_ENV} || "
        f'printf "export {variable}=%s\\n" "{value_expr}" '
        f"| sudo -n tee -a {PERSISTENT_ENV} >/dev/null"
    )


# Pinned rather than floating on `default-jdk`, which moves between distro
# releases and would silently change the compiler under a project.
JAVA_JDK_MAJOR = "21"

JAVA = Toolchain(
    name="java",
    wanted="java, javac, mvn, JAVA_HOME",
    # JAVA_HOME is part of the contract (#161: many build tools read it
    # directly), but it lives in a file rather than this probe's env — the
    # probe runs under a bare `sbx exec sh -c`, which sources nothing. So
    # check that the export was recorded, not that it is currently set.
    probe=(
        "command -v javac >/dev/null && command -v mvn >/dev/null "
        f"&& grep -qs '^export JAVA_HOME=' {PERSISTENT_ENV}"
    ),
    # apt for the JDK and Maven, per #161. Distro Gradle is materially
    # stale and most projects ship a `gradlew` wrapper anyway, so Gradle is
    # deliberately absent — the wrapper fetches its own distribution, which
    # is an egress question for #141 rather than a package to install.
    apt_packages=(f"openjdk-{JAVA_JDK_MAJOR}-jdk", "maven"),
    # Derive JAVA_HOME from the javac that apt just put on PATH rather than
    # hardcoding /usr/lib/jvm/... — the directory name embeds the distro
    # architecture (…-amd64 vs …-arm64) and would be wrong half the time.
    install_script=_persist_env(
        "JAVA_HOME", '$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")'
    ),
    aliases=("jdk", "jvm"),
)


# Composer is not reliably packaged at a useful version, so it comes from
# upstream — but pinned to a release and checked against that release's
# published sha256 rather than piped into the interpreter. #167 asks for
# exactly this. Bumping the version means bumping the digest with it; the
# digest is published at <download url>.sha256sum.
COMPOSER_VERSION = "2.10.2"
COMPOSER_SHA256 = "5ee7125f8a30a34d246cefdc0bc85b8a783b28f2aec968994118512350d28027"
_COMPOSER_PHAR = "/tmp/composer.phar"  # nosec B108 - path inside the sandbox VM, not host tmp

PHP = Toolchain(
    name="php",
    wanted="php, composer, mbstring/xml/curl/zip extensions",
    # Extensions matter more than the interpreter here (#167): a bare
    # php-cli passes a `command -v php` check and then fails the moment a
    # real project or Composer itself needs mbstring or zip. Probe for what
    # actually has to work.
    probe=(
        "command -v php >/dev/null && command -v composer >/dev/null && "
        'php -r \'exit((int)!(extension_loaded("mbstring") '
        '&& extension_loaded("curl") && extension_loaded("zip") '
        '&& extension_loaded("dom")));\''
    ),
    # apt for the interpreter and extensions. curl/ca-certificates are here
    # for the install script below — the apt batch runs first precisely so
    # installer-based entries can rely on them.
    apt_packages=(
        "php-cli",
        "php-mbstring",
        "php-xml",
        "php-curl",
        "php-zip",
        "curl",
        "ca-certificates",
    ),
    install_script=(
        "set -e; "
        f"curl -fsSL -o {_COMPOSER_PHAR} "
        f"https://getcomposer.org/download/{COMPOSER_VERSION}/composer.phar; "
        f"printf '%s  {_COMPOSER_PHAR}\\n' '{COMPOSER_SHA256}' | sha256sum -c - >/dev/null; "
        f"sudo -n install -m 0755 {_COMPOSER_PHAR} /usr/local/bin/composer; "
        f"rm -f {_COMPOSER_PHAR}"
    ),
)


# Debian/Ubuntu stable ship a Node several majors behind current LTS, which
# breaks packages declaring modern `engines` constraints — a functional
# failure, not cosmetic lag (#147). So: the official tarball, pinned to an
# exact LTS release so runs are reproducible. Digests are the upstream
# SHASUMS256.txt entries; bumping the version means bumping both.
NODE_VERSION = "24.18.0"
NODE_MAJOR = NODE_VERSION.split(".")[0]
_NODE_TARBALL = "/tmp/node.tar.xz"  # nosec B108 - path inside the sandbox VM, not host tmp
_NODE_DIGESTS = {
    "amd64": ("x64", "55aa7153f9d88f28d765fcdad5ae6945b5c0f98a36881703817e4c450fa76742"),
    "arm64": ("arm64", "58c9520501f6ae2b52d5b210444e24b9d0c029a58c5011b797bc1fe7105886f6"),
}

NODE = Toolchain(
    name="javascript",
    wanted=f"node {NODE_MAJOR}.x, npm, npx",
    # Check the pinned major, not merely that node exists: a template
    # carrying an older Node would otherwise satisfy the probe and leave
    # the agent with the very version #147 says breaks modern `engines`.
    probe=(
        "command -v npm >/dev/null && command -v npx >/dev/null "
        f'&& node -v 2>/dev/null | grep -q "^v{NODE_MAJOR}\\."'
    ),
    apt_packages=("curl", "ca-certificates", "xz-utils"),
    # Extracted into /usr/local with the leading directory stripped, which
    # also makes /usr/local npm's global prefix — so a later `npm i -g` (the
    # TypeScript entry, or the agent itself) lands on PATH without further
    # wiring. The three top-level doc files the tarball carries are removed
    # rather than left loose in /usr/local.
    install_script=(
        "set -e; " + _arch_dispatch(_NODE_DIGESTS) + "; "
        f'curl -fsSL -o {_NODE_TARBALL} "https://nodejs.org/dist/v{NODE_VERSION}'
        f'/node-v{NODE_VERSION}-linux-$arch.tar.xz"; '
        f"printf '%s  {_NODE_TARBALL}\\n' \"$sum\" | sha256sum -c - >/dev/null; "
        f"sudo -n tar -xJf {_NODE_TARBALL} -C /usr/local --strip-components=1; "
        "sudo -n rm -f /usr/local/CHANGELOG.md /usr/local/LICENSE /usr/local/README.md; "
        f"rm -f {_NODE_TARBALL}"
    ),
    aliases=("js", "node", "nodejs", "javascript-node"),
)


# There is no meaningful distro package for the TypeScript compiler — it is
# an npm package — so this is the one entry whose install comes from a
# package registry rather than a vendor. Pinned so a bootstrap is
# reproducible; a project with its own `typescript` devDependency should and
# will use that instead (#150).
TYPESCRIPT_VERSION = "7.0.2"

TYPESCRIPT = Toolchain(
    name="typescript",
    wanted="tsc (plus node, npm, npx)",
    # Only tsc: the Node half is the NODE entry's probe, run first because
    # `requires` pulls it in and registry order installs it ahead of this.
    probe="command -v tsc >/dev/null",
    requires=("javascript",),
    # Node's tarball lands in /usr/local, which is npm's global prefix
    # there, so a global install puts tsc on PATH with no extra wiring.
    # --no-fund/--no-audit keep the provisioning log to the point.
    install_script=(
        f"set -e; sudo -n npm install -g --no-fund --no-audit typescript@{TYPESCRIPT_VERSION}"
    ),
    aliases=("ts",),
)


# Debian/Ubuntu ship `golang-go`, but it commonly lags upstream and Go
# modules frequently declare a `go` directive newer than the distro build,
# which fails the build outright (#153). Official tarball, pinned; digests
# are upstream's own (https://go.dev/dl/?mode=json).
GO_VERSION = "1.26.5"
GO_SERIES = ".".join(GO_VERSION.split(".")[:2])
_GO_TARBALL = "/tmp/go.tar.gz"  # nosec B108 - path inside the sandbox VM, not host tmp
_GO_DIGESTS = {
    "amd64": ("amd64", "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053"),
    "arm64": ("arm64", "fe4789e92b1f33358680864bbe8704289e7bb5fc207d80623c308935bd696d49"),
}

GO = Toolchain(
    name="go",
    wanted=f"go {GO_SERIES}.x (build, test, fmt, vet)",
    # Same reasoning as Node: check the pinned series, since accepting an
    # older `go` would leave the agent with the stale-toolchain failure
    # this entry exists to prevent.
    probe=f'go version 2>/dev/null | grep -q "go{GO_SERIES}\\."',
    apt_packages=("curl", "ca-certificates"),
    # Upstream's documented layout: wipe any previous /usr/local/go rather
    # than extracting over it (mixing two versions leaves a broken tree),
    # then link the two entrypoints onto PATH. GOTOOLCHAIN is deliberately
    # left at its default — see the note below.
    install_script=(
        "set -e; " + _arch_dispatch(_GO_DIGESTS) + "; "
        f'curl -fsSL -o {_GO_TARBALL} "https://go.dev/dl/go{GO_VERSION}.linux-$arch.tar.gz"; '
        f"printf '%s  {_GO_TARBALL}\\n' \"$sum\" | sha256sum -c - >/dev/null; "
        "sudo -n rm -rf /usr/local/go; "
        f"sudo -n tar -xzf {_GO_TARBALL} -C /usr/local; "
        "sudo -n ln -sf /usr/local/go/bin/go /usr/local/bin/go; "
        "sudo -n ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt; "
        f"rm -f {_GO_TARBALL}"
    ),
    aliases=("golang",),
)

# #153 asks whether to pin GOTOOLCHAIN=local for reproducibility. No: a
# project whose go.mod demands a newer Go would then fail outright, which is
# precisely the distro-lag failure this entry exists to avoid. Left at the
# default, Go fetches the toolchain the module asks for — and that download
# happens during EXECUTE, where a plan CAN declare the Go proxy as egress,
# unlike the provision-time install above.


# Debian/Ubuntu do ship cargo/rustc, so apt is technically available — but
# distro Rust routinely lags stable by several releases, which breaks
# edition- and MSRV-sensitive projects outright rather than cosmetically,
# and rustup is the ecosystem norm nearly every Rust instruction assumes
# (#143). rustup-init is downloaded and checksum-verified rather than piped
# from sh.rustup.rs into a shell.
RUSTUP_VERSION = "1.29.0"
RUST_TOOLCHAIN = "1.97.1"
_RUSTUP_INIT = "/tmp/rustup-init"  # nosec B108 - path inside the sandbox VM, not host tmp
_RUSTUP_DIGESTS = {
    "amd64": (
        "x86_64-unknown-linux-gnu",
        "4acc9acc76d5079515b46346a485974457b5a79893cfb01112423c89aeb5aa10",
    ),
    "arm64": (
        "aarch64-unknown-linux-gnu",
        "9732d6c5e2a098d3521fca8145d826ae0aaa067ef2385ead08e6feac88fa5792",
    ),
}
# rustup writes its shims to ~/.cargo/bin and normally puts that on PATH by
# editing shell profiles — which a bare `sbx exec sh -c` never sources. So
# --no-modify-path, and link the shims into /usr/local/bin instead. Kept in
# the agent's home rather than a system dir so `cargo install` and the
# registry cache stay writable without sudo.
_RUST_SHIMS = ("cargo", "rustc", "rustup", "rustfmt", "cargo-fmt", "cargo-clippy")

RUST = Toolchain(
    name="rust",
    wanted="cargo, rustc, rustfmt, clippy",
    # rustfmt and clippy are "the common next asks" (#143) and are what a
    # plan's verify_commands usually reach for, so their absence is a real
    # gap — probe for them too rather than declaring victory on cargo.
    probe=(
        "command -v cargo >/dev/null && command -v rustc >/dev/null "
        "&& command -v rustfmt >/dev/null && command -v cargo-clippy >/dev/null"
    ),
    apt_packages=("curl", "ca-certificates", "build-essential"),
    # --profile minimal plus the two components explicitly, rather than the
    # default profile: exactly what #143 asks for, and it skips the large
    # rust-docs component nothing here needs.
    install_script=(
        "set -e; " + _arch_dispatch(_RUSTUP_DIGESTS) + "; "
        f'curl -fsSL -o {_RUSTUP_INIT} "https://static.rust-lang.org/rustup/archive'
        f'/{RUSTUP_VERSION}/$arch/rustup-init"; '
        f"printf '%s  {_RUSTUP_INIT}\\n' \"$sum\" | sha256sum -c - >/dev/null; "
        f"chmod +x {_RUSTUP_INIT}; "
        f"{_RUSTUP_INIT} -y --no-modify-path --profile minimal "
        f"--component rustfmt --component clippy --default-toolchain {RUST_TOOLCHAIN}; "
        f"for shim in {' '.join(_RUST_SHIMS)}; do "
        'sudo -n ln -sf "$HOME/.cargo/bin/$shim" "/usr/local/bin/$shim"; done; '
        f"rm -f {_RUSTUP_INIT}"
    ),
    aliases=("rs", "cargo"),
)


# Availability and currency of `dotnet-sdk-*` in the base Debian/Ubuntu
# archives varies by release and is unreliable to depend on (#164), so the
# SDK comes from Microsoft's own builds — pinned to an LTS patch and
# verified against the sha512 its release metadata publishes (the .NET feed
# publishes sha512 rather than sha256, hence the different checksum tool).
DOTNET_SDK_VERSION = "10.0.302"
DOTNET_SDK_MAJOR = DOTNET_SDK_VERSION.split(".")[0]
DOTNET_ROOT = "/usr/local/dotnet"
_DOTNET_TARBALL = "/tmp/dotnet-sdk.tar.gz"  # nosec B108 - inside the sandbox VM, not host tmp
_DOTNET_DIGESTS = {
    "amd64": (
        "x64",
        "10069bec8783596484a610332f090d562802a41b9b40e3327a5a5688b572e10c"
        "296ae300f940d40461f23c157ed1b0843c2f8e6b3f20d8d8d9d83432d8143bac",
    ),
    "arm64": (
        "arm64",
        "9e409c14e00686d661c78fa4dd9ad0e4dcf695c328bd5ff777d05b4a9c34b42c"
        "f89b12573b92e9fb2f565dbe12016b4835f77c7d9a42b55a7494df21634cd5d6",
    ),
}

DOTNET = Toolchain(
    name="dotnet",
    wanted=f"dotnet SDK {DOTNET_SDK_MAJOR}.x, DOTNET_ROOT",
    # Pinned major, plus the DOTNET_ROOT export: a manual (non-package) SDK
    # install is only half-done without it, and like JAVA_HOME it lives in
    # a file this probe's bare shell never sources.
    probe=(
        f'dotnet --list-sdks 2>/dev/null | grep -q "^{DOTNET_SDK_MAJOR}\\." '
        f"&& grep -qs '^export DOTNET_ROOT=' {PERSISTENT_ENV}"
    ),
    # libicu-dev is the version-agnostic way to get the ICU runtime the SDK
    # needs for globalization; naming libicuNN directly would pin us to one
    # Ubuntu release.
    apt_packages=("curl", "ca-certificates", "libicu-dev"),
    install_script=(
        "set -e; " + _arch_dispatch(_DOTNET_DIGESTS) + "; "
        f'curl -fsSL -o {_DOTNET_TARBALL} "https://builds.dotnet.microsoft.com/dotnet'
        f'/Sdk/{DOTNET_SDK_VERSION}/dotnet-sdk-{DOTNET_SDK_VERSION}-linux-$arch.tar.gz"; '
        f"printf '%s  {_DOTNET_TARBALL}\\n' \"$sum\" | sha512sum -c - >/dev/null; "
        f"sudo -n rm -rf {DOTNET_ROOT}; sudo -n mkdir -p {DOTNET_ROOT}; "
        f"sudo -n tar -xzf {_DOTNET_TARBALL} -C {DOTNET_ROOT}; "
        f"sudo -n ln -sf {DOTNET_ROOT}/dotnet /usr/local/bin/dotnet; "
        + _persist_env("DOTNET_ROOT", DOTNET_ROOT)
        + "; "
        # Telemetry would only ever be a blocked outbound request under a
        # default-deny egress policy; opting out keeps the noise out of the
        # agent's build logs.
        + _persist_env("DOTNET_CLI_TELEMETRY_OPTOUT", "1")
        + "; "
        f"rm -f {_DOTNET_TARBALL}"
    ),
    aliases=("csharp", "c#", "net", "dotnet-sdk"),
)


# Registry order is the install order, and the order packages appear in the
# batched apt call — keep it stable so the command is reproducible. New
# languages append; nothing depends on the position of an existing entry.
TOOLCHAINS: tuple[Toolchain, ...] = (
    PYTHON,
    CPP,
    RUBY,
    JAVA,
    PHP,
    NODE,
    TYPESCRIPT,
    GO,
    RUST,
    DOTNET,
)

# Baseline agent tooling: provisioned on every agent sandbox regardless of
# `[sandbox] languages`, and deliberately NOT selectable through it (issue
# #252). git is the one tool a project's tests or build shell out to no
# matter which ecosystem it belongs to — sbxloop's own suite does, on a
# hardcoded PATH — and a template without it fails those tasks on every
# revision. apt `git` is cheap and comes from the mirrors already in the
# always-reachable baseline, so it rides the same pooled apt call as the
# selected languages.
GIT = Toolchain(
    name="git",
    wanted="git",
    probe="command -v git >/dev/null",
    apt_packages=("git",),
)

BASELINE_TOOLS: tuple[Toolchain, ...] = (GIT,)

# The claude agent backend's in-sandbox runtime (#533): the Claude Agent SDK
# spawns the Claude Code CLI, which is not bundled with the pip package. Not
# a `[sandbox] languages` entry — it is a backend prerequisite the worker
# install ensures when `[agent] backend = "claude"` — but shaped as a
# Toolchain so the probe-first/batched-apt/loud-warning machinery applies.
# Node comes first via the javascript toolchain (npm's global prefix is
# /usr/local there, so the `npm i -g` below lands `claude` on PATH).
CLAUDE_CODE = Toolchain(
    name="claude-code",
    wanted="claude (the Claude Code CLI)",
    probe="command -v claude >/dev/null",
    install_script="sudo -n npm install -g @anthropic-ai/claude-code",
    requires=("javascript",),
)

# What a run provisions when `[sandbox] languages` is unset. Python has had
# this head start since 0.4.0 and keeping it as the default means #140
# changes nothing for existing runs; an explicit `languages` REPLACES it,
# so nothing is hardcoded as privileged once an operator has an opinion.
DEFAULT_LANGUAGES: tuple[str, ...] = ("python",)

_BY_KEY: dict[str, Toolchain] = {}
for _toolchain in TOOLCHAINS:
    for _key in (_toolchain.name, *_toolchain.aliases):
        _BY_KEY[_key] = _toolchain


def supported_languages() -> tuple[str, ...]:
    """Canonical ``[sandbox] languages`` values, in registry order."""
    return tuple(toolchain.name for toolchain in TOOLCHAINS)


def normalize_language(name: str) -> str | None:
    """Canonicalize one language name, or None when it is not in the set."""
    toolchain = _BY_KEY.get(name.strip().lower())
    return None if toolchain is None else toolchain.name


def resolve(names: Sequence[str]) -> tuple[Toolchain, ...]:
    """Map selected language names onto toolchains, deduped, registry order.

    Unknown names are dropped rather than raised on: config validation is
    where a typo gets rejected with a helpful message, and the ensure must
    never be the thing that fails a run.
    """
    wanted: set[str] = set()
    pending = [normalize_language(name) for name in names]
    while pending:
        key = pending.pop()
        if key is None or key in wanted:
            continue
        wanted.add(key)
        pending.extend(_BY_KEY[key].requires)
    return tuple(toolchain for toolchain in TOOLCHAINS if toolchain.name in wanted)


def apt_packages(toolchains: Iterable[Toolchain]) -> tuple[str, ...]:
    """The union of apt packages for ``toolchains``, deduped, order-stable.

    Toolchains overlap on purpose (``build-essential`` is wanted by C/C++,
    by Ruby's native gem extensions, and by Python C extensions); pooling
    here is what keeps that from becoming three separate apt installs of
    the same package.
    """
    packages: list[str] = []
    for toolchain in toolchains:
        for package in toolchain.apt_packages:
            if package not in packages:
                packages.append(package)
    return tuple(packages)
