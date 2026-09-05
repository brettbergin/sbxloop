"""Every persisted-state shape a released sbxloop wrote still opens (#524).

`tests/fakes/legacy_db.py` freezes the schemas; this module is the sweep
that proves each one migrates in place and that *every* row state and id
form a deployed daemon can hold survives the upgrade — the class of bug
that reached PR #512's review one row state per round because the plan
never named the path. A change to persisted state adds its pre-change
shape there and a case here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.store import StateStore
from sbxloop.ghids import normalize_item_id
from tests.fakes.legacy_db import (
    DAEMON_ITEM_STATES,
    DAEMON_SHAPES,
    ENGINE_SHAPES,
    daemon_db,
    engine_db,
    every_daemon_row,
    insert_phase_row,
    insert_row,
    insert_run_row,
    raw_daemon_rows,
)


class TestDaemonShapes:
    @pytest.mark.parametrize("shape", sorted(DAEMON_SHAPES))
    def test_every_row_state_and_id_form_survives_the_upgrade(
        self, tmp_path: Path, shape: str
    ) -> None:
        """Open a raw store of each released shape holding one row per
        (state x id form); every row reads back under its typed id with
        its state, pin and report intact, and a reopen changes nothing."""
        path = daemon_db(tmp_path, shape)
        repo = "o/r" if shape in ("pre_scheduled_retry", "pre_claim_token") else None
        written = every_daemon_row(path, repo=repo)
        assert len(written) == 2 * len(DAEMON_ITEM_STATES)

        store = DaemonStore(path)
        for item_id, state in written:
            got = store.get(item_id)
            assert got is not None, item_id
            assert got.item_id == normalize_item_id(item_id)
            assert got.state == state
            assert got.not_before is None and got.claim_token is None
        # The raw file is untouched by reading: ids stay as stored.
        assert raw_daemon_rows(path) == written
        # Live rows are still live: the run in flight and the pending resume.
        running = store.running_items()
        assert sorted(i.run_id for i in running if i.run_id) == ["r_live", "r_live"]
        queued = store.queued()
        assert sorted(q.run_id for q in queued if q.run_id) == ["r_resume", "r_resume"]
        store.close()
        # Migration is idempotent.
        again = DaemonStore(path)
        assert len(again.items()) == len(written)
        again.close()

    def test_pre_typed_ids_rows_transition_under_the_stored_id(self, tmp_path: Path) -> None:
        """#508's lesson: the store must bind the id *as stored* — a bare
        `gh:7` row updated under its normalised form matches nothing."""
        path = daemon_db(tmp_path, "pre_typed_ids")
        every_daemon_row(path, id_forms=("gh:{n}",))
        store = DaemonStore(path)
        live = next(i for i in store.running_items() if i.run_id == "r_live")
        store.mark_failed(live.item_id, "boom", now=9.0, requeue=False)
        assert store.get("gh:4").state == "failed"  # type: ignore[union-attr]
        assert ("gh:4", "failed") in raw_daemon_rows(path)

    def test_repoless_shapes_are_rebuilt_on_the_composite_key(self, tmp_path: Path) -> None:
        for shape in ("pre_typed_ids", "pre_multirepo"):
            path = daemon_db(tmp_path, shape, name=f"{shape}.db")
            store = DaemonStore(path)
            from tests.unit.test_daemon_store import item

            assert store.upsert_new(item("4", item_id="gh:o/a:issue:4", repo="o/a"), 1.0)
            assert store.upsert_new(item("4", item_id="gh:o/b:issue:4", repo="o/b"), 1.0)
            store.close()

    def test_pre_local_bridge_chat_state_survives(self, tmp_path: Path) -> None:
        """Threads, watches and the gate prompt written by a one-bridge
        daemon read back under the backend they were recorded for once the
        tables are keyed per backend, and a reopen changes nothing."""
        path = daemon_db(tmp_path, "pre_local_bridge")
        insert_row(
            path,
            "daemon_chat_threads",
            run_id="r1",
            backend="discord",
            channel_id="42",
            thread_id="4242",
            headline_id="100",
            status_id=None,
        )
        insert_row(
            path,
            "daemon_chat_threads",
            run_id="r2",
            backend="slack",
            channel_id="C1",
            thread_id="1724968573.123456",
            headline_id="1724968573.123456",
            status_id="1724968574.000100",
        )
        insert_row(path, "daemon_run_watches", run_id="r1", watcher_id="u1", created_at=1.0)
        insert_row(
            path,
            "daemon_merge_gates",
            run_id="r_gate",
            item_id="gh:issue:3",
            repo="o/r",
            pr_number=9,
            custom_id="tok",
            prompt_channel_id="42",
            prompt_message_id="555",
            created_at=1.0,
        )
        insert_row(
            path,
            "daemon_chat_threads",
            run_id="r_gate",
            backend="discord",
            channel_id="42",
            thread_id="4343",
        )
        for _ in range(2):
            store = DaemonStore(path)
            assert store.chat_thread("r1") == ("42", "4242", "100", None, "discord")
            assert store.chat_thread("r2", "slack") == (
                "C1",
                "1724968573.123456",
                "1724968573.123456",
                "1724968574.000100",
                "slack",
            )
            assert store.chat_thread("r1", "local") is None
            assert store.run_for_thread("1724968573.123456", "slack") == "r2"
            assert store.all_run_watches("discord") == {"r1": ["u1"]}
            assert store.all_run_watches("local") == {}
            assert store.gate_prompt("r_gate", "discord") == ("42", "555")
            assert store.gate_prompt("r_gate", "local") is None
            store.close()


class TestEngineShapes:
    @pytest.mark.parametrize("shape", sorted(ENGINE_SHAPES))
    def test_each_shape_opens_and_its_rows_read_with_defaults(
        self, tmp_path: Path, shape: str
    ) -> None:
        path = engine_db(tmp_path, shape)
        insert_run_row(path, run_id="old", outcome="legacy", state="completed")
        if shape == "pre_usage":
            insert_phase_row(
                path,
                run_id="old",
                task_id="t1",
                phase="execute",
                attempt=1,
                status="ok",
                output_json=None,
                started_at=1.0,
                ended_at=2.0,
            )
        store = StateStore(path)
        run = store.get_run("old")
        assert run.state == "completed" and run.workspace is None and run.reason is None
        assert (run.review_rounds, run.ci_rounds, run.granted_rounds) == (0, 0, 0)
        assert run.exhausted is None and run.stage is None and run.pr_number is None
        assert store.get_run_guidance("old") == []
        assert run.credentials == []
        if shape == "pre_usage":
            assert store.phase_attempts("old")[0]["input_tokens"] is None
        # The new columns are writable, and a reopen does not re-apply the ALTERs.
        assert store.grant_rounds("old", 1) == 1
        store.set_run_credentials("old", ["weather"])
        reopened = StateStore(path).get_run("old")
        assert (reopened.granted_rounds, reopened.credentials) == (1, ["weather"])
