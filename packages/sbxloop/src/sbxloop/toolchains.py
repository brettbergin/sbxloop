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
    "DEFAULT_LANGUAGES",
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


PYTHON = Toolchain(
    name="python",
    wanted="python3, pip, venv",
    # The historical probe, unchanged: templates ship a system python3 but
    # Debian/Ubuntu split ensurepip into python3-venv, and it is exactly
    # that split that made the agent's `python3 -m venv` die with
    # "ensurepip is not available" on every revision (field failure, 0.4.0).
    probe="python3 -c 'import ensurepip, pip'",
    apt_packages=("python3-venv", "python3-pip"),
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


# Registry order is the install order, and the order packages appear in the
# batched apt call — keep it stable so the command is reproducible. New
# languages append; nothing depends on the position of an existing entry.
TOOLCHAINS: tuple[Toolchain, ...] = (PYTHON, CPP, RUBY, JAVA)

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
    wanted = {normalize_language(name) for name in names}
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
