"""Registry invariants for the language toolchains (issue #140).

These guard the properties every Layer 1 sub-issue relies on rather than any
one language's package list: names are unambiguous, resolution is order-stable
and never raises, and apt packages pool instead of duplicating.
"""

from __future__ import annotations

import json
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
        "bun",
        "go",
        "rust",
        "dotnet",
        "make",
        "just",
        "task",
    }


def test_python_entry_keeps_the_pre_140_venv_heal() -> None:
    # The 0.4.0 field failure (`ensurepip is not available`) is still the
    # first thing this entry guards; #250 adds to it, never replaces it.
    python = toolchains.resolve(["python"])[0]
    assert "python3-venv" in python.apt_packages
    assert "python3-pip" in python.apt_packages
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


def test_python_installs_uv_pinned_and_checksum_verified() -> None:
    # #250: every other runtime is pinned and verified; Python was the one
    # left on "whatever the template ships". uv comes from its GitHub
    # release with per-arch digests, never from a curl-into-shell.
    python = toolchains.resolve(["python"])[0]
    assert python.install_script is not None
    assert toolchains.UV_VERSION in python.install_script
    assert "astral.sh/uv/install.sh" not in python.install_script
    for deb_arch, (upstream, digest) in toolchains._UV_DIGESTS.items():
        assert f"{deb_arch}) arch={upstream}" in python.install_script
        assert len(digest) == 64
        assert digest in python.install_script
    assert "sha256sum -c" in python.install_script


def test_python_probes_uv_and_the_pinned_series() -> None:
    # A template carrying an older python3.x would satisfy a bare presence
    # check and leave `requires-python >= 3.13` projects failing at sync.
    python = toolchains.resolve(["python"])[0]
    assert "command -v uv" in python.probe
    assert f"python{toolchains.PYTHON_SERIES} --version" in python.probe
    assert f'grep -q "^Python {toolchains.PYTHON_SERIES.replace(".", "\\.")}\\.' in python.probe
    assert python.install_script is not None
    assert f"uv python install {toolchains.PYTHON_SERIES}" in python.install_script
    assert f"/usr/local/bin/python{toolchains.PYTHON_SERIES}" in python.install_script


def test_python_probe_accepts_the_series_and_rejects_others() -> None:
    # Run the version half of the probe with a stubbed `python3.13` so the
    # grep anchors are exercised, not just eyeballed.
    python = toolchains.resolve(["python"])[0]
    version_check = python.probe.split("&& command -v uv >/dev/null && ", 1)[1]
    for output, expected in (
        (f"Python {toolchains.PYTHON_SERIES}.2", 0),
        ("Python 3.12.9", 1),
        (f"Python {toolchains.PYTHON_SERIES}0.1", 1),
    ):
        script = version_check.replace(
            f"python{toolchains.PYTHON_SERIES} --version", f"printf '%s\\n' '{output}'"
        )
        result = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
        assert result.returncode == expected, (output, script)


def test_python_downloads_stay_on_allowlisted_github_hosts() -> None:
    # GitHub release assets redirect from github.com to
    # release-assets.githubusercontent.com, and uv 0.12 tries Astral's own
    # CDN first for managed interpreters. Provisioning runs before PLAN, so
    # both must be settled in the baseline: the redirect host is
    # allowlisted, and uv is pointed at the canonical GitHub prefix rather
    # than needing a second vendor host reachable.
    from sbxloop.sbx.provision import AGENT_ALLOW_DOMAINS

    python = toolchains.resolve(["python"])[0]
    assert python.install_script is not None
    assert "release-assets.githubusercontent.com" in AGENT_ALLOW_DOMAINS
    assert "github.com" in AGENT_ALLOW_DOMAINS
    assert toolchains.UV_PYTHON_INSTALL_MIRROR.startswith("https://github.com/")
    assert (
        f'UV_PYTHON_INSTALL_MIRROR="{toolchains.UV_PYTHON_INSTALL_MIRROR}" '
        f"uv python install {toolchains.PYTHON_SERIES}"
    ) in python.install_script
    assert "releases.astral.sh" not in python.install_script


def test_python_leaves_the_system_interpreter_alone() -> None:
    # The worker runs on the template's python3; the pin is exposed under
    # its versioned name only.
    python = toolchains.resolve(["python"])[0]
    assert python.install_script is not None
    assert "/usr/local/bin/python3 " not in python.install_script
    assert "/usr/local/bin/python3;" not in python.install_script
    assert not python.install_script.endswith("/usr/local/bin/python3")


# -- #624 / #616: detection and installer hosts -----------------------------


def test_every_language_declares_manifests() -> None:
    # A registry entry detection can never select is a language that only
    # works with explicit config — the default-to-Python trap #624 removes.
    for toolchain in toolchains.TOOLCHAINS:
        assert toolchain.manifests, toolchain.name


def test_installer_entries_declare_their_hosts() -> None:
    # Every https:// host an install script fetches from is in the entry's
    # install_domains (the redirect targets are extra; they cannot be read
    # off the script). Colocation is the point of the field: a CDN change
    # in the URL that forgets the allowlist fails here, not in a sandbox.
    for toolchain in (*toolchains.TOOLCHAINS, toolchains.CLAUDE_CODE):
        script = toolchain.install_script or ""
        for host in re.findall(r"https://([a-zA-Z0-9.-]+)", script):
            assert host in toolchain.install_domains, (toolchain.name, host)


def test_apt_only_entries_open_no_hosts() -> None:
    for name in ("cpp", "ruby", "java"):
        assert toolchains.resolve([name])[0].install_domains == ()


class TestTaskRunners:
    """#685: the gate detector emits `make`/`just`/`task check`, so each is
    a toolchain the manifest selects rather than one assumed present."""

    def test_make_is_an_apt_package_selected_by_any_makefile_spelling(self, tmp_path: Path) -> None:
        make = toolchains.resolve(["make"])[0]
        assert make.apt_packages == ("make",) and make.install_script is None
        assert make.install_domains == ()
        for name in ("Makefile", "makefile", "GNUmakefile"):
            (tmp_path / name).write_text("check:\n")
            assert toolchains.detect_languages(tmp_path) == {"make": (name,)}
            (tmp_path / name).unlink()

    def test_a_makefile_without_a_gate_target_still_selects_make(self, tmp_path: Path) -> None:
        # Selection is by manifest like every other entry: the agent runs
        # `make build` too, and a runner that costs one apt package is not
        # worth a second detection rule.
        (tmp_path / "go.mod").write_text("module x\n")
        (tmp_path / "Makefile").write_text("build:\n\tgo build ./...\n")
        assert toolchains.resolve_languages((), tmp_path).languages == ("go", "make")

    @pytest.mark.parametrize(
        ("name", "version", "manifests", "asset"),
        [
            ("just", toolchains.JUST_VERSION, ("justfile", ".justfile", "Justfile"), "casey/just"),
            ("task", toolchains.TASK_VERSION, ("Taskfile.yml", "Taskfile.yaml"), "go-task/task"),
        ],
    )
    def test_just_and_task_are_pinned_github_release_binaries(
        self, name: str, version: str, manifests: tuple[str, ...], asset: str
    ) -> None:
        runner = toolchains.resolve([name])[0]
        assert runner.manifests == manifests
        assert runner.install_script is not None
        assert f"github.com/{asset}/releases/download/" in runner.install_script
        assert version in runner.install_script
        assert "sha256sum -c" in runner.install_script
        assert "dpkg --print-architecture" in runner.install_script
        assert f"-C /usr/local/bin {name}; " in runner.install_script
        assert "install.sh" not in runner.install_script, "pinned tarballs, not curl | sh"
        assert runner.install_domains == ("github.com", "release-assets.githubusercontent.com")

    def test_runner_digests_are_per_architecture(self) -> None:
        for digests in (toolchains._JUST_DIGESTS, toolchains._TASK_DIGESTS):
            assert set(digests) == {"amd64", "arm64"}
            for _upstream, digest in digests.values():
                assert len(digest) == 64

    def test_runners_are_never_the_default(self, tmp_path: Path) -> None:
        assert not set(toolchains.DEFAULT_LANGUAGES) & {"make", "just", "task"}
        # a repo carrying nothing but a justfile provisions just — and not
        # the Python default, which "detected" replaces
        (tmp_path / "justfile").write_text("check:\n    echo\n")
        resolved = toolchains.resolve_languages((), tmp_path)
        assert resolved.languages == ("just",) and resolved.source == "detected"


def test_install_domains_pools_without_duplicates() -> None:
    domains = toolchains.install_domains(toolchains.resolve(["typescript", "javascript"]))
    assert domains == ("nodejs.org", "registry.npmjs.org")


class TestJavascriptPackageManagers:
    """#684: pnpm and yarn come with the Node entry through corepack; bun
    is an entry of its own, selected by its lockfile."""

    def test_node_enables_corepack_and_wants_the_shims(self) -> None:
        node = toolchains.resolve(["javascript"])[0]
        assert node.install_script is not None
        assert "corepack enable --install-directory /usr/local/bin" in node.install_script
        assert f"corepack@{toolchains.COREPACK_VERSION}" in node.install_script
        assert "COREPACK_ENABLE_DOWNLOAD_PROMPT" in node.install_script
        # a template baked before the shims re-runs the (now cheap) install
        assert "command -v pnpm" in node.probe and "command -v yarn" in node.probe
        assert "registry.npmjs.org" in node.install_domains

    def test_node_install_skips_the_tarball_when_the_major_is_present(self) -> None:
        # The top-up on an older template must not re-download Node.
        node = toolchains.resolve(["javascript"])[0]
        assert node.install_script is not None
        assert node.install_script.startswith('set -e; if ! node -v 2>/dev/null | grep -q "^v')
        assert "; fi; command -v corepack" in node.install_script

    def test_no_pnpm_or_yarn_entry_exists(self) -> None:
        # Both ride on corepack; a separate global install would shadow
        # the version the project's `packageManager` pins.
        assert toolchains.normalize_language("pnpm") is None
        assert toolchains.normalize_language("yarn") is None
        assert toolchains.normalize_language("bun") == "bun"

    def test_bun_layers_on_node_and_is_pinned(self) -> None:
        resolved = toolchains.resolve(["bun"])
        assert [tc.name for tc in resolved] == ["javascript", "bun"]
        bun = resolved[1]
        assert bun.install_script is not None
        assert f"bun@{toolchains.BUN_VERSION}" in bun.install_script
        assert bun.install_domains == ("registry.npmjs.org",)
        assert bun.manifests == ("bun.lock", "bun.lockb")
        assert bun.series == toolchains.BUN_VERSION
        assert toolchains.BUN_VERSION in bun.probe

    def test_bun_is_detected_from_its_lockfile(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}\n")
        (tmp_path / "bun.lockb").write_bytes(b"\x00")
        resolved = toolchains.resolve_languages((), tmp_path)
        assert resolved.languages == ("javascript", "bun")
        assert resolved.signals["bun"] == ("bun.lockb",)

    def test_bun_takes_the_package_manager_pin(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"packageManager": "bun@1.2.3"}\n')
        (tmp_path / "bun.lock").write_text("{}\n")
        versions = toolchains.toolchain_versions(["bun"], tmp_path)
        assert versions["bun"] == toolchains.ToolchainVersion("1.2.3", "package.json", "bun@1.2.3")
        bun = toolchains.resolve(["bun"], versions)[1]
        assert bun.series == "1.2.3"
        assert "bun@1.2.3" in bun.install_script and '"1.2.3"' in bun.probe

    def test_a_pin_for_another_client_leaves_bun_at_its_default(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"packageManager": "pnpm@9.0.0"}\n')
        assert toolchains.toolchain_versions(["bun"], tmp_path)[
            "bun"
        ] == toolchains.ToolchainVersion(toolchains.BUN_VERSION, "default", None)


def test_detect_reads_root_and_two_levels_only(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "packages/ui").mkdir(parents=True)
    (tmp_path / "packages/ui/package.json").write_text("{}\n")
    (tmp_path / "packages/ui/deep").mkdir()
    (tmp_path / "packages/ui/deep/Cargo.toml").write_text("[package]\n")
    found = toolchains.detect_languages(tmp_path)
    assert found == {"javascript": ("package.json",), "go": ("go.mod",)}
    # registry order, not discovery order
    assert list(found) == ["javascript", "go"]


@pytest.mark.parametrize("skipped", [".git", ".venv", "node_modules", "vendor"])
def test_detect_skips_dependency_and_dot_directories(tmp_path: Path, skipped: str) -> None:
    (tmp_path / skipped).mkdir()
    (tmp_path / skipped / "package.json").write_text("{}\n")
    assert toolchains.detect_languages(tmp_path) == {}


def test_detect_matches_suffix_patterns(tmp_path: Path) -> None:
    (tmp_path / "App.csproj").write_text("<Project/>\n")
    (tmp_path / "widget.gemspec").write_text("Gem::Specification.new\n")
    found = toolchains.detect_languages(tmp_path)
    assert found == {"ruby": ("widget.gemspec",), "dotnet": ("App.csproj",)}


def test_detect_on_a_missing_workspace_is_empty(tmp_path: Path) -> None:
    assert toolchains.detect_languages(tmp_path / "absent") == {}


def test_resolve_languages_explicit_wins_over_manifests(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")
    resolved = toolchains.resolve_languages(["rust"], tmp_path)
    assert resolved == toolchains.LanguageResolution(("rust",), "config", {})


def test_resolve_languages_falls_back_to_default_last(tmp_path: Path) -> None:
    resolved = toolchains.resolve_languages((), tmp_path)
    assert resolved[:3] == (toolchains.DEFAULT_LANGUAGES, "default", {})
    assert resolved.versions == {"python": toolchains.ToolchainVersion("3.13", "default")}
    assert toolchains.resolve_languages((), None).source == "default"


# -- toolchain versions from the workspace (#627) ---------------------------


PY_DEFAULT = toolchains.ToolchainVersion(toolchains.PYTHON_SERIES, "default")
NODE_DEFAULT = toolchains.ToolchainVersion(toolchains.NODE_MAJOR, "default")


def pyproject(tmp_path: Path, requires: str) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nrequires-python = "{requires}"\n'
    )


def package_json(tmp_path: Path, engines: str) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"name": "x", "engines": {"node": engines}}))


def test_version_from_without_a_workspace_or_declaration_is_the_default(tmp_path: Path) -> None:
    # Undeclared projects behave exactly as before #627: the pinned series.
    assert toolchains.PYTHON.version_from(None) == PY_DEFAULT
    assert toolchains.PYTHON.version_from(tmp_path) == PY_DEFAULT
    assert toolchains.NODE.version_from(tmp_path) == NODE_DEFAULT
    # a toolchain without a series has no version to speak of
    assert toolchains.resolve(["rust"])[0].version_from(tmp_path) is None


@pytest.mark.parametrize(
    ("requires", "series"),
    [
        (">=3.11,<3.12", "3.11"),  # the acceptance case: the only series that fits
        ("==3.10.*", "3.10"),
        (">=3.9", toolchains.PYTHON_SERIES),  # default satisfies → default
        (">=3.14", "3.14"),  # default does not → the highest that does
        ("<3.12", "3.11"),
    ],
)
def test_requires_python_selects_a_series(tmp_path: Path, requires: str, series: str) -> None:
    pyproject(tmp_path, requires)
    assert toolchains.PYTHON.version_from(tmp_path) == toolchains.ToolchainVersion(
        series, "pyproject.toml", requires
    )


def test_requires_python_nobody_can_satisfy_keeps_the_default_on_the_record(
    tmp_path: Path,
) -> None:
    # Fail loud, then provision what was always provisioned: the event
    # names the constraint next to the default it fell back to.
    pyproject(tmp_path, ">=2.7,<3")
    assert toolchains.PYTHON.version_from(tmp_path) == toolchains.ToolchainVersion(
        toolchains.PYTHON_SERIES, "default", ">=2.7,<3"
    )


def test_requires_python_that_is_not_pep_440_reads_as_undeclared(tmp_path: Path) -> None:
    pyproject(tmp_path, "three point eleven")
    assert toolchains.PYTHON.version_from(tmp_path) == PY_DEFAULT


def test_pyproject_without_requires_python_reads_as_undeclared(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    assert toolchains.PYTHON.version_from(tmp_path) == PY_DEFAULT
    (tmp_path / "pyproject.toml").write_text("this is not toml = = =\n")
    assert toolchains.PYTHON.version_from(tmp_path) == PY_DEFAULT


@pytest.mark.parametrize(
    ("pin", "series"),
    [("3.11\n", "3.11"), ("3.11.4", "3.11"), ("cpython@3.12", "3.12"), ("cpython-3.10.2", "3.10")],
)
def test_python_version_file_pins_a_series(tmp_path: Path, pin: str, series: str) -> None:
    (tmp_path / ".python-version").write_text(pin)
    assert toolchains.PYTHON.version_from(tmp_path) == toolchains.ToolchainVersion(
        series, ".python-version", pin.strip()
    )


def test_python_version_file_wins_over_requires_python(tmp_path: Path) -> None:
    # The pin is the developer's exact choice; the specifier is the range.
    pyproject(tmp_path, ">=3.9")
    (tmp_path / ".python-version").write_text("3.11\n")
    assert toolchains.PYTHON.version_from(tmp_path).series == "3.11"  # type: ignore[union-attr]


def test_python_version_file_outside_the_candidates_falls_to_the_default(tmp_path: Path) -> None:
    (tmp_path / ".python-version").write_text("2.7\n")
    assert toolchains.PYTHON.version_from(tmp_path) == toolchains.ToolchainVersion(
        toolchains.PYTHON_SERIES, "default", "2.7"
    )
    (tmp_path / ".python-version").write_text("pypy3.10\n")
    assert toolchains.PYTHON.version_from(tmp_path) == PY_DEFAULT


@pytest.mark.parametrize(
    ("pin", "major"),
    [
        ("18", "18"),
        ("v20.5.0\n", "20"),
        ("22.1", "22"),
        ("lts/iron", "20"),
        ("lts/hydrogen", "18"),
        ("lts/*", toolchains.NODE_MAJOR),
        ("node", toolchains.NODE_MAJOR),
    ],
)
def test_nvmrc_pins_a_node_major(tmp_path: Path, pin: str, major: str) -> None:
    (tmp_path / ".nvmrc").write_text(pin)
    assert toolchains.NODE.version_from(tmp_path) == toolchains.ToolchainVersion(
        major, ".nvmrc", pin.strip()
    )


def test_node_version_file_is_read_like_nvmrc(tmp_path: Path) -> None:
    (tmp_path / ".node-version").write_text("20.11.0\n")
    assert toolchains.NODE.version_from(tmp_path) == toolchains.ToolchainVersion(
        "20", ".node-version", "20.11.0"
    )


def test_nvmrc_for_a_major_this_host_cannot_install_keeps_the_default(tmp_path: Path) -> None:
    (tmp_path / ".nvmrc").write_text("16\n")
    assert toolchains.NODE.version_from(tmp_path) == toolchains.ToolchainVersion(
        toolchains.NODE_MAJOR, "default", "16"
    )
    (tmp_path / ".nvmrc").write_text("lts/argon\n")  # a codename nobody ships any more
    assert toolchains.NODE.version_from(tmp_path) == NODE_DEFAULT


@pytest.mark.parametrize(
    ("engines", "major"),
    [
        (">=18", toolchains.NODE_MAJOR),  # default satisfies → default
        (">=18 <21", "20"),  # the highest that fits
        ("^22", "22"),
        ("~18.17.0", "18"),
        ("18.x", "18"),
        ("=20.5.0", "20"),
        ("18 || 20", "20"),
        ("16 - 20", "20"),
        (">=16.0.0 <19", "18"),
    ],
)
def test_engines_node_selects_the_highest_admitted_major(
    tmp_path: Path, engines: str, major: str
) -> None:
    package_json(tmp_path, engines)
    assert toolchains.NODE.version_from(tmp_path) == toolchains.ToolchainVersion(
        major, "package.json", engines
    )


def test_engines_node_nobody_can_satisfy_keeps_the_default_on_the_record(tmp_path: Path) -> None:
    package_json(tmp_path, "14.x")
    assert toolchains.NODE.version_from(tmp_path) == toolchains.ToolchainVersion(
        toolchains.NODE_MAJOR, "default", "14.x"
    )


def test_engines_node_this_host_cannot_read_is_undeclared(tmp_path: Path) -> None:
    package_json(tmp_path, "latest-and-greatest")
    assert toolchains.NODE.version_from(tmp_path) == NODE_DEFAULT
    (tmp_path / "package.json").write_text("{not json")
    assert toolchains.NODE.version_from(tmp_path) == NODE_DEFAULT
    (tmp_path / "package.json").write_text(json.dumps({"engines": {"npm": ">=9"}}))
    assert toolchains.NODE.version_from(tmp_path) == NODE_DEFAULT


def test_nvmrc_wins_over_engines_node(tmp_path: Path) -> None:
    package_json(tmp_path, ">=18")
    (tmp_path / ".nvmrc").write_text("18\n")
    assert toolchains.NODE.version_from(tmp_path).series == "18"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("range_text", "major", "verdict"),
    [
        ("*", "18", True),
        ("", "24", True),
        (">=18", "18", True),
        (">18", "18", False),  # node-semver reads `>18` as >=19.0.0
        (">18", "19", True),
        ("<21", "20", True),
        ("<21", "21", False),
        ("<=20.2.0", "20", True),
        ("^18.12", "18", True),
        ("^18.12", "19", False),
        ("~20.11", "20", True),
        ("18.x", "18", True),
        ("18.*", "20", False),
        ("v20", "20", True),
        ("18 - 20", "19", True),
        ("18 - 20", "21", False),
        (">=18 <21 || >=24", "22", False),
        (">=18 <21 || >=24", "24", True),
        (">=99", "24", False),
    ],
)
def test_semver_range_admits(range_text: str, major: str, verdict: bool) -> None:
    assert toolchains._semver_range_admits(range_text, major) is verdict


@pytest.mark.parametrize("range_text", ["banana", ">=", "1.2.3.4", "18.0.0.beta", "latest"])
def test_semver_range_this_host_cannot_read_is_none(range_text: str) -> None:
    assert toolchains._semver_range_admits(range_text, "20") is None


def test_for_version_rebuilds_the_python_entry_for_the_series() -> None:
    py311 = toolchains.PYTHON.for_version("3.11")
    assert py311.name == "python"
    assert py311.series == "3.11"
    assert "python3.11 --version" in py311.probe
    assert 'grep -q "^Python 3\\.11\\.' in py311.probe
    assert py311.install_script is not None
    assert "uv python install 3.11" in py311.install_script
    assert "/usr/local/bin/python3.11" in py311.install_script
    assert "3.13" not in py311.probe and "3.13" not in py311.install_script
    # the same declaration readers ride along, so a rebuilt entry resolves
    # the next workspace exactly like the registry's
    assert py311.declared_series is toolchains.PYTHON.declared_series
    # everything else is untouched: hosts, apt packages, aliases
    assert py311.install_domains == toolchains.PYTHON.install_domains
    assert py311.apt_packages == toolchains.PYTHON.apt_packages
    assert py311.aliases == toolchains.PYTHON.aliases


def test_for_version_at_the_default_is_the_registry_entry_itself() -> None:
    assert toolchains.PYTHON.for_version(toolchains.PYTHON_SERIES) is toolchains.PYTHON
    assert toolchains.NODE.for_version(toolchains.NODE_MAJOR) is toolchains.NODE
    # an entry with no series has nothing to rebuild
    rust = toolchains.resolve(["rust"])[0]
    assert rust.for_version("1.90") is rust


@pytest.mark.parametrize("major", sorted(toolchains.NODE_RELEASES))
def test_every_node_release_is_pinned_and_checksum_verified(major: str) -> None:
    version, digests = toolchains.NODE_RELEASES[major]
    assert version.split(".")[0] == major
    node = toolchains.NODE.for_version(major)
    assert node.series == major
    assert node.install_script is not None
    assert f"node-v{version}-linux-" in node.install_script
    assert f'grep -q "^v{major}\\.' in node.probe
    for arch in ("amd64", "arm64"):
        _folder, digest = digests[arch]
        assert re.fullmatch(r"[0-9a-f]{64}", digest), (major, arch)
        assert digest in node.install_script
    assert "sha256sum -c" in node.install_script


def test_resolve_takes_the_versions_it_is_handed() -> None:
    versions = {
        "python": toolchains.ToolchainVersion("3.11", "pyproject.toml", "<3.12"),
        "javascript": toolchains.ToolchainVersion("18", ".nvmrc", "18"),
    }
    python, node, ts, rust = toolchains.resolve(["python", "typescript", "rust"], versions)
    assert (python.series, node.series) == ("3.11", "18")
    # typescript's requirement on javascript picks up the declared major too
    assert node.name == "javascript" and ts.name == "typescript"
    assert rust.series is None
    # a version for a toolchain that was not selected is ignored
    assert [tc.name for tc in toolchains.resolve(["rust"], versions)] == ["rust"]


def test_toolchain_versions_reads_every_versioned_toolchain_once(tmp_path: Path) -> None:
    pyproject(tmp_path, ">=3.11,<3.12")
    (tmp_path / ".nvmrc").write_text("20\n")
    versions = toolchains.toolchain_versions(["python", "typescript", "rust"], tmp_path)
    assert versions == {
        "python": toolchains.ToolchainVersion("3.11", "pyproject.toml", ">=3.11,<3.12"),
        "javascript": toolchains.ToolchainVersion("20", ".nvmrc", "20"),
    }


def test_resolve_languages_carries_versions_for_explicit_config_too(tmp_path: Path) -> None:
    # `[sandbox] languages` decides WHICH toolchains; the workspace still
    # decides which series, so a config-pinned python project's
    # requires-python is honoured.
    pyproject(tmp_path, ">=3.12,<3.13")
    resolved = toolchains.resolve_languages(["python"], tmp_path)
    assert resolved.source == "config"
    assert resolved.versions["python"].series == "3.12"
    # positional construction (the pre-#627 shape) still works
    assert toolchains.LanguageResolution(("go",), "detected", {"go": ("go.mod",)}).versions == {}
