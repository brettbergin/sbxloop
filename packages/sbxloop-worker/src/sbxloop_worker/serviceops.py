"""``service.http`` — one authenticated HTTP request from the service sandbox.

The service sandbox is the github sandbox's pattern generalized: it holds the
run's credentials (delivered into its environment by the host, never the
agent sandbox's) and runs nothing but the fixed ops the host submits. This
module is the whole op set for now — one request, with the named credential
attached, to that credential's ONE host.

What the job may say and what it may not:

- ``credential`` names an entry of the catalogue the host wrote into this
  sandbox's environment (:data:`CATALOGUE_ENV`: name → env variable, host,
  header, scheme). The job does not carry a host, a header or an env name —
  the URL is ``https://<catalogue host><path>`` and nothing else, so neither
  the model (which only ever asks the host for a call) nor a mis-built job
  can point a credential at another server.
- ``method``, ``path``, ``query``, ``headers`` and ``body`` are the request.
  A header the credential owns (its own header name, ``Host``,
  ``Authorization``) is refused rather than overridden.
- Redirects are not followed: a 3xx comes back as the status it is, with
  its ``location``, and the credential never crosses to a host the
  catalogue did not name (the same reason ``githubops`` refuses them).

The response carries the status, the headers and the body clipped to
:data:`BODY_MAX_CHARS` — with the credential's value replaced wherever an
API echoes it — and never the request headers.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Any, Protocol

from sbxloop_worker.secrets import REDACTED

# The host writes the run's credential catalogue here (non-secret: names,
# env variable names, hosts) beside the values themselves, one env variable
# each, in the same environment.
CATALOGUE_ENV = "SBXLOOP_SERVICE_CREDENTIALS"
# Test seam: a JSON file of scripted responses (see :class:`FakeTransport`).
FAKE_ENV = "SBXLOOP_SERVICE_FAKE"

USER_AGENT = "sbxloop-worker"
DEFAULT_TIMEOUT_S = 60.0
MAX_TIMEOUT_S = 300.0
# A response is what the model reads back through the host; past this the
# tail is kept beside the head so an error trailer survives.
BODY_MAX_CHARS = 64_000
_HEAD = 48_000
_TAIL = BODY_MAX_CHARS - _HEAD

METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


class ServiceOpError(RuntimeError):
    """A service op that could not run as asked — a name the catalogue does
    not hold, a request shape the op refuses, or a transport failure. Never
    carries a credential value."""


@dataclass(frozen=True)
class Credential:
    """One catalogue entry: what the host wrote into :data:`CATALOGUE_ENV`."""

    name: str
    env: str
    host: str
    header: str = "Authorization"
    scheme: str = "Bearer"

    def header_value(self, value: str) -> str:
        return f"{self.scheme} {value}" if self.scheme else value


def load_catalogue(env: Mapping[str, str] | None = None) -> dict[str, Credential]:
    """The credentials this sandbox was provisioned with, by name."""
    env = os.environ if env is None else env
    raw = env.get(CATALOGUE_ENV, "").strip()
    if not raw:
        return {}
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ServiceOpError(f"{CATALOGUE_ENV} is not JSON: {exc}") from exc
    catalogue: dict[str, Credential] = {}
    for entry in entries:
        cred = Credential(
            name=str(entry["name"]),
            env=str(entry["env"]),
            host=str(entry["host"]).lower(),
            header=str(entry.get("header") or "Authorization"),
            scheme=str(entry.get("scheme", "Bearer")),
        )
        catalogue[cred.name] = cred
    return catalogue


class Transport(Protocol):
    def send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None,
        timeout: float,
    ) -> tuple[int, dict[str, str], bytes]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Every redirect surfaces as its status: the caller never follows one
    with the credential still attached."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


class UrllibTransport:
    """Pure-stdlib HTTPS client; a non-2xx answer is still an answer."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect())

    def send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None,
        timeout: float,
    ) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=timeout) as response:  # nosec B310 - https enforced by the caller
                return (
                    int(response.status),
                    {k.lower(): v for k, v in response.headers.items()},
                    response.read(),
                )
        except urllib.error.HTTPError as exc:
            body = exc.read()
            return int(exc.code), {k.lower(): v for k, v in exc.headers.items()}, body
        except urllib.error.URLError as exc:
            raise ServiceOpError(f"{method} {url} failed: {exc.reason}") from exc


class FakeTransport:
    """Scripted responses for tests: ``{"responses": [{"status", "headers",
    "body"}, …]}`` consumed in order across worker processes (a ``.state``
    cursor beside the file), every request appended to
    ``<file>.requests.jsonl`` so a test can see exactly what was sent."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None,
        timeout: float,
    ) -> tuple[int, dict[str, str], bytes]:
        record = {
            "method": method,
            "url": url,
            "headers": headers,
            "body": data.decode(errors="replace") if data is not None else None,
        }
        with self.path.with_suffix(self.path.suffix + ".requests.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")
        script = json.loads(self.path.read_text())
        responses = list(script.get("responses", []))
        state = self.path.with_suffix(self.path.suffix + ".state")
        index = int(state.read_text()) if state.is_file() else 0
        if index >= len(responses):
            raise ServiceOpError(f"fake service transport: no scripted response #{index}")
        state.write_text(str(index + 1))
        response = responses[index]
        body = response.get("body", "")
        if not isinstance(body, str):
            body = json.dumps(body)
        return (
            int(response.get("status", 200)),
            {str(k).lower(): str(v) for k, v in dict(response.get("headers", {})).items()},
            body.encode(),
        )


def select_transport(env: Mapping[str, str] | None = None) -> Transport:
    env = os.environ if env is None else env
    fake = env.get(FAKE_ENV, "").strip()
    if fake:
        return FakeTransport(Path(fake))
    return UrllibTransport()


def clip_body(text: str) -> tuple[str, bool]:
    if len(text) <= BODY_MAX_CHARS:
        return text, False
    return text[:_HEAD] + "\n…[clipped]…\n" + text[-_TAIL:], True


def _method(params: Mapping[str, Any]) -> str:
    method = str(params.get("method", "")).upper()
    if method not in METHODS:
        raise ServiceOpError(f"unsupported method {method!r}; one of {sorted(METHODS)}")
    return method


def _path(params: Mapping[str, Any]) -> str:
    path = str(params.get("path", ""))
    parts = urllib.parse.urlsplit(path)
    if parts.scheme or parts.netloc or not path.startswith("/"):
        raise ServiceOpError(
            f"path must be an absolute path on the credential's host, got {path!r}"
        )
    return path


def _query(params: Mapping[str, Any]) -> str:
    query = params.get("query") or {}
    if not isinstance(query, Mapping):
        raise ServiceOpError("query must be an object of string values")
    return urllib.parse.urlencode({str(k): str(v) for k, v in query.items()})


def _body(params: Mapping[str, Any]) -> tuple[bytes | None, str | None]:
    body = params.get("body")
    if body is None or body == "":
        return None, None
    if isinstance(body, (dict, list)):
        return json.dumps(body).encode(), "application/json"
    return str(body).encode(), None


def _headers(params: Mapping[str, Any], cred: Credential) -> dict[str, str]:
    raw = params.get("headers") or {}
    if not isinstance(raw, Mapping):
        raise ServiceOpError("headers must be an object of string values")
    reserved = {"host", "authorization", cred.header.lower(), "content-length"}
    headers: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key)
        if name.lower() in reserved:
            raise ServiceOpError(f"header {name!r} is set by the service op and cannot be given")
        headers[name] = str(value)
    return headers


def execute_http(
    params: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Run one ``service.http`` job; the result is what the host relays."""
    env = os.environ if env is None else env
    catalogue = load_catalogue(env)
    name = str(params.get("credential", ""))
    cred = catalogue.get(name)
    if cred is None:
        known = ", ".join(sorted(catalogue)) or "none"
        raise ServiceOpError(f"credential {name!r} is not in this sandbox (known: {known})")
    value = env.get(cred.env, "")
    if not value:
        raise ServiceOpError(f"credential {name!r}: {cred.env} is not set in this sandbox")
    method = _method(params)
    path = _path(params)
    query = _query(params)
    data, content_type = _body(params)
    headers = _headers(params, cred)
    if content_type and not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = content_type
    headers.setdefault("Accept", "application/json, text/*;q=0.8, */*;q=0.5")
    headers["User-Agent"] = USER_AGENT
    headers[cred.header] = cred.header_value(value)
    url = f"https://{cred.host}{path}" + (f"?{query}" if query else "")
    timeout = min(float(params.get("timeout_s") or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S)
    started = time.monotonic()
    status, response_headers, raw = (transport or select_transport(env)).send(
        method, url, headers, data, timeout
    )
    text = raw.decode(errors="replace").replace(value, REDACTED)
    body, truncated = clip_body(text)
    return {
        "credential": name,
        "method": method,
        "path": path,
        "status": status,
        "headers": {k: v.replace(value, REDACTED) for k, v in response_headers.items()},
        "body": body,
        "truncated": truncated,
        "elapsed_s": round(time.monotonic() - started, 3),
    }
