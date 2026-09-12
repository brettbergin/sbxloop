"""The forge transport descriptor (#1015): how a job tells the worker's
generic REST client which forge it is speaking to — the API root, how the
token rides, how lists page — and what the worker does with it."""

from __future__ import annotations

import io
from typing import Any, ClassVar

import pytest

from sbxloop_worker import githubops
from sbxloop_worker.githubops import (
    GITHUB,
    GhCliTransport,
    GithubOpError,
    RestTransport,
    _list_pages,
    execute_op,
    select_transport,
    transport_spec,
)
from sbxloop_worker.protocol import JobRequest, TransportSpec


class FakeResponse(io.BytesIO):
    headers: ClassVar[dict[str, str]] = {}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def capture_request(monkeypatch: pytest.MonkeyPatch, body: bytes = b"{}") -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float = 0) -> FakeResponse:
        captured["url"] = request.full_url
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        return FakeResponse(body)

    monkeypatch.setattr(githubops.urllib.request, "urlopen", fake_urlopen)
    return captured


class TestTheDescriptor:
    def test_the_default_is_github_as_it_always_was(self) -> None:
        spec = TransportSpec()
        assert spec == GITHUB
        assert spec.api_url is None and spec.auth == "bearer" and spec.pagination == "page"
        assert spec.accept == "application/vnd.github+json"
        assert spec.api_version_header == "X-GitHub-Api-Version"
        assert spec.token_env == ["GH_TOKEN", "GITHUB_TOKEN"]
        assert spec.gh_cli is True

    def test_it_carries_names_never_values(self) -> None:
        assert "token" not in TransportSpec.model_fields
        assert "GH_TOKEN" in TransportSpec(token_env=["GH_TOKEN"]).token_env

    def test_a_job_without_one_is_github(self) -> None:
        assert transport_spec({"repo": "o/r"}) is GITHUB

    def test_a_job_with_one_is_validated(self) -> None:
        spec = transport_spec(
            {
                "transport": {
                    "api_url": "https://gitlab.example.com/api/v4/",
                    "auth": "private-token",
                    "pagination": "x-next-page",
                    "accept": "application/json",
                    "api_version_header": None,
                    "api_version": None,
                    "token_env": ["GITLAB_TOKEN"],
                    "gh_cli": False,
                }
            }
        )
        assert spec.api_url == "https://gitlab.example.com/api/v4"
        assert spec.auth == "private-token" and spec.pagination == "x-next-page"

    def test_an_unknown_style_or_a_plain_http_root_is_refused(self) -> None:
        with pytest.raises(GithubOpError, match="transport descriptor rejected"):
            transport_spec({"transport": {"auth": "cookie"}})
        with pytest.raises(GithubOpError, match="transport descriptor rejected"):
            transport_spec({"transport": {"api_url": "http://gitlab.example.com"}})
        with pytest.raises(GithubOpError, match="must be an object"):
            transport_spec({"transport": "github"})

    def test_the_job_kind_is_neutral_and_the_old_spelling_still_loads(self) -> None:
        for kind in ("vcs.op", "github.op"):
            job = JobRequest(job_id="j1", run_id="r1", kind=kind, op="repo.get")
            assert job.op == "repo.get"
        with pytest.raises(ValueError, match=r"vcs\.op requires an op name"):
            JobRequest(job_id="j1", run_id="r1", kind="vcs.op")


class TestAuthStyles:
    def test_bearer_is_github(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = capture_request(monkeypatch)
        RestTransport(token="tok", spec=GITHUB).request("GET", "/rate_limit")
        assert captured["url"] == "https://api.github.com/rate_limit"
        assert captured["headers"]["authorization"] == "Bearer tok"
        assert captured["headers"]["accept"] == "application/vnd.github+json"
        assert captured["headers"]["x-github-api-version"] == "2022-11-28"

    def test_private_token_is_gitlab(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = capture_request(monkeypatch)
        spec = TransportSpec(
            api_url="https://gitlab.example.com/api/v4",
            auth="private-token",
            accept="application/json",
            api_version_header=None,
            api_version=None,
        )
        RestTransport(token="glpat", spec=spec).request("GET", "/projects/1")
        assert captured["url"] == "https://gitlab.example.com/api/v4/projects/1"
        assert captured["headers"]["private-token"] == "glpat"
        assert "authorization" not in captured["headers"]
        assert captured["headers"]["accept"] == "application/json"
        assert "x-github-api-version" not in captured["headers"]

    def test_token_is_gitea(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = capture_request(monkeypatch)
        spec = TransportSpec(
            api_url="https://gitea.example.com/api/v1",
            auth="token",
            accept="application/json",
            api_version_header=None,
        )
        RestTransport(token="gta", spec=spec).request("GET", "/repos/o/r")
        assert captured["headers"]["authorization"] == "token gta"
        assert "private-token" not in captured["headers"]

    def test_the_token_is_read_from_the_variable_the_descriptor_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GITLAB_TOKEN", "glpat")
        transport = RestTransport(spec=TransportSpec(token_env=["GITLAB_TOKEN"]))
        assert transport.token == "glpat"
        with pytest.raises(GithubOpError, match="OTHER_TOKEN are not set"):
            RestTransport(spec=TransportSpec(token_env=["OTHER_TOKEN"]))

    def test_the_root_comes_from_the_descriptor_before_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(githubops.API_URL_ENV, "https://ghe.example.com/api/v3")
        assert RestTransport(token="t").api_url == "https://ghe.example.com/api/v3"
        spec = TransportSpec(api_url="https://other.example.com/api/v3")
        assert RestTransport(token="t", spec=spec).api_url == "https://other.example.com/api/v3"


class PagedTransport:
    """Answers a paged listing under one style, recording the walk."""

    def __init__(self, style: str, pages: list[list[dict[str, Any]]]) -> None:
        self.pagination = style
        self.pages = pages
        self.calls: list[str] = []

    def _page(self, path: str) -> int:
        return int(path.rsplit("page=", 1)[1]) if "page=" in path else 1

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.calls.append(path)
        return {"check_runs": self.pages[self._page(path) - 1]}

    def request_with_headers(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[Any, dict[str, str]]:
        self.calls.append(path)
        number = self._page(path)
        headers: dict[str, str] = {}
        if number < len(self.pages):
            if self.pagination == "link":
                following = (
                    f"https://api.example/repos/o/r/commits/abc/check-runs?page={number + 1}"
                )
                headers["link"] = (
                    f'<{following}>; rel="next", <https://api.example/x?page=9>; rel="last"'
                )
            else:
                headers["x-next-page"] = str(number + 1)
        elif self.pagination == "x-next-page":
            headers["x-next-page"] = ""
        return {"check_runs": self.pages[number - 1]}, headers

    def request_text(self, method: str, path: str) -> str:
        return ""

    def request_headers(self, method: str, path: str) -> dict[str, str]:
        return {}


ROW = {"name": "ci"}


class TestPaginationStyles:
    def test_by_number_stops_at_a_short_page(self) -> None:
        t = PagedTransport("page", [[ROW] * 100, [ROW] * 3])
        assert len(_list_pages(t, "/repos/o/r/commits/abc/check-runs", "check_runs")) == 103
        assert [p.rsplit("page=", 1)[1] for p in t.calls] == ["1", "2"]

    def test_by_link_header_follows_rel_next_to_its_absolute_url(self) -> None:
        t = PagedTransport("link", [[ROW] * 2, [ROW] * 2, [ROW]])
        assert len(_list_pages(t, "/repos/o/r/commits/abc/check-runs", "check_runs")) == 5
        assert t.calls[1].startswith("https://api.example/") and t.calls[1].endswith("page=2")
        assert t.calls[2].endswith("page=3")

    def test_by_x_next_page_stops_on_an_empty_header(self) -> None:
        t = PagedTransport("x-next-page", [[ROW] * 2, [ROW]])
        assert len(_list_pages(t, "/repos/o/r/commits/abc/check-runs", "check_runs")) == 3
        assert [p.rsplit("page=", 1)[1] for p in t.calls] == ["1", "2"]

    def test_a_transport_without_a_style_pages_by_number(self) -> None:
        class Bare:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def request(self, method: str, path: str, body: Any = None) -> Any:
                self.calls.append(path)
                return {"check_runs": [ROW]}

        t = Bare()
        assert _list_pages(t, "/p", "check_runs") == [ROW]  # type: ignore[arg-type]
        assert t.calls == ["/p?per_page=100&page=1"]

    def test_too_many_pages_is_refused_under_every_style(self) -> None:
        for style in ("page", "link", "x-next-page"):
            t = PagedTransport(style, [[ROW] * 100] * 12)
            with pytest.raises(GithubOpError, match="more than 1000 entries"):
                _list_pages(t, "/repos/o/r/commits/abc/check-runs", "check_runs")


class TestSelection:
    def test_gh_serves_github_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(githubops.shutil, "which", lambda _: "/usr/bin/gh")
        assert isinstance(select_transport(GITHUB), GhCliTransport)

    def test_gh_never_serves_another_forge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(githubops.shutil, "which", lambda _: "/usr/bin/gh")
        monkeypatch.setenv("GITLAB_TOKEN", "glpat")
        spec = TransportSpec(
            api_url="https://gitlab.example.com/api/v4",
            auth="private-token",
            token_env=["GITLAB_TOKEN"],
            gh_cli=False,
        )
        chosen = select_transport(spec)
        assert isinstance(chosen, RestTransport) and chosen.spec is spec

    def test_execute_op_strips_the_descriptor_before_the_op_sees_the_params(self) -> None:
        seen: list[dict[str, Any]] = []

        class Recording:
            pagination = "page"

            def request(self, method: str, path: str, body: Any = None) -> Any:
                seen.append({"method": method, "path": path, "body": body})
                return {"full_name": "o/r"}

            def request_with_headers(self, method: str, path: str, body: Any = None) -> Any:
                return self.request(method, path, body), {}

            def request_text(self, method: str, path: str) -> str:
                return ""

            def request_headers(self, method: str, path: str) -> dict[str, str]:
                return {}

        params = {"repo": "o/r", "transport": {"auth": "token", "gh_cli": False}}
        execute_op("repo.get", params, Recording())  # type: ignore[arg-type]
        assert seen and seen[0]["path"] == "/repos/o/r"
        assert "transport" in params, "the caller's mapping is not mutated"
