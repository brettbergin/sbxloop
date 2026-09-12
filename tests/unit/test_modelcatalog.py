"""Successful discovery is reusable offline without persisting provider payloads."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from sbxloop import modelcatalog
from sbxloop.backends import backend_named
from sbxloop.cli.models import ModelRow
from sbxloop.config import Config
from sbxloop.paths import SbxloopHome
from sbxloop.tui.configkeys import is_model_key


def row(id: str = "selected", name: str = "Selected model") -> ModelRow:
    return ModelRow(
        id=id,
        name=name,
        multiplier=None,
        context_window=None,
        vision=False,
        reasoning_efforts=None,
        default_reasoning_effort=None,
        policy_state=None,
        raw={"secret": "never persist raw provider data"},
    )


@pytest.mark.parametrize("backend", ["copilot", "claude", "codex"])
def test_catalog_is_backend_specific_bounded_and_contains_only_picker_fields(tmp_path, backend):
    home = SbxloopHome(tmp_path)
    provider = backend_named(backend)
    catalog = modelcatalog.save_catalog(home, provider, [row(), row(), row("second")])
    assert modelcatalog.load_catalog(home, provider) == catalog
    assert [model.id for model in catalog.models] == ["selected", "second"]
    path = home.model_catalogs / f"{backend}.json"
    assert "secret" not in path.read_text() and "raw" not in path.read_text()
    assert not catalog.stale(catalog.fetched_at + 1)
    assert catalog.stale(catalog.fetched_at + modelcatalog.REFRESH_AFTER_S)
    other = backend_named("claude" if backend != "claude" else "codex")
    assert modelcatalog.load_catalog(home, other) is None
    (home.model_catalogs / f"{other.name}.json").write_text(path.read_text())
    assert modelcatalog.load_catalog(home, other) is None


@pytest.mark.parametrize("bad", [b"not json", b"{}", b"x" * (modelcatalog.MAX_CACHE_BYTES + 1)])
def test_bad_cache_is_unavailable(tmp_path, bad):
    home = SbxloopHome(tmp_path)
    home.model_catalogs.mkdir(parents=True)
    (home.model_catalogs / "claude.json").write_bytes(bad)
    assert modelcatalog.load_catalog(home, backend_named("claude")) is None


def test_cache_write_failure_and_empty_response_preserve_previous_catalog(tmp_path, monkeypatch):
    home, backend = SbxloopHome(tmp_path), backend_named("claude")
    original = modelcatalog.save_catalog(home, backend, [row()])
    with pytest.raises(ValueError, match="no models"):
        modelcatalog.save_catalog(home, backend, [])
    with pytest.raises(ValueError):
        modelcatalog.save_catalog(home, backend, [replace(row(), id="not a slug")])

    def refused(*args):
        raise OSError("read only")

    monkeypatch.setattr(modelcatalog.Path, "replace", refused)
    with pytest.raises(OSError):
        modelcatalog.save_catalog(home, backend, [row("new")])
    assert modelcatalog.load_catalog(home, backend) == original
    assert not list(home.model_catalogs.glob(".models-*"))


def test_refresh_uses_backend_listing_and_keeps_old_cache_on_failure(tmp_path, monkeypatch):
    home, backend = SbxloopHome(tmp_path), backend_named("codex")
    calls = []

    def discover(actual, timeout_s, config=None):
        calls.append((actual.name, timeout_s))
        return [row()]

    monkeypatch.setattr(modelcatalog, "fetch_backend_rows", discover)
    original = modelcatalog.refresh_catalog(home, backend)
    assert calls == [("codex", modelcatalog.QUERY_TIMEOUT_S)]

    def unavailable(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(modelcatalog, "fetch_backend_rows", unavailable)
    with pytest.raises(RuntimeError, match="offline"):
        modelcatalog.refresh_catalog(home, backend)
    assert modelcatalog.load_catalog(home, backend) == original


def test_provision_refresh_is_nonblocking_deduplicated_and_skips_fresh_cache(tmp_path, monkeypatch):
    config = Config(home=tmp_path)
    started, finish = threading.Event(), threading.Event()

    def discover(*args, **kwargs):
        started.set()
        assert finish.wait(5)
        return [row()]

    monkeypatch.setattr(modelcatalog, "fetch_backend_rows", discover)
    thread = modelcatalog.refresh_after_provision(config)
    assert thread is not None
    try:
        assert started.wait(2)
        assert modelcatalog.refresh_after_provision(config) is None
    finally:
        finish.set()
        thread.join(5)
    assert not thread.is_alive()
    assert modelcatalog.load_catalog(config.paths, backend_named("copilot")) is not None
    modelcatalog._retry_at.clear()
    assert modelcatalog.refresh_after_provision(config) is None


def test_automatic_failure_is_backed_off_and_logs_no_exception_payload(
    tmp_path, monkeypatch, caplog
):
    config = Config(home=tmp_path)

    def unavailable(*args, **kwargs):
        raise RuntimeError("secret-provider-token")

    monkeypatch.setattr(modelcatalog, "fetch_backend_rows", unavailable)
    thread = modelcatalog.refresh_after_provision(config)
    assert thread is not None
    thread.join(5)
    assert not thread.is_alive()
    assert modelcatalog.refresh_after_provision(config) is None
    assert "secret-provider-token" not in caplog.text
    assert "models.cache_unavailable" in caplog.text


def test_stale_catalog_triggers_provision_refresh(tmp_path, monkeypatch):
    config = Config(home=tmp_path)
    backend = backend_named("copilot")
    modelcatalog.save_catalog(config.paths, backend, [row("old")])
    path = config.paths.model_catalogs / "copilot.json"
    payload = json.loads(path.read_text())
    payload["fetched_at"] = time.time() - modelcatalog.REFRESH_AFTER_S - 1
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(modelcatalog, "fetch_backend_rows", lambda *args, **kwargs: [row("fresh")])
    thread = modelcatalog.refresh_after_provision(config)
    assert thread is not None
    thread.join(5)
    assert modelcatalog.load_catalog(config.paths, backend).models[0].id == "fresh"


@pytest.mark.parametrize("success", [False, True])
def test_run_catalog_refresh_only_follows_successful_worker_install(tmp_path, monkeypatch, success):
    from sbxloop.engine.engine import LoopEngine

    calls = []
    config = Config(home=tmp_path)
    monkeypatch.setattr(modelcatalog, "refresh_after_provision", calls.append)

    def install(**kwargs):
        if not success:
            raise RuntimeError("install failed")

    agent = SimpleNamespace(install=install, apt_installed=[])
    pair = SimpleNamespace(languages=SimpleNamespace(languages=[], versions={}))
    engine = SimpleNamespace(config=config)
    if success:
        LoopEngine._install_workers(engine, "r1", pair, agent, None)
        assert calls == [config]
    else:
        with pytest.raises(RuntimeError, match="install failed"):
            LoopEngine._install_workers(engine, "r1", pair, agent, None)
        assert not calls


def test_only_actual_model_settings_get_the_picker():
    from sbxloop.config import AgentModels

    for role in AgentModels.model_fields:
        assert is_model_key(f"agent.models.{role}")
        assert is_model_key(f"github.repos[10].agent_models.{role}")
    assert is_model_key("model") and is_model_key("concierge.model")
    for key in (
        "agent.backend",
        "agent.models.typo",
        "sandbox.env.model",
        "foo.agent_models.build",
    ):
        assert not is_model_key(key)


@pytest.mark.parametrize("success", [False, True])
def test_concierge_refresh_only_after_ready_and_only_once(tmp_path, monkeypatch, success):
    from sbxloop.daemon.agentbox import DaemonAgent

    calls = []
    config = Config(home=tmp_path)
    monkeypatch.setattr(modelcatalog, "refresh_after_provision", calls.append)
    client = object()

    def ensure():
        if not success:
            raise RuntimeError("provision failed")
        return client

    agent = SimpleNamespace(
        _client=None, _ensure=ensure, config=config, name="agent", install_workers=True
    )
    if success:
        assert DaemonAgent.client(agent) is client
        assert DaemonAgent.client(agent) is client
        assert calls == [config]
    else:
        with pytest.raises(RuntimeError, match="provision failed"):
            DaemonAgent.client(agent)
        assert not calls
