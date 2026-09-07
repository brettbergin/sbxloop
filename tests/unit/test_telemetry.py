"""GlitchTip reports contain diagnostic locations, never customer payloads."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from sbxloop import telemetry
from sbxloop.config import Config, TelemetryConfig, _project_layer, load_config
from sbxloop.log import configure_logging, get_logger


def test_reporting_is_disabled_without_a_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GLITCHTIP_DSN", raising=False)
    monkeypatch.setenv("SENTRY_DSN", "https://unrelated@errors.example.com/2")
    telemetry.configure_telemetry(TelemetryConfig())
    assert telemetry._client is None


def test_telemetry_is_operator_configuration(tmp_path: Path) -> None:
    config = load_config(tmp_path, env={"SBXLOOP_TELEMETRY__ENVIRONMENT": "staging"})
    assert config.telemetry.environment == "staging"
    assert Config().telemetry.dsn_env == "GLITCHTIP_DSN"
    kept, dropped = _project_layer({"telemetry": {"dsn_env": "ATTACKER", "environment": "x"}})
    assert kept == {}
    assert sorted(dropped) == ["telemetry.dsn_env", "telemetry.environment"]


@pytest.mark.parametrize("fmt", ["console", "json"])
def test_logged_errors_are_reported_once_without_payloads(
    monkeypatch: pytest.MonkeyPatch, fmt
) -> None:
    import sentry_sdk
    from sentry_sdk.transport import Transport

    envelopes = []

    class MemoryTransport(Transport):
        def capture_envelope(self, envelope):
            envelopes.append(envelope)

    real_client = sentry_sdk.Client
    monkeypatch.setattr(
        sentry_sdk, "Client", lambda **options: real_client(**options, transport=MemoryTransport)
    )
    monkeypatch.setenv("GLITCHTIP_DSN", "https://public@errors.example.com/1")
    configure_logging("DEBUG", stream=io.StringIO())
    try:
        telemetry.configure_telemetry(TelemetryConfig(environment="test"))
        # Reconfiguring the daemon renderer must not duplicate reports.
        configure_logging("DEBUG", fmt=fmt, stream=io.StringIO())
        log = get_logger("sbxloop.test")
        log.info("run.started", password="private-value")
        log.warning("run.retry")
        try:
            raise RuntimeError("private-value from a customer response")
        except RuntimeError:
            log.warning("run.crashed", exc_info=True, argv=["private-value"], token="private-value")
        log.error("run.abandoned", error="private-value")
        # Neither foreign libraries nor dynamic prose are reporting events.
        get_logger("foreign.test").error("foreign.error", token="private-value")
        log.error("private-value in a dynamic message")
        assert telemetry._client is not None
        assert not telemetry._client.integrations
        assert not sentry_sdk.is_initialized()
        telemetry.shutdown_telemetry()
        events = [item.payload.json for envelope in envelopes for item in envelope.items]
        assert len(events) == 2
        assert events[0]["exception"]["values"][0]["type"] == "RuntimeError"
        frames = events[0]["exception"]["values"][0]["stacktrace"]["frames"]
        assert frames[-1]["function"] == "test_logged_errors_are_reported_once_without_payloads"
        assert events[0]["message"] == "run.crashed"
        assert events[1]["message"] == "run.abandoned"
        assert events[0]["environment"] == "test"
        serialized = json.dumps(events)
        for forbidden in (
            "private-value",
            "customer response",
            "vars",
            "argv",
            "context_line",
            "breadcrumbs",
        ):
            assert forbidden not in serialized
    finally:
        telemetry.shutdown_telemetry()
        configure_logging("DEBUG")


def test_cli_reports_and_flushes_an_unhandled_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    cli = importlib.import_module("sbxloop.cli.app")
    calls = []
    failure = RuntimeError("failure")

    def crash():
        raise failure

    monkeypatch.setattr(cli, "app", crash)
    monkeypatch.setattr(telemetry, "capture_exception", lambda error: calls.append(error))
    monkeypatch.setattr(telemetry, "shutdown_telemetry", lambda: calls.append("flush"))
    with pytest.raises(RuntimeError, match="failure"):
        cli.main()
    assert calls == [failure, "flush"]


@pytest.mark.parametrize("failure", [SystemExit(0), SystemExit(2), KeyboardInterrupt()])
def test_cli_flushes_without_reporting_normal_exits(monkeypatch, failure) -> None:
    import importlib

    cli = importlib.import_module("sbxloop.cli.app")
    calls = []

    def stop():
        raise failure

    monkeypatch.setattr(cli, "app", stop)
    monkeypatch.setattr(telemetry, "capture_exception", lambda error: calls.append(error))
    monkeypatch.setattr(telemetry, "shutdown_telemetry", lambda: calls.append("flush"))
    with pytest.raises(type(failure)):
        cli.main()
    assert calls == ["flush"]


def test_invalid_dsn_warns_without_exposing_value(monkeypatch, caplog) -> None:
    monkeypatch.setenv("GLITCHTIP_DSN", "invalid-private-dsn")
    configure_logging("DEBUG", stream=io.StringIO())
    telemetry.configure_telemetry(TelemetryConfig())
    assert telemetry._client is None
    assert "telemetry.init_failed" in caplog.text
    assert "invalid-private-dsn" not in caplog.text


def test_transport_failures_do_not_break_logging_or_shutdown(monkeypatch) -> None:
    calls = []

    class BrokenClient:
        def capture_event(self, event):
            calls.append("capture")
            raise RuntimeError("transport failed")

        def close(self, *, timeout):
            calls.append(timeout)
            raise RuntimeError("close failed")

    monkeypatch.setattr(telemetry, "_client", BrokenClient())
    telemetry.capture_exception(RuntimeError("private-value"))
    event = {"event": "run.crashed", "logger": "sbxloop.test"}
    assert telemetry.capture_log(None, "error", event) is event
    telemetry.shutdown_telemetry()
    assert calls == ["capture", "capture", 2.0]
    assert telemetry._client is None


def test_cli_initializes_after_loading_home_secrets(monkeypatch, tmp_path) -> None:
    import importlib

    cli = importlib.import_module("sbxloop.cli.app")
    monkeypatch.setenv("SBXLOOP_HOME", str(tmp_path))
    monkeypatch.delenv("CUSTOM_GLITCHTIP_DSN", raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "sbxloop.toml").write_text('[telemetry]\ndsn_env="CUSTOM_GLITCHTIP_DSN"\n')
    (config_dir / "secrets.env").write_text("CUSTOM_GLITCHTIP_DSN=private-value\n")
    calls = []

    def configure(config):
        import os

        calls.append(os.environ.get(config.dsn_env))

    monkeypatch.setattr(telemetry, "configure_telemetry", configure)
    try:
        cli._main_callback()
        assert calls == ["private-value"]
    finally:
        monkeypatch.delenv("CUSTOM_GLITCHTIP_DSN", raising=False)
        configure_logging("DEBUG")


def test_invalid_config_does_not_prevent_repair_commands(monkeypatch) -> None:
    import importlib

    from sbxloop.errors import ConfigError

    cli = importlib.import_module("sbxloop.cli.app")

    def invalid():
        raise ConfigError("invalid config")

    monkeypatch.setattr(cli, "load_config", invalid)
    try:
        cli._main_callback()
    finally:
        configure_logging("DEBUG")


def test_sdk_ambient_context_is_discarded() -> None:
    event = {
        "event_id": "id",
        "message": "run.crashed",
        "extra": {"argv": "private"},
        "request": {"headers": {"Authorization": "private"}},
        "user": {"email": "private"},
        "server_name": "private",
        "contexts": {"trace": {"data": "private"}},
        "breadcrumbs": [{"message": "private"}],
        "modules": {"private": "1"},
    }
    assert telemetry._before_send(event, {}) == {"event_id": "id", "message": "run.crashed"}
