"""Backend labelling against state a deployed instance already holds (#601).

Chat now names the agent as ``<backend> · <model>``, but a deployed engine
store is full of rows written before ``backend`` was ever recorded:
``agent.message`` / ``agent.usage`` event payloads with a model and no
backend (or with neither), ``phase_attempts`` usage columns that never had a
backend, and ``runs.config_json`` from before ``[agent] backend`` existed.
None of that is rewritten by a migration — the read paths must simply
tolerate it — so the only test that proves the upgrade path is one whose
fixture database was **not** written through the new code, which stamps the
field on write and therefore cannot produce an old row.

Every store here is built with raw ``sqlite3`` inserts of hand-written JSON
and then opened with the *current* ``StateStore``, exactly as a daemon would
open a database it inherited from an earlier release.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sbxloop.daemon.discord_format import (
    agent_ident_from_config_json,
    agent_model_label,
    format_for_discord,
    headline_embed,
    headline_text,
)
from sbxloop.daemon.model import WorkItem
from sbxloop.engine.store import StateStore
from sbxloop.events import Event

RUN_ID = "r1legacy0"

# The runs-table columns the pre-change schema had. Written literally rather
# than imported so a later schema change cannot quietly rewrite history.
_RUNS_PRE_BACKEND = (
    "CREATE TABLE runs (run_id TEXT PRIMARY KEY, outcome TEXT NOT NULL,"
    " state TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}',"
    " created_at REAL NOT NULL, updated_at REAL NOT NULL, workspace TEXT,"
    " mounted INTEGER NOT NULL DEFAULT 0, kept_reason TEXT,"
    " user_guidance TEXT NOT NULL DEFAULT '[]', reason TEXT)"
)
_EVENTS_PRE_BACKEND = (
    "CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT,"
    " run_id TEXT NOT NULL, ts REAL NOT NULL, type TEXT NOT NULL,"
    " job_id TEXT, data_json TEXT NOT NULL DEFAULT '{}')"
)
# phase_attempts as it stood once usage columns existed but backend did not.
_PHASE_ATTEMPTS_PRE_BACKEND = (
    "CREATE TABLE phase_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " run_id TEXT NOT NULL, task_id TEXT, phase TEXT NOT NULL,"
    " attempt INTEGER NOT NULL, status TEXT NOT NULL, output_json TEXT,"
    " started_at REAL NOT NULL, ended_at REAL NOT NULL,"
    " input_tokens INTEGER, output_tokens INTEGER,"
    " cache_read_tokens INTEGER, cache_write_tokens INTEGER, turns INTEGER)"
)

# Hand-written payloads in exactly the JSON the pre-change code wrote: no
# `backend` key anywhere. Kept as text (not dicts dumped by this test) so it
# is obvious by reading that nothing new can leak in.
OLD_USAGE_MODEL_ONLY = (
    '{"agent": "executor", "model": "gpt-5", "input_tokens": 1200, "output_tokens": 90}'
)
OLD_USAGE_NO_MODEL = '{"agent": "planner", "input_tokens": 300, "output_tokens": 20}'
OLD_USAGE_NO_TOKENS_NO_MODEL = '{"agent": "planner"}'
OLD_MESSAGE_MODEL_ONLY = '{"agent": "executor", "content": "done", "model": "claude-opus-5"}'
OLD_MESSAGE_NO_MODEL = '{"agent": "executor", "content": "done"}'
# A `runs` row from before config persistence at all...
OLD_CONFIG_EMPTY = "{}"
# ...and one that persisted a config but predates the `[agent]` section.
OLD_CONFIG_NO_AGENT = '{"model": "gpt-5", "home": "/var/lib/sbxloop"}'


def legacy_store(tmp_path: Path, *, config_json: str = OLD_CONFIG_NO_AGENT) -> Path:
    """A raw pre-change engine database: old schema, hand-written old JSON,
    written straight through ``sqlite3``. The new code never touches it
    until a test opens it with :class:`StateStore`."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute(_RUNS_PRE_BACKEND)
    conn.execute(_EVENTS_PRE_BACKEND)
    conn.execute(_PHASE_ATTEMPTS_PRE_BACKEND)
    conn.execute(
        "INSERT INTO runs (run_id, outcome, state, config_json, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (RUN_ID, "Ship the widget", "completed", config_json, 1.0, 2.0),
    )
    # A usage row from the same era: token columns, no backend column.
    conn.execute(
        "INSERT INTO phase_attempts (run_id, task_id, phase, attempt, status,"
        " started_at, ended_at, input_tokens, output_tokens, turns)"
        " VALUES (?, 't1', 'build', 1, 'ok', 1.0, 2.0, 1200, 90, 3)",
        (RUN_ID,),
    )
    conn.commit()
    conn.close()
    return path


def insert_event(path: Path, type_: str, data_json: str, *, ts: float = 1.5) -> None:
    """One raw ``events`` row, its payload inserted verbatim."""
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO events (run_id, ts, type, job_id, data_json) VALUES (?, ?, ?, NULL, ?)",
        (RUN_ID, ts, type_, data_json),
    )
    conn.commit()
    conn.close()


def opened(path: Path) -> StateStore:
    """The pre-change database, opened by the current store (which migrates
    the schema in place but must not rewrite any payload)."""
    return StateStore(path)


def event_of(store: StateStore, type_prefix: str) -> Event:
    (first,) = [e for _seq, e in store.events(RUN_ID, type_prefix=type_prefix)][:1]
    return first


class TestFixtureIsGenuinelyOld:
    """The fixture is only evidence if the new code did not write it."""

    def test_payloads_on_disk_carry_no_backend_key(self, tmp_path: Path) -> None:
        path = legacy_store(tmp_path)
        insert_event(path, "agent.usage", OLD_USAGE_MODEL_ONLY)
        insert_event(path, "agent.message", OLD_MESSAGE_MODEL_ONLY)
        conn = sqlite3.connect(path)
        try:
            rows = [str(r[0]) for r in conn.execute("SELECT data_json FROM events")]
            configs = [str(r[0]) for r in conn.execute("SELECT config_json FROM runs")]
        finally:
            conn.close()
        assert rows and all("backend" not in json.loads(r) for r in rows)
        assert configs == [OLD_CONFIG_NO_AGENT]
        assert "agent" not in json.loads(configs[0])

    def test_opening_with_the_current_store_leaves_payloads_untouched(self, tmp_path: Path) -> None:
        """Opening migrates the schema but must not backfill any payload —
        otherwise "old rows still work" would be untestable next release."""
        path = legacy_store(tmp_path)
        insert_event(path, "agent.usage", OLD_USAGE_MODEL_ONLY)
        store = opened(path)
        try:
            assert store.get_run(RUN_ID).state == "completed"
        finally:
            store.close()
        conn = sqlite3.connect(path)
        try:
            (raw,) = conn.execute("SELECT data_json FROM events").fetchone()
        finally:
            conn.close()
        assert json.loads(str(raw)) == json.loads(OLD_USAGE_MODEL_ONLY)


class TestLegacyUsageReporting:
    """The concierge's usage report over pre-change ``agent.usage`` rows."""

    def _usage(self, store: StateStore, tmp_path: Path) -> Any:
        from tests.unit.test_daemon_concierge import make

        concierge, _client, _host, _loop, _dstore = make(tmp_path / "daemon", [])
        # The concierge opens its engine store lazily through this factory;
        # pointing it at the inherited database is exactly what a daemon
        # running against pre-change state does.
        concierge._store_factory = lambda: store
        return concierge._usage_for_run(RUN_ID)

    def test_model_only_usage_reports_unknown_backend_with_tokens_intact(
        self, tmp_path: Path
    ) -> None:
        """Acceptance: an old sample reads ``unknown · <model>`` and its
        token totals survive the read unchanged."""
        path = legacy_store(tmp_path)
        insert_event(path, "agent.usage", OLD_USAGE_MODEL_ONLY)
        store = opened(path)
        try:
            usage = self._usage(store, tmp_path)
        finally:
            store.close()
        assert usage.model_line == "unknown · gpt-5"
        assert usage.total.input_tokens == 1200 and usage.total.output_tokens == 90
        assert usage.samples == 1 and usage.recorded
        assert usage.by_agent["executor"].input_tokens == 1200

    def test_usage_without_model_or_backend_keeps_not_reported_wording(
        self, tmp_path: Path
    ) -> None:
        """Acceptance: neither key recorded is still not an error, and the
        wording a reader already knows is unchanged."""
        path = legacy_store(tmp_path)
        insert_event(path, "agent.usage", OLD_USAGE_NO_MODEL)
        store = opened(path)
        try:
            usage = self._usage(store, tmp_path)
        finally:
            store.close()
        assert usage.model_line == "model not reported"
        assert usage.total.input_tokens == 300 and usage.samples == 1

    def test_mixed_old_and_new_samples_both_render(self, tmp_path: Path) -> None:
        """A store that straddles the change (old rows plus rows a current
        worker wrote) lists both pairs, old ones as unknown."""
        path = legacy_store(tmp_path)
        insert_event(path, "agent.usage", OLD_USAGE_MODEL_ONLY)
        insert_event(
            path,
            "agent.usage",
            '{"agent": "executor", "backend": "claude", "model": "claude-opus-5",'
            ' "input_tokens": 10, "output_tokens": 2}',
        )
        store = opened(path)
        try:
            usage = self._usage(store, tmp_path)
        finally:
            store.close()
        assert usage.model_line == "unknown · gpt-5 + claude · claude-opus-5"
        assert usage.total.input_tokens == 1210

    def test_run_usage_tool_answers_over_a_legacy_store(self, tmp_path: Path) -> None:
        """End to end through the concierge host tool the chat user calls,
        not just the fold underneath it."""
        from tests.unit.test_daemon_concierge import make, turn

        path = legacy_store(tmp_path)
        insert_event(path, "agent.usage", OLD_USAGE_MODEL_ONLY)
        store = opened(path)
        concierge, client, _host, _loop, _dstore = make(
            tmp_path / "daemon", [{"calls": [("run_usage", {"run_id": RUN_ID})]}]
        )
        concierge._store_factory = lambda: store
        try:
            turn(concierge, f"what did {RUN_ID} spend?")
        finally:
            store.close()
        (resp,) = client.responses
        assert resp.ok
        assert "unknown · gpt-5" in resp.text
        assert "1,200" in resp.text
        assert "Traceback" not in resp.text


class TestLegacyAgentMessages:
    """Chat rendering of a pre-change ``agent.message`` payload."""

    def test_message_with_model_only_renders_unknown_backend_header(self, tmp_path: Path) -> None:
        """Acceptance: the header names the recorded model with an unknown
        backend — not a blank, a placeholder, or a traceback."""
        path = legacy_store(tmp_path)
        insert_event(path, "agent.message", OLD_MESSAGE_MODEL_ONLY)
        store = opened(path)
        try:
            event = event_of(store, "agent.message")
        finally:
            store.close()
        assert "backend" not in event.data  # read back exactly as written
        chunks = format_for_discord(event)
        assert chunks
        assert chunks[0].text.startswith("**executor** · `unknown · claude-opus-5`")
        assert "done" in chunks[0].text

    def test_message_without_model_renders_a_bare_attribution(self, tmp_path: Path) -> None:
        """Nothing recorded at all: the old model-less header is unchanged
        rather than growing an ``unknown · unknown`` no one can act on."""
        path = legacy_store(tmp_path)
        insert_event(path, "agent.message", OLD_MESSAGE_NO_MODEL)
        store = opened(path)
        try:
            event = event_of(store, "agent.message")
        finally:
            store.close()
        chunks = format_for_discord(event)
        assert chunks and chunks[0].text.startswith("**executor**\n")
        assert "unknown" not in chunks[0].text


class TestLegacyRunConfig:
    """``runs.config_json`` from before the ``[agent] backend`` key."""

    @pytest.mark.parametrize("config_json", [OLD_CONFIG_EMPTY, OLD_CONFIG_NO_AGENT])
    def test_a_run_row_without_an_agent_section_opens_and_reads_unknown(
        self, tmp_path: Path, config_json: str
    ) -> None:
        """Acceptance: the row opens, and its backend is reported unknown
        rather than borrowed from the daemon's current configuration."""
        path = legacy_store(tmp_path, config_json=config_json)
        store = opened(path)
        try:
            raw = store.get_run_config(RUN_ID)
        finally:
            store.close()
        ident = agent_ident_from_config_json(raw)
        assert ident["backend"] == "unknown"

    def test_headline_card_for_a_legacy_run_shows_unknown_backend(self, tmp_path: Path) -> None:
        """Acceptance: the run's headline still renders, naming the model it
        recorded next to an unknown backend."""
        path = legacy_store(tmp_path, config_json=OLD_CONFIG_NO_AGENT)
        store = opened(path)
        try:
            ident = agent_ident_from_config_json(store.get_run_config(RUN_ID))
        finally:
            store.close()
        item = WorkItem(item_id="gh:issue:4", source_key="4", title="Fix login")
        text = headline_text(item, RUN_ID, "completed", **ident)
        embed = headline_embed(item, RUN_ID, "completed", **ident)
        assert "unknown · gpt-5" in text
        assert ("Agent", "`unknown · gpt-5`", True) in embed.fields

    def test_a_pre_config_persistence_row_still_renders(self, tmp_path: Path) -> None:
        """``config_json = '{}'`` has no model either; both halves read
        unknown and the card is still a readable line."""
        path = legacy_store(tmp_path, config_json=OLD_CONFIG_EMPTY)
        store = opened(path)
        try:
            ident = agent_ident_from_config_json(store.get_run_config(RUN_ID))
        finally:
            store.close()
        item = WorkItem(item_id="gh:issue:4", source_key="4", title="Fix login")
        assert ident == {"backend": "unknown", "model": "unknown"}
        assert "unknown · unknown" in headline_text(item, RUN_ID, "completed", **ident)

    @pytest.mark.parametrize(
        "raw", ["", "   ", "not json at all", "[]", "null", '{"agent": "copilot"}', "123"]
    )
    def test_unreadable_config_json_never_raises(self, raw: str) -> None:
        """Whatever a corrupted or unexpected row holds, the label falls back
        to unknown instead of taking the daemon down."""
        ident = agent_ident_from_config_json(raw)
        assert ident["backend"] == "unknown"
        assert agent_model_label(**ident) == "unknown · unknown"
