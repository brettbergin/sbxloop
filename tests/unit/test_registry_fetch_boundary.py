"""Registry credentials must never be beside executable project metadata."""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from sbxloop.sbx.registries import fetch_plan
from sbxloop_worker.protocol import JobRequest
from sbxloop_worker.registryops import CATALOGUE_ENV, execute_fetch
from sbxloop_worker.runner import JobRunner
from sbxloop_worker.serviceops import FAKE_ENV

DUMMY = "TEST_ONLY_REGISTRY_SECRET_97b3"


@pytest.mark.parametrize("source", ["environment", "client-file"])
def test_legacy_dependency_fetch_cannot_execute_project_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    pip = shutil.which("pip")
    assert pip is not None, "the metadata-execution regression requires pip"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service_home = tmp_path / "service-home"
    service_home.mkdir()
    marker = workspace / "credential-copied-by-metadata"
    monkeypatch.setenv("HOME", str(service_home))
    monkeypatch.setenv("PIP_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    if source == "environment":
        monkeypatch.setenv("TEST_REGISTRY_TOKEN", DUMMY)
    else:
        monkeypatch.delenv("TEST_REGISTRY_TOKEN", raising=False)
        (service_home / ".netrc").write_text(DUMMY)
    (workspace / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "project_backend"\nbackend-path = ["."]\n'
    )
    (workspace / "project_backend.py").write_text(
        "import base64, os\nfrom pathlib import Path\n"
        "def get_requires_for_build_wheel(config_settings=None):\n"
        "    value = os.environ.get('TEST_REGISTRY_TOKEN')\n"
        "    if value is None:\n"
        "        value = (Path.home() / '.netrc').read_text()\n"
        f"    Path({str(marker)!r}).write_bytes(base64.b64encode(value.encode()))\n"
        "    raise RuntimeError('stop after the harmless metadata probe')\n"
    )
    plan = fetch_plan("pypi", "fetch", manifests=["pyproject.toml"])
    argv = list(plan.argv)
    argv[0] = pip
    argv[argv.index("-d") + 1] = str(tmp_path / "download-cache")
    try:
        job = JobRequest(
            job_id="legacy-fetch",
            run_id="r",
            kind="service.fetch",
            argv=argv,
            cwd=str(workspace),
            params={"ecosystem": "pypi", "verb": "fetch", "scrub_env": ["TEST_REGISTRY_TOKEN"]},
            timeout_s=30,
        )
    except ValidationError as exc:
        assert "service.fetch" in str(exc)
    else:
        JobRunner(job, tmp_path / "events", tmp_path / "result", heartbeat_s=0).run()
    assert not marker.exists(), "pip metadata ran with the service's credential access"
    if (tmp_path / "result").exists():
        assert base64.b64encode(DUMMY.encode()) not in (tmp_path / "result").read_bytes()


def test_downloaded_metadata_runs_only_in_the_credential_free_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pip = shutil.which("pip")
    assert pip is not None, "the metadata-execution regression requires pip"
    workspace = tmp_path / "agent-workspace"
    workspace.mkdir()
    marker = workspace / "metadata-executed"
    backend = (
        "import os\nfrom pathlib import Path\n"
        "def get_requires_for_build_wheel(config_settings=None):\n"
        "    value = os.environ.get('TEST_REGISTRY_TOKEN', '')\n"
        "    netrc = Path.home() / '.netrc'\n"
        "    if netrc.exists():\n"
        "        value += netrc.read_text()\n"
        f"    Path({str(marker)!r}).write_text(value)\n"
        "    raise RuntimeError('metadata executed in the agent')\n"
    ).encode()
    script = tmp_path / "response.json"
    script.write_text(
        json.dumps(
            {"responses": [{"status": 200, "body_base64": base64.b64encode(backend).decode()}]}
        )
    )
    service_env = {
        CATALOGUE_ENV: json.dumps(
            [
                {
                    "name": "private",
                    "kind": "pypi",
                    "url": "https://registry.example.test",
                    "user": "reader",
                    "env": "TEST_REGISTRY_TOKEN",
                }
            ]
        ),
        "TEST_REGISTRY_TOKEN": DUMMY,
        FAKE_ENV: str(script),
    }
    artifact = tmp_path / "service.artifact"
    execute_fetch(
        {"registry": "private", "path": "/backend.py"}, artifact, timeout_s=5, env=service_env
    )
    assert not marker.exists(), "the service must treat project code as bytes"
    shutil.copyfile(artifact, workspace / "project_backend.py")
    (workspace / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "project_backend"\nbackend-path = ["."]\n'
    )
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir()
    monkeypatch.setenv("HOME", str(agent_home))
    monkeypatch.delenv("TEST_REGISTRY_TOKEN", raising=False)
    monkeypatch.setenv("PIP_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    job = JobRequest(
        job_id="agent-metadata",
        run_id="r",
        kind="shell.check",
        argv=[pip, "download", "--no-deps", "--no-cache-dir", "-d", str(tmp_path / "cache"), "."],
        cwd=str(workspace),
        timeout_s=30,
    )
    result = JobRunner(job, tmp_path / "events", tmp_path / "result", heartbeat_s=0).run()
    assert result.exit_code != 0  # the controlled metadata probe stops pip
    assert marker.exists(), result.output_text
    assert marker.read_text() == "", "metadata must not see env or client-file credentials"
