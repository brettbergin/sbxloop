"""Every clone source is resolved from the selected forge, not GitHub defaults."""

from types import SimpleNamespace

import pytest

from sbxloop import hostgit, toolchains
from sbxloop.config import Config
from sbxloop.errors import ConfigError, ProvisionError
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner


@pytest.mark.parametrize(
    "kind,api,expected",
    [
        ("github", "https://api.github.com", "https://github.com"),
        ("github", "https://ghe.example:8443/api/v3", "https://ghe.example:8443"),
        ("github", "https://ghe.example/git/api/v3/", "https://ghe.example/git"),
        ("gitlab", "http://192.168.0.240:8929/api/v4", "http://192.168.0.240:8929"),
        ("gitlab", "https://forge.example/gitlab/api/v4", "https://forge.example/gitlab"),
        ("gitea", "http://forge.example:3000/gitea/api/v1", "http://forge.example:3000/gitea"),
    ],
)
def test_clone_url_preserves_the_selected_forges_origin_and_prefix(kind, api, expected):
    config = Config.model_validate({"vcs": {"kind": kind, "api_url": api}})
    assert config.clone_url_for_repo("sandbox-test/sandbox") == f"{expected}/sandbox-test/sandbox"
    assert config.forge_web_url() == expected


def test_repo_override_selects_github_without_using_the_gitlab_api_root():
    config = Config.model_validate(
        {
            "vcs": {"kind": "gitlab", "api_url": "https://gitlab.example/api/v4"},
            "github": {"repos": [{"repo": "owner/project", "kind": "github"}]},
        }
    )
    assert config.clone_url_for_repo("owner/project") == "https://github.com/owner/project"
    assert config.clone_url_for_repo("group/project") == "https://gitlab.example/group/project"


@pytest.mark.parametrize("api", [None, "https://forge.example/ambiguous-api"])
def test_unknown_web_root_fails_before_cloning(api):
    config = Config.model_validate({"vcs": {"kind": "gitlab", "api_url": api}})
    with pytest.raises(ConfigError, match="clone"):
        config.clone_url_for_repo("owner/project")


@pytest.mark.parametrize("route", ["remote", "workload", "unconfigured"])
@pytest.mark.parametrize(
    "kind,api,origin,token_env",
    [
        ("github", "https://api.github.com", "https://github.com", "GH_TOKEN"),
        ("github", "https://ghe.example:8443/api/v3", "https://ghe.example:8443", "GH_TOKEN"),
        ("gitlab", "http://forge.example:8929/api/v4", "http://forge.example:8929", "GITLAB_TOKEN"),
        (
            "gitea",
            "https://forge.example/gitea/api/v1",
            "https://forge.example/gitea",
            "GITEA_TOKEN",
        ),
    ],
)
def test_clone_routes_use_the_forge_url_and_token(
    tmp_path, monkeypatch, route, kind, api, origin, token_env
):
    config = Config.model_validate(
        {
            "home": str(tmp_path / "home"),
            "sandbox": {"workspace_isolation": "clone"},
            "vcs": {"kind": kind, "api_url": api},
            "github": {"repos": [{"repo": "sandbox-test/sandbox", "kind": kind}]},
        }
    )
    provisioner = Provisioner(
        SbxCLI(), config, env={"GH_TOKEN": "wrong-forge", token_env: "selected-token"}
    )
    calls = []
    monkeypatch.setattr(hostgit, "find_git", lambda: "git")

    def clone(url, target, branch, **kwargs):
        calls.append((url, kwargs.get("token")))
        target.mkdir(parents=True, exist_ok=True)
        return "a" * 40

    monkeypatch.setattr(hostgit, "clone_from_remote", clone)
    if route == "remote":
        provisioner._clone_repo_remote("r1", "sandbox-test/sandbox", tmp_path / "run")
    elif route == "workload":
        provisioner.clone_repo_into_data_dir("r1", tmp_path / "data", "sandbox-test/sandbox")
    else:
        provisioner._resolve_workspace_source("r1", "sandbox-test/sandbox")
    assert calls == [(f"{origin}/sandbox-test/sandbox", "selected-token")]


def test_forge_clone_credential_override_and_public_fallback(tmp_path):
    config = Config.model_validate(
        {"vcs": {"kind": "gitlab", "api_url": "https://forge.example/api/v4"}}
    )
    provisioner = Provisioner(SbxCLI(), config, env={"GH_TOKEN": "wrong-forge"})
    assert provisioner.clone_token("owner/project") is None
    config.vcs.token_env = "CUSTOM_FORGE_TOKEN"
    provisioner.env = {"CUSTOM_FORGE_TOKEN": "correct", "GH_TOKEN": "wrong-forge"}
    assert provisioner.clone_token("owner/project") == "correct"


@pytest.mark.parametrize(
    "repo,origin",
    [
        ("owner/project", "http://forge.example:8929/prefix"),
        ("other/project", "https://ghe.example:8443"),
    ],
)
def test_auxiliary_fetches_use_the_selected_repo_not_the_default_forge(
    tmp_path, monkeypatch, repo, origin
):
    config = Config.model_validate(
        {
            "vcs": {"kind": "gitlab", "api_url": "http://forge.example:8929/prefix/api/v4"},
            "github": {
                "api_url": "https://ghe.example:8443/api/v3",
                "repos": [
                    {"repo": "owner/project", "kind": "gitlab"},
                    {"repo": "other/project", "kind": "github"},
                ],
            },
            "sandbox": {"fetch_tags": "always"},
        }
    )
    provisioner = Provisioner(SbxCLI(), config, env={"GITLAB_TOKEN": "token"})
    calls = {}
    monkeypatch.setattr(hostgit, "list_submodules", lambda _: ["dependency"])
    monkeypatch.setattr(toolchains, "lfs_attribute_files", lambda _: [".gitattributes"])
    monkeypatch.setattr(toolchains, "tag_version_markers", lambda _: [])

    def submodules(path, **kwargs):
        calls["submodules"] = kwargs
        return []

    def lfs(path, **kwargs):
        calls["lfs"] = kwargs
        return SimpleNamespace(files=1, linked=0, fetched=1)

    def tags(path, **kwargs):
        calls["tags"] = kwargs
        return SimpleNamespace(tags=1, source="remote")

    monkeypatch.setattr(hostgit, "populate_submodules", submodules)
    monkeypatch.setattr(hostgit, "populate_lfs", lfs)
    monkeypatch.setattr(hostgit, "fetch_tags", tags)
    for method in [
        provisioner._populate_submodules,
        provisioner._populate_lfs,
        provisioner._fetch_tags,
    ]:
        method("r1", tmp_path, source=None, repo=repo, token=lambda: "token")
    assert calls["submodules"]["credential_url"] == origin
    assert calls["tags"]["credential_url"] == origin
    assert calls["lfs"]["lfs_url"] == f"{origin}/{repo}.git/info/lfs"
    assert all(call["token"] == "token" for call in calls.values())


def test_fix_round_base_fetch_uses_the_run_forge(tmp_path, monkeypatch):
    from sbxloop.engine.engine import LoopEngine

    engine = LoopEngine.__new__(LoopEngine)
    engine.config = Config.model_validate(
        {
            "vcs": {"kind": "gitlab", "api_url": "http://forge.example:8929/gitlab/api/v4"},
        }
    )
    pipeline = SimpleNamespace(
        repo="owner/project",
        run_id="r1",
        ops=object(),
        pair=SimpleNamespace(workspace=tmp_path, mounted=True),
        repo_config=SimpleNamespace(deliver_base="main"),
        provisioner=SimpleNamespace(clone_token=lambda _: "selected-token"),
    )
    calls = []

    def fetch(workspace, url, branch, *, token):
        calls.append((url, branch, token))
        raise ProvisionError("stop before network access")

    monkeypatch.setattr(hostgit, "base_bundle", fetch)
    assert engine._merge_base_into_clone(pipeline) is None
    assert calls == [
        ("http://forge.example:8929/gitlab/owner/project.git", "main", "selected-token")
    ]


def test_configured_tool_repository_url_resolves_to_its_authenticated_clone():
    from sbxloop.entrygraph import resolve_targets

    config = Config.model_validate(
        {
            "vcs": {"kind": "gitlab", "api_url": "https://forge.example/gitlab/api/v4"},
            "github": {"repos": [{"repo": "owner/project"}]},
            "entrygraph": {"allow_public_urls": False},
        }
    )
    assert resolve_targets(config, url="https://forge.example/gitlab/owner/project.git") == [
        "owner/project"
    ]


def test_nested_gitlab_project_has_a_managed_workspace_and_preserves_its_url(tmp_path, monkeypatch):
    repo = "group/subgroup/project"
    config = Config.model_validate(
        {
            "home": str(tmp_path / "home"),
            "vcs": {"kind": "gitlab", "api_url": "http://forge.example:8929/api/v4"},
            "github": {"repos": [{"repo": repo}]},
        }
    )
    assert (
        config.default_workspace_for_repo(repo)
        == config.paths.workspaces / "group/subgroup/project"
    )
    calls = []

    def clone(url, target, branch, **kwargs):
        calls.append((url, target))
        return "a" * 40

    monkeypatch.setattr(hostgit, "find_git", lambda: "git")
    monkeypatch.setattr(hostgit, "clone_from_remote", clone)
    provisioner = Provisioner(SbxCLI(), config, env={"GITLAB_TOKEN": "selected"})
    path = provisioner.clone_repo_into_data_dir("r1", tmp_path, repo)
    assert path == tmp_path / "project"
    assert calls == [("http://forge.example:8929/group/subgroup/project", path)]


def test_clone_url_does_not_accept_gitlab_namespaces_for_github():
    with pytest.raises(ConfigError, match="invalid repository"):
        Config().clone_url_for_repo("group/subgroup/project")
