"""The published contract is the committed one (#1041): ``docs/openapi.json``
is what ``/v1/openapi.json`` serves, version aside. A route or model that
changes the document changes the file in the same change — regenerate it
with ``sbxloop api openapi --snapshot --write docs/openapi.json``."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sbxloop.api.app import SNAPSHOT_VERSION, openapi_document
from sbxloop.cli.api import api_app
from tests.api.conftest import Api

SNAPSHOT = Path(__file__).resolve().parents[2] / "docs" / "openapi.json"
REGENERATE = "regenerate it: uv run sbxloop api openapi --snapshot --write docs/openapi.json"


def _normalised(document: dict[str, object]) -> str:
    copy = json.loads(json.dumps(document))
    copy["info"]["version"] = SNAPSHOT_VERSION
    return json.dumps(copy, indent=2, sort_keys=True) + "\n"


def test_the_committed_contract_is_what_the_listener_serves(api: Api) -> None:
    served = api.client.get("/v1/openapi.json").json()
    assert _normalised(served) == SNAPSHOT.read_text(), REGENERATE


def test_the_document_builds_without_a_daemon_and_the_cli_prints_it(tmp_path: Path) -> None:
    document = openapi_document(snapshot=True)
    assert document["info"] == {"title": "sbxloop", "version": SNAPSHOT_VERSION}
    assert "/v1/status" in document["paths"] and "/v1/ws" not in document["paths"]
    result = CliRunner().invoke(api_app, ["openapi", "--snapshot"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["paths"].keys() == document["paths"].keys()
    out = tmp_path / "openapi.json"
    written = CliRunner().invoke(api_app, ["openapi", "--snapshot", "--write", str(out)])
    assert written.exit_code == 0 and out.read_text() == SNAPSHOT.read_text()
