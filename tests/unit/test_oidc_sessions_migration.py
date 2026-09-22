"""OIDC session migration preserves old credentials and can run twice."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command

from sbxloop.db import ensure_schema, open_engine
from sbxloop.db.schema import _config


def test_oidc_session_revision_is_additive_and_rerunnable(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    engine = open_engine(path)
    try:
        with engine.connect() as connection:
            command.upgrade(_config(connection), "0035")
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO api_clients (id,name,secret_hash,created_at)"
                " VALUES ('machine','M','hash',1)"
            )
            connection.execute(
                "INSERT INTO api_refresh_tokens"
                " (id,client_id,family_id,token_hash,issued_at,expires_at)"
                " VALUES ('rt','machine','family','digest',1,9999999)"
            )
        ensure_schema(engine)
        with engine.connect() as connection:
            command.stamp(_config(connection), "0035")
        ensure_schema(engine)
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT id,secret_hash FROM api_clients").fetchall() == [
                ("machine", "hash")
            ]
            assert connection.execute("SELECT token_hash FROM api_refresh_tokens").fetchall() == [
                ("digest",)
            ]
            assert connection.execute("SELECT count(*) FROM api_oidc_sessions").fetchone() == (0,)
            assert connection.execute("SELECT count(*) FROM api_oidc_logouts").fetchone() == (0,)
    finally:
        engine.dispose()
