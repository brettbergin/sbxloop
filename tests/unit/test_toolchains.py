"""Registry invariants for the language toolchains (issue #140).

These guard the properties every Layer 1 sub-issue relies on rather than any
one language's package list: names are unambiguous, resolution is order-stable
and never raises, and apt packages pool instead of duplicating.
"""

from __future__ import annotations

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


def test_python_entry_matches_the_pre_140_behavior() -> None:
    python = toolchains.resolve(["python"])[0]
    assert python.apt_packages == ("python3-venv", "python3-pip")
    assert "ensurepip" in python.probe
