"""Registry invariants for the language toolchains (issue #140).

These guard the properties every Layer 1 sub-issue relies on rather than any
one language's package list: names are unambiguous, resolution is order-stable
and never raises, and apt packages pool instead of duplicating.
"""

from __future__ import annotations

import re
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


def test_php_probes_extensions_not_just_the_interpreter() -> None:
    # A bare php-cli passes `command -v php` and then fails the moment a
    # project or Composer itself needs mbstring or zip (#167).
    php = toolchains.resolve(["php"])[0]
    for extension in ("mbstring", "curl", "zip", "dom"):
        assert extension in php.probe
    for package in ("php-cli", "php-mbstring", "php-xml", "php-curl", "php-zip"):
        assert package in php.apt_packages


def test_php_composer_is_pinned_and_checksum_verified() -> None:
    # #167 asks for a pinned, verified download rather than piping the
    # installer into the interpreter.
    php = toolchains.resolve(["php"])[0]
    assert php.install_script is not None
    assert toolchains.COMPOSER_VERSION in php.install_script
    assert toolchains.COMPOSER_SHA256 in php.install_script
    assert "sha256sum -c" in php.install_script
    assert "| php" not in php.install_script and "| sudo php" not in php.install_script
    assert len(toolchains.COMPOSER_SHA256) == 64


def test_installer_entries_bring_their_own_curl() -> None:
    # install_script runs after the apt batch specifically so it can rely
    # on curl/ca-certificates; an entry that curls without asking for them
    # would work only by luck of the base image.
    for toolchain in toolchains.TOOLCHAINS:
        if toolchain.install_script and "curl" in toolchain.install_script:
            assert "curl" in toolchain.apt_packages, toolchain.name
            assert "ca-certificates" in toolchain.apt_packages, toolchain.name


@pytest.mark.parametrize("name", ["javascript", "js", "node", "nodejs", "JavaScript-Node"])
def test_node_is_reachable_by_every_spelling(name: str) -> None:
    assert toolchains.normalize_language(name) == "javascript"


def test_node_probe_checks_the_pinned_major() -> None:
    # A template carrying an older Node would satisfy a bare `command -v
    # node` and leave the agent with the version #147 says breaks modern
    # `engines` constraints.
    node = toolchains.resolve(["javascript"])[0]
    assert f'grep -q "^v{toolchains.NODE_MAJOR}\\.' in node.probe


def test_node_pins_a_verified_digest_per_architecture() -> None:
    # The fleet is mixed (arm64 microVMs, amd64 CI), so one hardcoded
    # digest would fail the checksum on half the hosts.
    node = toolchains.resolve(["javascript"])[0]
    assert node.install_script is not None
    assert toolchains.NODE_VERSION in node.install_script
    for deb_arch, (upstream, digest) in toolchains._NODE_DIGESTS.items():
        assert f"{deb_arch}) arch={upstream}" in node.install_script
        assert len(digest) == 64
        assert digest in node.install_script
    assert "sha256sum -c" in node.install_script


def test_arch_dispatch_rejects_unknown_architectures() -> None:
    # Downloading "whatever" for an unrecognized arch would produce a
    # binary that cannot run; fail loudly instead.
    script = toolchains._arch_dispatch({"amd64": ("x64", "d" * 64)})
    ok = subprocess.run(
        ["sh", "-c", script.replace("$(dpkg --print-architecture)", "amd64")],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0, ok.stderr
    bad = subprocess.run(
        ["sh", "-c", script.replace("$(dpkg --print-architecture)", "riscv64")],
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 1
    assert "unsupported architecture" in bad.stderr


def test_typescript_pulls_in_the_node_runtime_first() -> None:
    # #150: resolve the Node runtime in the JS entry and treat this as the
    # tsc layer on top. Order matters — `npm i -g typescript` is meaningless
    # before node exists.
    resolved = [toolchain.name for toolchain in toolchains.resolve(["typescript"])]
    assert resolved == ["javascript", "typescript"]


def test_requires_are_canonical_and_resolvable() -> None:
    names = {toolchain.name for toolchain in toolchains.TOOLCHAINS}
    for toolchain in toolchains.TOOLCHAINS:
        for required in toolchain.requires:
            assert required in names, f"{toolchain.name} requires unknown {required}"
            assert toolchains.normalize_language(required) == required


def test_requires_are_installed_before_their_dependents() -> None:
    # Registry order IS install order, so a requirement appearing later in
    # the tuple would silently install after the entry that needs it.
    position = {toolchain.name: i for i, toolchain in enumerate(toolchains.TOOLCHAINS)}
    for toolchain in toolchains.TOOLCHAINS:
        for required in toolchain.requires:
            assert position[required] < position[toolchain.name], (
                f"{required} must precede {toolchain.name} in TOOLCHAINS"
            )


def test_typescript_install_is_pinned() -> None:
    typescript = toolchains.resolve(["typescript"])[1]
    assert typescript.install_script is not None
    assert f"typescript@{toolchains.TYPESCRIPT_VERSION}" in typescript.install_script


def test_go_pins_a_verified_digest_per_architecture() -> None:
    go = toolchains.resolve(["go"])[0]
    assert go.install_script is not None
    assert toolchains.GO_VERSION in go.install_script
    for deb_arch, (upstream, digest) in toolchains._GO_DIGESTS.items():
        assert f"{deb_arch}) arch={upstream}" in go.install_script
        assert len(digest) == 64
        assert digest in go.install_script
    assert "sha256sum -c" in go.install_script


def test_go_replaces_rather_than_overlays_a_previous_install() -> None:
    # Extracting over an existing /usr/local/go mixes two versions into a
    # broken tree; upstream's own instructions say to remove it first.
    go = toolchains.resolve(["go"])[0]
    assert go.install_script is not None
    assert "rm -rf /usr/local/go" in go.install_script


def test_go_does_not_pin_gotoolchain_local() -> None:
    # Deliberate (#153): GOTOOLCHAIN=local would fail outright on a project
    # whose go.mod demands a newer Go — the exact distro-lag failure this
    # entry exists to avoid.
    go = toolchains.resolve(["go"])[0]
    assert go.install_script is not None
    assert "GOTOOLCHAIN" not in go.install_script


@pytest.mark.parametrize("toolchain", toolchains.TOOLCHAINS, ids=lambda t: t.name)
def test_probe_and_install_script_are_valid_shell(toolchain: toolchains.Toolchain) -> None:
    """Every entry is shell that ``sh -c`` must be able to parse.

    These strings are assembled with f-strings and nested quoting, and a
    syntax error would only ever surface in-sandbox as a confusing warning
    on somebody's real run. ``sh -n`` parses without executing.
    """
    for label, script in (("probe", toolchain.probe), ("install", toolchain.install_script)):
        if script is None:
            continue
        result = subprocess.run(["sh", "-n", "-c", script], capture_output=True, text=True)
        assert result.returncode == 0, f"{toolchain.name} {label}: {result.stderr}"


@pytest.mark.parametrize("toolchain", toolchains.TOOLCHAINS, ids=lambda t: t.name)
def test_downloads_are_checksum_verified(toolchain: toolchains.Toolchain) -> None:
    # Anything fetched from the network must be pinned and verified — no
    # curl-into-a-shell, no unchecked tarball.
    script = toolchain.install_script
    if script is None or "curl" not in script:
        return
    verified = "sha256sum -c" in script or "sha512sum -c" in script
    assert verified, f"{toolchain.name} downloads without verifying"
    # A real pipe-into-a-shell, not the `| sha256sum` that does the
    # verifying — hence the word boundary.
    piped = re.search(r"\|\s*(sudo\s+(-\S+\s+)*)?(sh|bash|python3?|php)\b", script)
    assert piped is None, f"{toolchain.name} pipes a download into an interpreter: {piped}"


def test_rust_installs_rustfmt_and_clippy_without_the_docs_profile() -> None:
    # #143: minimal profile unless clippy/rustfmt are needed — they are, so
    # request them explicitly rather than pulling the whole default profile.
    rust = toolchains.resolve(["rust"])[0]
    assert rust.install_script is not None
    assert "--profile minimal" in rust.install_script
    assert "--component rustfmt" in rust.install_script
    assert "--component clippy" in rust.install_script
    assert f"--default-toolchain {toolchains.RUST_TOOLCHAIN}" in rust.install_script


def test_rust_does_not_rely_on_shell_profiles_for_path() -> None:
    # rustup's default PATH wiring edits shell profiles, which a bare
    # `sbx exec sh -c` never sources.
    rust = toolchains.resolve(["rust"])[0]
    assert rust.install_script is not None
    assert "--no-modify-path" in rust.install_script
    assert "/usr/local/bin" in rust.install_script


@pytest.mark.parametrize("name", ["dotnet", "csharp", "c#", "net", "dotnet-sdk"])
def test_dotnet_is_reachable_by_every_spelling(name: str) -> None:
    assert toolchains.normalize_language(name) == "dotnet"


def test_dotnet_pins_the_sdk_major_and_verifies_sha512() -> None:
    # The .NET feed publishes sha512, not sha256 (#164 pins the SDK major
    # because a project's global.json can demand an exact SDK).
    dotnet = toolchains.resolve(["dotnet"])[0]
    assert dotnet.install_script is not None
    assert toolchains.DOTNET_SDK_VERSION in dotnet.install_script
    assert "sha512sum -c" in dotnet.install_script
    assert f'grep -q "^{toolchains.DOTNET_SDK_MAJOR}\\.' in dotnet.probe
    for _deb_arch, (_upstream, digest) in toolchains._DOTNET_DIGESTS.items():
        assert len(digest) == 128, "sha512 digests are 128 hex chars"
        assert digest in dotnet.install_script


def test_dotnet_records_root_and_opts_out_of_telemetry() -> None:
    # A manual SDK install is only half-done without DOTNET_ROOT, and
    # telemetry under default-deny egress is just a blocked request.
    dotnet = toolchains.resolve(["dotnet"])[0]
    assert dotnet.install_script is not None
    assert "export DOTNET_ROOT=" in dotnet.install_script
    assert "DOTNET_CLI_TELEMETRY_OPTOUT" in dotnet.install_script
    assert toolchains.PERSISTENT_ENV in dotnet.probe


def test_every_language_in_the_agreed_set_is_registered() -> None:
    # The 10-language set from #140. A missing entry means a sub-issue
    # silently regressed out of the registry.
    assert set(toolchains.supported_languages()) == {
        "python",
        "cpp",
        "ruby",
        "java",
        "php",
        "javascript",
        "typescript",
        "go",
        "rust",
        "dotnet",
    }


def test_python_entry_matches_the_pre_140_behavior() -> None:
    python = toolchains.resolve(["python"])[0]
    assert python.apt_packages == ("python3-venv", "python3-pip")
    assert "ensurepip" in python.probe


def test_git_is_baseline_tooling_not_a_selectable_language() -> None:
    # #252: git is provisioned on every agent sandbox regardless of
    # `[sandbox] languages`, so it must not be something an operator can
    # (or needs to) select — and must not leak into `supported_languages`.
    assert toolchains.GIT in toolchains.BASELINE_TOOLS
    assert toolchains.GIT not in toolchains.TOOLCHAINS
    assert toolchains.normalize_language("git") is None
    assert "git" not in toolchains.supported_languages()


def test_baseline_tools_have_a_probe_and_an_apt_path() -> None:
    # Baseline tooling rides the pooled apt call; an installer-only entry
    # here would add a round trip to every provision.
    for tool in toolchains.BASELINE_TOOLS:
        assert tool.probe.strip(), tool.name
        assert tool.apt_packages, tool.name
        assert tool.install_script is None, tool.name
    assert "git" in toolchains.apt_packages(toolchains.BASELINE_TOOLS)
