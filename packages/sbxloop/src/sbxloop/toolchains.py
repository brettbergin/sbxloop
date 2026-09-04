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
- ``install_domains`` — the hosts that script downloads from (#616). They
  are seeded into the agent sandbox's egress allowlist for the *selected*
  toolchains only, so the installer works under a default-deny preset and
  a language nobody asked for opens no host. Colocated with the URL so a
  CDN change cannot drift the two apart.
- ``manifests`` — the files whose presence in a workspace means "this
  project is written in this language" (#624). ``detect_languages`` reads
  them so a run on a Go repo gets Go without an operator editing config.

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

import json
import re
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal, NamedTuple
from urllib.parse import quote

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

from sbxloop.errors import ProvisionError
from sbxloop.log import get_logger

__all__ = [
    "BASELINE_TOOLS",
    "DEFAULT_LANGUAGES",
    "GIT",
    "TOOLCHAINS",
    "LanguageResolution",
    "Toolchain",
    "ToolchainVersion",
    "UnsatisfiablePin",
    "detect_languages",
    "install_domains",
    "normalize_language",
    "resolve",
    "resolve_languages",
    "supported_languages",
    "toolchain_versions",
]

log = get_logger(__name__)


class ToolchainVersion(NamedTuple):
    """The version series one run provisions for a toolchain, and why.

    ``source`` is ``"default"`` or the workspace file the declaration was
    read from (``pyproject.toml``, ``.python-version``, ``.nvmrc``,
    ``package.json``, ``global.json``, ``.ruby-version``, ``.java-version``
    …); ``constraint`` is the declaration as written, so the
    ``sandbox.toolchain`` event says what was asked for as well as what was
    installed (#627). A declaration no series can honour is not softened
    into the default: it raises :class:`UnsatisfiablePin` (#686).
    """

    series: str
    source: str
    constraint: str | None = None


class UnsatisfiablePin(ProvisionError):
    """A workspace pins a version no series in the registry can honour.

    Raised at language resolution, before any microVM exists, so the run
    fails with the pin and the installable series named — never at the
    gate, where a ``global.json`` the installed SDK does not satisfy makes
    ``dotnet`` refuse to run at all and a Gemfile ``ruby`` line makes
    ``bundle`` refuse likewise (#686). Provisioning the default instead
    would only move that failure into the agent's turns.
    """


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
    # Hosts ``install_script`` fetches from, plus the redirect targets those
    # hosts answer with (a redirect to an unlisted host fails the download
    # just as surely as the original host being blocked). Empty for pure-apt
    # entries: the mirrors are in the always-reachable baseline already.
    install_domains: tuple[str, ...] = ()
    # Workspace files that identify a project of this language. A plain
    # name matches exactly; a ``*.ext`` pattern matches by suffix.
    manifests: tuple[str, ...] = ()
    # The version series this entry installs and its probe requires; None
    # for an entry with no version of its own (apt-only, or one that
    # self-serves like Go — see the GOTOOLCHAIN note). A project may
    # declare another (#627): ``declared_series`` reads the workspace's own
    # pin and ``rebuild`` produces the entry for it, so ``version_from`` /
    # ``for_version`` are the whole per-project story.
    series: str | None = None
    declared_series: Callable[[Path], ToolchainVersion | None] | None = field(
        default=None, compare=False, repr=False
    )
    rebuild: Callable[[str], Toolchain] | None = field(default=None, compare=False, repr=False)
    # Seconds ``install_script`` may take when that is more than the
    # ensure's default budget: an entry that compiles from source (a pinned
    # Ruby) needs minutes a tarball never does.
    install_budget: float | None = None

    def version_from(self, workspace: Path | None) -> ToolchainVersion | None:
        """The series to provision for ``workspace``: its own declaration
        when it has one the entry can honour, else the default. None for
        an entry that has no version of its own."""
        if self.series is None:
            return None
        if workspace is not None and self.declared_series is not None:
            declared = self.declared_series(workspace)
            if declared is not None:
                return declared
        return ToolchainVersion(self.series, "default")

    def for_version(self, series: str) -> Toolchain:
        """This entry rebuilt for ``series`` (itself when that is its own)."""
        if series == self.series or self.rebuild is None:
            return self
        return self.rebuild(series)


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
# The series uv can install as managed interpreters, newest first — what a
# project's `requires-python` is matched against (#627). The default stays
# 3.13 whenever it satisfies the declaration, so a project that is happy
# with 3.13 is provisioned exactly like an undeclared one; only a
# declaration 3.13 fails moves the pick, to the highest series that passes.
PYTHON_SERIES_CANDIDATES = ("3.14", "3.13", "3.12", "3.11", "3.10", "3.9", "3.8")
# The canonical python-build-standalone release prefix, given to uv as its
# install mirror so the download stays on GitHub hosts (see above).
UV_PYTHON_INSTALL_MIRROR = "https://github.com/astral-sh/python-build-standalone/releases/download"
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
_PYTHON_VERSION_FILE = re.compile(r"^(?:cpython[@-])?(\d+\.\d+)")


def _series_satisfying(
    constraint: str,
    satisfies: Callable[[str], bool],
    default: str,
    candidates: Sequence[str],
    source: str,
    *,
    toolchain: str,
    hint: str = "",
) -> ToolchainVersion:
    """The default series when ``satisfies`` accepts it, else the highest
    candidate it accepts. None accepting is :class:`UnsatisfiablePin`: the
    project asked for something this registry cannot install, and the run
    says so now rather than after the default was provisioned and refused
    (#686 — .NET's ``global.json`` and Bundler's ``ruby`` line both hard
    fail on a mismatch; uv's ``requires-python`` does too)."""
    if satisfies(default):
        return ToolchainVersion(default, source, constraint)
    for series in candidates:
        if satisfies(series):
            return ToolchainVersion(series, source, constraint)
    installable = ", ".join(candidates)
    log.error(
        "toolchains.version_unsatisfiable",
        toolchain=toolchain,
        source=source,
        constraint=constraint,
        candidates=list(candidates),
    )
    raise UnsatisfiablePin(
        f"{source} pins {toolchain} to {constraint!r}, which no series this host can "
        f"install satisfies (installable: {installable}); the run stops here rather "
        f"than provisioning {default} for a project that would refuse it — pin an "
        "installable series, or widen the declaration" + (f" ({hint})" if hint else "")
    )


def _python_series_from(workspace: Path) -> ToolchainVersion | None:
    """The Python series the workspace pins: ``.python-version`` first (an
    exact pin uv itself honours ahead of the range), then ``[project]
    requires-python``. A series is taken to satisfy a range when any of its
    releases could — the range is checked at ``X.Y.0`` and ``X.Y.99``, so
    ``>=3.11.4`` still selects 3.11, whose latest patch uv installs."""
    pin = workspace / ".python-version"
    try:
        first = pin.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        first = ""
    if first:
        match = _PYTHON_VERSION_FILE.match(first)
        if match is not None:
            series = match.group(1)
            return _series_satisfying(
                first,
                lambda s: s == series,
                PYTHON_SERIES,
                PYTHON_SERIES_CANDIDATES,
                pin.name,
                toolchain="python",
            )
    try:
        data = tomllib.loads((workspace / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    constraint = project.get("requires-python") if isinstance(project, dict) else None
    if not isinstance(constraint, str) or not constraint.strip():
        return None
    try:
        spec = SpecifierSet(constraint)
    except InvalidSpecifier:
        log.warning(
            "toolchains.version_unreadable",
            source="pyproject.toml",
            constraint=constraint,
            hint=f"requires-python is not a PEP 440 specifier; provisioning {PYTHON_SERIES}",
        )
        return None

    def satisfies(series: str) -> bool:
        return any(spec.contains(Version(f"{series}.{patch}")) for patch in ("0", "99"))

    return _series_satisfying(
        constraint.strip(),
        satisfies,
        PYTHON_SERIES,
        PYTHON_SERIES_CANDIDATES,
        "pyproject.toml",
        toolchain="python",
    )


def _python_toolchain(series: str) -> Toolchain:
    pattern = series.replace(".", "\\.")
    return Toolchain(
        name="python",
        wanted=f"python3, pip, venv, uv, python{series}",
        # The historical ensurepip probe stays: templates ship a system
        # python3 but Debian/Ubuntu split ensurepip into python3-venv, and
        # it is exactly that split that made the agent's `python3 -m venv`
        # die with "ensurepip is not available" on every revision (field
        # failure, 0.4.0). Added to it: uv on PATH and the pinned series
        # answering to its versioned name — like Node and Go, checking the
        # version rather than mere presence, so a template with an older
        # python3.x cannot satisfy the probe and leave the agent with the
        # interpreter #250 says fails.
        probe=(
            "python3 -c 'import ensurepip, pip' && command -v uv >/dev/null "
            f"&& python{series} --version 2>/dev/null "
            f'| grep -q "^Python {pattern}\\."'
        ),
        apt_packages=("python3-venv", "python3-pip", "curl", "ca-certificates"),
        # uv and uvx go straight into /usr/local/bin (no profile edits to
        # source). The managed interpreter lives under the agent's home so
        # `uv run`/`uv sync` find it without sudo; its versioned name is
        # linked onto PATH so `python3.13 -m venv` and the probe work
        # without uv. The system `python3` is deliberately left alone — the
        # worker runs on it.
        install_script=(
            "set -e; " + _arch_dispatch(_UV_DIGESTS) + "; "
            f'curl -fsSL -o {_UV_TARBALL} "https://github.com/astral-sh/uv/releases/download'
            f'/{UV_VERSION}/uv-$arch.tar.gz"; '
            f"printf '%s  {_UV_TARBALL}\\n' \"$sum\" | sha256sum -c - >/dev/null; "
            f"sudo -n tar -xzf {_UV_TARBALL} -C /usr/local/bin --strip-components=1 "
            '"uv-$arch/uv" "uv-$arch/uvx"; '
            f"rm -f {_UV_TARBALL}; "
            f'UV_PYTHON_INSTALL_MIRROR="{UV_PYTHON_INSTALL_MIRROR}" uv python install {series}; '
            f'sudo -n ln -sf "$(uv python find {series})" /usr/local/bin/python{series}'
        ),
        # The uv tarball and uv's managed interpreter are both GitHub
        # release assets; github.com answers with a redirect to
        # release-assets.
        install_domains=("github.com", "release-assets.githubusercontent.com"),
        manifests=(
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "requirements.txt",
            "Pipfile",
            "uv.lock",
            "poetry.lock",
        ),
        aliases=("py", "python3"),
        series=series,
        declared_series=_python_series_from,
        rebuild=_python_toolchain,
    )


PYTHON = _python_toolchain(PYTHON_SERIES)


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
    manifests=("CMakeLists.txt", "meson.build", "configure.ac"),
    aliases=("c", "c++", "cxx", "c-cpp"),
)


# Ruby's version story (#686): the distro's `ruby-full` is complete and
# current enough for ordinary work, so it stays the default — but a Ruby
# project almost always pins, exactly, and Bundler enforces a Gemfile
# `ruby "3.2.2"` line to the patch. Debian/Ubuntu ship one series each,
# so a pin is honoured by compiling: ruby-build (pinned GitHub tarball,
# checksum-verified) builds the exact release from cache.ruby-lang.org,
# verifying the tarball against the sha256 in its own definition file.
# That costs minutes, so the install probes first — the distro ruby
# already answering to the pin is the fast path — and the build lands in
# the agent's home (gems install without sudo) with `/usr/local/bin`
# symlinks shadowing the distro binaries. Only a series the table knows
# is admitted; anything older is unsatisfiable rather than a build
# against an OpenSSL it predates.
RUBY_SERIES = "distro"
RUBY_BUILD_VERSION = "20260902"
_RUBY_BUILD_TARBALL = "/tmp/ruby-build.tar.gz"  # nosec B108 - inside the sandbox VM, not host tmp
_RUBY_BUILD_DIGEST = "c7a738bb6e6e06fa827c0d67d6c8e030ae766935400cf090dd8b8ddcddcfe818"
# The release each series resolves to when only the series (or a range) is
# pinned, newest first; an exact pin inside one of these series is built as
# written. ruby-build's definition list is the other half of the contract —
# bumping RUBY_BUILD_VERSION is what makes a newer patch buildable.
RUBY_RELEASES = {"4.0": "4.0.6", "3.4": "3.4.10", "3.3": "3.3.12", "3.2": "3.2.11", "3.1": "3.1.7"}
RUBY_SERIES_CANDIDATES = tuple(RUBY_RELEASES)
_RUBY_TOOLS = ("ruby", "gem", "bundle", "bundler", "irb", "rake", "erb", "rdoc", "ri")
_RUBY_VERSION_FILE = re.compile(r"^(?:ruby-)?(\d+\.\d+)(?:\.(\d+))?$")
_GEMFILE_RUBY = re.compile(r"""^\s*ruby(?:\s+|\s*\(\s*)(['"][^\n]*)$""", re.M)
_QUOTED = re.compile(r"""['"]([^'"]*)['"]""")
_GEM_OP = re.compile(r"^(~>|>=|<=|!=|>|<|=)?\s*(\d+(?:\.\d+)*)$")


def _ruby_pin(text: str) -> tuple[str, str | None] | None:
    """``(series, patch)`` from ``3.2.2`` / ``3.2`` / ``ruby-3.2.2``."""
    match = _RUBY_VERSION_FILE.match(text.strip())
    if match is None:
        return None
    return match.group(1), match.group(2)


def _ruby_series_for(pin: tuple[str, str | None], constraint: str, source: str) -> ToolchainVersion:
    """An exact pin is built as written; a series pin resolves to the
    table's release for it. Either way the series has to be one the
    table knows."""
    series, patch = pin
    wanted = f"{series}.{patch}" if patch is not None else series
    if series not in RUBY_RELEASES:
        return _series_satisfying(
            constraint,
            lambda _: False,
            RUBY_SERIES,
            RUBY_SERIES_CANDIDATES,
            source,
            toolchain="ruby",
        )
    return ToolchainVersion(wanted, source, constraint)


def _gem_requirement(text: str) -> SpecifierSet | None:
    """RubyGems' requirement syntax as a PEP 440 specifier — the operators
    are the same but for ``~>`` (``~=``) and ``=`` (``==``)."""
    clauses: list[str] = []
    for part in text.split(","):
        match = _GEM_OP.match(part.strip())
        if match is None:
            return None
        op, version = match.group(1) or "=", match.group(2)
        if op == "~>" and "." not in version:
            # `~> 3` is `>= 3, < 4`; PEP 440's `~=` wants two components.
            clauses.extend((f">={version}", f"<{int(version) + 1}"))
            continue
        clauses.append({"~>": "~=", "=": "=="}.get(op, op) + version)
    try:
        return SpecifierSet(",".join(clauses))
    except InvalidSpecifier:
        return None


def _ruby_series_from(workspace: Path) -> ToolchainVersion | None:
    """The Ruby the workspace pins: ``.ruby-version`` first (rbenv's
    file, and what a modern Gemfile's ``ruby file: ".ruby-version"``
    reads), then ``.tool-versions``, then the Gemfile's ``ruby`` line —
    an exact version, or a RubyGems requirement (``~> 3.2``, ``>= 3.1``)
    that selects the newest series in the table satisfying it."""
    pin = workspace / ".ruby-version"
    try:
        first = pin.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        first = ""
    if first:
        parsed = _ruby_pin(first)
        if parsed is None:
            log.warning(
                "toolchains.version_unreadable",
                source=pin.name,
                constraint=first,
                hint="not a Ruby release this host reads (a preview, or a non-MRI ruby); "
                "provisioning the distro ruby",
            )
            return None
        return _ruby_series_for(parsed, first, pin.name)
    try:
        lines = (workspace / ".tool-versions").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        words = line.split("#", 1)[0].split()
        if len(words) >= 2 and words[0] == "ruby":
            parsed = _ruby_pin(words[1])
            if parsed is None:
                log.warning(
                    "toolchains.version_unreadable",
                    source=".tool-versions",
                    constraint=words[1],
                    hint="not a Ruby release this host reads; provisioning the distro ruby",
                )
                return None
            return _ruby_series_for(parsed, words[1], ".tool-versions")
    try:
        gemfile = (workspace / "Gemfile").read_text(encoding="utf-8")
    except OSError:
        return None
    match = _GEMFILE_RUBY.search(gemfile)
    if match is None:
        return None
    # `ruby "3.2.2"`, `ruby ">= 3.1", "< 3.4"`, `ruby "~> 3.2", engine: …`:
    # the version strings are the quoted arguments before any keyword.
    arguments = _QUOTED.findall(match.group(1).split(":", 1)[0])
    constraint = ", ".join(a.strip() for a in arguments if a.strip())
    if not constraint:
        return None  # `ruby file: ".ruby-version"` — that file was read above
    parsed = _ruby_pin(constraint)
    if parsed is not None:
        return _ruby_series_for(parsed, constraint, "Gemfile")
    spec = _gem_requirement(constraint)
    if spec is None:
        log.warning(
            "toolchains.version_unreadable",
            source="Gemfile",
            constraint=constraint,
            hint="the ruby line is not a version or requirement this host reads; "
            "provisioning the distro ruby",
        )
        return None

    def satisfies(series: str) -> bool:
        if series == RUBY_SERIES:
            return False  # which ruby the distro ships is not known host-side
        return any(spec.contains(Version(f"{series}.{patch}")) for patch in ("0", "99"))

    return _series_satisfying(
        constraint, satisfies, RUBY_SERIES, RUBY_SERIES_CANDIDATES, "Gemfile", toolchain="ruby"
    )


def _ruby_toolchain(series: str) -> Toolchain:
    # `ruby-dev` and `build-essential` are not optional in practice: gems
    # with native extensions (nokogiri, pg, ...) fail to build without
    # headers and a compiler, which is the common failure mode when only
    # `ruby` is installed. build-essential is shared with cpp and installs
    # once thanks to the pooled apt call.
    apt = ("ruby-full", "ruby-dev", "bundler", "build-essential")
    if series == RUBY_SERIES:
        return Toolchain(
            name="ruby",
            wanted="ruby, gem, bundle",
            probe=(
                "command -v ruby >/dev/null && command -v gem >/dev/null "
                "&& command -v bundle >/dev/null"
            ),
            apt_packages=apt,
            install_domains=_RUBY_INSTALL_DOMAINS,
            manifests=("Gemfile", "Rakefile", "*.gemspec"),
            aliases=("rb",),
            series=series,
            declared_series=_ruby_series_from,
            rebuild=_ruby_toolchain,
        )
    exact = series.count(".") == 2
    version = series if exact else RUBY_RELEASES[series]
    pattern = series.replace(".", "\\.") + (" " if exact else "\\.")
    ruby_matches = f'ruby -v 2>/dev/null | grep -q "^ruby {pattern}"'
    prefix = f'"$HOME/.rubies/{version}"'
    return Toolchain(
        name="ruby",
        wanted=f"ruby {version if exact else series + '.x'}, gem, bundle",
        probe=f"{ruby_matches} && command -v gem >/dev/null && command -v bundle >/dev/null",
        # ruby-build's suggested build environment for Debian/Ubuntu, on
        # top of the distro ruby (the fast path when it already matches).
        apt_packages=(
            *apt,
            "autoconf",
            "bison",
            "curl",
            "ca-certificates",
            "libssl-dev",
            "libyaml-dev",
            "libreadline-dev",
            "zlib1g-dev",
            "libgmp-dev",
            "libncurses-dev",
            "libffi-dev",
            "libgdbm-dev",
        ),
        install_script=(
            f"set -e; if ! {ruby_matches}; then "
            f'curl -fsSL -o {_RUBY_BUILD_TARBALL} "https://github.com/rbenv/ruby-build'
            f'/archive/refs/tags/v{RUBY_BUILD_VERSION}.tar.gz"; '
            f"printf '%s  {_RUBY_BUILD_TARBALL}\\n' {_RUBY_BUILD_DIGEST} "
            "| sha256sum -c - >/dev/null; "
            f"rm -rf /tmp/ruby-build; mkdir -p /tmp/ruby-build; "
            f"tar -xzf {_RUBY_BUILD_TARBALL} -C /tmp/ruby-build --strip-components=1; "
            "sudo -n env PREFIX=/usr/local sh /tmp/ruby-build/install.sh; "
            f"rm -rf /tmp/ruby-build {_RUBY_BUILD_TARBALL}; "
            # Docs are the slow, useless half of a ruby build; -j the rest.
            'MAKE_OPTS="-j$(nproc)" RUBY_CONFIGURE_OPTS=--disable-install-doc '
            f"ruby-build --verbose {version} {prefix}; "
            f"for b in {' '.join(_RUBY_TOOLS)}; do "
            f"[ -x {prefix}/bin/$b ] && sudo -n ln -sf {prefix}/bin/$b /usr/local/bin/$b || true; "
            "done; fi"
        ),
        install_domains=_RUBY_INSTALL_DOMAINS,
        manifests=("Gemfile", "Rakefile", "*.gemspec"),
        aliases=("rb",),
        series=series,
        declared_series=_ruby_series_from,
        rebuild=_ruby_toolchain,
        # A from-source build of MRI on a two-core microVM is a matter of
        # minutes, not the seconds a tarball takes.
        install_budget=1800.0,
    )


# ruby-build's archive tarball is served by codeload; the ruby tarball it
# fetches comes from cache.ruby-lang.org, and a definition may pull an
# OpenSSL release asset from GitHub when the distro's is too old.
_RUBY_INSTALL_DOMAINS = (
    "github.com",
    "codeload.github.com",
    "release-assets.githubusercontent.com",
    "cache.ruby-lang.org",
)

RUBY = _ruby_toolchain(RUBY_SERIES)


# sbx documents /etc/sandbox-persistent.sh as where persistent sandbox env
# lives, and the worker loads it at startup (see
# ``sbxloop_worker.__main__.PERSISTENT_ENV_FILE``) so the agent session and
# its shell commands inherit it. That makes it the one place a toolchain can
# export a variable the agent will actually see — `sbx exec sh -c` does not
# source login profiles, so /etc/profile.d would be invisible here.
PERSISTENT_ENV = "/etc/sandbox-persistent.sh"


def _persist_env(variable: str, value_expr: str, *, replace: bool = False) -> str:
    """Shell that appends ``export VAR=...`` to the persistent env, once.

    ``value_expr`` is substituted into the shell unquoted, so it may be a
    command substitution. The grep guard keeps a re-run from stacking
    duplicate exports; ``replace`` drops the recorded value first instead,
    for a variable whose value legitimately changes between provisions
    (``JAVA_HOME`` follows the JDK a project pins, #686).
    """
    record = (
        f'printf "export {variable}=%s\\n" "{value_expr}" '
        f"| sudo -n tee -a {PERSISTENT_ENV} >/dev/null"
    )
    if replace:
        return (
            f"sudo -n touch {PERSISTENT_ENV}; "
            f"sudo -n sed -i '/^export {variable}=/d' {PERSISTENT_ENV}; {record}"
        )
    return f"grep -qs '^export {variable}=' {PERSISTENT_ENV} || {record}"


# Pinned rather than floating on `default-jdk`, which moves between distro
# releases and would silently change the compiler under a project.
JAVA_JDK_MAJOR = "21"
# The majors a project may pin instead (#686), newest first. The default
# comes from apt; every other major is an Eclipse Temurin JDK from its
# GitHub release — the distros do not agree on which JDKs they package
# (Debian trixie has no 8 or 11, Ubuntu noble no 25), and a pinned tarball
# with a published sha256 is the same shape as every other entry here.
# `tag` is the release, `build` the version as spelled in the asset name.
_JDK_TARBALL = "/tmp/jdk.tar.gz"  # nosec B108 - path inside the sandbox VM, not host tmp
JAVA_RELEASES: dict[str, tuple[str, str, dict[str, tuple[str, str]]]] = {
    "25": (
        "jdk-25.0.4.1+1",
        "25.0.4.1_1",
        {
            "amd64": ("x64", "dbb698396d478e7fa2b1e50f4103324b2a99b90569ee27c33f2261f9215cf41e"),
            "arm64": (
                "aarch64",
                "69df11a02cfa3ef7d7ca645e03edce6778ec090e100f6ae2b42097865730ac52",
            ),
        },
    ),
    "17": (
        "jdk-17.0.20.1+1",
        "17.0.20.1_1",
        {
            "amd64": ("x64", "3808d1d15e3ec6bd5b84057fb5d84c33d8a1536a258146bcea2e603fc726e08e"),
            "arm64": (
                "aarch64",
                "457b57af8f9c93ec39080bb8c764f559dc8c89a6da1a39d718a400b7890d3e41",
            ),
        },
    ),
    "11": (
        "jdk-11.0.32.1+1",
        "11.0.32.1_1",
        {
            "amd64": ("x64", "5c3f68887c325d36d852ba534303e1f5f1f5cae7d6cc1e951d73e0d8e98a058d"),
            "arm64": (
                "aarch64",
                "f27033e6f7523c1b0b2565a78e9c0e0abe5596a854ce00ca04ec1b06ece7a935",
            ),
        },
    ),
    "8": (
        "jdk8u504-b01",
        "8u504b01",
        {
            "amd64": ("x64", "9c70e102f527ac674ac2fe9c7d47b9a04e2d19842ba5ab8e9b33f368bbadfaea"),
            "arm64": (
                "aarch64",
                "57b7ed8af9d48542bb49ff7894448040b17bea0a48b41677d11ecaec6129768d",
            ),
        },
    ),
}
JAVA_MAJOR_CANDIDATES = tuple(sorted({JAVA_JDK_MAJOR, *JAVA_RELEASES}, key=int, reverse=True))
# The launchers a pinned JDK shadows in /usr/local/bin (ahead of apt's
# /usr/bin on PATH); the default's install removes the shadows again.
_JAVA_TOOLS = ("java", "javac", "jar", "javadoc", "jshell", "keytool")
# `17`, `17.0.9`, `1.8`, `1.8.0_292`, `temurin-17.0.9+9`, `17.0.9-tem`,
# `zulu-21.30.15`: the major is the first number after any vendor prefix,
# with Java 8's legacy `1.8` spelling folded to 8.
_JAVA_MAJOR = re.compile(r"^(?:[A-Za-z][A-Za-z0-9]*-)?(?:1\.)?(\d+)")
_GRADLE_TOOLCHAIN = re.compile(r"JavaLanguageVersion\.of\(\s*['\"]?(\d+)['\"]?\s*\)")
_GRADLE_COMPATIBILITY = re.compile(
    r"(?:sourceCompatibility|targetCompatibility)\s*=?\s*"
    r"(?:JavaVersion\.VERSION_(?:1_)?(\d+)|['\"]?(?:1\.)?(\d+)(?:\.\d+)*['\"]?)"
)
_POM_RELEASE = re.compile(
    r"<(?:maven\.compiler\.(?:release|source|target)|java\.version)>\s*(?:1\.)?(\d+)"
)
_GRADLE_FILES = ("build.gradle.kts", "build.gradle")
_JAVA_MANIFESTS = (
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
)
_JAVA_ALIASES = ("jdk", "jvm")
# Gradle's foojay toolchain resolver, for a project that has its build
# fetch a JDK itself: the API answers with a redirect to the vendor's
# download (Temurin's is a GitHub release asset). The pinned JDKs come
# from the same GitHub hosts, so one list serves every major.
_JAVA_INSTALL_DOMAINS = ("api.foojay.io", "github.com", "release-assets.githubusercontent.com")


def _java_major_from(workspace: Path) -> ToolchainVersion | None:
    """The JDK major the workspace pins. ``.java-version`` (jenv),
    ``.sdkmanrc`` and ``.tool-versions`` name a JDK outright, as does a
    Gradle toolchain block or ``sourceCompatibility`` — Gradle versions
    are tied to the JDK they run on, so those are exact. A POM's
    ``maven.compiler.release`` / ``source`` / ``java.version`` is a
    *language level*: any JDK from that major up compiles it, so the
    default is kept whenever it is new enough."""
    # (source, the version text, the declaration as written)
    exact_sources: list[tuple[str, str, str]] = []
    try:
        first = (workspace / ".java-version").read_text(encoding="utf-8").strip().splitlines()[0]
    except (OSError, IndexError):
        first = ""
    if first.strip():
        exact_sources.append((".java-version", first.strip(), first.strip()))
    for name, key in ((".sdkmanrc", "java="), (".tool-versions", "java ")):
        try:
            lines = (workspace / name).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            body = line.split("#", 1)[0].strip()
            if body.startswith(key) and body[len(key) :].strip():
                exact_sources.append((name, body[len(key) :].strip(), body))
                break
    for name in _GRADLE_FILES:
        try:
            text = (workspace / name).read_text(encoding="utf-8")
        except OSError:
            continue
        match = _GRADLE_TOOLCHAIN.search(text) or _GRADLE_COMPATIBILITY.search(text)
        if match is not None:
            exact_sources.append((name, next(g for g in match.groups() if g), match.group(0)))
            break
    for source, declared, written in exact_sources:
        match = _JAVA_MAJOR.match(declared)
        if match is None:
            log.warning(
                "toolchains.version_unreadable",
                source=source,
                constraint=written,
                hint=f"not a JDK version this host reads; provisioning {JAVA_JDK_MAJOR}",
            )
            return None
        major = match.group(1)
        return _series_satisfying(
            written, major.__eq__, JAVA_JDK_MAJOR, JAVA_MAJOR_CANDIDATES, source, toolchain="java"
        )
    try:
        pom = (workspace / "pom.xml").read_text(encoding="utf-8")
    except OSError:
        return None
    match = _POM_RELEASE.search(pom)
    if match is None:
        return None
    floor = int(match.group(1))
    return _series_satisfying(
        match.group(0).strip(),
        lambda m: int(m) >= floor,
        JAVA_JDK_MAJOR,
        JAVA_MAJOR_CANDIDATES,
        "pom.xml",
        toolchain="java",
    )


def _java_toolchain(major: str) -> Toolchain:
    # JAVA_HOME is part of the contract (#161: many build tools read it
    # directly), but it lives in a file rather than this probe's env — the
    # probe runs under a bare `sbx exec sh -c`, which sources nothing. So
    # check that the export was recorded, not that it is currently set.
    # The javac on PATH has to answer to the major too: a pinned JDK
    # shadows the distro's, and either can be left over from a template.
    javac_pattern = "1\\.8\\." if major == "8" else f"{major}\\."
    javac_matches = f'javac -version 2>&1 | grep -q "^javac {javac_pattern}"'
    if major == JAVA_JDK_MAJOR:
        return Toolchain(
            name="java",
            wanted="java, javac, mvn, JAVA_HOME",
            probe=(
                f"{javac_matches} && command -v mvn >/dev/null "
                f"&& grep -qs '^export JAVA_HOME=' {PERSISTENT_ENV}"
            ),
            # apt for the JDK and Maven, per #161. Distro Gradle is
            # materially stale and most projects ship a `gradlew` wrapper
            # anyway, so Gradle is deliberately absent — the wrapper
            # fetches its own distribution, which is an egress question
            # for #141 rather than a package to install.
            apt_packages=(f"openjdk-{major}-jdk", "maven"),
            # Derive JAVA_HOME from the javac that apt just put on PATH
            # rather than hardcoding /usr/lib/jvm/... — the directory name
            # embeds the distro architecture (…-amd64 vs …-arm64) and would
            # be wrong half the time. Any pinned JDK's shadows go first, so
            # that javac is apt's.
            install_script=(
                "set -e; "
                f"sudo -n rm -f {' '.join(f'/usr/local/bin/{b}' for b in _JAVA_TOOLS)}; "
                + _persist_env(
                    "JAVA_HOME",
                    '$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")',
                    replace=True,
                )
            ),
            manifests=_JAVA_MANIFESTS,
            aliases=_JAVA_ALIASES,
            install_domains=_JAVA_INSTALL_DOMAINS,
            series=major,
            declared_series=_java_major_from,
            rebuild=_java_toolchain,
        )
    tag, build, digests = JAVA_RELEASES[major]
    home = f"/opt/jdk-{major}"
    return Toolchain(
        name="java",
        wanted=f"java {major} (Temurin), javac, mvn, JAVA_HOME",
        probe=(
            f"{javac_matches} && command -v mvn >/dev/null "
            f"&& grep -qs '^export JAVA_HOME={home}$' {PERSISTENT_ENV}"
        ),
        # Maven from apt still — it runs on whatever JAVA_HOME names.
        apt_packages=("maven", "curl", "ca-certificates"),
        install_script=(
            "set -e; " + _arch_dispatch(digests) + "; "
            f'curl -fsSL -o {_JDK_TARBALL} "https://github.com/adoptium/temurin{major}-binaries'
            f"/releases/download/{quote(tag, safe='')}"
            f'/OpenJDK{major}U-jdk_${{arch}}_linux_hotspot_{build}.tar.gz"; '
            f"printf '%s  {_JDK_TARBALL}\\n' \"$sum\" | sha256sum -c - >/dev/null; "
            f"sudo -n rm -rf {home}; sudo -n mkdir -p {home}; "
            f"sudo -n tar -xzf {_JDK_TARBALL} -C {home} --strip-components=1; "
            f"rm -f {_JDK_TARBALL}; "
            f"for b in {' '.join(_JAVA_TOOLS)}; do "
            f"[ -x {home}/bin/$b ] && sudo -n ln -sf {home}/bin/$b /usr/local/bin/$b "
            f"|| sudo -n rm -f /usr/local/bin/$b; done; "
            + _persist_env("JAVA_HOME", home, replace=True)
        ),
        manifests=_JAVA_MANIFESTS,
        aliases=_JAVA_ALIASES,
        install_domains=_JAVA_INSTALL_DOMAINS,
        series=major,
        declared_series=_java_major_from,
        rebuild=_java_toolchain,
    )


JAVA = _java_toolchain(JAVA_JDK_MAJOR)


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
    install_domains=("getcomposer.org",),
    manifests=("composer.json",),
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
# One pinned release per LTS line a project may ask for through `.nvmrc`,
# `.node-version` or `engines.node` (#627), newest first; the default major
# is the head. Digests are upstream's SHASUMS256.txt entries, like the
# default's. A major outside this table cannot be honoured — the run stops
# at resolution naming the declaration and this table (UnsatisfiablePin).
NODE_RELEASES: dict[str, tuple[str, dict[str, tuple[str, str]]]] = {
    NODE_MAJOR: (NODE_VERSION, _NODE_DIGESTS),
    "22": (
        "22.23.2",
        {
            "amd64": ("x64", "d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307"),
            "arm64": ("arm64", "fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8"),
        },
    ),
    "20": (
        "20.20.2",
        {
            "amd64": ("x64", "df770b2a6f130ed8627c9782c988fda9669fa23898329a61a871e32f965e007d"),
            "arm64": ("arm64", "73093db209e4e9e09dd7d15a47aeaab1b74833830df03efa5f942a1122c5fa71"),
        },
    ),
    "18": (
        "18.20.8",
        {
            "amd64": ("x64", "5467ee62d6af1411d46b6a10e3fb5cacc92734dbcef465fea14e7b90993001c9"),
            "arm64": ("arm64", "224e569dbe7b0ea4628ce383d9d482494b57ee040566583f1c54072c86d1116b"),
        },
    ),
}
NODE_MAJOR_CANDIDATES = tuple(NODE_RELEASES)
# nvm's `lts/<codename>` spellings for the lines above.
_NODE_LTS_CODENAMES = {"hydrogen": "18", "iron": "20", "jod": "22", "krypton": "24"}
_SEMVER_PART = re.compile(
    r"^v?(\d+|x|X|\*)(?:\.(\d+|x|X|\*))?(?:\.(\d+|x|X|\*))?(?:[-+][0-9A-Za-z.-]*)?$"
)
_SEMVER_OP = re.compile(r"^(>=|<=|>|<|=|\^|~)?\s*(.*)$")


def _semver_triple(text: str) -> tuple[list[int], int] | None:
    """``(parts, given)``: the numeric parts of a semver-ish version with
    wildcards cut off, and how many were given; None when unparseable."""
    match = _SEMVER_PART.match(text)
    if match is None:
        return None
    parts: list[int] = []
    for group in match.groups():
        if group is None or not group.isdigit():
            break
        parts.append(int(group))
    return parts, len(parts)


_SEMVER_ZERO = (0, 0, 0)


def _semver_bounds(comparator: str) -> tuple[tuple[int, ...], tuple[int, ...] | None] | None:
    """A node-semver comparator as ``(lower inclusive, upper exclusive)``,
    upper None = unbounded; None when the comparator cannot be read."""
    match = _SEMVER_OP.match(comparator.strip())
    if match is None:
        return None
    op, rest = match.group(1) or "", match.group(2).strip()
    parsed = _semver_triple(rest)
    if parsed is None:
        return None
    parts, given = parsed
    if given == 0:  # `*`, `x`, empty: anything
        return _SEMVER_ZERO, None
    pad = [0] * (3 - given)
    full = (*parts, *pad)
    # The release after the last given part: `1.2.3` → 1.2.4, `1.2` →
    # 1.3.0, `1` → 2.0.0 — what a partial version means as an upper bound.
    after = (*parts[:-1], parts[-1] + 1, *pad)
    if op in ("", "="):
        return full, after
    if op == ">=":
        return full, None
    if op == ">":
        return after, None
    if op == "<":
        return _SEMVER_ZERO, full
    if op == "<=":
        return _SEMVER_ZERO, after
    if op == "^":
        if parts[0]:
            return full, (parts[0] + 1, 0, 0)
        return full, (0, (parts[1] if given > 1 else 0) + 1, 0)
    # `~`: patch-level changes when a minor is given, else minor-level.
    return full, (parts[0], parts[1] + 1, 0) if given > 1 else (parts[0] + 1, 0, 0)


def _semver_range_admits(range_text: str, major: str) -> bool | None:
    """Whether some release of ``major`` can satisfy a node-semver range.

    Enough of node-semver for `engines.node` in the wild: ``||``
    alternatives, space-joined comparators, ``^``/``~``, ``x`` wildcards
    and hyphen ranges. Each alternative intersects to one interval; the
    major is admitted when that interval meets ``[M.0.0, M+1.0.0)``. None
    when no alternative can be read.
    """
    span = ((int(major), 0, 0), (int(major) + 1, 0, 0))
    readable = False
    for alternative in range_text.split("||"):
        text = alternative.strip()
        if " - " in text:
            low, high = text.split(" - ", 1)
            text = f">={low.strip()} <={high.strip()}"
        bounds = [_semver_bounds(c) for c in text.split() or ["*"]]
        if any(b is None for b in bounds):
            continue
        readable = True
        lower = max(b[0] for b in bounds if b is not None)
        uppers = [b[1] for b in bounds if b is not None and b[1] is not None]
        upper = min(uppers) if uppers else None
        if lower < span[1] and (upper is None or (upper > span[0] and lower < upper)):
            return True
    return False if readable else None


def _node_major_from(workspace: Path) -> ToolchainVersion | None:
    """The Node major the workspace pins: ``.nvmrc`` / ``.node-version``
    first (an exact pin), then ``package.json`` ``engines.node``."""
    for name in (".nvmrc", ".node-version"):
        try:
            first = (workspace / name).read_text(encoding="utf-8").strip().splitlines()[0]
        except (OSError, IndexError):
            continue
        first = first.strip()
        if not first:
            continue
        lowered = first.lower()
        major: str | None
        if lowered in ("node", "stable", "lts/*", "lts"):
            return ToolchainVersion(NODE_MAJOR, name, first)
        if lowered.startswith("lts/"):
            major = _NODE_LTS_CODENAMES.get(lowered[4:])
        else:
            parsed = _semver_triple(lowered)
            major = str(parsed[0][0]) if parsed is not None and parsed[1] else None
        if major is None:
            log.warning(
                "toolchains.version_unreadable",
                source=name,
                constraint=first,
                hint=f"not a Node version this host recognises; provisioning {NODE_MAJOR}",
            )
            return None
        return _series_satisfying(
            first, major.__eq__, NODE_MAJOR, NODE_MAJOR_CANDIDATES, name, toolchain="javascript"
        )
    try:
        data = json.loads((workspace / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    engines = data.get("engines") if isinstance(data, dict) else None
    constraint = engines.get("node") if isinstance(engines, dict) else None
    if not isinstance(constraint, str) or not constraint.strip():
        return None
    verdicts = {major: _semver_range_admits(constraint, major) for major in NODE_MAJOR_CANDIDATES}
    if all(v is None for v in verdicts.values()):
        log.warning(
            "toolchains.version_unreadable",
            source="package.json",
            constraint=constraint,
            hint=f"engines.node is not a semver range this host reads; provisioning {NODE_MAJOR}",
        )
        return None
    return _series_satisfying(
        constraint.strip(),
        lambda m: bool(verdicts.get(m)),
        NODE_MAJOR,
        NODE_MAJOR_CANDIDATES,
        "package.json",
        toolchain="javascript",
    )


# pnpm and yarn come through corepack, which every Node line in
# NODE_RELEASES bundles (#684): `corepack enable` puts `pnpm` and `yarn`
# shims on PATH, and the shim runs whatever version the project's
# package.json `packageManager` field pins — downloading it from the npm
# registry (always reachable) on first use, with the interactive download
# prompt switched off for the sandbox. That is the whole point of using
# corepack rather than `npm i -g pnpm`: a workspace pinning `pnpm@9.0.0`
# gets exactly that. The npm install of corepack is a fallback for a Node
# that ships without it (25+ dropped the bundle).
COREPACK_VERSION = "0.36.0"
_COREPACK_SHIMS = "/usr/local/bin"
_COREPACK_ENABLE = (
    "command -v corepack >/dev/null || "
    f"sudo -n npm install -g --no-fund --no-audit corepack@{COREPACK_VERSION}; "
    f"sudo -n corepack enable --install-directory {_COREPACK_SHIMS}; "
    + _persist_env("COREPACK_ENABLE_DOWNLOAD_PROMPT", "0")
)


def _node_toolchain(major: str) -> Toolchain:
    version, digests = NODE_RELEASES[major]
    node_present = f'node -v 2>/dev/null | grep -q "^v{major}\\."'
    return Toolchain(
        name="javascript",
        wanted=f"node {major}.x, npm, npx, pnpm, yarn (corepack shims)",
        # Check the pinned major, not merely that node exists: a template
        # carrying an older Node would otherwise satisfy the probe and
        # leave the agent with the very version #147 says breaks modern
        # `engines`. The corepack shims are part of "present" too, so a
        # template baked before #684 tops them up rather than handing a
        # pnpm workspace `pnpm: command not found`.
        probe=(
            "command -v npm >/dev/null && command -v npx >/dev/null "
            "&& command -v pnpm >/dev/null && command -v yarn >/dev/null "
            f"&& {node_present}"
        ),
        apt_packages=("curl", "ca-certificates", "xz-utils"),
        # Extracted into /usr/local with the leading directory stripped,
        # which also makes /usr/local npm's global prefix — so a later
        # `npm i -g` (the TypeScript entry, or the agent itself) lands on
        # PATH without further wiring. The three top-level doc files the
        # tarball carries are removed rather than left loose in /usr/local.
        # The tarball step is skipped when this major is already installed
        # (a top-up that only needs the shims costs no download).
        install_script=(
            f"set -e; if ! {node_present}; then " + _arch_dispatch(digests) + "; "
            f'curl -fsSL -o {_NODE_TARBALL} "https://nodejs.org/dist/v{version}'
            f'/node-v{version}-linux-$arch.tar.xz"; '
            f"printf '%s  {_NODE_TARBALL}\\n' \"$sum\" | sha256sum -c - >/dev/null; "
            f"sudo -n tar -xJf {_NODE_TARBALL} -C /usr/local --strip-components=1; "
            "sudo -n rm -f /usr/local/CHANGELOG.md /usr/local/LICENSE /usr/local/README.md; "
            f"rm -f {_NODE_TARBALL}; fi; " + _COREPACK_ENABLE
        ),
        # corepack's own fallback install and every pnpm/yarn download it
        # makes resolve through the npm registry, which is in the
        # always-reachable baseline; listed so the entry is self-describing.
        install_domains=("nodejs.org", "registry.npmjs.org"),
        manifests=("package.json",),
        aliases=("js", "node", "nodejs", "javascript-node"),
        series=major,
        declared_series=_node_major_from,
        rebuild=_node_toolchain,
    )


NODE = _node_toolchain(NODE_MAJOR)


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
    # `npm i -g typescript` resolves through the npm registry, which is in
    # the always-reachable baseline; listed so the entry is self-describing.
    install_domains=("registry.npmjs.org",),
    manifests=("tsconfig.json",),
    aliases=("ts",),
)


# bun is the one JavaScript client corepack does not carry, so it has an
# entry of its own (#684), selected by its lockfile. Installed from the npm
# registry — the `bun` package pulls the platform binary from its own
# optional dependency (`@oven/bun-linux-<arch>`), so no host beyond the
# baseline is contacted — and pinned. A project's `packageManager =
# "bun@x.y.z"` declaration selects that version instead (#627).
BUN_VERSION = "1.4.1"
_PACKAGE_MANAGER_PIN = re.compile(r"^bun@(\d+\.\d+\.\d+)")


def _bun_series_from(workspace: Path) -> ToolchainVersion | None:
    try:
        data = json.loads((workspace / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    declared = data.get("packageManager") if isinstance(data, dict) else None
    if not isinstance(declared, str):
        return None
    match = _PACKAGE_MANAGER_PIN.match(declared.strip())
    if match is None:
        return None
    return ToolchainVersion(match.group(1), "package.json", declared.strip())


def _bun_toolchain(version: str) -> Toolchain:
    return Toolchain(
        name="bun",
        wanted=f"bun {version}",
        probe=f'bun --version 2>/dev/null | grep -qx "{version}"',
        requires=("javascript",),
        install_script=(f"set -e; sudo -n npm install -g --no-fund --no-audit bun@{version}"),
        install_domains=("registry.npmjs.org",),
        manifests=("bun.lock", "bun.lockb"),
        series=version,
        declared_series=_bun_series_from,
        rebuild=_bun_toolchain,
    )


BUN = _bun_toolchain(BUN_VERSION)


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
    # go.dev/dl answers with a redirect to Google's download CDN — the same
    # trap release-assets is listed for on the Python entry.
    install_domains=("go.dev", "dl.google.com"),
    manifests=("go.mod",),
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
    install_domains=("static.rust-lang.org",),
    manifests=("Cargo.toml",),
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
# One pinned SDK per major a `global.json` may ask for (#686), newest
# first; the default major is the head. Digests are the `sha512` of the
# linux-x64 / linux-arm64 `sdk` files in each channel's release metadata
# (`builds.dotnet.microsoft.com/dotnet/release-metadata/<major>.0/releases.json`).
DOTNET_RELEASES: dict[str, tuple[str, dict[str, tuple[str, str]]]] = {
    DOTNET_SDK_MAJOR: (DOTNET_SDK_VERSION, _DOTNET_DIGESTS),
    "9": (
        "9.0.317",
        {
            "amd64": (
                "x64",
                "145bf69dcb88c4b905feb531cfdd7894a75fc875d2a030e958a13d1fb1131521"
                "c8cebd8a8a6e0fbd1a433ebae9cde86356b6adad07b1ad81efb92b36ff8a3333",
            ),
            "arm64": (
                "arm64",
                "fdf30fe705c91304d890115e955f738055f8c0885ea9891e7df1153321120fa2"
                "c38b6ae4dd132f871cb8facc0d1fabbd2b25ddd53d0a5b4293aa85d296e3b98d",
            ),
        },
    ),
    "8": (
        "8.0.424",
        {
            "amd64": (
                "x64",
                "6503fd9f464d5e3a4f43a881d2b74afc6a2c46ceda74d027f1565b7239f4b3ec"
                "884857c03c0dcd49eb52f384d5ae1fa5aaf135f0a6aabc5518103aceed643c74",
            ),
            "arm64": (
                "arm64",
                "bb19b6779ad93d146055583d644ef269bb42501f6c7fdef51e14026cde9d5fd7"
                "26d370de098a8d8504867fb24bfcb5ab88cc22bec812461aede334de1aacf7b6",
            ),
        },
    ),
}
DOTNET_MAJOR_CANDIDATES = tuple(DOTNET_RELEASES)
# `global.json` `sdk.version` is a full `x.y.znn` — the SDK rejects
# anything shorter — where `z` is the feature band and `nn` the patch.
_DOTNET_SDK_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d)(\d\d)$")
# `rollForward` policies as the number of leading (major, minor, band,
# patch) components an installed SDK must share with the pin; it must be
# at least the pin in every policy but `disable`, which is exact. The
# `latest*` spellings differ from the plain ones only in which of several
# installed SDKs wins — moot with one SDK per sandbox. The SDK's default
# when `version` is set is `patch`.
_DOTNET_ROLL_FORWARD = {
    "disable": 4,
    "patch": 3,
    "latestPatch": 3,
    "feature": 2,
    "latestFeature": 2,
    "minor": 1,
    "latestMinor": 1,
    "major": 0,
    "latestMajor": 0,
}
_JSON_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def _dotnet_sdk_tuple(version: str) -> tuple[int, int, int, int] | None:
    match = _DOTNET_SDK_VERSION.match(version)
    if match is None:
        return None
    major, minor, band, patch = (int(g) for g in match.groups())
    return major, minor, band, patch


def _dotnet_series_from(workspace: Path) -> ToolchainVersion | None:
    """The .NET SDK major the workspace's ``global.json`` admits, read the
    way the SDK itself reads it: the pinned ``sdk.version`` plus its
    ``rollForward`` policy decide which installed SDK is acceptable, and
    an SDK outside the policy makes ``dotnet`` refuse to run at all."""
    try:
        text = (workspace / "global.json").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        try:  # the SDK tolerates JavaScript-style comments here
            data = json.loads(_JSON_COMMENT.sub("", text))
        except ValueError:
            log.warning(
                "toolchains.version_unreadable",
                source="global.json",
                constraint=text.strip()[:80],
                hint=f"not JSON; provisioning the default SDK {DOTNET_SDK_VERSION}",
            )
            return None
    sdk = data.get("sdk") if isinstance(data, dict) else None
    if not isinstance(sdk, dict):
        return None
    version = sdk.get("version")
    if not isinstance(version, str) or not version.strip():
        return None
    pin = _dotnet_sdk_tuple(version.strip())
    policy = sdk.get("rollForward", "patch")
    shared = _DOTNET_ROLL_FORWARD.get(policy) if isinstance(policy, str) else None
    if pin is None or shared is None:
        log.warning(
            "toolchains.version_unreadable",
            source="global.json",
            constraint=version.strip(),
            hint=f"sdk.version must be a full x.y.znn and rollForward one of "
            f"{', '.join(_DOTNET_ROLL_FORWARD)}; provisioning the default SDK "
            f"{DOTNET_SDK_VERSION}",
        )
        return None
    constraint = version.strip() + (f" rollForward={policy}" if "rollForward" in sdk else "")

    def admits(major: str) -> bool:
        installed = _dotnet_sdk_tuple(DOTNET_RELEASES[major][0])
        assert installed is not None
        return installed[:shared] == pin[:shared] and installed >= pin

    return _series_satisfying(
        constraint,
        admits,
        DOTNET_SDK_MAJOR,
        DOTNET_MAJOR_CANDIDATES,
        "global.json",
        toolchain="dotnet",
        hint="the installable SDKs are "
        + ", ".join(release for release, _ in DOTNET_RELEASES.values())
        + "; rollForward=latestFeature admits any later feature band of the pinned major",
    )


def _dotnet_toolchain(major: str) -> Toolchain:
    version, digests = DOTNET_RELEASES[major]
    return Toolchain(
        name="dotnet",
        wanted=f"dotnet SDK {version}, DOTNET_ROOT",
        # The pinned SDK itself — `--list-sdks` prints `<version> [<dir>]` —
        # plus the DOTNET_ROOT export: a manual (non-package) SDK install
        # is only half-done without it, and like JAVA_HOME it lives in a
        # file this probe's bare shell never sources. The exact version
        # rather than the major: a `global.json` rolls forward within a
        # feature band, so a template's other 8.0.x need not satisfy it.
        probe=(
            f'dotnet --list-sdks 2>/dev/null | grep -q "^{version.replace(".", "\\.")} " '
            f"&& grep -qs '^export DOTNET_ROOT=' {PERSISTENT_ENV}"
        ),
        # libicu-dev is the version-agnostic way to get the ICU runtime the
        # SDK needs for globalization; naming libicuNN directly would pin
        # us to one Ubuntu release.
        apt_packages=("curl", "ca-certificates", "libicu-dev"),
        install_script=(
            "set -e; " + _arch_dispatch(digests) + "; "
            f'curl -fsSL -o {_DOTNET_TARBALL} "https://builds.dotnet.microsoft.com/dotnet'
            f'/Sdk/{version}/dotnet-sdk-{version}-linux-$arch.tar.gz"; '
            f"printf '%s  {_DOTNET_TARBALL}\\n' \"$sum\" | sha512sum -c - >/dev/null; "
            f"sudo -n rm -rf {DOTNET_ROOT}; sudo -n mkdir -p {DOTNET_ROOT}; "
            f"sudo -n tar -xzf {_DOTNET_TARBALL} -C {DOTNET_ROOT}; "
            f"sudo -n ln -sf {DOTNET_ROOT}/dotnet /usr/local/bin/dotnet; "
            + _persist_env("DOTNET_ROOT", DOTNET_ROOT)
            + "; "
            # Telemetry would only ever be a blocked outbound request under
            # a default-deny egress policy; opting out keeps the noise out
            # of the agent's build logs.
            + _persist_env("DOTNET_CLI_TELEMETRY_OPTOUT", "1")
            + "; "
            f"rm -f {_DOTNET_TARBALL}"
        ),
        install_domains=("builds.dotnet.microsoft.com",),
        manifests=("global.json", "Directory.Build.props", "*.sln", "*.csproj", "*.fsproj"),
        aliases=("csharp", "c#", "net", "dotnet-sdk"),
        series=major,
        declared_series=_dotnet_series_from,
        rebuild=_dotnet_toolchain,
    )


DOTNET = _dotnet_toolchain(DOTNET_SDK_MAJOR)


# Registry order is the install order, and the order packages appear in the
# batched apt call — keep it stable so the command is reproducible. New
# languages append; nothing depends on the position of an existing entry.
# Task runners (#685). The gate detector emits `make check` / `just check` /
# `task check` for a repository fronted by one, and "any sandbox has a task
# runner" was never true: `make` only arrived with build-essential (the
# cpp, ruby and rust entries), `just` and `task` never did. So each is a
# toolchain the manifest selects — a Makefile in a Go repo provisions make
# — and the detector names it (`GateDetector.language`), so the existing
# "emit only what the resolved set can run" rule applies. A runner is a
# runner: selection is by manifest, like every other entry, not by whether
# the file happens to declare a gate target — the agent runs `make build`
# too, and under default-deny egress it could not fetch `just` itself.
MAKE = Toolchain(
    name="make",
    wanted="make",
    probe="command -v make >/dev/null",
    apt_packages=("make",),
    # GNU make's own search order, so the case variants count too.
    manifests=("GNUmakefile", "makefile", "Makefile"),
    aliases=("gnumake",),
)

# Pinned musl builds from the project's GitHub releases; github.com answers
# with a redirect to release-assets, the same pair the Python entry lists.
# The tarball is flat (the binary beside docs and completions), so only the
# binary is extracted.
JUST_VERSION = "1.58.0"
_JUST_TARBALL = "/tmp/just.tar.gz"  # nosec B108 - path inside the sandbox VM, not host tmp
_JUST_DIGESTS = {
    "amd64": ("x86_64", "4a5cc2f53e6f0f8c59092a6cc38291eb729d46a7dd95d3ae582008881b84931d"),
    "arm64": ("aarch64", "748237128c4c40cbdabc65e841d05ceba13cc23a91eaba395495894c1d9764df"),
}

JUST = Toolchain(
    name="just",
    wanted=f"just {JUST_VERSION}",
    probe="command -v just >/dev/null",
    apt_packages=("curl", "ca-certificates"),
    install_script=(
        "set -e; " + _arch_dispatch(_JUST_DIGESTS) + "; "
        f'curl -fsSL -o {_JUST_TARBALL} "https://github.com/casey/just/releases/download/'
        f'{JUST_VERSION}/just-{JUST_VERSION}-$arch-unknown-linux-musl.tar.gz"; '
        f"printf '%s  {_JUST_TARBALL}\\n' \"$sum\" | sha256sum -c - >/dev/null; "
        f"sudo -n tar -xzf {_JUST_TARBALL} -C /usr/local/bin just; "
        f"rm -f {_JUST_TARBALL}"
    ),
    install_domains=("github.com", "release-assets.githubusercontent.com"),
    manifests=("justfile", ".justfile", "Justfile"),
)

TASK_VERSION = "3.53.1"
_TASK_TARBALL = "/tmp/task.tar.gz"  # nosec B108 - path inside the sandbox VM, not host tmp
_TASK_DIGESTS = {
    "amd64": ("amd64", "a54a408f6861ff921f6e87774180db31bacd8c1e7c944ca696db9fea49a82fc7"),
    "arm64": ("arm64", "e3ad19101493a0112e1f22ae8ccc54bf03e533b1076a0ca1e6c782a09ad2e588"),
}

TASK = Toolchain(
    name="task",
    wanted=f"task (go-task) {TASK_VERSION}",
    probe="command -v task >/dev/null",
    apt_packages=("curl", "ca-certificates"),
    install_script=(
        "set -e; " + _arch_dispatch(_TASK_DIGESTS) + "; "
        f'curl -fsSL -o {_TASK_TARBALL} "https://github.com/go-task/task/releases/download/'
        f'v{TASK_VERSION}/task_linux_$arch.tar.gz"; '
        f"printf '%s  {_TASK_TARBALL}\\n' \"$sum\" | sha256sum -c - >/dev/null; "
        f"sudo -n tar -xzf {_TASK_TARBALL} -C /usr/local/bin task; "
        f"rm -f {_TASK_TARBALL}"
    ),
    install_domains=("github.com", "release-assets.githubusercontent.com"),
    manifests=("Taskfile.yml", "Taskfile.yaml"),
    aliases=("go-task", "taskfile"),
)


TOOLCHAINS: tuple[Toolchain, ...] = (
    PYTHON,
    CPP,
    RUBY,
    JAVA,
    PHP,
    NODE,
    TYPESCRIPT,
    BUN,
    GO,
    RUST,
    DOTNET,
    MAKE,
    JUST,
    TASK,
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
    install_domains=("registry.npmjs.org",),
)

# What a run provisions when `[sandbox] languages` is unset AND the
# workspace declares nothing detect_languages recognizes. Python has had
# this head start since 0.4.0; since #624 it is the last resort rather than
# the default — a workspace with a go.mod gets Go, not Python — and an
# explicit `languages` REPLACES both, so nothing is hardcoded as privileged
# once an operator has an opinion.
DEFAULT_LANGUAGES: tuple[str, ...] = ("python",)

# Subdirectory names detect_languages never descends into: dependency and
# tool trees carry other ecosystems' manifests (a Python repo's node_modules
# is full of package.json), and dot-directories are tooling, not project.
_SKIP_DIRS = frozenset({"node_modules", "vendor", "third_party", "site-packages"})

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


def resolve(
    names: Sequence[str], versions: Mapping[str, ToolchainVersion] | None = None
) -> tuple[Toolchain, ...]:
    """Map selected language names onto toolchains, deduped, registry order.

    Unknown names are dropped rather than raised on: config validation is
    where a typo gets rejected with a helpful message, and the ensure must
    never be the thing that fails a run. ``versions`` — a run's per-project
    series (:attr:`LanguageResolution.versions`) — rebuilds the entries it
    names for that series; without it every entry is its default.
    """
    wanted: set[str] = set()
    pending = [normalize_language(name) for name in names]
    while pending:
        key = pending.pop()
        if key is None or key in wanted:
            continue
        wanted.add(key)
        pending.extend(_BY_KEY[key].requires)
    selected = (toolchain for toolchain in TOOLCHAINS if toolchain.name in wanted)
    if not versions:
        return tuple(selected)
    return tuple(
        tc.for_version(versions[tc.name].series) if tc.name in versions else tc for tc in selected
    )


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


def install_domains(toolchains: Iterable[Toolchain]) -> tuple[str, ...]:
    """The union of installer hosts for ``toolchains``, deduped, order-stable."""
    domains: list[str] = []
    for toolchain in toolchains:
        for domain in toolchain.install_domains:
            if domain not in domains:
                domains.append(domain)
    return tuple(domains)


def _manifest_matches(name: str, manifests: Iterable[str]) -> bool:
    for pattern in manifests:
        if pattern.startswith("*."):
            if name.endswith(pattern[1:]):
                return True
        elif name == pattern:
            return True
    return False


# How far below the root detect_languages reads. Two levels, not a full
# walk: monorepos put their packages at `packages/<name>/` or `apps/<name>/`
# (a pnpm workspace's tsconfig.json lives there, not at the root), while
# descending further starts reading fixtures and dependency trees — the
# false positives outweigh the finds.
_DETECT_DEPTH = 2


def _candidate_files(workspace: Path) -> list[str]:
    """Names of the files at the workspace root and up to ``_DETECT_DEPTH``
    levels down, skipping dot-directories and dependency trees.

    A workspace that does not exist yields nothing rather than raising: the
    per-run dir of a workspace-less run is legitimately empty.
    """
    names: list[str] = []
    pending = [(workspace, 0)]
    while pending:
        directory, depth = pending.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_file():
                names.append(entry.name)
            elif (
                depth < _DETECT_DEPTH
                and entry.is_dir()
                and not entry.name.startswith(".")
                and entry.name not in _SKIP_DIRS
            ):
                pending.append((entry, depth + 1))
    return names


def detect_languages(workspace: Path) -> dict[str, tuple[str, ...]]:
    """Which registry languages ``workspace`` declares, and on what evidence.

    Pure filesystem read, no process spawn, bounded at ``_DETECT_DEPTH``
    levels below the root. Returns ``{language: signals}`` in registry
    order — the signals being the manifest names that matched, so the run
    event can say *why* Go was provisioned. Union, not "best guess": a repo
    with both ``pyproject.toml`` and ``package.json`` needs both toolchains,
    and provisioning an extra one costs seconds while missing one costs
    revision budget.
    """
    names = _candidate_files(workspace)
    found: dict[str, tuple[str, ...]] = {}
    for toolchain in TOOLCHAINS:
        signals = tuple(
            sorted({name for name in names if _manifest_matches(name, toolchain.manifests)})
        )
        if signals:
            found[toolchain.name] = signals
    return found


LanguageSource = Literal["config", "detected", "default"]


class LanguageResolution(NamedTuple):
    """The language set one run provisions, and where it came from.

    ``source`` is what the ``sandbox.languages`` event reports: an operator
    reading the run log can tell an explicit choice from an inference from
    the fallback, which is the difference between "I asked for this" and
    "sbxloop guessed" when the toolchain turns out wrong.
    """

    languages: tuple[str, ...]
    source: LanguageSource
    # manifest names per detected language — empty unless source is
    # "detected"
    signals: dict[str, tuple[str, ...]]
    # the version series each versioned toolchain in the set provisions,
    # by toolchain name — the workspace's own pin where it has one the
    # host can honour, else the default (#627); what `resolve(languages,
    # versions)` rebuilds the entries for
    versions: Mapping[str, ToolchainVersion] = MappingProxyType({})


def toolchain_versions(
    languages: Sequence[str], workspace: Path | None
) -> dict[str, ToolchainVersion]:
    """Each versioned toolchain in ``languages`` (requirements included)
    and the series ``workspace`` picks for it — see
    :meth:`Toolchain.version_from`."""
    versions: dict[str, ToolchainVersion] = {}
    for toolchain in resolve(languages):
        version = toolchain.version_from(workspace)
        if version is not None:
            versions[toolchain.name] = version
    return versions


def resolve_languages(explicit: Sequence[str], workspace: Path | None) -> LanguageResolution:
    """Explicit ``[sandbox] languages`` → workspace detection → the default.

    Explicit wins outright (an operator's opinion is never second-guessed
    by a manifest); detection fires only when nothing is configured; the
    default applies only when nothing was detected either. The result is
    the ONE language set every consumer — egress allowlist, toolchain
    install, verify-command lint — reads, resolved once per run (#624).
    The workspace's version pins are read for whichever set won: an
    operator choosing the language does not choose to ignore the project's
    `requires-python` (#627).
    """
    source: LanguageSource
    if explicit:
        languages, source, signals = tuple(explicit), "config", {}
    else:
        detected = detect_languages(workspace) if workspace is not None else {}
        if detected:
            languages, source, signals = tuple(detected), "detected", detected
        else:
            languages, source, signals = DEFAULT_LANGUAGES, "default", {}
    return LanguageResolution(languages, source, signals, toolchain_versions(languages, workspace))
