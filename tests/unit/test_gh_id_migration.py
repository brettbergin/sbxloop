"""#508 end-to-end: a state store written entirely with legacy `gh:1234` ids.

The migration is read-time normalisation, not a rewrite of history: rows the
pre-#508 daemon wrote keep resolving, and every surface a human sees — the
operator command dispatcher, the Discord render path and the daemon log —
prints the typed `gh:issue:<n>` form.
"""

from __future__ import annotations

import io
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from sbxloop.daemon.control import dispatch, plain
from sbxloop.daemon.discord_format import headline_text, items_lines, queue_lines
from sbxloop.daemon.store import DaemonStore
from sbxloop.log import bind_run, clear_run, configure_logging, get_logger
from tests.unit.test_daemon_discord import FakeLoop

LEGACY_ID = "gh:1234"
TYPED_ID = "gh:issue:1234"
BARE = re.compile(r"gh:\d")


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Put the root logger back on the session default after the log test."""
    yield
    configure_logging("DEBUG")
    clear_run()


def legacy_store(tmp_path: Path) -> Path:
    """A state db whose every row carries the pre-migration bare id."""
    db = tmp_path / "state" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    DaemonStore(db).close()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO daemon_work_items (item_id, source_key, title, body, url, state, "
        "attempts, claimed, run_id, last_error, created_at, updated_at) "
        "VALUES (?, '1234', 'Fix login', '', 'https://x/1234', 'queued', 0, 0, NULL, NULL, "
        "1.0, 1.0)",
        (LEGACY_ID,),
    )
    conn.execute(
        "INSERT INTO daemon_runs (run_id, item_id, started_at) VALUES ('r9', ?, 1.0)",
        (LEGACY_ID,),
    )
    conn.commit()
    conn.close()
    return db


class TestLegacyStateFixture:
    def test_operator_commands_read_legacy_rows_and_print_typed_ids(self, tmp_path: Path) -> None:
        loop = FakeLoop(DaemonStore(legacy_store(tmp_path)))
        for word in ("items", "queue"):
            text = plain(dispatch(loop, word).text)
            assert TYPED_ID in text and not BARE.search(text)
        # both spellings reach the same legacy row; only the typed id echoes
        for command in (f"requeue {LEGACY_ID}", f"requeue {TYPED_ID}"):
            reply = dispatch(loop, command)
            assert reply.ok and TYPED_ID in reply.text and not BARE.search(reply.text)
        reply = dispatch(loop, f"abandon {LEGACY_ID} enough")
        assert reply.ok and TYPED_ID in reply.text
        reply = dispatch(loop, f"retry {LEGACY_ID}", by="ops")
        assert reply.ok and loop.retried[-1] == (TYPED_ID, "ops")
        assert loop.dstore.get(LEGACY_ID) is not None
        assert loop.dstore.runs_for_item(LEGACY_ID) == ["r9"]
        assert loop.dstore.item_for_run("r9") == TYPED_ID

    def test_render_path_over_legacy_rows_is_typed(self, tmp_path: Path) -> None:
        store = DaemonStore(legacy_store(tmp_path))
        item = store.get(LEGACY_ID)
        assert item is not None and item.item_id == TYPED_ID
        for text in (headline_text(item, "r9"), queue_lines([item]), items_lines([item])):
            assert TYPED_ID in text and not BARE.search(text)

    def test_daemon_log_lines_carry_the_typed_id(
        self, tmp_path: Path, restore_logging: None
    ) -> None:
        store = DaemonStore(legacy_store(tmp_path))
        stream = io.StringIO()
        configure_logging("INFO", stream=stream)
        item = store.get(LEGACY_ID)
        assert item is not None
        bind_run("r9", item.item_id, source="github")
        get_logger("sbxloop.test").info("run.dispatch")
        clear_run()
        line = stream.getvalue()
        assert f"item={TYPED_ID}" in line and not BARE.search(line)
