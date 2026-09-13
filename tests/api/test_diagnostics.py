"""Bounded diagnostics: the log ring redacted, and the configuration a
remote operator may read — never a secret value, never a host path
(#1040)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sbxloop import log as logmod
from sbxloop.api.diagnostics import CONFIGURATION_SECTIONS, redact
from sbxloop.log import LogBuffer, LogRecordLine
from tests.api.conftest import Api, build

DIAG = frozenset({"diagnostics:read"})
SECRET = "hunter2-very-secret-value"


@pytest.fixture
def buffer(monkeypatch: pytest.MonkeyPatch) -> LogBuffer:
    fresh = LogBuffer()
    monkeypatch.setattr(logmod, "_LOG_BUFFER", fresh)
    return fresh


def _line(i: int, text: str, level: str = "INFO") -> LogRecordLine:
    return LogRecordLine(f"2026-01-01T00:00:{i:02d}Z", level, "sbxloop.test", text)


class TestLogs:
    def test_the_tail_is_the_ring_newest_last_filtered_and_bounded(
        self, api: Api, buffer: LogBuffer
    ) -> None:
        for i in range(5):
            buffer.append(_line(i, f"line {i}", "WARNING" if i % 2 else "INFO"))
        headers = api.bearer(DIAG)
        body = api.client.get("/v1/logs", params={"tail": 2}, headers=headers).json()
        assert [r["message"] for r in body["records"]] == ["line 3", "line 4"]
        assert body["buffer_size"] == 5 and body["tail"] == 2
        assert body["records"][0] == {
            "timestamp": "2026-01-01T00:00:03Z",
            "level": "WARNING",
            "logger": "sbxloop.test",
            "message": "line 3",
        }
        assert body["observed_at"].endswith("Z")
        warned = api.client.get("/v1/logs", params={"level": "warning"}, headers=headers).json()
        assert [r["message"] for r in warned["records"]] == ["line 1", "line 3"]
        assert warned["level"] == "WARNING"
        found = api.client.get("/v1/logs", params={"grep": "LINE 4"}, headers=headers).json()
        assert [r["message"] for r in found["records"]] == ["line 4"] and found["grep"] == "LINE 4"
        # Bounds and refusals: the ring's cap, an unknown level, no capability.
        assert api.client.get("/v1/logs", params={"tail": 501}, headers=headers).status_code == 422
        loud = api.client.get("/v1/logs", params={"level": "loud"}, headers=headers)
        assert loud.status_code == 422 and "unknown log level" in loud.json()["detail"]
        assert (
            api.client.get("/v1/logs", headers=api.bearer(frozenset({"runs:read"}))).status_code
            == 403
        )
        assert api.client.get("/v1/logs").status_code == 401

    def test_credentials_never_leave_the_host(self, api: Api, buffer: LogBuffer) -> None:
        planted = [
            f"authorization: Bearer {SECRET}.tail",
            f"github token={SECRET}",
            "a jwt eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJjbGlfeCJ9.c2lnbmF0dXJlLXNpZ25hdHVyZQ here",
            "client_secret: sk_abcdefghijklmnop rotated",
            "pat ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcd used",
            "password='p4ssw0rd' seen",
        ]
        for i, text in enumerate(planted):
            buffer.append(_line(i, text))
        body = api.client.get("/v1/logs", headers=api.bearer(DIAG)).json()
        text = " ".join(r["message"] for r in body["records"])
        for needle in (SECRET, "eyJhbGci", "sk_abcdefghijklmnop", "ghp_ABCDEF", "p4ssw0rd"):
            assert needle not in text, text
        messages = [r["message"] for r in body["records"]]
        assert messages[0] == "authorization: Bearer [redacted]"
        assert messages[1] == "github token=[redacted]"
        assert messages[3] == "client_secret: [redacted] rotated"
        assert messages[4] == "pat [redacted] used"
        assert messages[5] == "password='[redacted]' seen"

    def test_redaction_leaves_ordinary_text_alone(self) -> None:
        line = "run r1 finished: 3 tasks, token budget 12000, bearer of bad news"
        assert redact(line) == line
        assert redact("cli_abcdefghijklmn requested") == "cli_abcdefghijklmn requested"


class TestConfiguration:
    def test_the_allowlisted_sections_with_provenance_and_nothing_else(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PLANTED_TOKEN", SECRET)
        api = build(
            tmp_path,
            config={
                "github": {"repos": [{"repo": "o/r", "token_env": "PLANTED_TOKEN"}]},
                "telemetry": {"dsn_env": "PLANTED_TOKEN"},
                "daemon": {"trigger_label": "go"},
                "workloads": [{"name": "brief"}],
                "concierge": {"config_locked": ["daemon.max_runs_per_day"]},
            },
        )
        # The operator's file says something else for one key: the daemon
        # runs on what it loaded; the read says an edit is pending.
        home_toml = api.loop.config.home / "config" / "sbxloop.toml"
        home_toml.parent.mkdir(parents=True, exist_ok=True)
        home_toml.write_text('[daemon]\ntrigger_label = "later"\n')
        with api.client:
            headers = api.bearer(DIAG)
            response = api.client.get("/v1/configuration", headers=headers)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["sections"] == list(CONFIGURATION_SECTIONS)
            entries = {e["key"]: e for e in body["entries"]}
            assert set(entries) and all(
                k.split(".")[0].split("[")[0] in CONFIGURATION_SECTIONS for k in entries
            )
            # Names of secrets travel; values never do. No host path, no listener config.
            assert entries["github.repos[0].token_env"]["value"] == "PLANTED_TOKEN"
            assert entries["telemetry.dsn_env"]["value"] == "PLANTED_TOKEN"
            dumped = response.text
            assert SECRET not in dumped
            assert str(tmp_path) not in dumped and "/home/" not in dumped
            assert not any(
                k.startswith(("api.", "discord.", "slack.", "mattermost.", "tui.", "chat."))
                for k in entries
            )
            assert "home" not in entries and "worker_python" not in entries
            assert not any(k.endswith(("_path", ".path", "_dir", ".workspace")) for k in entries)
            # Provenance: the layer, whether it applies live, the pending edit, the lock.
            trigger = entries["daemon.trigger_label"]
            assert trigger["value"] == "go" and trigger["source"] == "home config"
            assert trigger["applies"] == "restart" and trigger["pending"] is True
            assert trigger["locked"] is None and trigger["doc"]
            cap = entries["daemon.max_runs_per_day"]
            assert cap["locked"] == "daemon.max_runs_per_day" and cap["pending"] is False
            assert cap["source"] == "default"
            assert entries["concierge.enabled"]["locked"] == "it is the concierge's own switch"
            assert entries["model"]["applies"] == "live"
            assert entries["workloads[0].name"]["value"] == "brief"
            assert body["observed_at"].endswith("Z") and body["workspace_id"] == "local"
            # A reader without diagnostics:read sees none of it.
            assert (
                api.client.get(
                    "/v1/configuration", headers=api.bearer(frozenset({"runs:read"}))
                ).status_code
                == 403
            )
        api.ctx.close()

    def test_an_unloadable_file_leaves_provenance_unknown(self, tmp_path: Path) -> None:
        api = build(tmp_path)
        home_toml = api.loop.config.home / "config" / "sbxloop.toml"
        home_toml.parent.mkdir(parents=True, exist_ok=True)
        home_toml.write_text("this is not = [toml\n")
        with api.client:
            body = api.client.get("/v1/configuration", headers=api.bearer(DIAG)).json()
            entries: dict[str, Any] = {e["key"]: e for e in body["entries"]}
            assert entries["daemon.trigger_label"]["value"] == "sbxloop:run"
            assert entries["daemon.trigger_label"]["source"] is None
            assert all(not e["pending"] for e in entries.values())
        api.ctx.close()
