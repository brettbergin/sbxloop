"""``service.http`` inside the worker: the request the credential's host
gets, and the shape of what comes back. Pure unit tests — an in-memory
transport records the one request and answers whatever the test scripts.

The two things the op must never do are pinned here: point the credential
at any host the catalogue did not name, and let the value out (in a header
the job supplies, in an echoed body, in an error)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sbxloop_worker.secrets import REDACTED
from sbxloop_worker.serviceops import (
    BODY_MAX_CHARS,
    CATALOGUE_ENV,
    FAKE_ENV,
    FakeTransport,
    ServiceOpError,
    UrllibTransport,
    execute_http,
    load_catalogue,
    select_transport,
)

VALUE = "sk-live-0123456789"

CATALOGUE = [
    {"name": "weather", "env": "WEATHER_API_KEY", "host": "api.weather.example.com"},
    {
        "name": "keyed",
        "env": "KEYED_TOKEN",
        "host": "keyed.example.com",
        "header": "X-Api-Key",
        "scheme": "",
    },
]

ENV = {
    CATALOGUE_ENV: json.dumps(CATALOGUE),
    "WEATHER_API_KEY": VALUE,
    "KEYED_TOKEN": "keyed-secret",
}


class RecordingTransport:
    """Answers one scripted response and keeps the request it was sent."""

    def __init__(self, status: int = 200, headers: dict[str, str] | None = None, body: str = ""):
        self.status = status
        self.headers = headers or {}
        self.body = body
        self.requests: list[tuple[str, str, dict[str, str], bytes | None, float]] = []

    def send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None,
        timeout: float,
    ) -> tuple[int, dict[str, str], bytes]:
        self.requests.append((method, url, headers, data, timeout))
        return self.status, self.headers, self.body.encode()


class TestCatalogue:
    def test_empty_env_means_no_credentials(self) -> None:
        assert load_catalogue({}) == {}
        assert load_catalogue({CATALOGUE_ENV: "  "}) == {}

    def test_entries_by_name_with_defaults(self) -> None:
        catalogue = load_catalogue(ENV)
        assert set(catalogue) == {"weather", "keyed"}
        weather = catalogue["weather"]
        assert (weather.header, weather.scheme) == ("Authorization", "Bearer")
        assert weather.header_value("v") == "Bearer v"
        keyed = catalogue["keyed"]
        assert (keyed.header, keyed.scheme) == ("X-Api-Key", "")
        assert keyed.header_value("v") == "v"

    def test_host_is_lowercased(self) -> None:
        env = {CATALOGUE_ENV: json.dumps([{"name": "a", "env": "A", "host": "API.Example.COM"}])}
        assert load_catalogue(env)["a"].host == "api.example.com"

    def test_bad_json_is_an_op_error(self) -> None:
        with pytest.raises(ServiceOpError, match="is not JSON"):
            load_catalogue({CATALOGUE_ENV: "{nope"})


class TestRequest:
    def test_url_is_pinned_to_the_catalogue_host(self) -> None:
        transport = RecordingTransport(body="{}")
        execute_http(
            {
                "credential": "weather",
                "method": "get",
                "path": "/v1/forecast",
                "query": {"city": "Oslo", "days": 3},
            },
            env=ENV,
            transport=transport,
        )
        (method, url, _headers, data, timeout) = transport.requests[0]
        assert method == "GET"
        assert url == "https://api.weather.example.com/v1/forecast?city=Oslo&days=3"
        assert data is None
        assert timeout == 60.0

    def test_bearer_header_is_attached(self) -> None:
        transport = RecordingTransport()
        execute_http(
            {"credential": "weather", "method": "GET", "path": "/"}, env=ENV, transport=transport
        )
        headers = transport.requests[0][2]
        assert headers["Authorization"] == f"Bearer {VALUE}"
        assert headers["User-Agent"] == "sbxloop-worker"
        assert "Accept" in headers

    def test_bare_scheme_uses_the_value_alone(self) -> None:
        transport = RecordingTransport()
        execute_http(
            {"credential": "keyed", "method": "GET", "path": "/x"}, env=ENV, transport=transport
        )
        headers = transport.requests[0][2]
        assert headers["X-Api-Key"] == "keyed-secret"
        assert "Authorization" not in headers

    def test_json_body_is_serialised_with_content_type(self) -> None:
        transport = RecordingTransport()
        execute_http(
            {
                "credential": "weather",
                "method": "POST",
                "path": "/v1/alerts",
                "body": {"city": "Oslo"},
            },
            env=ENV,
            transport=transport,
        )
        (_, _, headers, data, _) = transport.requests[0]
        assert data == b'{"city": "Oslo"}'
        assert headers["Content-Type"] == "application/json"

    def test_string_body_is_sent_verbatim_with_given_content_type(self) -> None:
        transport = RecordingTransport()
        execute_http(
            {
                "credential": "weather",
                "method": "PUT",
                "path": "/v1/x",
                "body": "a=1",
                "headers": {"content-type": "application/x-www-form-urlencoded"},
            },
            env=ENV,
            transport=transport,
        )
        (_, _, headers, data, _) = transport.requests[0]
        assert data == b"a=1"
        assert headers["content-type"] == "application/x-www-form-urlencoded"
        assert "Content-Type" not in headers

    def test_timeout_is_capped(self) -> None:
        transport = RecordingTransport()
        execute_http(
            {"credential": "weather", "method": "GET", "path": "/", "timeout_s": 9999},
            env=ENV,
            transport=transport,
        )
        assert transport.requests[0][4] == 300.0

    @pytest.mark.parametrize(
        "path",
        ["", "v1/forecast", "https://evil.example.com/v1", "//evil.example.com/v1"],
    )
    def test_path_must_be_absolute_on_the_host(self, path: str) -> None:
        transport = RecordingTransport()
        with pytest.raises(ServiceOpError, match="absolute path"):
            execute_http(
                {"credential": "weather", "method": "GET", "path": path},
                env=ENV,
                transport=transport,
            )
        assert transport.requests == []

    @pytest.mark.parametrize("header", ["Authorization", "authorization", "Host", "X-Api-Key"])
    def test_reserved_headers_are_refused(self, header: str) -> None:
        transport = RecordingTransport()
        with pytest.raises(ServiceOpError, match="cannot be given"):
            execute_http(
                {
                    "credential": "keyed",
                    "method": "GET",
                    "path": "/",
                    "headers": {header: "Bearer mine"},
                },
                env=ENV,
                transport=transport,
            )
        assert transport.requests == []

    def test_unsupported_method_is_refused(self) -> None:
        with pytest.raises(ServiceOpError, match="unsupported method"):
            execute_http(
                {"credential": "weather", "method": "TRACE", "path": "/"},
                env=ENV,
                transport=RecordingTransport(),
            )

    def test_unknown_credential_names_the_known_ones(self) -> None:
        with pytest.raises(ServiceOpError, match=r"'nope' is not in this sandbox .*keyed, weather"):
            execute_http(
                {"credential": "nope", "method": "GET", "path": "/"},
                env=ENV,
                transport=RecordingTransport(),
            )

    def test_unset_value_is_refused_without_a_request(self) -> None:
        transport = RecordingTransport()
        env = {**ENV, "WEATHER_API_KEY": ""}
        with pytest.raises(ServiceOpError, match="WEATHER_API_KEY is not set"):
            execute_http(
                {"credential": "weather", "method": "GET", "path": "/"},
                env=env,
                transport=transport,
            )
        assert transport.requests == []


class TestResponse:
    def test_result_shape(self) -> None:
        transport = RecordingTransport(
            status=201, headers={"content-type": "application/json"}, body='{"ok": true}'
        )
        result = execute_http(
            {"credential": "weather", "method": "POST", "path": "/v1/x"},
            env=ENV,
            transport=transport,
        )
        assert result["credential"] == "weather"
        assert (result["method"], result["path"], result["status"]) == ("POST", "/v1/x", 201)
        assert result["headers"] == {"content-type": "application/json"}
        assert result["body"] == '{"ok": true}'
        assert result["truncated"] is False
        assert isinstance(result["elapsed_s"], float)
        # The request headers (credential included) never ride back.
        assert "request" not in result and VALUE not in json.dumps(result)

    def test_redirect_is_returned_not_followed(self) -> None:
        transport = RecordingTransport(status=302, headers={"location": "https://elsewhere/"})
        result = execute_http(
            {"credential": "weather", "method": "GET", "path": "/"},
            env=ENV,
            transport=transport,
        )
        assert result["status"] == 302
        assert result["headers"]["location"] == "https://elsewhere/"
        assert len(transport.requests) == 1

    def test_value_is_redacted_from_body_and_headers(self) -> None:
        transport = RecordingTransport(
            headers={"x-echo": f"Bearer {VALUE}"}, body=f'{{"token": "{VALUE}"}}'
        )
        result = execute_http(
            {"credential": "weather", "method": "GET", "path": "/"},
            env=ENV,
            transport=transport,
        )
        assert VALUE not in json.dumps(result)
        assert result["body"] == f'{{"token": "{REDACTED}"}}'
        assert result["headers"]["x-echo"] == f"Bearer {REDACTED}"

    def test_long_body_keeps_head_and_tail(self) -> None:
        body = "H" * 50_000 + "T" * 50_000
        result = execute_http(
            {"credential": "weather", "method": "GET", "path": "/"},
            env=ENV,
            transport=RecordingTransport(body=body),
        )
        assert result["truncated"] is True
        assert result["body"].startswith("H" * 100)
        assert result["body"].endswith("T" * 100)
        assert "[clipped]" in result["body"]
        assert len(result["body"]) <= BODY_MAX_CHARS + 20


class TestTransports:
    def test_fake_transport_is_selected_by_env(self, tmp_path: Path) -> None:
        script = tmp_path / "fake.json"
        assert isinstance(select_transport({}), UrllibTransport)
        assert isinstance(select_transport({FAKE_ENV: str(script)}), FakeTransport)

    def test_fake_transport_records_requests_and_walks_the_script(self, tmp_path: Path) -> None:
        script = tmp_path / "fake.json"
        script.write_text(
            json.dumps(
                {
                    "responses": [
                        {"status": 200, "body": {"temp": 3}},
                        {"status": 404, "headers": {"X-Reason": "gone"}, "body": "nope"},
                    ]
                }
            )
        )
        env = {**ENV, FAKE_ENV: str(script)}
        first = execute_http({"credential": "weather", "method": "GET", "path": "/a"}, env=env)
        second = execute_http({"credential": "weather", "method": "GET", "path": "/b"}, env=env)
        assert (first["status"], first["body"]) == (200, '{"temp": 3}')
        assert (second["status"], second["headers"], second["body"]) == (
            404,
            {"x-reason": "gone"},
            "nope",
        )
        with pytest.raises(ServiceOpError, match="no scripted response #2"):
            execute_http({"credential": "weather", "method": "GET", "path": "/c"}, env=env)

        requests = [
            json.loads(line)
            for line in (tmp_path / "fake.json.requests.jsonl").read_text().splitlines()
        ]
        assert [r["url"] for r in requests] == [
            "https://api.weather.example.com/a",
            "https://api.weather.example.com/b",
            "https://api.weather.example.com/c",
        ]
        assert requests[0]["headers"]["Authorization"] == f"Bearer {VALUE}"
