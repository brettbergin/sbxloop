"""``GET /v1/repositories/available``: the repositories the host's forge
credential can see, for a person picking one to register. Read on the host
with the connection check's credential snapshot; never a secret in the
answer, never a write."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from sbxloop.vcs.github import appauth
from sbxloop.vcs.github.appauth import InstallationToken
from tests.api.conftest import Api, build

READ = frozenset({"runs:read"})

APP_SECRETS = (
    "GITHUB_APP_ID=123\nGITHUB_APP_INSTALLATION_ID=456\n"
    'GITHUB_APP_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\\nMIIB\\n-----END PRIVATE KEY-----"\n'
)


def _repo(full_name: str, **extra: Any) -> dict[str, Any]:
    owner, name = full_name.split("/", 1)
    return {
        "full_name": full_name,
        "name": name,
        "owner": {"login": owner},
        "private": False,
        "archived": False,
        "default_branch": "main",
        "html_url": f"https://github.com/{full_name}",
        **extra,
    }


class Forge:
    """A scripted forge behind ``httpx.MockTransport``: what it was asked,
    and what it answers for the listing endpoints."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.user_status = 200
        self.login = "octo"
        self.user_repos: list[dict[str, Any]] = []
        self.installation_repos: list[dict[str, Any]] = []
        self.projects: list[dict[str, Any]] = []
        self.list_status = 200

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        params = parse_qs(request.url.query.decode())
        page = int(params.get("page", ["1"])[0])
        size = int(params.get("per_page", ["30"])[0])
        window = slice((page - 1) * size, page * size)
        if path.endswith("/user"):
            if self.user_status != 200:
                return httpx.Response(self.user_status, json={"message": "Bad credentials"})
            return httpx.Response(200, json={"login": self.login, "username": self.login})
        if self.list_status != 200:
            return httpx.Response(self.list_status, json={"message": "refused"})
        if path.endswith("/user/repos"):
            return httpx.Response(200, json=self.user_repos[window])
        if path.endswith("/installation/repositories"):
            rows = self.installation_repos[window]
            return httpx.Response(
                200, json={"total_count": len(self.installation_repos), "repositories": rows}
            )
        if path.endswith("/projects"):
            return httpx.Response(200, json=self.projects[window])
        return httpx.Response(404, json={"message": f"unexpected {path}"})


@pytest.fixture
def forge(monkeypatch: pytest.MonkeyPatch) -> Forge:
    scripted = Forge()
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(scripted.handle), **kwargs),
    )
    for name in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GITLAB_TOKEN",
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_PRIVATE_KEY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    return scripted


def _secrets(api: Api, text: str) -> None:
    home = api.ctx.config.paths
    home.secrets_env.parent.mkdir(parents=True, exist_ok=True)
    home.secrets_env.write_text(text, encoding="utf-8")


class TestWithAPersonalToken:
    def test_lists_what_the_token_sees_and_marks_the_configured_ones(
        self, api: Api, forge: Forge
    ) -> None:
        _secrets(api, "GH_TOKEN=pat-secret\n")
        forge.user_repos = [
            _repo("o/zeta", private=True),
            _repo("Other/alpha", archived=True, default_branch="trunk"),
            _repo("o/r"),
        ]
        response = api.client.get("/v1/repositories/available", headers=api.bearer())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["forge"] == "github"
        assert body["credential"] == {"mode": "pat", "login": "octo"}
        assert [r["repository"] for r in body["data"]] == ["o/r", "o/zeta", "Other/alpha"]
        configured = {r["repository"]: r["configured"] for r in body["data"]}
        assert configured == {"o/r": True, "o/zeta": False, "Other/alpha": False}
        alpha = body["data"][2]
        assert alpha == {
            "repository": "Other/alpha",
            "forge": "github",
            "owner": "Other",
            "name": "alpha",
            "private": False,
            "archived": True,
            "default_branch": "trunk",
            "url": "https://github.com/Other/alpha",
            "configured": False,
        }
        assert body["data"][1]["private"] is True
        listing = next(r for r in forge.requests if r.url.path.endswith("/user/repos"))
        assert listing.headers["Authorization"] == "Bearer pat-secret"
        params = parse_qs(listing.url.query.decode())
        assert params["per_page"] == ["100"]
        assert set(params["affiliation"][0].split(",")) == {
            "owner",
            "collaborator",
            "organization_member",
        }
        assert "pat-secret" not in response.text

    def test_reads_every_page_until_a_short_one(
        self, api: Api, forge: Forge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.api import discovery

        monkeypatch.setattr(discovery, "PAGE_SIZE", 2)
        _secrets(api, "GITHUB_TOKEN=pat-secret\n")
        forge.user_repos = [_repo(f"o/r{i}") for i in range(5)]
        response = api.client.get("/v1/repositories/available", headers=api.bearer())
        assert response.status_code == 200, response.text
        assert [r["repository"] for r in response.json()["data"]] == [f"o/r{i}" for i in range(5)]
        pages = [
            parse_qs(r.url.query.decode())["page"][0]
            for r in forge.requests
            if r.url.path.endswith("/user/repos")
        ]
        assert pages == ["1", "2", "3"]

    def test_a_refused_credential_is_reported_without_the_secret(
        self, api: Api, forge: Forge
    ) -> None:
        _secrets(api, "GH_TOKEN=pat-secret\n")
        forge.user_status = 401
        response = api.client.get("/v1/repositories/available", headers=api.bearer())
        assert response.status_code == 502, response.text
        assert response.json()["code"] == "provider_error"
        assert "401" in response.json()["detail"]
        assert "pat-secret" not in response.text

    def test_an_unreachable_forge_is_a_502_too(
        self, api: Api, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        _secrets(api, "GH_TOKEN=pat-secret\n")

        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        real_client = httpx.Client
        monkeypatch.setattr(
            httpx,
            "Client",
            lambda **kwargs: real_client(transport=httpx.MockTransport(refuse), **kwargs),
        )
        response = api.client.get("/v1/repositories/available", headers=api.bearer())
        assert response.status_code == 502
        assert response.json()["code"] == "provider_unreachable"


class TestWithAGithubApp:
    def test_lists_the_installations_repositories(
        self, api: Api, forge: Forge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        minted: list[tuple[str, str]] = []

        def mint(creds: Any, **kwargs: Any) -> InstallationToken:
            minted.append((creds.installation_id, kwargs.get("api_url", "")))
            return InstallationToken("ghs_minted", api.clock() + 3600)

        monkeypatch.setattr(appauth, "mint_installation_token", mint)
        _secrets(api, APP_SECRETS)
        forge.installation_repos = [_repo("o/app-two"), _repo("o/app-one")]
        response = api.client.get("/v1/repositories/available", headers=api.bearer())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["credential"] == {"mode": "app", "login": None}
        assert [r["repository"] for r in body["data"]] == ["o/app-one", "o/app-two"]
        assert minted == [("456", "https://api.github.com")]
        listing = next(r for r in forge.requests if "/installation/" in r.url.path)
        assert listing.headers["Authorization"] == "Bearer ghs_minted"
        assert not any(r.url.path.endswith("/user") for r in forge.requests)
        assert "ghs_minted" not in response.text

    def test_a_personal_token_wins_when_both_are_set(
        self, api: Api, forge: Forge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            appauth,
            "mint_installation_token",
            lambda *a, **k: pytest.fail("the App must not be minted"),
        )
        _secrets(api, APP_SECRETS + "GH_TOKEN=pat-secret\n")
        forge.user_repos = [_repo("o/r")]
        response = api.client.get("/v1/repositories/available", headers=api.bearer())
        assert response.status_code == 200, response.text
        assert response.json()["credential"]["mode"] == "pat"

    def test_an_incomplete_app_is_refused_by_name(self, api: Api, forge: Forge) -> None:
        _secrets(api, "GITHUB_APP_ID=123\n")
        response = api.client.get("/v1/repositories/available", headers=api.bearer())
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "discovery_unavailable"
        assert "GITHUB_APP_INSTALLATION_ID" in response.json()["detail"]


class TestWithoutACredential:
    def test_says_what_to_configure(self, api: Api, forge: Forge) -> None:
        response = api.client.get("/v1/repositories/available", headers=api.bearer())
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["code"] == "discovery_unavailable"
        assert "GH_TOKEN" in body["detail"] and "GitHub App" in body["detail"]
        assert forge.requests == []

    def test_gitea_has_no_backend_to_ask(self, api: Api, forge: Forge) -> None:
        response = api.client.get(
            "/v1/repositories/available", params={"forge": "gitea"}, headers=api.bearer()
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "discovery_unavailable"

    def test_an_unknown_forge_is_a_validation_error(self, api: Api, forge: Forge) -> None:
        response = api.client.get(
            "/v1/repositories/available", params={"forge": "svn"}, headers=api.bearer()
        )
        assert response.status_code == 422, response.text


class TestWithGitlab:
    def test_lists_the_tokens_projects(self, tmp_path: Path, forge: Forge) -> None:
        api = build(
            tmp_path,
            config={
                "github": {},
                "vcs": {
                    "kind": "gitlab",
                    "api_url": "https://gitlab.example/api/v4",
                    "repos": [{"repo": "group/one"}],
                },
            },
        )
        forge.login = "gl-user"
        forge.projects = [
            {
                "path_with_namespace": "group/two",
                "path": "two",
                "namespace": {"full_path": "group"},
                "visibility": "private",
                "archived": False,
                "default_branch": "main",
                "web_url": "https://gitlab.example/group/two",
            },
            {
                "path_with_namespace": "group/one",
                "path": "one",
                "namespace": {"full_path": "group"},
                "visibility": "public",
                "archived": True,
                "default_branch": "develop",
                "web_url": "https://gitlab.example/group/one",
            },
        ]
        with api.client:
            _secrets(api, "GITLAB_TOKEN=glpat-secret\n")
            response = api.client.get("/v1/repositories/available", headers=api.bearer())
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["forge"] == "gitlab"
            assert body["credential"] == {"mode": "pat", "login": "gl-user"}
            assert [r["repository"] for r in body["data"]] == ["group/one", "group/two"]
            assert body["data"][0]["configured"] is True and body["data"][0]["archived"]
            assert body["data"][1]["private"] is True and not body["data"][1]["configured"]
            listing = next(r for r in forge.requests if r.url.path.endswith("/projects"))
            assert listing.headers["PRIVATE-TOKEN"] == "glpat-secret"
            assert listing.url.host == "gitlab.example"
            assert parse_qs(listing.url.query.decode())["membership"] == ["true"]
        api.ctx.close()


class TestWhoMayAsk:
    def test_the_owner_role_is_required(self, api: Api, forge: Forge) -> None:
        _secrets(api, "GH_TOKEN=pat-secret\n")
        forge.user_repos = [_repo("o/r")]
        refused = api.client.get("/v1/repositories/available", headers=api.bearer(READ))
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "forbidden_role"
        assert forge.requests == []

    def test_the_feature_is_advertised(self, api: Api) -> None:
        features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
        assert "repositories.discover" in features


def test_the_contract_names_the_route() -> None:
    document = json.loads(
        (Path(__file__).resolve().parents[2] / "docs" / "openapi.json").read_text()
    )
    assert "/v1/repositories/available" in document["paths"]
