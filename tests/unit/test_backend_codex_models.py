"""Model listing uses Codex's catalogue without starting a model turn."""

from __future__ import annotations

import json
import subprocess
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sbxloop.backends import backend_named
from sbxloop.cli import models
from sbxloop.cli.app import app
from sbxloop.errors import SbxloopError

MODEL = {
    "id": "catalogue-entry-id",
    "model": "example-codex-model",
    "displayName": "Example model",
    "inputModalities": ["text", "image"],
    "defaultReasoningEffort": "medium",
    "supportedReasoningEfforts": [
        {"reasoningEffort": "medium", "description": "Standard effort"},
        {"reasoningEffort": "high", "description": "More effort"},
    ],
}


def install_sdk(
    monkeypatch: pytest.MonkeyPatch,
    pages: list[tuple[list[dict[str, Any]], str | None]],
    *,
    error: Exception | None = None,
) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class Record:
        def __init__(self, value: dict[str, Any]) -> None:
            self.value = value

        def model_dump(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs == {"mode": "json", "by_alias": True}
            return self.value

    class Client:
        def request(self, method: str, params: dict[str, Any], **kwargs: Any) -> Any:
            assert method == "model/list"
            assert kwargs["response_model"] is response_type
            seen.append(params)
            if error is not None:
                raise error
            records, cursor = pages[len(seen) - 2]
            return types.SimpleNamespace(
                data=[Record(record) for record in records], next_cursor=cursor
            )

    @contextmanager
    def authenticated_client(**kwargs: Any) -> Iterator[Client]:
        seen.append(kwargs)
        yield Client()

    response_type = type("ModelListResponse", (), {})
    sdk_types = types.ModuleType("openai_codex.types")
    sdk_types.ModelListResponse = response_type  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai_codex", types.ModuleType("openai_codex"))
    monkeypatch.setitem(sys.modules, "openai_codex.types", sdk_types)
    runtime = types.ModuleType("sbxloop_worker.backends.codex_runtime")
    runtime.authenticated_client = authenticated_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sbxloop_worker.backends.codex_runtime", runtime)
    return seen


def test_lists_every_page_with_bounded_ephemeral_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = install_sdk(monkeypatch, [([MODEL], "next-page"), ([], None)])
    assert models.fetch_codex_models(timeout_s=12) == [MODEL]
    assert seen == [
        {"persistent": False, "timeout_s": 12},
        {"includeHidden": False, "cursor": None, "limit": 100},
        {"includeHidden": False, "cursor": "next-page", "limit": 100},
    ]


def test_missing_key_is_actionable_before_loading_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "openai_codex", None)
    with pytest.raises(SbxloopError, match="OPENAI_API_KEY is not set"):
        models.fetch_codex_models()


def test_missing_host_extra_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai_codex", None)
    monkeypatch.setitem(sys.modules, "openai_codex.types", None)
    with pytest.raises(SbxloopError, match=r"openai-codex.*sbxloop\[codex\]"):
        models.fetch_codex_models()


def test_duplicate_pagination_cursor_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    install_sdk(monkeypatch, [([MODEL], "same"), ([MODEL], "same")])
    with pytest.raises(SbxloopError, match="pagination"):
        models.fetch_codex_models()


def test_model_listing_timeout_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    install_sdk(monkeypatch, [], error=subprocess.TimeoutExpired("codex", 12))
    with pytest.raises(SbxloopError, match="timed out after 12s"):
        models.fetch_codex_models(timeout_s=12)


def test_sdk_error_does_not_expose_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "sk-proj-" + "A1b2C3d4" * 4
    install_sdk(monkeypatch, [], error=RuntimeError(f"rejected {token}"))
    with pytest.raises(SbxloopError, match="rejected") as error:
        models.fetch_codex_models()
    assert token not in str(error.value)


def test_model_row_uses_the_runnable_slug_and_supported_columns() -> None:
    row = models.codex_model_row(MODEL)
    assert row.id == "example-codex-model"
    assert row.name == "Example model"
    assert row.vision is True
    assert row.reasoning_efforts == ("medium", "high")
    assert row.default_reasoning_effort == "medium"
    assert row.raw == MODEL
    assert models.table_columns(backend_named("codex")) == ("model", "name", "vision", "reasoning")


def test_command_uses_codex_catalogue_for_table_and_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", "300")
    (tmp_path / "sbxloop.toml").write_text(
        'model = "example-codex-model"\n[agent]\nbackend = "codex"\n'
    )
    install_sdk(monkeypatch, [([MODEL], None)])
    runner = CliRunner()
    result = runner.invoke(app, ["list-models"])
    assert result.exit_code == 0, result.output
    assert "codex models" in result.output
    assert "example-codex-model" in result.output
    assert "configured model (example-codex-model)" in result.output
    assert "billing" not in result.output
    install_sdk(monkeypatch, [([MODEL], None)])
    result = runner.invoke(app, ["list-models", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [MODEL]
