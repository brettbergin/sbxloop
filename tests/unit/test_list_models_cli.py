"""`sbxloop list-models`: host-side SDK model listing.

The real SDK is never installed in the unit environment; these tests inject
a stub `copilot` module into sys.modules shaped like the field-verified
github-copilot-sdk 1.0.8 surface (async CopilotClient context manager with
`list_models() -> list[ModelInfo]`), so the whole command path — deferred
import, asyncio bridge, row flattening, rendering — runs for real.
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sbxloop.cli.app import app
from sbxloop.cli.models import (
    auth_hint,
    fetch_models,
    format_context,
    format_efforts,
    model_row,
)
from sbxloop.errors import SbxloopError

runner = CliRunner()


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    # rich wraps 80-col tables mid-cell under CliRunner; keep asserts literal.
    monkeypatch.setenv("COLUMNS", "300")
    return tmp_path


# --- stub SDK objects (mirror ModelInfo's verified attribute shape) ---


@dataclass
class StubSupports:
    vision: bool = False
    reasoning_effort: bool = False


@dataclass
class StubLimits:
    max_context_window_tokens: int | None = None


@dataclass
class StubCapabilities:
    supports: StubSupports = field(default_factory=StubSupports)
    limits: StubLimits = field(default_factory=StubLimits)


@dataclass
class StubBilling:
    multiplier: float | None = None


@dataclass
class StubPolicy:
    state: str = "enabled"


@dataclass
class StubModel:
    id: str
    name: str
    capabilities: StubCapabilities = field(default_factory=StubCapabilities)
    billing: StubBilling | None = None
    policy: StubPolicy | None = None
    supported_reasoning_efforts: list[str] | None = None
    default_reasoning_effort: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name}


SAMPLE_MODELS = [
    StubModel(
        id="claude-sonnet-4.5",
        name="Claude Sonnet 4.5",
        capabilities=StubCapabilities(
            supports=StubSupports(vision=True, reasoning_effort=True),
            limits=StubLimits(max_context_window_tokens=200_000),
        ),
        billing=StubBilling(multiplier=1.0),
        policy=StubPolicy(state="enabled"),
        supported_reasoning_efforts=["low", "medium", "high"],
        default_reasoning_effort="medium",
    ),
    StubModel(id="gpt-5-mini", name="GPT-5 mini", billing=StubBilling(multiplier=0.0)),
]


def install_stub_sdk(
    monkeypatch: pytest.MonkeyPatch,
    models: list[Any] | None = None,
    error: Exception | None = None,
) -> None:
    """Publish a `copilot` module whose CopilotClient serves `models`."""

    class StubClient:
        async def __aenter__(self) -> StubClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def list_models(self) -> list[Any]:
            if error is not None:
                raise error
            return list(models or [])

    module = types.ModuleType("copilot")
    module.CopilotClient = StubClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "copilot", module)


class TestFetchModels:
    def test_missing_sdk_is_actionable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "copilot", None)  # forces ImportError
        with pytest.raises(SbxloopError, match="github-copilot-sdk is not installed"):
            fetch_models()

    def test_returns_sdk_models(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_stub_sdk(monkeypatch, models=SAMPLE_MODELS)
        assert [m.id for m in fetch_models()] == ["claude-sonnet-4.5", "gpt-5-mini"]

    def test_sdk_error_carries_auth_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_stub_sdk(monkeypatch, error=RuntimeError("not authenticated"))
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with pytest.raises(SbxloopError, match="not authenticated") as excinfo:
            fetch_models()
        assert "COPILOT_GITHUB_TOKEN" in str(excinfo.value)


class TestRowFlattening:
    def test_full_model(self) -> None:
        row = model_row(SAMPLE_MODELS[0])
        assert row.id == "claude-sonnet-4.5"
        assert row.multiplier == 1.0
        assert row.context_window == 200_000
        assert row.vision is True
        assert row.reasoning_efforts == ("low", "medium", "high")
        assert row.default_reasoning_effort == "medium"
        assert row.policy_state == "enabled"
        assert row.raw == {"id": "claude-sonnet-4.5", "name": "Claude Sonnet 4.5"}

    def test_sparse_model_degrades_to_blanks(self) -> None:
        row = model_row(types.SimpleNamespace(id="bare", name="Bare"))
        assert row.id == "bare"
        assert row.multiplier is None
        assert row.context_window is None
        assert row.vision is False
        assert row.reasoning_efforts is None
        assert row.policy_state is None
        assert row.raw == {}

    def test_formatting_helpers(self) -> None:
        assert format_context(200_000) == "200k"
        assert format_context(512) == "512"
        assert format_context(None) == ""
        assert format_efforts(model_row(SAMPLE_MODELS[0])) == "low, medium*, high"
        assert format_efforts(model_row(SAMPLE_MODELS[1])) == ""

    def test_auth_hint_names_the_set_var(self) -> None:
        assert "GH_TOKEN is set" in auth_hint({"GH_TOKEN": "x"})
        assert "none of" in auth_hint({})


class TestListModelsCommand:
    def test_table_lists_models(self, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        install_stub_sdk(monkeypatch, models=SAMPLE_MODELS)
        result = runner.invoke(app, ["list-models"])
        assert result.exit_code == 0
        assert "claude-sonnet-4.5" in result.output
        assert "gpt-5-mini" in result.output
        assert "medium*" in result.output
        assert "auto" in result.output  # configured-model footnote

    def test_configured_model_is_marked(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_stub_sdk(monkeypatch, models=SAMPLE_MODELS)
        monkeypatch.setenv("SBXLOOP_MODEL", "gpt-5-mini")
        result = runner.invoke(app, ["list-models"])
        assert result.exit_code == 0
        assert "◀ = configured model (gpt-5-mini)" in result.output

    def test_configured_model_absent_warns(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_stub_sdk(monkeypatch, models=SAMPLE_MODELS)
        monkeypatch.setenv("SBXLOOP_MODEL", "gone-4")
        result = runner.invoke(app, ["list-models"])
        assert result.exit_code == 0
        assert "not in this list" in result.output

    def test_json_is_bare_and_parseable(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_stub_sdk(monkeypatch, models=SAMPLE_MODELS)
        result = runner.invoke(app, ["list-models", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert [entry["id"] for entry in payload] == ["claude-sonnet-4.5", "gpt-5-mini"]

    def test_empty_list_says_so(self, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        install_stub_sdk(monkeypatch, models=[])
        result = runner.invoke(app, ["list-models"])
        assert result.exit_code == 0
        assert "no models" in result.output

    def test_missing_sdk_exits_2_with_hint(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "copilot", None)
        result = runner.invoke(app, ["list-models"])
        assert result.exit_code == 2
        assert "github-copilot-sdk is not installed" in result.output
        # the extra must survive rich rendering (`[copilot]` is not markup)
        assert "sbxloop[copilot]" in result.output

    def test_sdk_failure_exits_2(self, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        install_stub_sdk(monkeypatch, error=RuntimeError("boom"))
        result = runner.invoke(app, ["list-models"])
        assert result.exit_code == 2
        assert "list-models failed" in result.output
