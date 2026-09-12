"""`sbxloop doctor`, `sbxloop list-models` and the model catalog against
a configured endpoint: the listing, its absence, an unreachable endpoint,
and a catalog whose endpoint no longer matches the config."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sbxloop import modelcatalog
from sbxloop.backends import backend_named
from sbxloop.cli import doctor, models
from sbxloop.cli.app import app
from sbxloop.config import Config
from sbxloop.errors import SbxloopError
from sbxloop.paths import SbxloopHome
from sbxloop.sbx.cli import SbxCLI
from tests.conftest import FakeSbx

runner = CliRunner()
LISTING = {
    "object": "list",
    "data": [
        {"id": "served-model", "object": "model", "owned_by": "vllm"},
        {"id": "other-model", "object": "model", "display_name": "Other"},
    ],
}


def openai_config(base_url: str = "https://models.example.com/v1", **extra: Any) -> Config:
    return Config.model_validate(
        {"agent": {"backend": "openai", "openai": {"base_url": base_url, **extra}}}
    )


def opener(answer: bytes | Exception, seen: list[Any]) -> models.OpenUrl:
    def open_url(request: Any, timeout_s: float) -> bytes:
        seen.append(request)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return open_url


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://models.example.com/v1/models", code, "x", {}, None)  # type: ignore[arg-type]


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", "300")
    monkeypatch.setenv("SBXLOOP_HOME", str(tmp_path / "home"))
    for name in ("COPILOT_GITHUB_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "VLLM_KEY"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "sbxloop.toml").write_text(
        'model = "served-model"\n[agent]\nbackend = "openai"\n'
        '[agent.openai]\nbase_url = "https://models.example.com/v1"\napi_key_env = "VLLM_KEY"\n'
    )
    return tmp_path


# -- the listing --------------------------------------------------------------


def test_listing_is_requested_with_the_configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    config = openai_config(
        "http://vllm:8000/v1/", api_key_env="VLLM_KEY", allow_insecure_endpoint=True
    )
    records = models.fetch_openai_models(
        config, env={"VLLM_KEY": "placeholder"}, open_url=opener(json.dumps(LISTING).encode(), seen)
    )
    assert [r["id"] for r in records] == ["served-model", "other-model"]
    assert seen[0].full_url == "http://vllm:8000/v1/models"
    assert seen[0].get_header("Authorization") == "Bearer placeholder"


def test_missing_key_names_the_configured_variable_and_endpoint() -> None:
    config = openai_config(api_key_env="VLLM_KEY")
    with pytest.raises(SbxloopError, match=r"VLLM_KEY is not set.*models\.example\.com"):
        models.fetch_openai_models(config, env={}, open_url=opener(b"{}", []))


def test_404_is_no_listing_not_a_failure() -> None:
    with pytest.raises(models.NoModelListing, match="serves no model listing"):
        models.fetch_openai_models(
            openai_config(), env={"OPENAI_API_KEY": "k"}, open_url=opener(http_error(404), [])
        )


def test_refused_key_names_the_variable_never_the_value() -> None:
    with pytest.raises(
        SbxloopError, match="HTTP 401 — the key in OPENAI_API_KEY was refused"
    ) as err:
        models.fetch_openai_models(
            openai_config(),
            env={"OPENAI_API_KEY": "sk-secret"},
            open_url=opener(http_error(401), []),
        )
    assert "sk-secret" not in str(err.value)


def test_unreachable_endpoint_is_named() -> None:
    refused = urllib.error.URLError("[Errno 111] Connection refused")
    with pytest.raises(SbxloopError, match=r"models\.example\.com.*Connection refused"):
        models.fetch_openai_models(
            openai_config(), env={"OPENAI_API_KEY": "k"}, open_url=opener(refused, [])
        )


@pytest.mark.parametrize("body", [b"<html>", b"[]", b'{"data": "no"}'])
def test_a_reply_that_is_not_a_listing_is_an_error(body: bytes) -> None:
    with pytest.raises(SbxloopError, match=r"not (JSON|a model list)"):
        models.fetch_openai_models(
            openai_config(), env={"OPENAI_API_KEY": "k"}, open_url=opener(body, [])
        )


def test_row_is_id_and_name_only() -> None:
    row = models.openai_model_row(LISTING["data"][0])
    assert (row.id, row.name) == ("served-model", "vllm")
    assert models.openai_model_row(LISTING["data"][1]).name == "Other"
    assert row.multiplier is None and row.context_window is None and not row.vision
    assert models.table_columns(backend_named("openai")) == ("model", "name")


def test_fetch_backend_rows_needs_the_config_for_openai() -> None:
    with pytest.raises(SbxloopError, match="configured endpoint"):
        models.fetch_backend_rows(backend_named("openai"))


# -- the command ------------------------------------------------------------------


def test_command_lists_the_endpoints_models(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_KEY", "placeholder")
    monkeypatch.setattr(models, "_open_url", opener(json.dumps(LISTING).encode(), []))
    result = runner.invoke(app, ["list-models"])
    assert result.exit_code == 0, result.output
    assert "openai models" in result.output
    assert "other-model" in result.output
    assert "◀ = configured model (served-model)" in result.output
    for column in ("billing", "context", "reasoning", "created"):
        assert column not in result.output
    catalog = modelcatalog.load_catalog(
        SbxloopHome(workdir / "home"),
        backend_named("openai"),
        endpoint="https://models.example.com/v1",
    )
    assert catalog is not None
    assert [m.id for m in catalog.models] == ["served-model", "other-model"]
    assert catalog.endpoint == "https://models.example.com/v1"


def test_command_json_is_the_listing_records(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VLLM_KEY", "placeholder")
    monkeypatch.setattr(models, "_open_url", opener(json.dumps(LISTING).encode(), []))
    result = runner.invoke(app, ["list-models", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == LISTING["data"]


def test_command_reports_an_absent_listing_and_keeps_the_model(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VLLM_KEY", "placeholder")
    monkeypatch.setattr(models, "_open_url", opener(http_error(404), []))
    result = runner.invoke(app, ["list-models"])
    assert result.exit_code == 0, result.output
    assert "serves no model listing" in result.output
    assert "configured model (served-model) is still valid" in result.output
    assert not list((workdir / "home").rglob("openai.json"))
    result = runner.invoke(app, ["list-models", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []


def test_command_without_the_key_exits_2(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "_open_url", lambda r, t: pytest.fail("no request without a key"))
    result = runner.invoke(app, ["list-models"])
    assert result.exit_code == 2
    assert "VLLM_KEY is not set" in result.output


# -- the catalog ------------------------------------------------------------------


def test_catalog_is_keyed_by_endpoint(tmp_path: Path) -> None:
    home, backend = SbxloopHome(tmp_path), backend_named("openai")
    row = models.openai_model_row({"id": "served-model"})
    catalog = modelcatalog.save_catalog(home, backend, [row], endpoint="https://a.example/v1")
    assert modelcatalog.load_catalog(home, backend, endpoint="https://a.example/v1") == catalog
    assert modelcatalog.load_catalog(home, backend, endpoint="https://b.example/v1") is None
    assert modelcatalog.load_catalog(home, backend) is None
    assert modelcatalog.catalog_endpoint(openai_config("https://a.example/v1")) == (
        "https://a.example/v1"
    )
    assert modelcatalog.catalog_endpoint(Config()) is None


def test_vendor_catalogs_carry_no_endpoint(tmp_path: Path) -> None:
    home, backend = SbxloopHome(tmp_path), backend_named("codex")
    row = models.openai_model_row({"id": "m"})
    catalog = modelcatalog.save_catalog(home, backend, [row])
    assert catalog.endpoint is None
    assert modelcatalog.load_catalog(home, backend) == catalog
    assert "endpoint" in json.loads((home.model_catalogs / "codex.json").read_text())


def test_refresh_after_provision_refetches_when_the_endpoint_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str | None] = []

    def discover(backend: Any, timeout_s: float, config: Config | None = None) -> list[Any]:
        calls.append(modelcatalog.catalog_endpoint(config) if config else None)
        return [models.openai_model_row({"id": "m"})]

    monkeypatch.setattr(modelcatalog, "fetch_backend_rows", discover)
    monkeypatch.setattr(modelcatalog, "REFRESH_AFTER_S", 10**9)
    modelcatalog._retry_at.clear()
    first = Config.model_validate(
        {
            "home": str(tmp_path),
            "agent": {"backend": "openai", "openai": {"base_url": "https://a.example/v1"}},
        }
    )
    thread = modelcatalog.refresh_after_provision(first)
    assert thread is not None
    thread.join(5)
    assert modelcatalog.refresh_after_provision(first) is None  # fresh for this endpoint
    moved = first.model_copy(
        update={
            "agent": first.agent.model_copy(
                update={
                    "openai": first.agent.openai.model_copy(
                        update={"base_url": "https://b.example/v1"}
                    )
                }
            )
        }
    )
    thread = modelcatalog.refresh_after_provision(moved)
    assert thread is not None
    thread.join(5)
    assert calls == ["https://a.example/v1", "https://b.example/v1"]
    home = SbxloopHome(tmp_path)
    assert (
        modelcatalog.load_catalog(home, backend_named("openai"), endpoint="https://a.example/v1")
        is None
    )
    assert (
        modelcatalog.load_catalog(home, backend_named("openai"), endpoint="https://b.example/v1")
        is not None
    )


# -- doctor -----------------------------------------------------------------------


def checks(env: dict[str, str], fake_sbx: FakeSbx) -> dict[str, doctor.Check]:
    return {c.name: c for c in doctor.collect_checks(env, cli=SbxCLI(binary=str(fake_sbx.binary)))}


def test_doctor_names_the_credential_endpoint_and_policy_host(
    workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        doctor, "installed_sdk_permission_kinds", lambda: pytest.fail("Copilot SDK probed")
    )
    monkeypatch.setattr(models, "_open_url", opener(json.dumps(LISTING).encode(), []))
    rows = checks({"VLLM_KEY": "placeholder"}, fake_sbx)
    credential = rows["VLLM_KEY (agent backend: openai)"]
    assert (
        credential.ok and credential.detail == "set — bound to the endpoint at models.example.com"
    )
    assert "policy: models.example.com" in rows
    endpoint = rows["endpoint: models.example.com"]
    assert endpoint.ok and not endpoint.hard
    assert endpoint.detail.startswith("answers from the host (served https://models.example.com")
    assert "agent sandbox can reach it is a separate question" in endpoint.detail
    assert not any("copilot" in name.lower() for name in rows)
    assert "OPENAI_API_KEY (agent backend: openai)" not in rows


def test_doctor_reports_an_endpoint_that_answers_404_as_answering(
    workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models, "_open_url", opener(http_error(404), []))
    endpoint = checks({"VLLM_KEY": "placeholder"}, fake_sbx)["endpoint: models.example.com"]
    assert endpoint.ok and "HTTP 404" in endpoint.detail


def test_doctor_reports_an_unreachable_endpoint_from_the_host(
    workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = urllib.error.URLError("[Errno 111] Connection refused")
    monkeypatch.setattr(models, "_open_url", opener(refused, []))
    rows = checks({"VLLM_KEY": "placeholder"}, fake_sbx)
    endpoint = rows["endpoint: models.example.com"]
    assert not endpoint.ok and not endpoint.hard
    assert "no answer from the host" in endpoint.detail and "Connection refused" in endpoint.detail
    missing = checks({}, fake_sbx)["VLLM_KEY (agent backend: openai)"]
    assert not missing.ok and "models.example.com" in missing.detail


def test_doctor_checks_each_repositorys_endpoint_host(
    workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workdir / "sbxloop.toml").write_text(
        '[agent]\nbackend = "openai"\n[agent.openai]\nbase_url = "https://models.example.com/v1"\n'
        '[[github.repos]]\nrepo = "o/private"\n'
        '[github.repos.openai]\nbase_url = "https://private.example.com:8443/v1"\n'
        '[[github.repos]]\nrepo = "o/other"\n'
    )
    monkeypatch.setattr(models, "_open_url", opener(json.dumps(LISTING).encode(), []))
    rows = checks({"OPENAI_API_KEY": "k", "GH_TOKEN": "t"}, fake_sbx)
    assert "policy: models.example.com" in rows
    assert "policy: private.example.com" in rows
    assert [name for name in rows if name.startswith("policy: ")].count(
        "policy: models.example.com"
    ) == 1
