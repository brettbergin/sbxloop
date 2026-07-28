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


# Registry order is the install order, and the order packages appear in the
# batched apt call — keep it stable so the command is reproducible. New
# languages append; nothing depends on the position of an existing entry.
TOOLCHAINS: tuple[Toolchain, ...] = (PYTHON, CPP, RUBY)

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
