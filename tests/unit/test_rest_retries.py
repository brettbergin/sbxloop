"""Transient forge reads retry; uncertain writes are never replayed."""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from datetime import UTC, datetime
from email.message import Message
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from sbxloop_worker import githubops
from sbxloop_worker.githubops import GithubOpError, RestTransport


def test_rate_limited_read_honors_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    delays = []
    monkeypatch.setattr(githubops.time, "sleep", delays.append)
    headers = Message()
    headers["Retry-After"] = "2"

    def request(req: urllib.request.Request, **kwargs: object) -> io.BytesIO:
        calls.append(req.get_method())
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                req.full_url, 429, "limited", headers, io.BytesIO(b"retry")
            )
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", request)
    assert RestTransport(token="test").request("GET", "/test") == {"ok": True}
    assert calls == ["GET", "GET"]
    assert delays == [2.0]


def test_uncertain_post_is_not_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def request(req: urllib.request.Request, **kwargs: object) -> None:
        calls.append(req.get_method())
        raise urllib.error.URLError("connection reset after submission")

    monkeypatch.setattr(urllib.request, "urlopen", request)
    with pytest.raises(GithubOpError):
        RestTransport(token="test").request("POST", "/test", {"body": "one comment"})
    assert calls == ["POST"]


@pytest.mark.parametrize(
    "status, attempts",
    [(401, 1), (403, 1), (404, 1), (409, 1), (422, 1), (429, 3), (502, 3), (503, 3), (504, 3)],
)
def test_only_transient_failures_retry_and_attempts_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    attempts: int,
) -> None:
    calls, delays = [], []
    monkeypatch.setattr(githubops.time, "sleep", delays.append)

    def request(req: urllib.request.Request, **kwargs: object) -> None:
        calls.append(req.get_method())
        raise urllib.error.HTTPError(
            req.full_url, status, "failure", Message(), io.BytesIO(b"details")
        )

    monkeypatch.setattr(urllib.request, "urlopen", request)
    with pytest.raises(GithubOpError) as error:
        RestTransport(token="test").request("GET", "/test")
    assert error.value.http_status == status
    assert len(calls) == attempts and len(delays) == attempts - 1


def test_retry_after_beyond_the_budget_does_not_retry_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    headers = Message()
    headers["Retry-After"] = "3600"
    monkeypatch.setattr(
        githubops.time, "sleep", lambda _: pytest.fail("must not sleep past budget")
    )

    def request(req: urllib.request.Request, **kwargs: object) -> None:
        calls.append(req.get_method())
        raise urllib.error.HTTPError(req.full_url, 429, "limited", headers, io.BytesIO(b"later"))

    monkeypatch.setattr(urllib.request, "urlopen", request)
    with pytest.raises(GithubOpError):
        RestTransport(token="test").request("GET", "/test")
    assert calls == ["GET"]


def test_http_date_and_malformed_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(githubops.time, "time", lambda: 1000.0)
    date = format_datetime(datetime.fromtimestamp(1003, UTC), usegmt=True)
    assert githubops._retry_after({"Retry-After": date}) == 3
    assert githubops._retry_after({"Retry-After": "not a date"}) == 0


@pytest.mark.parametrize("kind", ["json", "head", "trace"])
def test_timeouts_retry_on_every_read_surface(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    calls, delays = [], []
    monkeypatch.setattr(githubops.time, "sleep", delays.append)

    def request(req: urllib.request.Request, **kwargs: object) -> io.BytesIO:
        calls.append(req.get_method())
        if len(calls) == 1:
            raise TimeoutError("timed out")
        return io.BytesIO(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", request)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: SimpleNamespace(open=request))
    transport = RestTransport(token="test")
    if kind == "json":
        assert transport.request("GET", "/test") == {}
    elif kind == "head":
        assert transport.request_headers("HEAD", "/test") == {}
    else:
        assert transport.request_text("GET", "/test") == "{}"
    assert len(calls) == 2 and len(delays) == 1
