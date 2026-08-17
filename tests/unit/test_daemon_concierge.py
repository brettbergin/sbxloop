"""Concierge: the control channel's agent, without a sandbox or the SDK.

A FakeHost stands in for DaemonAgent: its WorkerClient stand-in receives
the JobRequest the concierge builds, plays the model by calling the
``tool_handler`` with scripted host-tool calls, and returns a JobResult.
So these tests cover the whole host half — the tool registry against real
stores and a real inbox directory, session persistence/rotation, the
retry-once paths, and the reply shaping — with the sandbox transport
covered separately (test_worker_client / test_daemon_agentbox).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.daemon.concierge import (
    CONCIERGE_AGENT,
    STATE_SESSION_ID,
    STATE_SESSION_TURNS,
    Concierge,
    ConciergeReply,
)
from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.daemon.sources import InboxSource
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import TaskSpec
from sbxloop.engine.store import StateStore
from sbxloop.errors import DaemonError, WorkerError, WorkerTimeoutError
from sbxloop.events import EventBus
from sbxloop_worker.protocol import (
    ErrorInfo,
    HostToolCall,
    HostToolResponse,
    JobRequest,
    JobResult,
)
from tests.unit.test_daemon_discord import FakeLoop

# One scripted "model": a list of host-tool calls to make, then the text
# to answer with. ``session_id`` is what the SDK would hand back.
Script = dict[str, Any]


class FakeClient:
    """WorkerClient stand-in that plays the model."""

    def __init__(self, scripts: list[Script]) -> None:
        self.scripts = list(scripts)
        self.jobs: list[JobRequest] = []
        self.agents: list[str | None] = []
        self.responses: list[HostToolResponse] = []

    def submit(
        self,
        job: JobRequest,
        *,
        agent: str | None = None,
        tool_handler: Callable[[HostToolCall], HostToolResponse] | None = None,
    ) -> JobResult:
        self.jobs.append(job)
        self.agents.append(agent)
        if not self.scripts:
            raise AssertionError("FakeClient script exhausted")
        script = self.scripts.pop(0)
        if "raise" in script:
            raise script["raise"]
        assert tool_handler is not None
        for i, (name, args) in enumerate(script.get("calls", [])):
            response = tool_handler(HostToolCall(call_id=f"c{i}", name=name, arguments=args))
            self.responses.append(response)
        if "error" in script:
            return JobResult(
                job_id=job.job_id,
                status=script.get("status", "error"),
                error=ErrorInfo(type="X", message=script["error"]),
            )
        return JobResult(
            job_id=job.job_id,
            status="ok",
            output_text=script.get("text", "ok"),
            session_id=script.get("session_id", "s1"),
        )


class FakeHost:
    def __init__(self, client: FakeClient) -> None:
        self._client = client
        self.failures: list[BaseException] = []
        self.drop = True
        self.closed = False
        self.client_calls = 0

    def client(self) -> FakeClient:
        self.client_calls += 1
        return self._client

    def note_failure(self, exc: BaseException) -> bool:
        self.failures.append(exc)
        return self.drop

    def close(self) -> None:
        self.closed = True


class FakeGithub:
    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers = answers or {}
        self.paths: list[str] = []

    def call(self, fn: Callable[[Any], Any]) -> Any:
        return fn(self)

    def raw(self, method: str, path: str, body: Any = None) -> Any:
        self.paths.append(path)
        for key, value in self.answers.items():
            if key in path:
                return value
        return {}

    def contents_read(self, repo: str, path: str, ref: str | None = None) -> str:
        self.paths.append(f"contents:{path}@{ref}")
        return "print('hi')\n"

    def issue_create(
        self, repo: str, title: str, body: str = "", labels: list[str] | None = None
    ) -> Any:
        from sbxloop.gh.ops import IssueRef

        self.created = getattr(self, "created", [])
        self.created.append((repo, title, body, labels))
        return IssueRef(number=41, url="https://gh/i/41")


class LoopWithRuns(FakeLoop):
    """FakeLoop plus the report/current surface the concierge's tools use."""

    def __init__(self, dstore: DaemonStore) -> None:
        super().__init__(dstore)
        self.reports: dict[str, RunReport] = {}
        self.current = None

    def report_for(self, run_id: str) -> RunReport:
        return self.reports.get(run_id, RunReport(run_id, "completed", "1/1 tasks done"))


def make(
    tmp_path: Path,
    scripts: list[Script],
    *,
    github: FakeGithub | None = None,
    inbox: bool = True,
    config: dict[str, Any] | None = None,
) -> tuple[Concierge, FakeClient, FakeHost, LoopWithRuns, DaemonStore]:
    raw: dict[str, Any] = {
        "state_dir": str(tmp_path / "state"),
        "discord": {"channel_id": 42},
        "daemon": {"inbox_dir": str(tmp_path / "inbox")},
    }
    if github is not None:
        raw["github"] = {"repo": "owner/repo"}
    for key, value in (config or {}).items():
        raw.setdefault(key, {}).update(value) if isinstance(value, dict) else raw.update(
            {key: value}
        )
    cfg = Config.model_validate(raw)
    dstore = DaemonStore(cfg.state_dir / "state.db")
    loop = LoopWithRuns(dstore)
    client = FakeClient(scripts)
    host = FakeHost(client)
    concierge = Concierge(
        cfg,
        loop=loop,  # type: ignore[arg-type]
        dstore=dstore,
        store_factory=lambda: StateStore(cfg.state_dir / "state.db"),
        inbox=InboxSource(tmp_path / "inbox") if inbox else None,
        github=github,  # type: ignore[arg-type]
        host=host,
        bus=EventBus(),
        clock=lambda: 1_000_000.0,
    )
    return concierge, client, host, loop, dstore


def turn(
    concierge: Concierge, text: str = "hello", author: str = "Discord user `brett`"
) -> ConciergeReply:
    return concierge.submit_turn(text, author=author).result(timeout=10)


class TestJobShape:
    def test_job_carries_tools_prompt_and_session(self, tmp_path: Path) -> None:
        concierge, client, _, _, dstore = make(tmp_path, [{"text": "hi there", "session_id": "sA"}])
        reply = turn(concierge, "what's up?")
        assert reply == ConciergeReply("hi there")
        (job,) = client.jobs
        assert job.kind == "agent.session" and job.run_id == "concierge"
        assert client.agents == [CONCIERGE_AGENT]
        assert job.permission_mode == "read_only" and job.available_tools == []
        assert job.resume_session_id is None
        assert job.timeout_s == 180.0 and job.max_tool_calls == 16
        names = [t.name for t in job.host_tools]
        assert names == [
            "sbx_control",
            "list_runs",
            "run_detail",
            "run_events",
            "item_detail",
            "enqueue_work",
        ]  # no github_get: no repo configured
        assert job.host_tools_dir is None  # the WorkerClient fills it in
        assert job.system_message and "sbxloop concierge" in job.system_message
        assert "`sbx_control`" in job.system_message
        assert job.prompt is not None
        assert job.prompt.startswith("[situation @ ")
        assert "queued: 2" in job.prompt and "speaker: Discord user `brett`" in job.prompt
        assert job.prompt.endswith("\n---\nwhat's up?")
        # Session persisted for the next turn.
        assert dstore.get_value(STATE_SESSION_ID) == "sA"
        assert dstore.get_value(STATE_SESSION_TURNS) == "1"

    def test_second_turn_resumes_and_rotation_starts_fresh(self, tmp_path: Path) -> None:
        concierge, client, _, _, dstore = make(
            tmp_path,
            [{"session_id": "sA"}, {"session_id": "sA"}, {"session_id": "sB"}],
            config={"concierge": {"session_turns": 2}},
        )
        turn(concierge)
        turn(concierge)
        assert client.jobs[1].resume_session_id == "sA"
        assert dstore.get_value(STATE_SESSION_TURNS) == "2"
        turn(concierge)  # turns >= session_turns → fresh session
        assert client.jobs[2].resume_session_id is None
        assert dstore.get_value(STATE_SESSION_ID) == "sB"
        assert dstore.get_value(STATE_SESSION_TURNS) == "1"

    def test_github_tool_present_when_repo_configured(self, tmp_path: Path) -> None:
        concierge, client, *_ = make(tmp_path, [{}], github=FakeGithub())
        turn(concierge)
        names = [t.name for t in client.jobs[0].host_tools]
        assert "github_get" in names
        assert "create_issue" in names and "label_issue_for_run" in names
        concierge3, client3, *_ = make(
            tmp_path / "c",
            [{}],
            github=FakeGithub(),
            config={"concierge": {"create_issues": False}},
        )
        turn(concierge3)
        names3 = [t.name for t in client3.jobs[0].host_tools]
        assert "github_get" in names3 and "create_issue" not in names3
        concierge2, client2, *_ = make(
            tmp_path / "b", [{}], github=FakeGithub(), config={"concierge": {"github_tools": False}}
        )
        turn(concierge2)
        assert "github_get" not in [t.name for t in client2.jobs[0].host_tools]

    def test_reply_is_clipped(self, tmp_path: Path) -> None:
        concierge, *_ = make(
            tmp_path, [{"text": "x" * 6000}], config={"concierge": {"max_reply_chars": 500}}
        )
        reply = turn(concierge)
        assert len(reply.text) <= 500 and reply.text.endswith("(truncated)")


class TestTools:
    def test_sbx_control_dispatches_with_attribution(self, tmp_path: Path) -> None:
        concierge, client, _, loop, _ = make(
            tmp_path,
            [
                {
                    "calls": [
                        ("sbx_control", {"command": "pause"}),
                        ("sbx_control", {"command": "status"}),
                    ]
                }
            ],
        )
        turn(concierge, "pause please")
        assert loop.paused is True
        pause, status = client.responses
        assert pause.ok and "paused" in pause.text
        assert status.ok and "queued" in status.text
        # status carries the raw dict as JSON on the second line
        assert json.loads(status.text.splitlines()[-1])["paused"] is True

    def test_sbx_control_bad_verb_is_not_accepted(self, tmp_path: Path) -> None:
        concierge, client, *_ = make(
            tmp_path, [{"calls": [("sbx_control", {"command": "explode"})]}]
        )
        turn(concierge)
        (resp,) = client.responses
        assert resp.ok  # the tool ran; the daemon just did not accept the verb
        assert resp.text.startswith("(command not accepted)")

    def test_enqueue_work_writes_a_pending_inbox_item(self, tmp_path: Path) -> None:
        concierge, client, *_ = make(
            tmp_path,
            [
                {
                    "calls": [
                        ("enqueue_work", {"title": "Add retries to fetch", "body": "Wrap fetch()…"})
                    ]
                }
            ],
        )
        turn(concierge, "also please add retries", author="Discord user `ana`")
        (resp,) = client.responses
        assert resp.ok and resp.text.startswith("queued inbox:add-retries-to-fetch-")
        (path,) = (tmp_path / "inbox" / "pending").glob("*.md")
        text = path.read_text()
        assert text.startswith("# Add retries to fetch\n\nWrap fetch()…")
        assert "Requested by Discord user `ana` (via concierge) via the concierge" in text
        # ...and the daemon's inbox source will poll it (after the settle window).
        source = InboxSource(tmp_path / "inbox", clock=lambda: time.time() + 10)
        assert [i.title for i in source.poll()] == ["Add retries to fetch"]

    def test_enqueue_without_inbox_is_an_error_text(self, tmp_path: Path) -> None:
        concierge, client, *_ = make(
            tmp_path, [{"calls": [("enqueue_work", {"title": "t", "body": "b"})]}], inbox=False
        )
        turn(concierge)
        assert "no inbox source" in client.responses[0].text

    def test_enqueue_mentions_paused_daemon(self, tmp_path: Path) -> None:
        concierge, client, _, loop, _ = make(
            tmp_path, [{"calls": [("enqueue_work", {"title": "t", "body": "b"})]}]
        )
        loop.paused = True
        turn(concierge)
        assert "PAUSED" in client.responses[0].text

    def _seed_run(self, tmp_path: Path, dstore: DaemonStore, loop: LoopWithRuns) -> StateStore:
        store = StateStore(tmp_path / "state" / "state.db")
        store.create_run("r1abcdefg", "Ship the widget")
        store.save_tasks(
            "r1abcdefg",
            [TaskSpec(id="t1", title="Build widget"), TaskSpec(id="t2", title="Test widget")],
        )
        store.set_run_state("r1abcdefg", "completed")
        from sbxloop.events import Event

        for i in range(3):
            store.append_event(
                Event.now("agent.message", "r1abcdefg", content=f"msg {i}", agent="executor")
            )
        store.append_event(Event.now("task.end", "r1abcdefg", task="t1", state="done"))
        store.append_run_guidance("r1abcdefg", "prefer small commits")
        item = WorkItem(item_id="inbox:w.md", source="inbox", source_key="w.md", title="Widget")
        dstore.upsert_new(item, 1.0)
        dstore.mark_running("inbox:w.md", "r1abcdefg", 2.0)
        dstore.record_discord_thread("r1abcdefg", 42, 4242, None)
        loop.reports["r1abcdefg"] = RunReport(
            "r1abcdefg", "completed", "2/2 tasks done", delivery=(7, "https://gh/pr/7")
        )
        return store

    def test_list_runs_run_detail_and_events(self, tmp_path: Path) -> None:
        concierge, client, _, loop, dstore = make(
            tmp_path,
            [
                {
                    "calls": [
                        ("list_runs", {"limit": 5}),
                        ("run_detail", {"run_id": "r1abcdefg"}),
                        (
                            "run_events",
                            {"run_id": "r1abcdefg", "type_prefix": "agent.message", "tail": 2},
                        ),
                        ("run_detail", {"run_id": "nope"}),
                        ("item_detail", {"item_id": "inbox:w.md"}),
                    ]
                }
            ],
        )
        self._seed_run(tmp_path, dstore, loop)
        turn(concierge)
        listing, detail, events, missing, item = client.responses
        assert "r1abcdefg · completed" in listing.text and "inbox:w.md · Widget" in listing.text
        assert "outcome: Ship the widget" in detail.text
        assert "- t1 [pending] Build widget" in detail.text
        assert "delivered PR: #7 https://gh/pr/7" in detail.text
        assert "prefer small commits" in detail.text
        assert "work item: inbox:w.md [running]" in detail.text
        assert "Discord thread: <#4242>" in detail.text
        assert events.text.startswith("(4 events; showing last 2)") or "msg 2" in events.text
        assert "msg 2" in events.text and "msg 0" not in events.text
        assert missing.text.startswith("no run 'nope'")
        assert "inbox:w.md: patch from inbox · state running" in item.text
        assert "runs: r1abcdefg" in item.text and "<#4242>" in item.text

    def test_github_get_reads_through_the_ops_sandbox(self, tmp_path: Path) -> None:
        github = FakeGithub(
            {
                "/pulls/7/files": [
                    {
                        "filename": "a.py",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 0,
                        "patch": "+x",
                    }
                ],
                "/pulls/7": {
                    "number": 7,
                    "title": "T",
                    "state": "open",
                    "html_url": "u",
                    "head": {"ref": "h"},
                    "base": {"ref": "main"},
                },
                "/issues/3/comments": [
                    {"user": {"login": "ana"}, "created_at": "now", "body": "hi"}
                ],
                "/issues/3": {
                    "number": 3,
                    "title": "I",
                    "state": "open",
                    "labels": [{"name": "bug"}],
                },
            }
        )
        concierge, client, *_ = make(
            tmp_path,
            [
                {
                    "calls": [
                        ("github_get", {"what": "pr", "number": 7}),
                        ("github_get", {"what": "pr_diff", "number": 7}),
                        ("github_get", {"what": "issue", "number": 3}),
                        ("github_get", {"what": "issue_comments", "number": 3}),
                        ("github_get", {"what": "file", "path": "src/a.py", "ref": "main"}),
                        ("github_get", {"what": "pr"}),
                    ]
                }
            ],
            github=github,
        )
        turn(concierge)
        pr, diff, issue, comments, file, bad = client.responses
        assert "PR #7: T" in pr.text and "h → main" in pr.text
        assert "modified a.py" in diff.text and "+x" in diff.text
        assert "issue #3: I [open]" in issue.text and "labels: bug" in issue.text
        assert "- ana (now): hi" in comments.text
        assert file.text.startswith("src/a.py@main:")
        assert bad.text == "pr needs number"
        assert github.paths[0] == "/repos/owner/repo/pulls/7"

    def test_create_issue_files_in_triage_and_labels_only_on_request(self, tmp_path: Path) -> None:
        github = FakeGithub()
        concierge, client, *_ = make(
            tmp_path,
            [
                {
                    "calls": [
                        (
                            "create_issue",
                            {"title": "Add retries to fetch", "body": "Wrap fetch()."},
                        ),
                        ("create_issue", {"title": "", "body": "x"}),
                    ],
                    "text": "Filed #41. Should I label it sbxloop:run?",
                },
                {"calls": [("label_issue_for_run", {"number": 41})], "text": "done"},
            ],
            github=github,
        )
        turn(concierge, "file an issue: add retries to fetch", author="Discord user `ana`")
        created, bad = client.responses
        assert created.ok and created.text.startswith("created issue #41 https://gh/i/41")
        assert "`sbxloop:backlog`" in created.text and "ask the person" in created.text
        assert bad.text == "both title and body are required"
        (repo, title, body, labels) = github.created[0]
        assert repo == "owner/repo" and title == "Add retries to fetch"
        assert labels == ["sbxloop:backlog"]
        assert body.startswith("Wrap fetch().\n\n---\nFiled by Discord user `ana` (via concierge)")
        assert not any("/labels" in p for p in github.paths)  # not labelled for a run yet
        turn(concierge, "yes", author="Discord user `ana`")
        (labelled,) = client.responses[2:]
        assert labelled.ok and labelled.text.startswith("added `sbxloop:run` to #41")
        assert github.paths[-1] == "/repos/owner/repo/issues/41/labels"

    def test_tool_exception_becomes_error_response_and_turn_survives(self, tmp_path: Path) -> None:
        concierge, client, _, loop, _ = make(
            tmp_path, [{"calls": [("sbx_control", {"command": "status"})], "text": "sorry"}]
        )

        def boom() -> dict[str, Any]:
            raise RuntimeError("store is locked")

        loop.status = boom  # type: ignore[method-assign]
        reply = turn(concierge)
        assert reply.ok and reply.text == "sorry"
        (resp,) = client.responses
        assert not resp.ok and "RuntimeError: store is locked" in resp.text

    def test_unknown_tool(self, tmp_path: Path) -> None:
        concierge, client, *_ = make(tmp_path, [{"calls": [("teleport", {})]}])
        turn(concierge)
        assert client.responses[0].error == "unknown tool 'teleport'"

    def test_tool_result_is_clipped(self, tmp_path: Path) -> None:
        concierge, client, _, loop, dstore = make(
            tmp_path,
            [{"calls": [("run_events", {"run_id": "r1abcdefg", "tail": 200})]}],
            config={"concierge": {"max_tool_result_chars": 1000}},
        )
        store = self._seed_run(tmp_path, dstore, loop)
        from sbxloop.events import Event

        for _i in range(200):
            store.append_event(Event.now("agent.message", "r1abcdefg", content="y" * 50))
        turn(concierge)
        text = client.responses[0].text
        assert len(text) <= 1000 and text.endswith("(truncated)")

    def test_on_tool_callback_sees_every_call(self, tmp_path: Path) -> None:
        concierge, *_ = make(
            tmp_path, [{"calls": [("sbx_control", {"command": "queue"}), ("teleport", {})]}]
        )
        seen: list[tuple[str, dict[str, Any], bool]] = []
        concierge.submit_turn(
            "x", author="a", on_tool=lambda name, args, resp: seen.append((name, args, resp.ok))
        ).result(timeout=10)
        assert seen == [("sbx_control", {"command": "queue"}, True), ("teleport", {}, False)]


class TestFailures:
    def test_worker_error_drops_sandbox_resets_session_and_retries_once(
        self, tmp_path: Path
    ) -> None:
        concierge, client, host, _, dstore = make(
            tmp_path,
            [{"session_id": "sA"}, {"raise": WorkerError("worker died")}, {"text": "back"}],
        )
        turn(concierge)
        reply = turn(concierge)
        assert reply.ok and reply.text == "back"
        assert [type(e).__name__ for e in host.failures] == ["WorkerError"]
        # The retry ran WITHOUT the old session id (the sandbox was rebuilt).
        assert client.jobs[1].resume_session_id == "sA"
        assert client.jobs[2].resume_session_id is None
        assert dstore.get_value(STATE_SESSION_ID) == "s1"

    def test_worker_error_without_drop_is_reported(self, tmp_path: Path) -> None:
        concierge, client, host, *_ = make(tmp_path, [{"raise": WorkerError("worker died")}])
        host.drop = False
        reply = turn(concierge)
        assert not reply.ok and reply.error == "worker died"
        assert len(client.jobs) == 1

    def test_lost_session_starts_over(self, tmp_path: Path) -> None:
        concierge, client, host, *_ = make(
            tmp_path,
            [
                {"session_id": "sA"},
                {"raise": WorkerError("session sA not found in store")},
                {"text": "fresh"},
            ],
        )
        turn(concierge)
        assert turn(concierge).text == "fresh"
        assert client.jobs[2].resume_session_id is None
        assert host.failures == []  # not a sandbox failure: no drop

    def test_timeout_is_an_actionable_error(self, tmp_path: Path) -> None:
        concierge, _client, host, *_ = make(
            tmp_path, [{"raise": WorkerTimeoutError("job timed out after 180s")}]
        )
        reply = turn(concierge)
        assert not reply.ok and reply.error is not None
        assert "longer than 180s" in reply.error
        assert host.failures == []  # a slow answer is not a dead sandbox

    def test_result_error_status_is_reported(self, tmp_path: Path) -> None:
        concierge, _client, host, *_ = make(tmp_path, [{"error": "SDK exploded | auth: none"}])
        host.drop = False
        reply = turn(concierge)
        assert not reply.ok and reply.error == "SDK exploded | auth: none"

    def test_missing_token_is_explained(self, tmp_path: Path) -> None:
        concierge, *_ = make(
            tmp_path,
            [
                {
                    "raise": DaemonError(
                        "cannot provision: COPILOT_GITHUB_TOKEN is not set on the host."
                    )
                }
            ],
        )
        reply = turn(concierge)
        assert reply.error == "the concierge needs COPILOT_GITHUB_TOKEN on the daemon host"


class TestQueueing:
    def test_turns_are_serialised_and_pending_counts(self, tmp_path: Path) -> None:
        import threading

        gate = threading.Event()
        concierge, client, *_ = make(tmp_path, [{"text": "one"}, {"text": "two"}])
        original = client.submit

        def slow_submit(job: JobRequest, **kwargs: Any) -> JobResult:
            gate.wait(5)
            return original(job, **kwargs)

        client.submit = slow_submit  # type: ignore[method-assign]
        f1 = concierge.submit_turn("a", author="x")
        f2 = concierge.submit_turn("b", author="y")
        time.sleep(0.05)
        assert concierge.pending == 1  # one running, one behind it
        gate.set()
        assert f1.result(5).text == "one" and f2.result(5).text == "two"
        assert concierge.pending == 0

    def test_close_cancels_queued_turns_and_closes_host(self, tmp_path: Path) -> None:
        concierge, _client, host, *_ = make(tmp_path, [{"text": "one"}])
        concierge.close()
        assert host.closed
        with pytest.raises(RuntimeError, match="closed"):
            concierge.submit_turn("x", author="a")

    def test_warm_up_provisions_in_background(self, tmp_path: Path) -> None:
        concierge, _client, host, *_ = make(tmp_path, [])
        concierge.warm_up()
        assert concierge._warm is not None
        concierge._warm.join(5)
        assert host.client_calls == 1
