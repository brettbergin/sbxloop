-- The exact shape Alembic revision 0001 produces, frozen the day it shipped.
--
-- 0001 is the pre-ORM migrators (sbxloop.engine.store.apply_engine_schema and
-- sbxloop.daemon.store.apply_daemon_schema), and every deployed database is
-- already stamped at it -- so Alembic will never run it against one again.
-- That makes its output a released artefact, not a live definition: a column
-- added to it now reaches a fresh install and no other database in the world.
--
-- tests/unit/test_db_schema.py compares the migrators against this file. If
-- you changed the schema and landed here, the change belongs in a new
-- revision under sbxloop/db/migrations/versions -- not in the baseline.
--
-- Normalised for comparison: one statement per name, whitespace collapsed,
-- sorted by name. Not executable as-is -- collapsing a table body that holds
-- `--` comments puts them on the same line as the closing paren.
-- daemon_chat_threads
CREATE TABLE daemon_chat_threads (run_id TEXT NOT NULL, backend TEXT NOT NULL DEFAULT 'discord', channel_id TEXT NOT NULL, thread_id TEXT NOT NULL, headline_id TEXT, status_id TEXT, PRIMARY KEY (run_id, backend));
-- daemon_gate_prompts
CREATE TABLE daemon_gate_prompts ( run_id TEXT NOT NULL, backend TEXT NOT NULL, channel_id TEXT, message_id TEXT, PRIMARY KEY (run_id, backend) );
-- daemon_local_messages
CREATE TABLE daemon_local_messages ( id INTEGER PRIMARY KEY AUTOINCREMENT, direction TEXT NOT NULL, channel_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'message', text TEXT NOT NULL DEFAULT '', embed_json TEXT, choices_json TEXT, gate_run_id TEXT, reply_to_id INTEGER, mention_users INTEGER NOT NULL DEFAULT 0, author_id TEXT NOT NULL DEFAULT 'sbx', author_name TEXT NOT NULL DEFAULT 'sbx', reactions_json TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, edited_at REAL, updated_at REAL NOT NULL DEFAULT 0, taken_at REAL );
-- daemon_merge_gates
CREATE TABLE daemon_merge_gates ( run_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, repo TEXT NOT NULL, pr_number INTEGER NOT NULL, pr_url TEXT NOT NULL DEFAULT '', branch TEXT, notify_ids TEXT NOT NULL DEFAULT '[]', custom_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open', kind TEXT NOT NULL DEFAULT 'merge', prompt_channel_id TEXT, prompt_message_id TEXT, created_at REAL NOT NULL, resolved_at REAL, resolved_by TEXT, detail TEXT );
-- daemon_pending_clarifications
CREATE TABLE daemon_pending_clarifications ( id INTEGER PRIMARY KEY AUTOINCREMENT, backend TEXT NOT NULL DEFAULT 'discord', channel_id TEXT, prompt_message_id TEXT, asker_id TEXT, asker_name TEXT, question TEXT NOT NULL, assumption TEXT NOT NULL, deadline REAL NOT NULL, created_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'open', resolved_at REAL );
-- daemon_prior_attempts
CREATE TABLE daemon_prior_attempts ( source_key TEXT NOT NULL, repo TEXT NOT NULL DEFAULT '', run_id TEXT, branch TEXT, pr_number INTEGER, updated_at REAL NOT NULL DEFAULT 0, PRIMARY KEY (source_key, repo) );
-- daemon_requesters
CREATE TABLE daemon_requesters ( source_key TEXT NOT NULL, repo TEXT NOT NULL DEFAULT '', requester_id TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY (source_key, repo) );
-- daemon_review_holds
CREATE TABLE daemon_review_holds ( run_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, repo TEXT NOT NULL, pr_number INTEGER NOT NULL, pr_url TEXT NOT NULL DEFAULT '', branch TEXT, login TEXT NOT NULL DEFAULT '', is_bot INTEGER, approvals_required INTEGER NOT NULL DEFAULT 1, held_by_draft INTEGER NOT NULL DEFAULT 0, notify_ids TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL, since_at REAL NOT NULL, next_poll_at REAL NOT NULL, polls INTEGER NOT NULL DEFAULT 0, resolved_at REAL, resolved_by TEXT, detail TEXT );
-- daemon_run_resumes
CREATE TABLE daemon_run_resumes ( id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, item_id TEXT NOT NULL, resumed_at REAL NOT NULL );
-- daemon_run_watches
CREATE TABLE daemon_run_watches (run_id TEXT NOT NULL, watcher_id TEXT NOT NULL, created_at REAL NOT NULL, backend TEXT NOT NULL DEFAULT 'discord', UNIQUE(run_id, watcher_id, backend));
-- daemon_runs
CREATE TABLE daemon_runs ( run_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL, result TEXT );
-- daemon_schedules
CREATE TABLE daemon_schedules ( name TEXT PRIMARY KEY, anchor REAL NOT NULL, last_due REAL, last_fired_at REAL, last_item TEXT, paused_by TEXT, paused_at REAL, profile TEXT, ask TEXT, every TEXT, cron TEXT, timezone TEXT, source TEXT, created_by TEXT, created_at REAL );
-- daemon_state
CREATE TABLE daemon_state ( key TEXT PRIMARY KEY, value TEXT );
-- daemon_work_items
CREATE TABLE daemon_work_items ( item_id TEXT PRIMARY KEY, source_key TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, claimed INTEGER NOT NULL DEFAULT 0, run_id TEXT, last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, pending_report TEXT, requested_by TEXT, -- The owner/name the item came from, '' for rows written before -- multi-repo support (they belong to the sole configured repository). -- Identity is (source_key, repo): issue #4 in two repositories is two -- work items, which a bare UNIQUE(source_key) would have collided. repo TEXT NOT NULL DEFAULT '', -- Earliest dispatch time, when a retry is scheduled rather than backed -- off by attempt count (an exhausted run resuming its own PR, #523). not_before REAL, -- The claim comment's token, written before the comment is posted so -- a half-claim survives the process that made it (#530). claim_token TEXT, -- What the previous attempt left on the GitHub origin (#600): the run -- it ran under, the branch it pushed and the PR it opened, kept across -- a re-queue so a restart continues that work instead of redoing it. prior_run_id TEXT, prior_branch TEXT, prior_pr_number INTEGER, -- The run the item becomes (#760) and the workload profile it runs -- under. Named run_kind: a bare `kind` column on this table is the -- pre-1.0 lanes' marker the archive check looks for. run_kind TEXT NOT NULL DEFAULT 'code', profile TEXT, UNIQUE(source_key, repo) );
-- events
CREATE TABLE events ( seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts REAL NOT NULL, type TEXT NOT NULL, job_id TEXT, data_json TEXT NOT NULL DEFAULT '{}' );
-- idx_chat_threads_thread
CREATE INDEX idx_chat_threads_thread ON daemon_chat_threads(thread_id);
-- idx_daemon_items_state
CREATE INDEX idx_daemon_items_state ON daemon_work_items(state, created_at);
-- idx_daemon_resumes_at
CREATE INDEX idx_daemon_resumes_at ON daemon_run_resumes(resumed_at);
-- idx_daemon_resumes_item
CREATE INDEX idx_daemon_resumes_item ON daemon_run_resumes(item_id);
-- idx_daemon_run_watches_run
CREATE INDEX idx_daemon_run_watches_run ON daemon_run_watches(run_id);
-- idx_daemon_runs_started
CREATE INDEX idx_daemon_runs_started ON daemon_runs(started_at);
-- idx_events_run
CREATE INDEX idx_events_run ON events (run_id, seq);
-- idx_local_messages_channel
CREATE INDEX idx_local_messages_channel ON daemon_local_messages(channel_id, id);
-- idx_local_messages_pending
CREATE INDEX idx_local_messages_pending ON daemon_local_messages(id) WHERE direction = 'in' AND taken_at IS NULL;
-- idx_local_messages_updated
CREATE INDEX idx_local_messages_updated ON daemon_local_messages(channel_id, updated_at);
-- idx_merge_gates_item
CREATE INDEX idx_merge_gates_item ON daemon_merge_gates(item_id);
-- idx_merge_gates_state
CREATE INDEX idx_merge_gates_state ON daemon_merge_gates(state);
-- idx_pending_clarify_due
CREATE INDEX idx_pending_clarify_due ON daemon_pending_clarifications(state, deadline);
-- idx_review_holds_item
CREATE INDEX idx_review_holds_item ON daemon_review_holds(item_id);
-- idx_review_holds_state
CREATE INDEX idx_review_holds_state ON daemon_review_holds(state);
-- phase_attempts
CREATE TABLE phase_attempts ( id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, task_id TEXT, phase TEXT NOT NULL, attempt INTEGER NOT NULL, status TEXT NOT NULL, output_json TEXT, started_at REAL NOT NULL, ended_at REAL NOT NULL, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, turns INTEGER );
-- reconciliations
CREATE TABLE reconciliations ( run_id TEXT NOT NULL, round INTEGER NOT NULL, anchor TEXT NOT NULL, status TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL, PRIMARY KEY (run_id, round, anchor) );
-- runs
CREATE TABLE runs ( run_id TEXT PRIMARY KEY, outcome TEXT NOT NULL, state TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL, workspace TEXT, mounted INTEGER NOT NULL DEFAULT 0, kept_reason TEXT, user_guidance TEXT NOT NULL DEFAULT '[]', reason TEXT, stage TEXT, pr_number INTEGER, pr_url TEXT, pr_node_id TEXT, branch TEXT, head_sha TEXT, review_rounds INTEGER NOT NULL DEFAULT 0, ci_rounds INTEGER NOT NULL DEFAULT 0, update_attempts INTEGER NOT NULL DEFAULT 0, update_head TEXT, last_verdict TEXT, exhausted TEXT, granted_rounds INTEGER NOT NULL DEFAULT 0, pr_title TEXT, credentials TEXT NOT NULL DEFAULT '[]', kind TEXT NOT NULL DEFAULT 'code', published TEXT NOT NULL DEFAULT '[]' );
-- tasks
CREATE TABLE tasks ( run_id TEXT NOT NULL, task_id TEXT NOT NULL, order_idx INTEGER NOT NULL, state TEXT NOT NULL, spec_json TEXT NOT NULL, revisions INTEGER NOT NULL DEFAULT 0, replans INTEGER NOT NULL DEFAULT 0, last_feedback TEXT NOT NULL DEFAULT '', session_id TEXT, verify_fingerprints TEXT NOT NULL DEFAULT '[]', verify_suspect INTEGER NOT NULL DEFAULT 0, output_json TEXT, PRIMARY KEY (run_id, task_id) );
