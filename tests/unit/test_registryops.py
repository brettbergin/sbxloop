"""Fixed registry operations preserve bytes and never evaluate project metadata."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from sbxloop_worker import registryops
from sbxloop_worker.protocol import JobRequest
from sbxloop_worker.registryops import CATALOGUE_ENV, RegistryFetchError, execute_fetch
from sbxloop_worker.runner import JobRunner
from sbxloop_worker.serviceops import FAKE_ENV

TOKEN = "TEST_ONLY_ARTIFACT_CREDENTIAL_183a"


def scripted_env(
    tmp_path: Path, body: bytes, *, kind: str = "pypi", status: int = 200
) -> dict[str, str]:
    script = tmp_path / "responses.json"
    script.write_text(
        json.dumps(
            {"responses": [{"status": status, "body_base64": base64.b64encode(body).decode()}]}
        )
    )
    return {
        CATALOGUE_ENV: json.dumps(
            [
                {
                    "name": "private",
                    "kind": kind,
                    "url": "https://registry.example.test/index/",
                    "env": "REG_TOKEN",
                    "user": "reader",
                }
            ]
        ),
        "REG_TOKEN": TOKEN,
        FAKE_ENV: str(script),
    }


@pytest.mark.parametrize("kind", ["npm", "pypi", "go", "cargo", "maven", "nuget", "gem"])
def test_registry_bytes_and_authentication_are_preserved(tmp_path: Path, kind: str) -> None:
    body = b"opaque package bytes\x00\xff\n"
    env = scripted_env(tmp_path, body, kind=kind)
    artifact = tmp_path / "result.artifact"
    result = execute_fetch(
        {"registry": "private", "path": "/packages/a"}, artifact, timeout_s=5, env=env
    )
    assert artifact.read_bytes() == body
    assert result["sha256"] == hashlib.sha256(body).hexdigest()
    assert result["bytes"] == len(body)
    request = json.loads((tmp_path / "responses.json.requests.jsonl").read_text())
    assert request["method"] == "GET"
    assert request["url"] == "https://registry.example.test/packages/a"
    expected = (
        "Bearer " + TOKEN
        if kind == "npm"
        else TOKEN
        if kind == "cargo"
        else "Basic " + base64.b64encode(f"reader:{TOKEN}".encode()).decode()
    )
    assert request["headers"]["Authorization"] == expected
    assert TOKEN not in json.dumps(result)


@pytest.mark.parametrize(
    "path",
    [
        "//elsewhere.test/a",
        "https://elsewhere.test/a",
        "/%0d%0aAuthorization:x",
        "/a#fragment",
        "/%5celsewhere",
    ],
)
def test_request_cannot_choose_another_authority_or_inject_headers(
    tmp_path: Path, path: str
) -> None:
    env = scripted_env(tmp_path, b"data")
    with pytest.raises(RegistryFetchError, match="absolute path"):
        execute_fetch(
            {"registry": "private", "path": path}, tmp_path / "artifact", timeout_s=5, env=env
        )
    assert not (tmp_path / "responses.json.requests.jsonl").exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://registry.example.test",
        "file:///etc/passwd",
        "https://reader:password@registry.example.test",
    ],
)
def test_insecure_or_credential_bearing_catalogue_urls_are_refused(
    tmp_path: Path, url: str
) -> None:
    env = scripted_env(tmp_path, b"data")
    entries = json.loads(env[CATALOGUE_ENV])
    entries[0]["url"] = url
    env[CATALOGUE_ENV] = json.dumps(entries)
    with pytest.raises(RegistryFetchError, match="HTTPS authority"):
        execute_fetch(
            {"registry": "private", "path": "/a"}, tmp_path / "artifact", timeout_s=5, env=env
        )


@pytest.mark.parametrize("encoding", ["raw", "base64"])
def test_response_cannot_copy_a_credential_back_even_across_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encoding: str
) -> None:
    secret = TOKEN.encode() if encoding == "raw" else base64.b64encode(TOKEN.encode())
    env = scripted_env(tmp_path, b"prefix" + secret + b"suffix")
    monkeypatch.setattr(registryops, "CHUNK_BYTES", 7)
    with pytest.raises(RegistryFetchError, match="contained a service credential"):
        execute_fetch(
            {"registry": "private", "path": "/a"}, tmp_path / "artifact", timeout_s=5, env=env
        )
    assert not (tmp_path / "artifact").exists()
    assert not (tmp_path / "artifact.partial").exists()


def test_other_workload_credentials_are_also_kept_out_of_artifacts(tmp_path: Path) -> None:
    env = scripted_env(tmp_path, b"other-workload-secret")
    env["SBXLOOP_SERVICE_CREDENTIALS"] = json.dumps([{"env": "WORKLOAD_TOKEN"}])
    env["WORKLOAD_TOKEN"] = "other-workload-secret"
    with pytest.raises(RegistryFetchError, match="contained a service credential"):
        execute_fetch(
            {"registry": "private", "path": "/a"}, tmp_path / "artifact", timeout_s=5, env=env
        )


def test_integrity_mismatch_removes_the_partial_artifact(tmp_path: Path) -> None:
    env = scripted_env(tmp_path, b"unexpected content")
    with pytest.raises(RegistryFetchError, match="SHA-256"):
        execute_fetch(
            {"registry": "private", "path": "/a", "sha256": "0" * 64},
            tmp_path / "artifact",
            timeout_s=5,
            env=env,
        )
    assert not (tmp_path / "artifact").exists()
    assert not (tmp_path / "artifact.partial").exists()


def test_size_limit_is_enforced_while_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = scripted_env(tmp_path, b"too much data")
    monkeypatch.setattr(registryops, "MAX_ARTIFACT_BYTES", 5)
    with pytest.raises(RegistryFetchError, match="limit"):
        execute_fetch(
            {"registry": "private", "path": "/a"}, tmp_path / "artifact", timeout_s=5, env=env
        )
    assert not (tmp_path / "artifact").exists()


def test_worker_fetch_has_no_command_or_workspace_and_keeps_bytes_out_of_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"print('this is downloaded code, never imported')\n"
    for key, value in scripted_env(tmp_path, body).items():
        monkeypatch.setenv(key, value)
    job = JobRequest(
        job_id="fetch",
        run_id="run",
        kind="service.fetch",
        params={"registry": "private", "path": "/package"},
    )
    result = JobRunner(job, tmp_path / "events", tmp_path / "result.json", heartbeat_s=0).run()
    assert result.status == "ok"
    assert (tmp_path / "result.artifact").read_bytes() == body
    events = (tmp_path / "events").read_text()
    assert TOKEN not in events
    assert "downloaded code" not in events
    assert job.argv is None and job.cwd is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("argv", ["pip", "download", "."]),
        ("cwd", "/workspace"),
        ("commands", ["anything"]),
        ("prompt", "run code"),
    ],
)
def test_worker_contract_rejects_executable_fetch_requests(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=r"service.fetch"):
        JobRequest.model_validate(
            {
                "job_id": "f",
                "run_id": "r",
                "kind": "service.fetch",
                "params": {"registry": "private", "path": "/a"},
                field: value,
            }
        )
