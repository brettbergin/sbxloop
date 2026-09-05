"""Raw state databases in the shapes released sbxloop versions wrote (#524).

A store migrates in place on open (``CREATE TABLE IF NOT EXISTS`` plus
idempotent ``ALTER``s), so the only test that proves an upgrade path is one
that starts from a database **the old code wrote** — not one the new code
produced, which normalises on write and cannot hold an old value. Every
schema snapshot here is hand-written SQL, frozen at the shape a release
left behind; a change to persisted state adds the shape *before* it and a
test that opens it.

Daemon store (``daemon_work_items`` and friends):

- ``pre_typed_ids`` — before #508: item ids are bare ``gh:<n>``, no
  ``repo`` column, ``UNIQUE(source_key)``.
- ``pre_multirepo`` — before #511: typed ``gh:issue:<n>`` ids, still no
  ``repo`` column and still ``UNIQUE(source_key)``.
- ``pre_scheduled_retry`` — before #523: ``repo`` and the
  ``(source_key, repo)`` key, but no ``not_before``.
- ``pre_claim_token`` — before #530: ``not_before`` but no ``claim_token``.
- ``pre_prior_attempt`` — before #600: ``claim_token`` but none of the
  ``prior_run_id`` / ``prior_branch`` / ``prior_pr_number`` columns a
  label-restart carries the previous attempt's pushed work in.
- ``pre_local_bridge`` — before the operator console's local chat bridge:
  the current item columns, but chat state keyed by run alone —
  ``daemon_chat_threads`` with ``PRIMARY KEY (run_id)``,
  ``daemon_run_watches`` without a ``backend`` column, and the gate's
  prompt location on the ``daemon_merge_gates`` row itself.

Engine store (``runs`` / ``phase_attempts``):

- ``pre_workspace`` — before 0.3: ``runs`` without the workspace columns.
- ``pre_guidance`` — before persisted user guidance and ``reason``.
- ``pre_usage`` — ``phase_attempts`` without the token/turn columns.
- ``pre_pipeline`` — before the 1.0 pipeline columns (``stage``, the PR
  fields, the round counters).
- ``pre_granted_rounds`` — before #523: no ``exhausted`` / ``granted_rounds``.
- ``pre_pr_title`` — before #621: no ``pr_title``.

``every_daemon_row`` writes one work item per (state x id form) a deployed
daemon can hold, so an upgrade test can sweep the whole space instead of
the one row the finding named.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

DaemonShape = str
EngineShape = str

# -- daemon store ---------------------------------------------------------------

_DAEMON_ITEMS_NO_REPO = (
    "CREATE TABLE daemon_work_items (item_id TEXT PRIMARY KEY, "
    "source_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL, "
    "body TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', "
    "state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
    "claimed INTEGER NOT NULL DEFAULT 0, run_id TEXT, last_error TEXT, "
    "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
    "pending_report TEXT, requested_by TEXT)"
)
_DAEMON_REQUESTERS_NO_REPO = (
    "CREATE TABLE daemon_requesters (source_key TEXT PRIMARY KEY, "
    "requester_id TEXT NOT NULL, created_at REAL NOT NULL)"
)
_DAEMON_ITEMS_REPO = (
    "CREATE TABLE daemon_work_items (item_id TEXT PRIMARY KEY, "
    "source_key TEXT NOT NULL, title TEXT NOT NULL, "
    "body TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', "
    "state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
    "claimed INTEGER NOT NULL DEFAULT 0, run_id TEXT, last_error TEXT, "
    "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
    "pending_report TEXT, requested_by TEXT, repo TEXT NOT NULL DEFAULT '', "
    "UNIQUE(source_key, repo))"
)
_DAEMON_REQUESTERS_REPO = (
    "CREATE TABLE daemon_requesters (source_key TEXT NOT NULL, "
    "requester_id TEXT NOT NULL, created_at REAL NOT NULL, "
    "repo TEXT NOT NULL DEFAULT '', PRIMARY KEY (source_key, repo))"
)

_DAEMON_ITEMS_NOT_BEFORE = _DAEMON_ITEMS_REPO.replace(
    "repo TEXT NOT NULL DEFAULT '', ", "repo TEXT NOT NULL DEFAULT '', not_before REAL, "
)

_DAEMON_ITEMS_CLAIM_TOKEN = _DAEMON_ITEMS_NOT_BEFORE.replace(
    "not_before REAL, ", "not_before REAL, claim_token TEXT, "
)

_DAEMON_ITEMS_PRIOR = _DAEMON_ITEMS_CLAIM_TOKEN.replace(
    "claim_token TEXT, ",
    "claim_token TEXT, prior_run_id TEXT, prior_branch TEXT, prior_pr_number INTEGER, ",
)

# The chat tables as every release before the local bridge wrote them:
# one thread per run, one watcher list per run, the prompt on the gate.
_DAEMON_CHAT_THREADS_RUN_KEYED = (
    "CREATE TABLE daemon_chat_threads (run_id TEXT PRIMARY KEY, "
    "backend TEXT NOT NULL DEFAULT 'discord', channel_id TEXT NOT NULL, "
    "thread_id TEXT NOT NULL, headline_id TEXT, status_id TEXT)"
)
_DAEMON_RUN_WATCHES_NO_BACKEND = (
    "CREATE TABLE daemon_run_watches (run_id TEXT NOT NULL, watcher_id TEXT NOT NULL, "
    "created_at REAL NOT NULL, UNIQUE(run_id, watcher_id))"
)
_DAEMON_MERGE_GATES_PROMPT_ON_ROW = (
    "CREATE TABLE daemon_merge_gates (run_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, "
    "repo TEXT NOT NULL, pr_number INTEGER NOT NULL, pr_url TEXT NOT NULL DEFAULT '', "
    "branch TEXT, notify_ids TEXT NOT NULL DEFAULT '[]', custom_id TEXT NOT NULL, "
    "state TEXT NOT NULL DEFAULT 'open', prompt_channel_id TEXT, prompt_message_id TEXT, "
    "created_at REAL NOT NULL, resolved_at REAL, resolved_by TEXT, detail TEXT)"
)
_DAEMON_STATE = "CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT)"

DAEMON_SHAPES: dict[DaemonShape, tuple[str, ...]] = {
    "pre_typed_ids": (_DAEMON_ITEMS_NO_REPO, _DAEMON_REQUESTERS_NO_REPO),
    "pre_multirepo": (_DAEMON_ITEMS_NO_REPO, _DAEMON_REQUESTERS_NO_REPO),
    "pre_scheduled_retry": (_DAEMON_ITEMS_REPO, _DAEMON_REQUESTERS_REPO),
    "pre_claim_token": (_DAEMON_ITEMS_NOT_BEFORE, _DAEMON_REQUESTERS_REPO),
    "pre_prior_attempt": (_DAEMON_ITEMS_CLAIM_TOKEN, _DAEMON_REQUESTERS_REPO),
    "pre_local_bridge": (
        _DAEMON_ITEMS_PRIOR,
        _DAEMON_REQUESTERS_REPO,
        _DAEMON_CHAT_THREADS_RUN_KEYED,
        _DAEMON_RUN_WATCHES_NO_BACKEND,
        _DAEMON_MERGE_GATES_PROMPT_ON_ROW,
        _DAEMON_STATE,
    ),
}

# Every state a work-item row can be in, with the bookkeeping a deployed
# daemon would have left on it. ``queued`` + ``run_id`` is a resume-pending
# row; ``running`` + ``claimed`` is the run in flight.
DAEMON_ITEM_STATES: tuple[dict[str, Any], ...] = (
    {"state": "queued", "claimed": 0, "run_id": None},
    {"state": "queued", "claimed": 1, "run_id": None},  # claimed, never started
    {"state": "queued", "claimed": 1, "run_id": "r_resume"},  # resume pending
    {"state": "running", "claimed": 1, "run_id": "r_live"},
    {"state": "done", "claimed": 1, "run_id": "r_done", "pending_report": "merged"},
    {"state": "failed", "claimed": 1, "run_id": "r_failed", "last_error": "gave up"},
    {"state": "blocked", "claimed": 1, "run_id": "r_blocked", "pending_report": "blocked"},
    {"state": "cancelled", "claimed": 1, "run_id": "r_cancelled"},
)


def daemon_db(tmp_path: Path, shape: DaemonShape, *, name: str = "state.db") -> Path:
    """A raw daemon ``state.db`` in one of :data:`DAEMON_SHAPES`, empty."""
    path = tmp_path / name
    conn = sqlite3.connect(path)
    for ddl in DAEMON_SHAPES[shape]:
        conn.execute(ddl)
    conn.commit()
    conn.close()
    return path


def insert_daemon_row(path: Path, **fields: Any) -> None:
    """Insert one ``daemon_work_items`` row exactly as given — no
    normalisation, no defaults beyond the table's own."""
    row = {"body": "", "url": "", "attempts": 0, "created_at": 1.0, "updated_at": 1.0}
    row.update(fields)
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn = sqlite3.connect(path)
    conn.execute(
        f"INSERT INTO daemon_work_items ({columns}) VALUES ({marks})",  # nosec B608 - test SQL
        tuple(row.values()),
    )
    conn.commit()
    conn.close()


def insert_row(path: Path, table: str, **fields: Any) -> None:
    """Insert one row into any table exactly as given."""
    columns = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    conn = sqlite3.connect(path)
    conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})",  # nosec B608 - test SQL
        tuple(fields.values()),
    )
    conn.commit()
    conn.close()


def every_daemon_row(
    path: Path,
    *,
    id_forms: Iterable[str] = ("gh:{n}", "gh:issue:{n}"),
    states: Iterable[Mapping[str, Any]] = DAEMON_ITEM_STATES,
    repo: str | None = None,
    start: int = 1,
) -> list[tuple[str, str]]:
    """One row per (id form x state); returns ``(item_id, state)`` as
    written. ``repo`` is set only on shapes that have the column."""
    written: list[tuple[str, str]] = []
    n = start
    for form in id_forms:
        for state in states:
            item_id = form.format(n=n)
            fields: dict[str, Any] = {
                "item_id": item_id,
                "source_key": str(n),
                "title": f"Item {n}",
                "url": f"https://github.com/o/r/issues/{n}",
                **state,
            }
            if repo is not None:
                fields["repo"] = repo
            insert_daemon_row(path, **fields)
            written.append((item_id, str(state["state"])))
            n += 1
    return written


def raw_daemon_rows(path: Path) -> list[tuple[str, str]]:
    """``(item_id, state)`` straight from the file, bypassing the store."""
    conn = sqlite3.connect(path)
    try:
        return [
            (str(a), str(b))
            for a, b in conn.execute(
                "SELECT item_id, state FROM daemon_work_items ORDER BY rowid"
            ).fetchall()
        ]
    finally:
        conn.close()


# -- engine store ---------------------------------------------------------------

_RUNS_BASE = (
    "CREATE TABLE runs (run_id TEXT PRIMARY KEY, outcome TEXT NOT NULL,"
    " state TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}',"
    " created_at REAL NOT NULL, updated_at REAL NOT NULL"
)
_PHASE_ATTEMPTS_PRE_USAGE = (
    "CREATE TABLE phase_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " run_id TEXT NOT NULL, task_id TEXT, phase TEXT NOT NULL,"
    " attempt INTEGER NOT NULL, status TEXT NOT NULL, output_json TEXT,"
    " started_at REAL NOT NULL, ended_at REAL NOT NULL)"
)

ENGINE_SHAPES: dict[EngineShape, tuple[str, ...]] = {
    "pre_workspace": (_RUNS_BASE + ")",),
    "pre_guidance": (
        _RUNS_BASE + ", workspace TEXT, mounted INTEGER NOT NULL DEFAULT 0, kept_reason TEXT)",
    ),
    "pre_usage": (_RUNS_BASE + ")", _PHASE_ATTEMPTS_PRE_USAGE),
    "pre_pipeline": (
        _RUNS_BASE + ", workspace TEXT, mounted INTEGER NOT NULL DEFAULT 0, kept_reason TEXT,"
        " user_guidance TEXT NOT NULL DEFAULT '[]', reason TEXT)",
    ),
    "pre_granted_rounds": (
        _RUNS_BASE + ", workspace TEXT, mounted INTEGER NOT NULL DEFAULT 0, kept_reason TEXT,"
        " user_guidance TEXT NOT NULL DEFAULT '[]', reason TEXT, stage TEXT,"
        " pr_number INTEGER, pr_url TEXT, pr_node_id TEXT, branch TEXT, head_sha TEXT,"
        " review_rounds INTEGER NOT NULL DEFAULT 0, ci_rounds INTEGER NOT NULL DEFAULT 0,"
        " update_attempts INTEGER NOT NULL DEFAULT 0, update_head TEXT, last_verdict TEXT)",
    ),
    "pre_pr_title": (
        _RUNS_BASE + ", workspace TEXT, mounted INTEGER NOT NULL DEFAULT 0, kept_reason TEXT,"
        " user_guidance TEXT NOT NULL DEFAULT '[]', reason TEXT, stage TEXT,"
        " pr_number INTEGER, pr_url TEXT, pr_node_id TEXT, branch TEXT, head_sha TEXT,"
        " review_rounds INTEGER NOT NULL DEFAULT 0, ci_rounds INTEGER NOT NULL DEFAULT 0,"
        " update_attempts INTEGER NOT NULL DEFAULT 0, update_head TEXT, last_verdict TEXT,"
        " exhausted TEXT, granted_rounds INTEGER NOT NULL DEFAULT 0)",
    ),
}


def engine_db(tmp_path: Path, shape: EngineShape, *, name: str = "state.db") -> Path:
    """A raw engine ``state.db`` in one of :data:`ENGINE_SHAPES`, empty."""
    path = tmp_path / name
    conn = sqlite3.connect(path)
    for ddl in ENGINE_SHAPES[shape]:
        conn.execute(ddl)
    conn.commit()
    conn.close()
    return path


def insert_run_row(path: Path, **fields: Any) -> None:
    """Insert one ``runs`` row exactly as given."""
    row = {"config_json": "{}", "created_at": 1.0, "updated_at": 1.0}
    row.update(fields)
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn = sqlite3.connect(path)
    conn.execute(
        f"INSERT INTO runs ({columns}) VALUES ({marks})",  # nosec B608 - test SQL
        tuple(row.values()),
    )
    conn.commit()
    conn.close()


def insert_phase_row(path: Path, **fields: Any) -> None:
    """Insert one ``phase_attempts`` row exactly as given."""
    columns = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    conn = sqlite3.connect(path)
    conn.execute(
        f"INSERT INTO phase_attempts ({columns}) VALUES ({marks})",  # nosec B608 - test SQL
        tuple(fields.values()),
    )
    conn.commit()
    conn.close()
