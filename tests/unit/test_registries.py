"""What each `[[registries]]` kind writes into the agent sandbox (#680)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop.config import USERNAME_REGISTRY_KINDS, RegistryConfig
from sbxloop.sbx import registries
from sbxloop.sbx.registries import (
    CARGO_CONFIG,
    GEMRC,
    MAVEN_SETTINGS,
    NETRC,
    NPMRC,
    NUGET_CONFIG,
)

TOKEN = "registry-token-4f2c"  # nosec B105 - test fixture
VALUES = {"REG_TOKEN": TOKEN}


def registry(kind: str, **fields: object) -> RegistryConfig:
    return RegistryConfig.model_validate({"kind": kind, "host": "reg.example.com", **fields})


def files_by_path(regs: list[RegistryConfig]) -> dict[str, str]:
    return {f.path: f.text for f in registries.client_files(regs, VALUES)}


class TestNpm:
    def test_scoped_registry_names_the_token_variable_not_its_value(self) -> None:
        reg = registry(
            "npm",
            url="https://reg.example.com/api/npm/npm-virtual",
            auth_env="REG_TOKEN",
            scope="@example",
        )
        text = files_by_path([reg])[NPMRC]
        assert text == (
            "@example:registry=https://reg.example.com/api/npm/npm-virtual\n"
            "//reg.example.com/api/npm/npm-virtual/:_authToken=${REG_TOKEN}\n"
        )
        assert TOKEN not in text
        # the variable itself is delivered so `${REG_TOKEN}` resolves
        assert registries.secret_env([reg], VALUES) == {"REG_TOKEN": TOKEN}
        assert registries.plain_env([reg]) == {}

    def test_unscoped_registry_is_the_default_and_several_share_one_file(self) -> None:
        default = registry("npm", url="https://reg.example.com/npm/")
        scoped = registry("npm", url="https://reg.example.com/npm-corp/", scope="@corp")
        text = files_by_path([default, scoped])[NPMRC]
        assert text == (
            "registry=https://reg.example.com/npm/\n@corp:registry=https://reg.example.com/npm-corp/\n"
        )


class TestPypi:
    def test_env_points_pip_and_uv_at_the_index_and_netrc_carries_the_login(self) -> None:
        reg = registry(
            "pypi",
            url="https://reg.example.com/api/pypi/pypi-virtual/simple",
            auth_env="REG_TOKEN",
            auth_user="svc-ci",
        )
        assert registries.plain_env([reg]) == {
            "PIP_INDEX_URL": "https://reg.example.com/api/pypi/pypi-virtual/simple",
            "UV_DEFAULT_INDEX": "https://reg.example.com/api/pypi/pypi-virtual/simple",
        }
        assert registries.secret_env([reg], VALUES) == {"REG_TOKEN": TOKEN}
        assert files_by_path([reg]) == {
            NETRC: f"machine reg.example.com login svc-ci password {TOKEN}\n"
        }

    def test_without_auth_no_netrc(self) -> None:
        reg = registry("pypi", url="https://reg.example.com/simple")
        assert files_by_path([reg]) == {}


class TestGo:
    def test_goprivate_joins_every_go_host(self) -> None:
        a = registry("go")
        b = RegistryConfig(kind="go", host="git.example.net")
        assert registries.plain_env([a, b]) == {"GOPRIVATE": "reg.example.com,git.example.net"}
        assert files_by_path([a, b]) == {}

    def test_auth_lands_in_netrc_beside_the_other_netrc_kinds(self) -> None:
        go = registry("go", auth_env="REG_TOKEN", auth_user="oauth2")
        generic = RegistryConfig(
            kind="generic", host="files.example.com", auth_env="REG_TOKEN", auth_user="ci"
        )
        assert files_by_path([go, generic]) == {
            NETRC: (
                f"machine reg.example.com login oauth2 password {TOKEN}\n"
                f"machine files.example.com login ci password {TOKEN}\n"
            )
        }


class TestCargo:
    def test_named_sparse_registry_with_the_token_in_cargos_env_variable(self) -> None:
        reg = registry(
            "cargo",
            url="https://reg.example.com/api/cargo/crates-remote/index/",
            auth_env="REG_TOKEN",
            name="corp-crates",
        )
        assert files_by_path([reg]) == {
            CARGO_CONFIG: (
                "[registries.corp-crates]\n"
                'index = "sparse+https://reg.example.com/api/cargo/crates-remote/index/"\n\n'
            )
        }
        assert registries.secret_env([reg], VALUES) == {
            "REG_TOKEN": TOKEN,
            "CARGO_REGISTRIES_CORP_CRATES_TOKEN": TOKEN,
        }

    def test_name_defaults_to_the_host(self) -> None:
        reg = registry("cargo", url="sparse+https://reg.example.com/index/")
        assert reg.effective_name == "reg-example-com"
        assert "[registries.reg-example-com]" in files_by_path([reg])[CARGO_CONFIG]
        assert (
            'index = "sparse+https://reg.example.com/index/"' in files_by_path([reg])[CARGO_CONFIG]
        )


class TestMaven:
    def test_settings_mirror_everything_and_reference_the_password_by_env(self) -> None:
        reg = registry(
            "maven",
            url="https://reg.example.com/api/maven/maven-virtual",
            auth_env="REG_TOKEN",
            auth_user="svc-ci",
            name="corp",
        )
        text = files_by_path([reg])[MAVEN_SETTINGS]
        assert "<id>corp</id>" in text
        assert "<mirrorOf>*</mirrorOf>" in text
        assert "<url>https://reg.example.com/api/maven/maven-virtual</url>" in text
        assert "<username>svc-ci</username>" in text
        assert "<password>${env.REG_TOKEN}</password>" in text
        assert TOKEN not in text


class TestNuget:
    def test_source_plus_credentials_by_env_reference(self) -> None:
        reg = registry(
            "nuget",
            url="https://reg.example.com/api/nuget/v3/nuget-virtual/index.json",
            auth_env="REG_TOKEN",
            auth_user="svc-ci",
            name="corp",
        )
        text = files_by_path([reg])[NUGET_CONFIG]
        assert (
            '<add key="corp" '
            'value="https://reg.example.com/api/nuget/v3/nuget-virtual/index.json" />'
        ) in text
        assert "<corp>" in text
        assert '<add key="Username" value="svc-ci" />' in text
        assert '<add key="ClearTextPassword" value="%REG_TOKEN%" />' in text
        assert TOKEN not in text


class TestGem:
    def test_bundler_credential_variable_and_optional_gemrc_source(self) -> None:
        reg = registry(
            "gem",
            url="https://reg.example.com/api/gems/gems-virtual/",
            auth_env="REG_TOKEN",
            auth_user="svc",
        )
        assert registries.secret_env([reg], VALUES) == {
            "REG_TOKEN": TOKEN,
            "BUNDLE_REG__EXAMPLE__COM": f"svc:{TOKEN}",
        }
        assert files_by_path([reg]) == {
            GEMRC: ":sources:\n- https://reg.example.com/api/gems/gems-virtual/\n"
        }
        credential_only = registry("gem", auth_env="REG_TOKEN", auth_user="svc")
        assert files_by_path([credential_only]) == {}


class TestConfig:
    def test_domains_dedupe_in_order(self) -> None:
        regs = [
            registry("npm", url="https://reg.example.com/npm/"),
            registry("go"),
            RegistryConfig(kind="go", host="git.example.net"),
        ]
        assert registries.domains(regs) == ["reg.example.com", "git.example.net"]

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"kind": "go", "url": "https://reg.example.com/"}, "takes only host"),
            ({"kind": "pypi"}, "needs url"),
            ({"kind": "pypi", "url": "https://other.example.com/simple"}, "is not on host"),
            ({"kind": "pypi", "url": "ftp://reg.example.com/simple"}, "must be http"),
            (
                {"kind": "pypi", "url": "https://reg.example.com/simple", "auth_env": "T"},
                "set auth_user",
            ),
            (
                {
                    "kind": "npm",
                    "url": "https://reg.example.com/",
                    "auth_env": "T",
                    "auth_user": "u",
                },
                "drop auth_user",
            ),
            (
                {"kind": "npm", "url": "https://reg.example.com/", "auth_user": "u"},
                "needs auth_env",
            ),
            ({"kind": "npm", "url": "https://reg.example.com/", "scope": "example"}, "npm scope"),
            ({"kind": "cargo", "url": "https://reg.example.com/", "scope": "@x"}, "npm-only"),
            (
                {"kind": "cargo", "url": "https://reg.example.com/", "name": "1st"},
                "letters, digits",
            ),
            (
                {"kind": "npm", "url": "https://reg.example.com/", "auth_env": "GH_TOKEN"},
                "delivered by sbxloop",
            ),
            (
                {"kind": "npm", "url": "https://reg.example.com/", "auth_env": "SBXLOOP_X"},
                "delivered by sbxloop",
            ),
            ({"kind": "svn", "url": "https://reg.example.com/"}, "kind"),
        ],
    )
    def test_ill_formed_entries_are_refused_at_load(
        self, fields: dict[str, object], message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            RegistryConfig.model_validate({"host": "reg.example.com", **fields})

    def test_host_must_be_a_bare_hostname(self) -> None:
        with pytest.raises(ValueError, match="bare hostname"):
            RegistryConfig(kind="go", host="https://reg.example.com")
        assert RegistryConfig(kind="go", host=" Reg.Example.COM ").host == "reg.example.com"


class TestFetchRecipes:
    """The fixed fetch recipes (#766): what the service sandbox runs per
    ecosystem, and what the agent sandbox is told so it builds offline
    from the shared cache."""

    def test_kinds_and_languages_follow_the_registries_generic_excluded(self) -> None:
        regs = [
            registry("npm", auth_env="T", url="https://reg.example.com/npm/"),
            registry("generic"),
            registry("pypi", auth_env="T", auth_user="u", url="https://reg.example.com/simple/"),
        ]
        assert registries.kinds(regs) == ["npm", "pypi"]
        assert registries.languages(regs) == ["node", "python"]
        assert registries.kinds([]) == [] and registries.languages([]) == []

    @pytest.mark.parametrize(
        ("kind", "manifests", "argv"),
        [
            ("npm", ["package.json"], ("npm", "install", "--ignore-scripts")),
            ("npm", ["package.json", "package-lock.json"], ("npm", "ci", "--ignore-scripts")),
            (
                "pypi",
                ["requirements.txt"],
                ("pip", "download", "-d", f"{registries.DEPS_HOME}/pypi", "-r", "requirements.txt"),
            ),
            (
                "pypi",
                ["pyproject.toml"],
                ("pip", "download", "-d", f"{registries.DEPS_HOME}/pypi", "."),
            ),
            ("go", ["go.mod"], ("go", "mod", "download")),
            ("cargo", ["Cargo.toml"], ("cargo", "fetch")),
            (
                "maven",
                ["pom.xml"],
                (
                    "mvn",
                    "-B",
                    "dependency:go-offline",
                    f"-Dmaven.repo.local={registries.DEPS_HOME}/maven",
                ),
            ),
            (
                "nuget",
                ["*.csproj"],
                ("dotnet", "restore", "--packages", f"{registries.DEPS_HOME}/nuget"),
            ),
            ("gem", ["Gemfile"], ("bundle", "cache", "--all", "--no-install")),
        ],
    )
    def test_fetch_from_the_manifest(self, kind: str, manifests: list[str], argv: tuple) -> None:
        plan = registries.fetch_plan(kind, "fetch", manifests=manifests)  # type: ignore[arg-type]
        assert plan.argv == argv
        assert plan.manifest == manifests[0]

    def test_fetch_needs_a_manifest(self) -> None:
        with pytest.raises(ValueError, match=r"no package\.json in the workspace"):
            registries.fetch_plan("npm", "fetch", manifests=[])

    @pytest.mark.parametrize(
        ("kind", "argv"),
        [
            ("npm", ("npm", "install", "--ignore-scripts", "left-pad@1.3.0", "@ex/lib")),
            (
                "pypi",
                (
                    "pip",
                    "download",
                    "-d",
                    f"{registries.DEPS_HOME}/pypi",
                    "left-pad@1.3.0",
                    "@ex/lib",
                ),
            ),
            ("go", ("go", "mod", "download", "left-pad@1.3.0", "@ex/lib")),
        ],
    )
    def test_add_names_packages_with_scripts_off(self, kind: str, argv: tuple) -> None:
        plan = registries.fetch_plan(kind, "add", ["left-pad@1.3.0", "@ex/lib"])  # type: ignore[arg-type]
        assert plan.argv == argv

    @pytest.mark.parametrize(
        ("kind", "verb", "packages", "match"),
        [
            ("cargo", "add", ["serde"], "takes no package list"),
            ("npm", "add", [], "at least one package"),
            ("npm", "fetch", ["left-pad"], "packages"),
            ("npm", "install", [], "unknown fetch verb"),
            ("generic", "fetch", [], "nothing to fetch"),
            ("npm", "add", ["--registry=https://evil.example.com"], "not a package spec"),
            ("npm", "add", ["left-pad; rm -rf /"], "not a package spec"),
            ("npm", "add", [""], "not a package spec"),
        ],
    )
    def test_refusals(self, kind: str, verb: str, packages: list[str], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            registries.fetch_plan(kind, verb, packages, manifests=["package.json"])  # type: ignore[arg-type]

    def test_workspace_manifests_report_what_is_present(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "package-lock.json").write_text("{}")
        (tmp_path / "app.csproj").write_text("<Project/>")
        assert registries.workspace_manifests(tmp_path, "npm") == [
            "package.json",
            "package-lock.json",
        ]
        assert registries.workspace_manifests(tmp_path, "nuget") == ["*.csproj"]
        assert registries.workspace_manifests(tmp_path, "pypi") == []

    def test_offline_env_points_every_kind_at_the_cache_and_fetch_env_fills_it(self) -> None:
        regs = [
            registry(
                k,
                auth_env="T",
                auth_user="u" if k in USERNAME_REGISTRY_KINDS else None,
                url=None if k == "go" else f"https://reg.example.com/{k}/",
            )
            for k in ("npm", "pypi", "go", "cargo", "maven", "nuget", "gem")
        ]
        offline = registries.offline_env(regs)
        assert offline["npm_config_offline"] == "true"
        assert offline["npm_config_cache"] == f"{registries.DEPS_HOME}/npm"
        assert offline["PIP_NO_INDEX"] == "1"
        assert offline["PIP_FIND_LINKS"] == f"{registries.DEPS_HOME}/pypi"
        assert offline["UV_NO_INDEX"] == "1"
        assert offline["GOPROXY"] == "off"
        assert offline["GOMODCACHE"] == f"{registries.DEPS_HOME}/go"
        assert offline["CARGO_NET_OFFLINE"] == "true"
        assert offline["MAVEN_ARGS"] == f"-o -Dmaven.repo.local={registries.DEPS_HOME}/maven"
        assert offline["NUGET_PACKAGES"] == f"{registries.DEPS_HOME}/nuget"
        assert offline["BUNDLE_LOCAL"] == "true"
        fetch = registries.fetch_env(regs)
        # The fetcher fills the same cache locations, and is not offline.
        assert fetch["npm_config_cache"] == offline["npm_config_cache"]
        assert fetch["GOMODCACHE"] == offline["GOMODCACHE"]
        assert fetch["CARGO_HOME"] == offline["CARGO_HOME"]
        assert not any(k in fetch for k in ("npm_config_offline", "GOPROXY", "PIP_NO_INDEX"))
        assert registries.offline_env([]) == {} and registries.fetch_env([]) == {}
