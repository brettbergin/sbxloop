"""``sbxloop api client …`` and ``sbxloop api key …`` on the host, and the
daemon refusing to start with the listener enabled but the extra absent."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sbxloop.api import MISSING_EXTRA, api_available
from sbxloop.api.auth.keys import load_or_create
from sbxloop.api.auth.store import ApiAuthStore, StandaloneSessions
from sbxloop.cli.app import app
from sbxloop.paths import SbxloopHome

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SbxloopHome:
    monkeypatch.chdir(tmp_path)
    # A wide terminal: rich would otherwise elide the ids the tests read.
    monkeypatch.setenv("COLUMNS", "200")
    return SbxloopHome(tmp_path / ".sbxloop")


class TestClients:
    def test_create_prints_the_secret_once_and_stores_a_verifier(self, home: SbxloopHome) -> None:
        result = runner.invoke(
            app,
            ["api", "client", "create", "reporter", "--cap", "runs:read", "--cap", "audit:read"],
        )
        assert result.exit_code == 0, result.output
        lines = dict(line.split(":", 1) for line in result.output.splitlines() if ":" in line)
        client_id = lines["client_id"].strip()
        secret = lines["client_secret"].strip()
        assert client_id.startswith("cli_") and secret.startswith("sk_")
        sessions = StandaloneSessions(home.state_db, owns_schema=False)
        try:
            store = ApiAuthStore(sessions)
            client = store.authenticate(client_id, secret, now=1.0)
            assert client.name == "reporter" and client.capabilities == {"runs:read", "audit:read"}
            assert client.created_by is not None and client.created_by.endswith("via sbxloop api")
        finally:
            sessions.close()
        listed = runner.invoke(app, ["api", "client", "list"])
        assert (
            client_id in listed.output and "reporter" in listed.output and "active" in listed.output
        )
        assert secret not in listed.output

    def test_create_refuses_unknown_or_no_capabilities(self, home: SbxloopHome) -> None:
        assert runner.invoke(app, ["api", "client", "create", "x"]).exit_code == 2
        bad = runner.invoke(app, ["api", "client", "create", "x", "--cap", "root"])
        assert bad.exit_code == 2 and "unknown capabilities: root" in bad.output

    def test_revoke(self, home: SbxloopHome) -> None:
        created = runner.invoke(app, ["api", "client", "create", "x", "--cap", "runs:read"])
        client_id = next(
            line.split(":", 1)[1].strip()
            for line in created.output.splitlines()
            if line.startswith("client_id")
        )
        assert runner.invoke(app, ["api", "client", "revoke", client_id]).exit_code == 0
        assert "revoked" in runner.invoke(app, ["api", "client", "list"]).output
        assert runner.invoke(app, ["api", "client", "revoke", "cli_nope"]).exit_code == 2


class TestKeys:
    def test_show_creates_and_rotate_keeps_the_previous(self, home: SbxloopHome) -> None:
        shown = runner.invoke(app, ["api", "key", "show"])
        assert shown.exit_code == 0 and "kid:" in shown.output
        key_file = home.config / "api-signing.key"
        assert key_file.exists() and (key_file.stat().st_mode & 0o777) == 0o600
        first = load_or_create(home).current.kid
        rotated = runner.invoke(app, ["api", "key", "rotate"])
        assert rotated.exit_code == 0 and first in rotated.output
        keys = load_or_create(home)
        assert keys.previous is not None and keys.previous.kid == first
        assert keys.current.kid != first


class TestMissingExtra:
    def test_available_here(self) -> None:
        assert api_available()

    def test_the_daemon_refuses_by_name_without_the_extra(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What `sbxloop daemon` runs before any sandbox work when
        `[api] enabled = true`: a missing extra is a configuration error
        that names the extra, not something discovered after recovery."""
        from sbxloop.api import require_available
        from sbxloop.errors import ConfigError

        require_available()
        for name in ("fastapi", "uvicorn", "jwt"):
            monkeypatch.setitem(sys.modules, name, None)
        assert not api_available()
        with pytest.raises(ConfigError, match="sbxloop\\[api\\]") as excinfo:
            require_available()
        assert str(excinfo.value) == MISSING_EXTRA
