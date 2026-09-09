"""Fetch registry data without loading manifests, build tools or project code.

The service owns the catalogue and credentials. A request names a catalogue
entry and a path; it cannot supply a host, header, environment or command.
Artifacts stay beside the worker result until the host copies them out.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from sbxloop_worker.protocol import RegistryFetchParams
from sbxloop_worker.serviceops import FAKE_ENV, FakeTransport, _NoRedirect

CATALOGUE_ENV = "SBXLOOP_REGISTRIES"
MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


class RegistryFetchError(RuntimeError):
    pass


def catalogue(env: Mapping[str, str]) -> dict[str, dict[str, str]]:
    try:
        entries = json.loads(env.get(CATALOGUE_ENV, "[]"))
        if not isinstance(entries, list):
            raise ValueError
        result = {}
        for entry in entries:
            if not isinstance(entry, dict) or not all(isinstance(v, str) for v in entry.values()):
                raise ValueError
            if not all(entry.get(key) for key in ("name", "kind", "url", "env")):
                raise ValueError
            if entry["name"] in result:
                raise ValueError
            result[entry["name"]] = entry
        return result
    except (ValueError, TypeError, KeyError) as exc:
        raise RegistryFetchError("invalid host-authored registry catalogue") from exc


def _authority(url: str) -> tuple[str, int]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise RegistryFetchError("registry downloads require the configured HTTPS authority")
    return parts.hostname.lower(), parts.port or 443


def request_url(entry: Mapping[str, str], path: str) -> str:
    base = entry["url"].removeprefix("sparse+")
    _authority(base)
    parts = urllib.parse.urlsplit(path)
    decoded = urllib.parse.unquote(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parts.scheme
        or parts.netloc
        or parts.fragment
        or "\\" in decoded
        or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
    ):
        raise RegistryFetchError("registry path must be an absolute path on its configured host")
    return "https://" + urllib.parse.urlsplit(base).netloc + path


def authorization(entry: Mapping[str, str], env: Mapping[str, str]) -> str:
    value = env.get(entry["env"], "")
    if not value:
        raise RegistryFetchError(f"registry credential {entry['env']} is not available")
    if any(char in value for char in "\r\n"):
        raise RegistryFetchError("registry credential contains a newline")
    if entry["kind"] == "npm":
        return "Bearer " + value
    if entry["kind"] == "cargo":
        return value
    user = entry.get("user", "")
    if not user or ":" in user or any(char in user for char in "\r\n"):
        raise RegistryFetchError("this registry requires its configured authentication username")
    return "Basic " + base64.b64encode(f"{user}:{value}".encode()).decode()


def _secret_bytes(env: Mapping[str, str], entries: Mapping[str, Mapping[str, str]]) -> list[bytes]:
    names = {entry["env"] for entry in entries.values()}
    # The same service VM may also hold workload credentials.
    for entry in json.loads(env.get("SBXLOOP_SERVICE_CREDENTIALS", "[]")):
        names.add(entry["env"])
    values = [env[name].encode() for name in names if env.get(name)]
    return [*values, *(base64.b64encode(value) for value in values)]


def _copy_data(
    stream: BinaryIO, destination: Path, secrets: list[bytes], deadline: float
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    tail = b""
    overlap = max((len(value) for value in secrets), default=1) - 1
    with destination.open("xb") as output:
        while chunk := stream.read(CHUNK_BYTES):
            if time.monotonic() >= deadline:
                raise RegistryFetchError("registry download timed out")
            size += len(chunk)
            if size > MAX_ARTIFACT_BYTES:
                raise RegistryFetchError("registry artifact exceeds the 1 GiB limit")
            window = tail + chunk
            if any(value in window for value in secrets):
                raise RegistryFetchError("registry response contained a service credential")
            tail = window[-overlap:] if overlap else b""
            digest.update(chunk)
            output.write(chunk)
    return size, digest.hexdigest()


@contextmanager
def _download(url: str, header: str, env: Mapping[str, str], timeout: float) -> Iterator[BinaryIO]:
    headers = {
        "Authorization": header,
        "User-Agent": "sbxloop-worker",
        "Accept-Encoding": "identity",
    }
    if env.get(FAKE_ENV):
        status, _, body = FakeTransport(Path(env[FAKE_ENV])).send(
            "GET", url, headers, None, timeout
        )
        if not 200 <= status < 300:
            raise RegistryFetchError(f"registry returned HTTP {status}")
        with io.BytesIO(body) as stream:
            yield stream
        return
    opener = urllib.request.build_opener(_NoRedirect())
    authority = _authority(url)
    for _ in range(6):
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            response = opener.open(request, timeout=timeout)  # nosec B310 - HTTPS authority checked before every request
        except urllib.error.HTTPError as exc:
            try:
                if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                    target = urllib.parse.urljoin(url, exc.headers["Location"])
                    if _authority(target) != authority:
                        raise RegistryFetchError(
                            "registry redirect crosses its credential authority"
                        )
                    url = target
                    continue
                raise RegistryFetchError(f"registry returned HTTP {exc.code}") from None
            finally:
                exc.close()
        except urllib.error.URLError as exc:
            raise RegistryFetchError("registry HTTPS request failed") from exc
        with response:
            yield response
        return
    raise RegistryFetchError("registry redirect limit exceeded")


def _git_bundle(url: str, ref: str, header: str, destination: Path, timeout: float) -> None:
    """Fetch objects into a fresh bare repository: no checkout or project config."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "GIT_ALLOW_PROTOCOL": "https",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
    )
    if os.environ.get("GIT_SSL_CAINFO"):
        env["GIT_SSL_CAINFO"] = os.environ["GIT_SSL_CAINFO"]
    settings = {
        "credential.helper": "",
        "http.extraHeader": f"Authorization: {header}",
        "http.followRedirects": "false",
        "core.hooksPath": os.devnull,
        "core.fsmonitor": "false",
        "init.templateDir": "",
    }
    env["GIT_CONFIG_COUNT"] = str(len(settings))
    for index, (key, value) in enumerate(settings.items()):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="sbxloop-registry-git-") as directory:
        for args in (
            ("init", "--bare", "."),
            ("fetch", "--no-tags", "--no-recurse-submodules", "--", url, ref),
            ("update-ref", "refs/sbxloop/dependency", "FETCH_HEAD"),
            ("bundle", "create", str(destination), "refs/sbxloop/dependency"),
        ):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RegistryFetchError("registry Git fetch timed out")
            result = subprocess.run(  # nosec B603 B607 - fixed Git ops, fresh bare metadata, HTTPS only, no checkout
                ["git", *args],
                cwd=directory,
                env=env,
                capture_output=True,
                timeout=remaining,
                check=False,
            )
            if result.returncode:
                # Remote error text can echo authentication headers. The
                # fixed operation and exit status suffice; never relay it.
                raise RegistryFetchError(
                    f"registry Git {args[0]} failed (exit {result.returncode})"
                )


def execute_fetch(
    params: Mapping[str, Any],
    artifact: Path,
    *,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    request = RegistryFetchParams.model_validate(params)
    env = os.environ if env is None else env
    entries = catalogue(env)
    if request.registry not in entries:
        raise RegistryFetchError("registry is not in this service sandbox's catalogue")
    entry = entries[request.registry]
    url = request_url(entry, request.path)
    header = authorization(entry, env)
    secrets = [*_secret_bytes(env, entries), header.encode()]
    deadline = time.monotonic() + timeout_s
    artifact.parent.mkdir(parents=True, exist_ok=True)
    # Neither temporary path is derived from a manifest or response.
    temporary = artifact.with_suffix(".partial")
    try:
        if request.operation == "git":
            with tempfile.TemporaryDirectory(prefix="sbxloop-registry-bundle-") as directory:
                bundle = Path(directory) / "dependency.bundle"
                _git_bundle(url, request.ref, header, bundle, timeout_s)
                with bundle.open("rb") as stream:
                    size, digest = _copy_data(stream, temporary, secrets, deadline)
        else:
            with _download(url, header, env, min(timeout_s, 300)) as stream:
                size, digest = _copy_data(stream, temporary, secrets, deadline)
        if request.sha256 is not None and digest != request.sha256:
            raise RegistryFetchError(
                "registry artifact SHA-256 does not match the requested digest"
            )
        temporary.replace(artifact)
        return {
            "bytes": size,
            "sha256": digest,
            "registry": request.registry,
            "operation": request.operation,
        }
    finally:
        temporary.unlink(missing_ok=True)
