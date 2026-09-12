"""The small HTTPS client the seed scripts and the field probes share.

Stdlib only. Tokens and passwords ride headers, never a URL or argv, and a
:class:`Recorder` keeps what an evidence transcript needs (method, path,
body, status, a trimmed response) with every credential-shaped key redacted
and no header recorded at all.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.live.harness import ssl_context

SECRET_KEYS = frozenset({"token", "sha1", "password", "private_token", "token_last_eight"})


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "<redacted>" if k in SECRET_KEYS and v else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def pick(value: Any, keep: Sequence[str]) -> Any:
    """Only the ``keep`` keys of an object (or of each object in a list), so a
    transcript shows the fields a finding rests on rather than a hundred."""
    if not keep:
        return value
    if isinstance(value, dict):
        return {k: value[k] for k in keep if k in value}
    if isinstance(value, list):
        return [pick(v, keep) for v in value]
    return value


def trim(value: Any, *, items: int = 3) -> Any:
    """A response cut to what a reader needs to see its shape."""
    if isinstance(value, dict):
        return {k: trim(v, items=items) for k, v in value.items()}
    if isinstance(value, list):
        head = [trim(v, items=items) for v in value[:items]]
        return head + ([f"... {len(value) - items} more"] if len(value) > items else [])
    if isinstance(value, str) and len(value) > 160:
        return value[:157] + "..."
    return value


@dataclass
class Response:
    status: int
    data: Any
    headers: dict[str, str]

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass
class Recorder:
    exchanges: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Client:
    """One identity on one forge: ``base`` is the API root, ``auth`` the
    header that carries the credential, ``who`` the permission level an
    evidence transcript names."""

    base: str
    auth: dict[str, str]
    who: str
    recorder: Recorder | None = None

    @classmethod
    def basic(cls, base: str, user: str, password: str, who: str) -> Client:
        pair = base64.b64encode(f"{user}:{password}".encode()).decode()
        return cls(base, {"Authorization": f"Basic {pair}"}, who)

    def recording(self, recorder: Recorder) -> Client:
        return Client(self.base, self.auth, self.who, recorder)

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        check: bool = True,
        note: str = "",
        keep: Sequence[str] = (),
    ) -> Response:
        url = self.base.rstrip("/") + path
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "User-Agent": "sbxloop-live-harness",
                **({"Content-Type": "application/json"} if data is not None else {}),
                **self.auth,
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120, context=ssl_context()) as resp:  # nosec B310 - https root the harness serves
                status, raw, got = resp.status, resp.read(), dict(resp.headers.items())
        except urllib.error.HTTPError as exc:
            status, raw, got = exc.code, exc.read(), dict(exc.headers.items())
        text = raw.decode("utf-8", errors="replace")
        try:
            parsed: Any = json.loads(text) if text.strip() else None
        except ValueError:
            parsed = text[:500]
        split = urllib.parse.urlsplit(url)
        if self.recorder is not None:
            entry: dict[str, Any] = {
                "as": self.who,
                "request": f"{method} {split.path}" + (f"?{split.query}" if split.query else ""),
            }
            if body is not None:
                entry["body"] = redact(body)
            entry["status"] = status
            entry["response"] = trim(redact(pick(parsed, keep)))
            if note:
                entry["note"] = note
            self.recorder.exchanges.append(entry)
        if check and not 200 <= status < 300:
            raise RuntimeError(f"{method} {split.path} -> {status}: {text[:400]}")
        return Response(status, parsed, {k.lower(): v for k, v in got.items()})

    def get(self, path: str, **kwargs: Any) -> Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, body: Any = None, **kwargs: Any) -> Response:
        return self.request("POST", path, body, **kwargs)

    def put(self, path: str, body: Any = None, **kwargs: Any) -> Response:
        return self.request("PUT", path, body, **kwargs)

    def patch(self, path: str, body: Any = None, **kwargs: Any) -> Response:
        return self.request("PATCH", path, body, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Response:
        return self.request("DELETE", path, **kwargs)


def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=value`` lines; blank lines and ``#`` comments skipped."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def write_env_file(path: Path, values: dict[str, str]) -> None:
    """Merge ``values`` into the env file, keeping what the other seed wrote."""
    merged = {**read_env_file(path), **values}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{k}={v}\n" for k, v in sorted(merged.items())))
