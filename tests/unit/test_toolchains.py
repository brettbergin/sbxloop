"""Registry invariants for the language toolchains (issue #140).

These guard the properties every Layer 1 sub-issue relies on rather than any
one language's package list: names are unambiguous, resolution is order-stable
and never raises, and apt packages pool instead of duplicating.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sbxloop import toolchains


def test_names_and_aliases_are_unique() -> None:
    keys: list[str] = []
    for toolchain in toolchains.TOOLCHAINS:
        keys.extend((toolchain.name, *toolchain.aliases))
    assert len(keys) == len(set(keys)), "a name/alias is claimed by two toolchains"


def test_every_toolchain_has_a_probe_and_an_install_path() -> None:
    # Probe-first is the contract: an entry with no probe would install on
    # every run, and one with no install path would be a silent no-op.
    for toolchain in toolchains.TOOLCHAINS:
        assert toolchain.probe.strip(), toolchain.name
        assert toolchain.wanted.strip(), toolchain.name
        assert toolchain.apt_packages or toolchain.install_script, toolchain.name


def test_default_languages_are_registered() -> None:
    for name in toolchains.DEFAULT_LANGUAGES:
        assert toolchains.normalize_language(name) == name


@pytest.mark.parametrize("name", ["python", "Python", " PY ", "python3"])
def test_normalize_language_accepts_aliases_and_case(name: str) -> None:
    assert toolchains.normalize_language(name) == "python"


def test_normalize_language_rejects_unknown() -> None:
    assert toolchains.normalize_language("cobol") is None


def test_resolve_is_deduped_and_registry_ordered() -> None:
    names = [toolchain.name for toolchain in toolchains.TOOLCHAINS]
    resolved = toolchains.resolve([*reversed(names), *names])
    assert [toolchain.name for toolchain in resolved] == names


def test_resolve_drops_unknown_without_raising() -> None:
    # Config validation rejects typos; the ensure must never fail a run.
    assert toolchains.resolve(["cobol"]) == ()
    assert [t.name for t in toolchains.resolve(["cobol", "python"])] == ["python"]


def test_apt_packages_pools_without_duplicates() -> None:
    packages = toolchains.apt_packages(toolchains.TOOLCHAINS)
    assert len(packages) == len(set(packages))
    for toolchain in toolchains.TOOLCHAINS:
        assert set(toolchain.apt_packages) <= set(packages)


@pytest.mark.parametrize("name", ["cpp", "c", "C++", "cxx", "c-cpp"])
def test_cpp_is_reachable_by_every_spelling(name: str) -> None:
    assert toolchains.normalize_language(name) == "cpp"


def test_cpp_entry_is_pure_apt() -> None:
    # #170's whole claim is that C/C++ needs no installer and no new egress.
    cpp = toolchains.resolve(["cpp"])[0]
    assert cpp.install_script is None
    assert "build-essential" in cpp.apt_packages
    assert "cmake" in cpp.apt_packages


def test_ruby_installs_native_extension_prerequisites() -> None:
    # Only `ruby` is the classic half-install: gems with C extensions then
    # fail to build. #158 calls this out explicitly.
    ruby = toolchains.resolve(["ruby"])[0]
    assert ruby.install_script is None
    for package in ("ruby-full", "ruby-dev", "bundler", "build-essential"):
        assert package in ruby.apt_packages


def test_ruby_and_cpp_share_build_essential_without_duplicating_it() -> None:
    packages = toolchains.apt_packages(toolchains.resolve(["ruby", "cpp"]))
    assert packages.count("build-essential") == 1


def test_java_pins_the_jdk_major_rather_than_default_jdk() -> None:
    # `default-jdk` moves between distro releases and would silently change
    # the compiler under a project (#161).
    java = toolchains.resolve(["java"])[0]
    assert f"openjdk-{toolchains.JAVA_JDK_MAJOR}-jdk" in java.apt_packages
    assert "default-jdk" not in java.apt_packages
    assert "maven" in java.apt_packages


def test_java_records_java_home_in_the_persistent_env() -> None:
    # JAVA_HOME must survive into the agent's env, and the probe has to
    # notice when it was never recorded — a bare `sh -c` sources nothing,
    # so the probe checks the file rather than the variable.
    java = toolchains.resolve(["java"])[0]
    assert java.install_script is not None
    assert toolchains.PERSISTENT_ENV in java.install_script
    assert "JAVA_HOME" in java.probe
    assert toolchains.PERSISTENT_ENV in java.probe


def test_persist_env_is_idempotent(tmp_path: Path) -> None:
    # Running the export twice must not stack duplicate lines.
    target = tmp_path / "persistent.sh"
    script = toolchains._persist_env("DEMO_HOME", "/opt/demo").replace(
        toolchains.PERSISTENT_ENV, str(target)
    )
    script = script.replace("sudo -n tee", "tee")
    for _ in range(2):
        subprocess.run(["sh", "-c", script], check=True)
    assert target.read_text() == "export DEMO_HOME=/opt/demo\n"


def test_python_entry_matches_the_pre_140_behavior() -> None:
    python = toolchains.resolve(["python"])[0]
    assert python.apt_packages == ("python3-venv", "python3-pip")
    assert "ensurepip" in python.probe
