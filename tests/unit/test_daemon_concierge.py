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

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

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
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import TaskSpec
from sbxloop.engine.store import StateStore
from sbxloop.errors import DaemonError, GithubOpsError, WorkerError, WorkerTimeoutError
from sbxloop.events import EventBus
from sbxloop_worker.protocol import (
    ErrorInfo,
    HostToolCall,
    HostToolResponse,
    JobRequest,
    JobResult,
)
from tests.fakes.github_errors import github_error
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
    """Stands in for both ``DaemonGithub`` and the ``GithubOps`` it hands to
    the lambda. ``paths`` is the raw-REST path log the read tests index into;
    ``calls`` is the full ordered write ledger (comments included) the
    close tests assert on. ``fail`` maps a ``"METHOD /path"`` substring to
    the exception that call should raise — recorded first, so a failed
    attempt is still visible."""

    def __init__(
        self,
        answers: dict[str, Any] | None = None,
        fail: dict[str, Exception] | None = None,
    ) -> None:
        self.answers = answers or {}
        self.fail = fail or {}
        self.paths: list[str] = []
        self.calls: list[tuple[str, str, Any]] = []
        self.comments: list[tuple[str, int, str]] = []

    def call(self, fn: Callable[[Any], Any]) -> Any:
        return fn(self)

    def _maybe_fail(self, method: str, path: str) -> None:
        for key, exc in self.fail.items():
            if key in f"{method} {path}":
                raise exc

    def raw(self, method: str, path: str, body: Any = None) -> Any:
        self.paths.append(path)
        self.calls.append((method, path, body))
        self._maybe_fail(method, path)
        for key, value in self.answers.items():
            if key in path:
                return value
        return {}

    def issue_comment(self, repo: str, number: int, body: str) -> str:
        path = f"/repos/{repo}/issues/{number}/comments"
        self.comments.append((repo, number, body))
        self.calls.append(("POST", path, {"body": body}))
        self._maybe_fail("POST", path)
        return f"https://gh/i/{number}#c1"

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


class FakeVersions:
    """Stands in for VersionProbe: the tool is a thin wrapper over summary()."""

    def __init__(self, text: str = "sbxloop 0.7.12 · 0.7.15 on PyPI · BEHIND") -> None:
        self.text = text
        self.calls = 0

    def summary(self) -> str:
        self.calls += 1
        return self.text


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
    config: dict[str, Any] | None = None,
    versions: Any = None,
    on_watch: Callable[[str, str], str | None] | None = None,
) -> tuple[Concierge, FakeClient, FakeHost, LoopWithRuns, DaemonStore]:
    raw: dict[str, Any] = {
        "state_dir": str(tmp_path / "state"),
        "discord": {"channel_id": 42},
    }
    if github is not None:
        raw["github"] = {"repo": "owner/repo"}
    for key, value in (config or {}).items():
        raw.setdefault(key, {}).update(value) if isinstance(value, dict) else raw.update(
            {key: value}
        )
    if raw.get("github", {}).get("repos"):
        # An explicit repo list replaces the single-repo default above.
        raw["github"].pop("repo", None)
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
        github=github,  # type: ignore[arg-type]
        host=host,
        bus=EventBus(),
        clock=lambda: 1_000_000.0,
        versions=versions if versions is not None else FakeVersions(),
        on_watch=on_watch,
    )
    return concierge, client, host, loop, dstore


def turn(
    concierge: Concierge,
    text: str = "hello",
    author: str = "Discord user `brett`",
    author_id: str | None = None,
) -> ConciergeReply:
    return concierge.submit_turn(text, author=author, author_id=author_id).result(timeout=10)


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
            "watch_run",
            "run_events",
            "item_detail",
            "version_status",
            "run_usage",
            "usage_today",
            "daemon_log",
        ]  # no github_get: no repo configured; the rest need nothing
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
        assert "list_issues" in names
        assert "comment_on_issue" in names and "close_issue" in names
        concierge3, client3, *_ = make(
            tmp_path / "c",
            [{}],
            github=FakeGithub(),
            config={"concierge": {"create_issues": False}},
        )
        turn(concierge3)
        names3 = [t.name for t in client3.jobs[0].host_tools]
        assert "github_get" in names3 and "create_issue" not in names3
        assert "comment_on_issue" not in names3 and "close_issue" not in names3
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
        # what the reply text does not spell out follows it as prose, not as a
        # JSON blob the concierge could paste into the channel
        assert "{" not in status.text and "consecutive failures: 0" in status.text
        assert "paused: True" in status.text

    def test_sbx_control_bad_verb_is_not_accepted(self, tmp_path: Path) -> None:
        concierge, client, *_ = make(
            tmp_path, [{"calls": [("sbx_control", {"command": "explode"})]}]
        )
        turn(concierge)
        (resp,) = client.responses
        assert resp.ok  # the tool ran; the daemon just did not accept the verb
        assert resp.text.startswith("(command not accepted)")

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
        item = WorkItem(item_id="inbox:w.md", source_key="w.md", title="Widget")
        dstore.upsert_new(item, 1.0)
        dstore.mark_running("inbox:w.md", "r1abcdefg", 2.0)
        dstore.record_discord_thread("r1abcdefg", 42, 4242, None)
        loop.reports["r1abcdefg"] = RunReport(
            "r1abcdefg", "completed", "2/2 tasks done", pr=(7, "https://gh/pr/7")
        )
        return store

    def test_run_usage_totals_by_persona(self, tmp_path: Path) -> None:
        """#334: Copilot spend is invisible from chat otherwise. Tokens are
        folded per agent persona — there is no phase field on a worker event,
        the persona the host stamps on is the phase."""
        concierge, client, _, loop, dstore = make(
            tmp_path, [{"calls": [("run_usage", {"run_id": "r1abcdefg"})]}]
        )
        store = self._seed_run(tmp_path, dstore, loop)
        from sbxloop.events import Event

        for tokens in (1200, 800):
            store.append_event(
                Event.now(
                    "agent.usage",
                    "r1abcdefg",
                    model="claude-opus-5",
                    input_tokens=tokens,
                    output_tokens=90,
                    agent="executor",
                )
            )
        store.append_event(
            Event.now(
                "agent.usage",
                "r1abcdefg",
                model="claude-opus-5",
                input_tokens=300,
                output_tokens=20,
                agent="planner",
            )
        )
        turn(concierge, "what did r1abcdefg spend?")
        (resp,) = client.responses
        assert resp.ok
        assert "claude-opus-5" in resp.text and "3 sample(s)" in resp.text
        # executor 2000+180 outranks planner 300+20, and sorts first.
        assert resp.text.index("executor") < resp.text.index("planner")
        assert "2,000 in" in resp.text and "180 out" in resp.text
        assert "total" in resp.text and "2,300 in" in resp.text and "200 out" in resp.text
        # The backend reports tokens, never spend: say so rather than show 0.
        assert "spend: not reported" in resp.text and "0.00" not in resp.text

    def test_run_usage_breaks_spend_down_by_turns_jobs_and_cache(self, tmp_path: Path) -> None:
        """Turns — not jobs — are what a run is billed and timed by: every turn
        re-sends the whole session context. A persona that is expensive because
        it takes many turns must be distinguishable from one that is expensive
        because it runs many times, and the cache column says how much of the
        input was actually re-billed."""
        concierge, client, _, loop, dstore = make(
            tmp_path, [{"calls": [("run_usage", {"run_id": "r1abcdefg"})]}]
        )
        store = self._seed_run(tmp_path, dstore, loop)
        from sbxloop.events import Event

        # One executor job of two turns, plus a second job of one turn.
        for job, tokens in (("jaaa", 1200), ("jaaa", 800), ("jbbb", 500)):
            store.append_event(
                Event.now(
                    "agent.usage",
                    "r1abcdefg",
                    job_id=job,
                    model="claude-opus-5",
                    input_tokens=tokens,
                    output_tokens=90,
                    cache_read_tokens=100,
                    agent="executor",
                )
            )
        turn(concierge, "what did r1abcdefg spend?")
        (resp,) = client.responses
        assert resp.ok
        assert "3 turns/2 jobs" in resp.text
        assert "300 cached" in resp.text
        # Spend stays unreported (#386, #439): the backend's per-turn figure
        # is a constant of unknown unit, so accumulating it across turns
        # fabricates a total (147 x 15.0 = 2205.0 in the field). `Usage` has
        # no field for it, and the block says so.
        assert "spend: not reported" in resp.text
        assert "0.7500" not in resp.text

    def test_a_run_without_usage_events_says_not_recorded(self, tmp_path: Path) -> None:
        """Acceptance: runs predating usage reporting answer "not recorded",
        which is not the same claim as zero spend."""
        concierge, client, _, loop, dstore = make(
            tmp_path,
            [
                {
                    "calls": [
                        ("run_usage", {"run_id": "r1abcdefg"}),
                        ("run_usage", {"run_id": "nope"}),
                    ]
                }
            ],
        )
        self._seed_run(tmp_path, dstore, loop)
        turn(concierge)
        recorded, missing = client.responses
        assert "no usage recorded for r1abcdefg" in recorded.text
        assert "not the same as zero" in recorded.text
        assert missing.text.startswith("no run 'nope'")

    def test_usage_today_totals_and_names_the_cap(self, tmp_path: Path) -> None:
        """The daily view sits next to runs_today/max_runs_per_day and shares
        its window — the calendar day in ``run_cap_timezone``, not a trailing
        24 hours — so the two numbers on the head line describe one period. It
        counts tokens by when they were spent, so a sample from before today
        is a different day's spend even on a run that is still active."""
        concierge, client, _, loop, dstore = make(tmp_path, [{"calls": [("usage_today", {})]}])
        store = self._seed_run(tmp_path, dstore, loop)
        from sbxloop.events import Event

        fresh = Event.now(
            "agent.usage", "r1abcdefg", model="claude-opus-5", input_tokens=500, output_tokens=40
        )
        fresh.ts = 1_000_000.0  # the frozen clock: inside today's window
        store.append_event(fresh)
        stale = Event.now(
            "agent.usage", "r1abcdefg", model="claude-opus-5", input_tokens=9999, output_tokens=999
        )
        stale.ts = 1_000_000.0 - 200_000.0  # two days back
        store.append_event(stale)
        last_night = Event.now(
            "agent.usage", "r1abcdefg", model="claude-opus-5", input_tokens=7777, output_tokens=777
        )
        # Inside a trailing 24 hours of the frozen clock, but before today's
        # midnight UTC — the boundary the run cap uses, so it is not today's.
        last_night.ts = 930_000.0
        store.append_event(last_night)
        turn(concierge, "how much have we spent today?")
        (resp,) = client.responses
        assert resp.ok
        assert "500 in" in resp.text and "40 out" in resp.text
        assert "9,999" not in resp.text  # yesterday's spend is not today's
        assert "7,777" not in resp.text  # nor is last night's, 24h window or no
        assert "today (UTC)" in resp.text and "runs today" in resp.text

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
        assert "inbox:w.md: state running" in item.text
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

    def test_list_issues_lists_everything_open_and_flags_daemon_states(
        self, tmp_path: Path
    ) -> None:
        github = FakeGithub(
            {
                "/issues?": [
                    {
                        "number": 7,
                        "title": "Retry the fetch client",
                        "labels": [{"name": "bug"}],
                        "created_at": "2026-08-01T00:00:00Z",
                        "user": {"login": "ana"},
                        "comments": 2,
                        "html_url": "https://gh/i/7",
                    },
                    {
                        "number": 9,
                        "title": "Already going",
                        "labels": [{"name": "sbxloop:run"}],
                        "created_at": "bogus",
                        "user": {"login": "bo"},
                        "comments": 0,
                        "html_url": "https://gh/i/9",
                    },
                    {
                        "number": 11,
                        "title": "Stuck",
                        "labels": [{"name": "sbxloop:blocked"}],
                        "created_at": "bogus",
                        "user": {"login": "bo"},
                        "comments": 0,
                        "html_url": "https://gh/i/11",
                    },
                    {"number": 10, "title": "a PR", "pull_request": {}, "labels": []},
                ]
            }
        )
        concierge, client, *_ = make(
            tmp_path,
            [
                {
                    "calls": [
                        ("list_issues", {}),
                        ("list_issues", {"limit": 5}),
                        ("list_issues", {"label": "bug"}),
                    ]
                }
            ],
            github=github,
        )
        turn(concierge, "what's open?")
        everything, capped, bugs = client.responses
        assert everything.text.startswith("3 open issue(s) in owner/repo (newest activity first")
        assert "- #7 Retry the fetch client · [bug]" in everything.text
        assert "by ana · 2 comments · https://gh/i/7" in everything.text
        assert "#9 Already going" in everything.text and "QUEUED for a run" in everything.text
        assert "#11 Stuck" in everything.text and "BLOCKED — needs a human" in everything.text
        assert "#10" not in everything.text  # pull requests are not issues
        assert "label_issue_for_run" in everything.text and "create_issue" in everything.text
        assert "labels=" not in github.paths[0]
        assert "per_page=5" in github.paths[1]
        assert "labels=bug" in github.paths[2]
        assert capped.ok and bugs.ok

    def test_create_issue_files_and_queues_in_one_hop(self, tmp_path: Path) -> None:
        github = FakeGithub()
        concierge, client, _, _, dstore = make(
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
                    "text": "Filed and queued #41; a run thread will appear here.",
                },
                {"calls": [("label_issue_for_run", {"number": 12})], "text": "done"},
            ],
            github=github,
        )
        turn(concierge, "add retries to fetch", author="Discord user `ana`", author_id="777")
        created, bad = client.responses
        assert created.ok and created.text.startswith(
            "created and queued issue #41 https://gh/i/41"
        )
        assert "`sbxloop:run`" in created.text and "run thread will appear" in created.text
        assert "ask the person" not in created.text
        assert bad.text == "both title and body are required"
        (repo, title, body, labels) = github.created[0]
        assert repo == "owner/repo" and title == "Add retries to fetch"
        assert labels == ["sbxloop:run"]
        assert body.startswith("Wrap fetch().\n\n---\nFiled by Discord user `ana` (via concierge)")
        assert "777" not in body  # the requester never reaches the public issue
        assert not any("/labels" in p for p in github.paths)  # already queued: no label call
        # ...but the daemon's store knows who asked, so the work item will.
        dstore.upsert_new(
            WorkItem(item_id="gh:issue:41", source_key="41", title="Add retries"), 1.0
        )
        assert dstore.get("gh:issue:41").requested_by == "777"  # type: ignore[union-attr]
        # An issue that already exists is queued with label_issue_for_run.
        turn(concierge, "run #12 too", author="Discord user `ana`")
        (labelled,) = client.responses[2:]
        assert labelled.ok and labelled.text.startswith("added `sbxloop:run` to #12")
        assert github.paths[-1] == "/repos/owner/repo/issues/12/labels"

    def test_create_issue_without_an_author_id_records_no_requester(self, tmp_path: Path) -> None:
        github = FakeGithub()
        concierge, client, _, _, dstore = make(
            tmp_path,
            [{"calls": [("create_issue", {"title": "T", "body": "B"})]}],
            github=github,
        )
        turn(concierge, "do T")
        assert client.responses[0].ok
        dstore.upsert_new(WorkItem(item_id="gh:issue:41", source_key="41", title="T"), 1.0)
        assert dstore.get("gh:issue:41").requested_by is None  # type: ignore[union-attr]

    def test_create_issue_mentions_a_paused_daemon(self, tmp_path: Path) -> None:
        concierge, client, _, loop, _ = make(
            tmp_path,
            [{"calls": [("create_issue", {"title": "t", "body": "b"})]}],
            github=FakeGithub(),
        )
        loop.paused = True
        turn(concierge)
        assert "PAUSED" in client.responses[0].text

    def test_version_status_reports_drift(self, tmp_path: Path) -> None:
        versions = FakeVersions("sbxloop 0.7.12 installed · 0.7.15 on PyPI · BEHIND")
        concierge, client, *_ = make(
            tmp_path, [{"calls": [("version_status", {})]}], versions=versions
        )
        turn(concierge, "are we up to date?")
        (resp,) = client.responses
        assert resp.ok and "0.7.15 on PyPI" in resp.text
        assert versions.calls == 1

    def test_version_status_survives_an_unreachable_pypi(self, tmp_path: Path) -> None:
        """The real probe already degrades to text; this pins that an
        exception from it still becomes a readable answer, not a dead turn."""

        class Exploding:
            def summary(self) -> str:
                raise DaemonError("state dir vanished")

        concierge, client, *_ = make(
            tmp_path, [{"calls": [("version_status", {})]}], versions=Exploding()
        )
        turn(concierge)
        (resp,) = client.responses
        assert resp.ok and resp.text.startswith("reading versions failed:")

    def test_comment_on_issue_posts_signed_with_the_speaker(self, tmp_path: Path) -> None:
        github = FakeGithub()
        concierge, client, *_ = make(
            tmp_path,
            [
                {
                    "calls": [
                        ("comment_on_issue", {"number": 12, "body": "On it — see #7."}),
                        ("comment_on_issue", {"number": 12, "body": "   "}),
                        ("comment_on_issue", {"number": 0, "body": "x"}),
                    ]
                }
            ],
            github=github,
        )
        turn(concierge, "reply on #12", author="Discord user `ana`")
        posted, blank, no_number = client.responses
        assert posted.ok and posted.text == "commented on #12 — https://gh/i/12#c1"
        assert blank.text == "body is required" and no_number.text == "number is required"
        (repo, number, body) = github.comments[0]
        assert (repo, number) == ("owner/repo", 12) and len(github.comments) == 1
        assert body.startswith(
            "On it — see #7.\n\n---\nPosted by Discord user `ana` (via concierge)"
        )
        # a comment touches nothing else
        assert [m for m, _, _ in github.calls] == ["POST"]

    def test_comment_on_issue_reports_a_github_failure_as_text(self, tmp_path: Path) -> None:
        github = FakeGithub(
            fail={
                "POST /repos/owner/repo/issues/12/comments": GithubOpsError(
                    "gh api failed: forbidden"
                )
            }
        )
        concierge, client, *_ = make(
            tmp_path,
            [{"calls": [("comment_on_issue", {"number": 12, "body": "hi"})]}],
            github=github,
        )
        turn(concierge)
        (resp,) = client.responses
        assert resp.ok  # an expected failure is text the model can read, not a tool error
        assert resp.text.startswith("commenting on #12 failed:")

    def test_close_issue_comments_unlabels_then_closes(self, tmp_path: Path) -> None:
        github = FakeGithub(
            {
                "/issues/12": {
                    "number": 12,
                    "title": "Retry the fetch client",
                    "state": "open",
                    "labels": [{"name": "sbxloop:backlog"}, {"name": "sbxloop:run"}],
                    "html_url": "https://gh/i/12",
                }
            }
        )
        concierge, client, *_ = make(
            tmp_path,
            [
                {
                    "calls": [
                        (
                            "close_issue",
                            {
                                "number": 12,
                                "reason": "not_planned",
                                "comment": "Duplicate of #7.",
                                "confirmation": "yes, close 12 as a dup of 7",
                            },
                        )
                    ]
                }
            ],
            github=github,
        )
        turn(concierge, "close #12 as a duplicate of #7", author="Discord user `ana`")
        (closed,) = client.responses
        # read first, then comment, then unlabel, then close — in that order
        assert [(m, p) for m, p, _ in github.calls] == [
            ("GET", "/repos/owner/repo/issues/12"),
            ("POST", "/repos/owner/repo/issues/12/comments"),
            ("DELETE", "/repos/owner/repo/issues/12/labels/sbxloop%3Arun"),
            ("PATCH", "/repos/owner/repo/issues/12"),
        ]
        assert github.calls[-1][2] == {"state": "closed", "state_reason": "not_planned"}
        assert github.comments[0][2].startswith(
            "Duplicate of #7.\n\n---\nClosed as not_planned by Discord user `ana` (via concierge)"
        )
        assert closed.text.startswith(
            'closed #12 "Retry the fetch client" as not_planned — https://gh/i/12'
        )
        assert "posted the reason as a comment" in closed.text
        assert "removed `sbxloop:run`" in closed.text

    def test_close_issue_refuses_without_confirmation_or_reason(self, tmp_path: Path) -> None:
        github = FakeGithub()
        concierge, client, *_ = make(
            tmp_path,
            [
                {
                    "calls": [
                        ("close_issue", {"number": 12, "reason": "not_planned"}),
                        (
                            "close_issue",
                            {"number": 12, "reason": "not_planned", "confirmation": "   "},
                        ),
                        (
                            "close_issue",
                            {"number": 12, "reason": "tidying", "confirmation": "go ahead"},
                        ),
                    ]
                }
            ],
            github=github,
        )
        turn(concierge, "close #12")
        missing, blank, bad_reason = client.responses
        for resp in (missing, blank):
            assert "close_issue needs the person's own words" in resp.text
        assert bad_reason.text == "reason must be one of completed, not_planned, not 'tidying'"
        assert github.calls == []  # the gate runs before anything is read or written

    def test_close_issue_refuses_a_pr_a_closed_issue_and_a_running_one(
        self, tmp_path: Path
    ) -> None:
        github = FakeGithub(
            {
                "/issues/12": {"number": 12, "title": "a PR", "state": "open", "pull_request": {}},
                "/issues/13": {
                    "number": 13,
                    "title": "Done already",
                    "state": "closed",
                    "state_reason": "completed",
                    "html_url": "https://gh/i/13",
                },
                "/issues/14": {
                    "number": 14,
                    "title": "Being worked",
                    "state": "open",
                    "labels": [{"name": "sbxloop:in-progress"}],
                    "html_url": "https://gh/i/14",
                },
            }
        )
        yes = {"reason": "not_planned", "confirmation": "yes close it"}
        concierge, client, *_ = make(
            tmp_path,
            [
                {
                    "calls": [
                        ("close_issue", {"number": 12, **yes}),
                        ("close_issue", {"number": 13, **yes}),
                        ("close_issue", {"number": 14, **yes}),
                    ]
                }
            ],
            github=github,
        )
        turn(concierge, "close them")
        pr, already, running = client.responses
        assert pr.text == "#12 is a pull request, not an issue — close_issue only closes issues."
        assert already.text.startswith('#13 "Done already" is already closed (completed)')
        assert "is being worked right now" in running.text and "cancel" in running.text
        assert [m for m, _, _ in github.calls] == ["GET", "GET", "GET"]  # no writes at all

    def test_close_issue_tolerates_a_missing_label_and_skips_an_unlabelled_one(
        self, tmp_path: Path
    ) -> None:
        github = FakeGithub(
            {"/issues/12": {"number": 12, "title": "T", "state": "open", "labels": []}}
        )
        concierge, client, *_ = make(
            tmp_path,
            [
                {
                    "calls": [
                        (
                            "close_issue",
                            {"number": 12, "reason": "completed", "confirmation": "yes"},
                        )
                    ]
                }
            ],
            github=github,
        )
        turn(concierge)
        (closed,) = client.responses
        # no trigger label: no DELETE, and no comment because none was written
        assert [m for m, _, _ in github.calls] == ["GET", "PATCH"]
        assert github.calls[-1][2] == {"state": "closed", "state_reason": "completed"}
        assert closed.text == 'closed #12 "T" as completed'

    def test_close_issue_reports_a_partial_failure_without_claiming_success(
        self, tmp_path: Path
    ) -> None:
        issue = {
            "number": 12,
            "title": "T",
            "state": "open",
            "labels": [{"name": "sbxloop:run"}],
            "html_url": "https://gh/i/12",
        }
        args = {
            "number": 12,
            "reason": "not_planned",
            "comment": "dup of #7",
            "confirmation": "yes",
        }
        # the close itself fails after the comment landed
        gh1 = FakeGithub(
            {"/issues/12": issue}, fail={"PATCH ": GithubOpsError("gh api failed: server error")}
        )
        concierge, client, *_ = make(tmp_path, [{"calls": [("close_issue", args)]}], github=gh1)
        turn(concierge)
        (failed,) = client.responses
        assert failed.text.startswith("closing #12 failed:")
        assert "already done: posted the reason as a comment" in failed.text
        assert not failed.text.startswith("closed #12")

        # the comment fails: nothing else is attempted
        gh2 = FakeGithub(
            {"/issues/12": issue}, fail={"POST ": GithubOpsError("gh api failed: forbidden")}
        )
        concierge2, client2, *_ = make(
            tmp_path / "b", [{"calls": [("close_issue", args)]}], github=gh2
        )
        turn(concierge2)
        (no_comment,) = client2.responses
        assert "so it was NOT closed" in no_comment.text
        assert [m for m, _, _ in gh2.calls] == ["GET", "POST"]

        # a 404 on the label DELETE is "already absent", not a failure
        gh3 = FakeGithub({"/issues/12": issue}, fail={"DELETE ": github_error("label_missing_404")})
        concierge3, client3, *_ = make(
            tmp_path / "c", [{"calls": [("close_issue", args)]}], github=gh3
        )
        turn(concierge3)
        (tolerated,) = client3.responses
        assert tolerated.text.startswith('closed #12 "T" as not_planned')
        assert "could NOT remove" not in tolerated.text

        # any other label failure is reported, but the close still happens:
        # a closed issue carrying the trigger label is not discovered anyway
        gh4 = FakeGithub({"/issues/12": issue}, fail={"DELETE ": GithubOpsError("gh api: boom")})
        concierge4, client4, *_ = make(
            tmp_path / "d", [{"calls": [("close_issue", args)]}], github=gh4
        )
        turn(concierge4)
        (noisy,) = client4.responses
        assert noisy.text.startswith('closed #12 "T" as not_planned')
        assert "could NOT remove `sbxloop:run`" in noisy.text
        assert [m for m, _, _ in gh4.calls] == ["GET", "POST", "DELETE", "PATCH"]

    def test_close_issue_reports_a_read_failure_and_a_bad_number(self, tmp_path: Path) -> None:
        github = FakeGithub(
            {"/issues/12": "not an issue at all"},
            fail={"GET /repos/owner/repo/issues/13": GithubOpsError("gh api: gone")},
        )
        yes = {"reason": "completed", "confirmation": "yes"}
        concierge, client, *_ = make(
            tmp_path,
            [
                {
                    "calls": [
                        ("close_issue", {"number": 0, **yes}),
                        ("close_issue", {"number": "twelve", **yes}),
                        ("close_issue", {"number": 13, **yes}),
                        ("close_issue", {"number": 12, **yes}),
                    ]
                }
            ],
            github=github,
        )
        turn(concierge)
        zero, unparseable, unreadable, garbage = client.responses
        assert zero.text == "number is required" and unparseable.text == "number is required"
        assert unreadable.text.startswith("reading #13 failed, so it was not closed:")
        assert garbage.text.startswith("#12 did not come back as an issue:")
        assert [m for m, _, _ in github.calls] == ["GET", "GET"]  # nothing was written

    def test_close_issue_warns_only_when_the_daemon_already_claimed_the_item(
        self, tmp_path: Path
    ) -> None:
        def close(path: Path, claimed: bool) -> str:
            github = FakeGithub(
                {"/issues/12": {"number": 12, "title": "T", "state": "open", "labels": []}}
            )
            concierge, client, _, _, dstore = make(
                path,
                [
                    {
                        "calls": [
                            (
                                "close_issue",
                                {"number": 12, "reason": "not_planned", "confirmation": "yes"},
                            )
                        ]
                    }
                ],
                github=github,
            )
            item = WorkItem(item_id="gh:issue:12", source_key="12", title="T")
            dstore.upsert_new(item, 1.0)
            if claimed:
                dstore.mark_claimed("gh:issue:12", 2.0)
            turn(concierge)
            return client.responses[0].text

        # unclaimed: the loop re-reads the issue before dispatching, so the
        # close alone is enough — say so without alarming anyone
        unclaimed_text = close(tmp_path, claimed=False)
        assert "nothing will run" in unclaimed_text and "WARNING" not in unclaimed_text
        # claimed: the claim check is skipped, so a run can still start
        claimed_text = close(tmp_path / "b", claimed=True)
        assert "WARNING" in claimed_text and "abandon gh:issue:12" in claimed_text

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

    def test_run_events_renders_old_and_new_shape_tool_events(self, tmp_path: Path) -> None:
        """`output_lines`/`duration_ms`/`tool_call_id` are additive: a stored
        chronology mixing an old worker's tool events with a new worker's
        renders without error, both showing the command (#403 t7)."""
        concierge, client, _, loop, dstore = make(
            tmp_path, [{"calls": [("run_events", {"run_id": "r1abcdefg", "tail": 50})]}]
        )
        store = self._seed_run(tmp_path, dstore, loop)
        from sbxloop.events import Event

        prefix = "cd /home/x/.local/state/sbxloop/sbxloop-work/runs/r1abcdefg/workspace && "
        # old shape: no tool_call_id, no output_lines, no duration_ms
        store.append_event(
            Event.now(
                "agent.tool_start", "r1abcdefg", tool="bash", args=prefix + "uv run ruff check ."
            )
        )
        store.append_event(
            Event.now(
                "agent.tool_end",
                "r1abcdefg",
                tool="bash",
                args=prefix + "uv run ruff check .",
                success=True,
                exit_code=0,
            )
        )
        # new shape
        store.append_event(
            Event.now(
                "agent.tool_end",
                "r1abcdefg",
                tool="bash",
                tool_call_id="c9",
                args=prefix + "uv run mypy",
                success=False,
                exit_code=1,
                error="error: bad type",
                output_lines=120,
                duration_ms=1500,
            )
        )
        turn(concierge)
        text = client.responses[0].text
        assert "cd $RUN && uv run ruff check ." in text
        assert "cd $RUN && uv run mypy" in text
        assert "error: bad type" in text


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


class TestDaemonLogTool:
    """The journal without ssh: the concierge quotes the daemon's own recent
    log lines out of the bounded in-process ring buffer."""

    @pytest.fixture(autouse=True)
    def _buffer(self) -> Any:
        from sbxloop.log import log_buffer

        log_buffer().clear()
        yield
        log_buffer().clear()

    def _seed(self) -> None:
        from sbxloop.log import LogRecordLine, log_buffer

        for level, logger, line in [
            ("INFO", "sbxloop.daemon.loop", "daemon.idle queued=0 sbxq"),
            ("WARNING", "sbxloop.daemon.loop", "breaker open failures=3 sbxq"),
            ("ERROR", "sbxloop.gh.poll", "github.poll_failed status=502 sbxq"),
        ]:
            log_buffer().append(LogRecordLine("2026-01-01T00:00:00+00:00", level, logger, line))

    def _call(self, tmp_path: Path, args: dict[str, Any]) -> str:
        concierge, client, _, _, _ = make(tmp_path, [{"calls": [("daemon_log", args)]}])
        turn(concierge, "why is nothing running?")
        (response,) = client.responses
        assert response.ok
        return response.text or ""

    def test_registered_and_quotes_recent_lines(self, tmp_path: Path) -> None:
        self._seed()
        text = self._call(tmp_path, {"grep": "sbxq"})
        assert text.startswith("showing 3 of ")
        assert "daemon.idle queued=0 sbxq" in text
        assert "breaker open failures=3 sbxq" in text
        assert "github.poll_failed status=502 sbxq" in text
        # oldest-first, one record per line
        lines = text.splitlines()[1:]
        assert len(lines) == 3
        assert "daemon.idle" in lines[0] and "github.poll_failed" in lines[2]
        assert lines[0].startswith("2026-01-01T00:00:00+00:00 INFO sbxloop.daemon.loop ")

    def test_level_filter_is_at_or_above(self, tmp_path: Path) -> None:
        self._seed()
        text = self._call(tmp_path, {"level": "error", "grep": "sbxq"})
        assert "github.poll_failed" in text
        assert "daemon.idle" not in text and "breaker open" not in text
        assert "level=ERROR" in text

    def test_grep_is_a_plain_substring(self, tmp_path: Path) -> None:
        self._seed()
        text = self._call(tmp_path, {"grep": "breaker open"})
        assert "breaker open failures=3 sbxq" in text and "daemon.idle" not in text
        # A regex-ish needle matches nothing rather than being compiled.
        assert "no matching log records" in self._call(tmp_path, {"grep": "(a+)+b"})
        assert "no matching log records" in self._call(tmp_path, {"grep": "daemon.*idle"})

    def test_invalid_level_is_a_friendly_error(self, tmp_path: Path) -> None:
        self._seed()
        text = self._call(tmp_path, {"level": "TRACE", "grep": "sbxq"})
        assert "unknown log level 'TRACE'" in text and "DEBUG, INFO, WARNING, ERROR" in text

    def test_no_match_says_so(self, tmp_path: Path) -> None:
        self._seed()
        text = self._call(tmp_path, {"grep": "nothing-matches-this"})
        assert text.startswith("no matching log records (grep='nothing-matches-this')")
        assert "the buffer holds " in text

    def test_tail_is_clamped(self, tmp_path: Path) -> None:
        from sbxloop.log import LogRecordLine, log_buffer

        for i in range(600):
            log_buffer().append(LogRecordLine("t", "INFO", "l", f"sbxline {i}"))
        concierge, client, _, _, _ = make(
            tmp_path,
            [{"calls": [("daemon_log", {"tail": 9999, "grep": "sbxline"})]}],
            config={"concierge": {"max_tool_result_chars": 20000}},
        )
        turn(concierge)
        (response,) = client.responses
        text = response.text or ""
        assert len(text.splitlines()) == 501  # header + at most 500 records
        assert "sbxline 599" in text and "sbxline 99 " not in text

    def test_tool_result_is_clipped(self, tmp_path: Path) -> None:
        from sbxloop.log import LogRecordLine, log_buffer

        for i in range(400):
            log_buffer().append(LogRecordLine("t", "INFO", "logger", f"line {i} " + "x" * 100))
        concierge, client, _, _, _ = make(
            tmp_path,
            [{"calls": [("daemon_log", {"tail": 500})]}],
            config={"concierge": {"max_tool_result_chars": 1000}},
        )
        turn(concierge)
        (response,) = client.responses
        assert len(response.text or "") <= 1000  # _clip's hard bound
        assert (response.text or "").endswith("… (truncated)")

    @pytest.fixture
    def _logging(self) -> Any:
        """Real logging into the ring buffer, torn down afterwards."""
        import io

        from sbxloop.log import configure_logging

        configure_logging("DEBUG", fmt="console", stream=io.StringIO())
        yield
        configure_logging("INFO")

    def _emit(self) -> None:
        from sbxloop.log import get_logger

        log = get_logger("sbxloop.daemon.loop")
        log.debug("daemon.tick", marker="sbxq")
        log.info("daemon.idle", queued=0, marker="sbxq")
        log.warning("breaker.open", failures=3, marker="sbxq")
        get_logger("sbxloop.gh.poll").error("github.poll_failed", status=502, marker="sbxq")

    def test_real_log_records_reach_the_tool(self, tmp_path: Path, _logging: Any) -> None:
        self._emit()
        text = self._call(tmp_path, {"grep": "sbxq", "tail": 50})
        assert "daemon.idle" in text
        assert "breaker.open" in text
        assert "github.poll_failed" in text

    def test_real_records_honour_the_level_filter(self, tmp_path: Path, _logging: Any) -> None:
        self._emit()
        text = self._call(tmp_path, {"level": "warning", "grep": "sbxq"})
        assert "level=WARNING" in text
        assert "breaker.open" in text and "github.poll_failed" in text
        assert "daemon.idle" not in text and "daemon.tick" not in text

    def test_real_records_grep_is_literal(self, tmp_path: Path, _logging: Any) -> None:
        self._emit()
        text = self._call(tmp_path, {"grep": "breaker.open"})
        assert "breaker.open" in text
        assert "daemon.idle" not in text and "github.poll_failed" not in text
        regexish = self._call(tmp_path, {"grep": "breaker\\.op.*"})
        assert regexish.startswith("no matching log records")

    def test_daemon_log_is_advertised(self, tmp_path: Path) -> None:
        concierge, client, *_ = make(tmp_path, [{}])
        turn(concierge)
        specs = {spec.name: spec for spec in client.jobs[0].host_tools}
        assert "daemon_log" in specs
        properties = specs["daemon_log"].parameters["properties"]
        assert {"tail", "level", "grep"} <= set(properties)


class TestWatchRun:
    """``watch_run``: register interest, or answer at once if already done."""

    def _watcher(self) -> tuple[list[tuple[str, str]], Callable[[str, str], str | None]]:
        seen: list[tuple[str, str]] = []

        def on_watch(run_id: str, requester: str) -> str | None:
            seen.append((run_id, requester))
            return None

        return seen, on_watch

    def test_tool_is_registered(self, tmp_path: Path) -> None:
        concierge, _, _, _, _ = make(tmp_path, [])
        assert "watch_run" in concierge.tool_names

    def test_registers_unfinished_run(self, tmp_path: Path) -> None:
        seen, on_watch = self._watcher()
        concierge, client, _, _, _ = make(
            tmp_path,
            [{"calls": [("watch_run", {"run_id_or_item_id": "r7abcdefg"})]}],
            on_watch=on_watch,
        )
        store = StateStore(tmp_path / "state" / "state.db")
        store.create_run("r7abcdefg", "Ship it")
        store.set_run_state("r7abcdefg", "building")
        turn(concierge, author="alice")
        text = client.responses[0].text
        assert seen == [("r7abcdefg", "alice (via concierge)")]
        assert "r7abcdefg" in text
        assert "survives a daemon restart" in text

    def test_finished_run_answers_immediately(self, tmp_path: Path) -> None:
        seen, on_watch = self._watcher()
        concierge, client, _, loop, _ = make(
            tmp_path,
            [{"calls": [("watch_run", {"run_id_or_item_id": "r8abcdefg"})]}],
            on_watch=on_watch,
        )
        store = StateStore(tmp_path / "state" / "state.db")
        store.create_run("r8abcdefg", "widget shipped")
        store.set_run_state("r8abcdefg", "completed")
        loop.reports["r8abcdefg"] = RunReport(
            "r8abcdefg",
            "completed",
            "2/2 tasks done",
            pr=(9, "https://github.com/owner/repo/pull/9"),
        )
        turn(concierge, author="alice")
        text = client.responses[0].text
        assert seen == []
        assert "completed" in text
        assert "widget shipped" in text
        assert "https://github.com/owner/repo/pull/9" in text

    def test_resolves_work_item_to_newest_run(self, tmp_path: Path) -> None:
        seen, on_watch = self._watcher()
        concierge, client, _, _, dstore = make(
            tmp_path,
            [{"calls": [("watch_run", {"run_id_or_item_id": "inbox:w.md"})]}],
            on_watch=on_watch,
        )
        store = StateStore(tmp_path / "state" / "state.db")
        for run_id in ("r1abcdefg", "r2abcdefg"):
            store.create_run(run_id, "Ship it")
        store.set_run_state("r1abcdefg", "failed")
        store.set_run_state("r2abcdefg", "building")
        item = WorkItem(item_id="inbox:w.md", source_key="w.md", title="Widget")
        dstore.upsert_new(item, 1.0)
        dstore.mark_running("inbox:w.md", "r1abcdefg", 2.0)
        dstore.mark_running("inbox:w.md", "r2abcdefg", 3.0)
        turn(concierge, author="alice")
        assert seen == [("r2abcdefg", "alice (via concierge)")]
        assert "r2abcdefg" in client.responses[0].text

    def test_unknown_id(self, tmp_path: Path) -> None:
        seen, on_watch = self._watcher()
        concierge, client, _, _, _ = make(
            tmp_path,
            [{"calls": [("watch_run", {"run_id_or_item_id": "nope"})]}],
            on_watch=on_watch,
        )
        turn(concierge)
        assert "no run or work item" in client.responses[0].text
        assert seen == []

    def test_without_callback_says_unavailable(self, tmp_path: Path) -> None:
        concierge, client, _, _, _ = make(
            tmp_path, [{"calls": [("watch_run", {"run_id_or_item_id": "r9abcdefg"})]}]
        )
        store = StateStore(tmp_path / "state" / "state.db")
        store.create_run("r9abcdefg", "Ship it")
        store.set_run_state("r9abcdefg", "building")
        turn(concierge)
        assert "not available on this transport" in client.responses[0].text

    def test_concierge_module_has_no_discord_import(self) -> None:
        import re

        from sbxloop.daemon import concierge as module

        source = Path(str(module.__file__)).read_text(encoding="utf-8")
        assert re.search(r"^\s*(import|from)\s+discord", source, re.M) is None


class TestTypedGithubIds:
    """Item ids are rendered typed and accepted in either spelling."""

    def _seed(self, tmp_path: Path, dstore: DaemonStore, loop: LoopWithRuns) -> None:
        store = StateStore(tmp_path / "state" / "state.db")
        store.create_run("r1abcdefg", "Ship the widget")
        store.set_run_state("r1abcdefg", "building")
        item = WorkItem(item_id="gh:issue:12", source_key="12", title="Widget")
        dstore.upsert_new(item, 1.0)
        dstore.mark_running("gh:issue:12", "r1abcdefg", 2.0)
        loop.reports["r1abcdefg"] = RunReport("r1abcdefg", "building", "1/2 tasks done")

    def test_tool_descriptions_use_typed_example(self, tmp_path: Path) -> None:
        concierge, _, _, _, _ = make(tmp_path, [])
        blob = " ".join(t.spec.description for t in concierge._tools.values())
        assert "gh:issue:12" in blob
        assert "gh:12" not in blob

    def test_item_and_run_detail_accept_legacy_and_render_typed(self, tmp_path: Path) -> None:
        concierge, client, _, loop, dstore = make(
            tmp_path,
            [
                {
                    "calls": [
                        ("item_detail", {"item_id": "gh:12"}),
                        ("run_detail", {"run_id": "r1abcdefg"}),
                        ("list_runs", {"limit": 5}),
                        ("item_detail", {"item_id": "gh:999"}),
                    ]
                }
            ],
        )
        self._seed(tmp_path, dstore, loop)
        turn(concierge)
        item, detail, listing, missing = client.responses
        assert item.text.startswith("gh:issue:12: state running")
        assert "work item: gh:issue:12" in detail.text
        assert "gh:issue:12 · Widget" in listing.text
        assert "ids look like gh:issue:12" in missing.text

    def test_watch_accepts_legacy_item_id(self, tmp_path: Path) -> None:
        seen: list[tuple[str, str]] = []
        concierge, client, _, loop, dstore = make(
            tmp_path,
            [{"calls": [("watch_run", {"run_id_or_item_id": "gh:12"})]}],
            on_watch=lambda run_id, by: seen.append((run_id, by)) or None,
        )
        self._seed(tmp_path, dstore, loop)
        turn(concierge, author="alice")
        assert seen == [("r1abcdefg", "alice (via concierge)")]
        assert "r1abcdefg" in client.responses[0].text


class TestMultiRepo:
    """One daemon, several repositories: the concierge must be able to say
    which it works on, and every GitHub tool must know which one it is
    acting against."""

    MULTI: ClassVar[dict[str, Any]] = {
        "github": {
            "repos": [
                {"repo": "owner/one", "deliver_base": "main"},
                {"repo": "owner/two", "deliver_base": "trunk"},
                {"repo": "owner/three", "enabled": False},
            ]
        }
    }

    def test_list_repos_reports_state_and_base(self, tmp_path: Path) -> None:
        concierge, client, *_ = make(
            tmp_path,
            [{"calls": [("list_repos", {})]}],
            github=FakeGithub(),
            config=self.MULTI,
        )
        turn(concierge, "what projects are you configured to work on?")
        (repos,) = client.responses
        assert repos.ok
        assert "3 configured repository(ies):" in repos.text
        assert "- owner/one — enabled, base main, trigger label `sbxloop:run`" in repos.text
        assert "- owner/two — enabled, base trunk, trigger label `sbxloop:run`" in repos.text
        assert (
            "- owner/three — disabled, base (repo default), trigger label `sbxloop:run`"
            in repos.text
        )
        assert "daemon-wide" in repos.text
        names = [t.name for t in client.jobs[0].host_tools]
        assert "list_repos" in names
        for name in ("owner/one", "owner/two", "owner/three"):
            assert name in client.jobs[0].system_message

    def test_single_repo_tools_default_to_the_sole_repo(self, tmp_path: Path) -> None:
        github = FakeGithub({"/issues/12/comments": {"html_url": "https://gh/c/1"}})
        concierge, client, *_ = make(
            tmp_path,
            [{"calls": [("comment_on_issue", {"number": 12, "body": "hi"})]}],
            github=github,
        )
        turn(concierge)
        (commented,) = client.responses
        assert commented.ok
        assert github.comments[0][0] == "owner/repo"

    def test_multi_repo_tools_need_a_selector_and_route_to_it(self, tmp_path: Path) -> None:
        github = FakeGithub({"/issues/12/comments": {"html_url": "https://gh/c/1"}})
        concierge, client, *_ = make(
            tmp_path,
            [
                {
                    "calls": [
                        ("comment_on_issue", {"number": 12, "body": "hi"}),
                        ("comment_on_issue", {"number": 12, "body": "hi", "repo": "owner/two"}),
                        ("comment_on_issue", {"number": 12, "body": "hi", "repo": "owner/nope"}),
                        ("list_issues", {"repo": "one"}),
                    ]
                }
            ],
            github=github,
            config=self.MULTI,
        )
        turn(concierge)
        ambiguous, routed, unknown, listed = client.responses
        assert "explicit `repo` argument" in ambiguous.text
        assert "owner/one, owner/two" in ambiguous.text
        assert routed.ok and github.comments[0][0] == "owner/two"
        assert "unknown repository 'owner/nope'" in unknown.text
        assert listed.ok
        assert any(p.startswith("/repos/owner/one/issues?") for p in github.paths)

    def test_tool_descriptions_name_every_repository(self, tmp_path: Path) -> None:
        concierge, client, *_ = make(tmp_path, [{}], github=FakeGithub(), config=self.MULTI)
        turn(concierge)
        specs = {t.name: t.description for t in client.jobs[0].host_tools}
        assert "owner/one" in specs["list_issues"] and "owner/two" in specs["list_issues"]


class TestSymptomFirstIssues:
    """#535: `create_issue` is symptom-first — the body leads with what the
    person saw, criteria are written against it, and a fix-shaped ask with
    no symptom is refused with the question to ask."""

    def test_compose_structured_body(self) -> None:
        from sbxloop.daemon.concierge import compose_issue_body

        body = compose_issue_body(
            {
                "symptom": "grey GitHub preview cards appear\nunder every bridge message",
                "requested_change": "remove the embeds",
                "goal": "Bridge messages should not drag a link preview under them.",
                "acceptance_criteria": [
                    "no link-preview card appears under a bridge message",
                    "the bridge's own status cards still render",
                ],
                "body": "Seen in #sbxloop since the 1.0 deploy.",
            }
        )
        assert body.startswith(
            "## Symptom (as observed)\n\n"
            "grey GitHub preview cards appear under every bridge message"
        )
        assert (
            "## Requested change\n\nremove the embeds\n\n_The mechanism the person asked for"
            in body
        )
        assert "## Goal\n\nBridge messages should not drag a link preview under them." in body
        assert (
            "## Acceptance criteria\n\n- [ ] no link-preview card appears under a bridge message\n"
            "- [ ] the bridge's own status cards still render" in body
        )
        assert body.endswith("Seen in #sbxloop since the 1.0 deploy.")
        assert body.index("## Symptom") < body.index("## Requested change") < body.index("## Goal")

    def test_a_fix_shaped_ask_without_a_symptom_is_refused_with_the_question(self) -> None:
        from sbxloop.daemon.concierge import compose_issue_body

        with pytest.raises(ValueError, match="symptom is required") as exc:
            compose_issue_body({"requested_change": "remove the embeds", "goal": "cleaner"})
        assert "What are you seeing that you want gone or changed?" in str(exc.value)
        with pytest.raises(ValueError, match="acceptance_criteria is required"):
            compose_issue_body({"symptom": "cards everywhere", "goal": "no cards"})

    def test_a_plain_body_still_files_as_is(self) -> None:
        from sbxloop.daemon.concierge import compose_issue_body

        assert compose_issue_body({"body": "Wrap fetch()."}) == "Wrap fetch()."
        assert compose_issue_body({}) == ""

    def test_the_tool_files_the_symptom_first_body(self, tmp_path: Path) -> None:
        github = FakeGithub()
        concierge, client, _, _, _ = make(
            tmp_path,
            [
                {
                    "calls": [
                        (
                            "create_issue",
                            {
                                "title": "Suppress link-preview unfurls under bridge messages",
                                "symptom": "grey GitHub preview cards under every message",
                                "requested_change": "remove the embeds",
                                "goal": "no unfurls, keep our own cards",
                                "acceptance_criteria": ["no preview card appears"],
                            },
                        ),
                        (
                            "create_issue",
                            {
                                "title": "Remove the Discord embeds",
                                "requested_change": "remove the embeds",
                                "goal": "cleaner channel",
                                "acceptance_criteria": ["embeds removed"],
                            },
                        ),
                    ],
                    "text": "Filed #41; and I need to ask about the second.",
                }
            ],
            github=github,
        )
        turn(concierge, "remove the Discord embeds", author="Discord user `ana`")
        filed, refused = client.responses
        assert filed.ok and filed.text.startswith("created and queued issue #41")
        (_, title, body, labels) = github.created[0]
        assert title.startswith("Suppress link-preview unfurls") and labels == ["sbxloop:run"]
        assert body.startswith("## Symptom (as observed)\n\ngrey GitHub preview cards")
        assert "- [ ] no preview card appears" in body
        assert "Filed by Discord user `ana`" in body
        assert not refused.ok or "symptom is required" in refused.text
        assert "What are you seeing that you want gone or changed?" in refused.text
        assert len(github.created) == 1, "the fix-shaped ask was not filed"


class TestRepoLinesHealth:
    """#516: the concierge's repository listing shows a suspended repo."""

    def test_list_repos_flags_suspended_and_backing_off(self, tmp_path: Path) -> None:
        concierge, client, _, loop, _ = make(
            tmp_path,
            [{"calls": [("list_repos", {})], "text": "here they are"}],
            config={
                "github": {"repos": [{"repo": "owner/repo"}, {"repo": "owner/other"}]},
            },
        )
        loop.repos = [
            {"repo": "owner/repo", "state": "suspended", "reason": "gone for this token"},
            {"repo": "owner/other", "state": "backoff", "failures": 3},
        ]
        turn(concierge, "what repos are configured?")
        (resp,) = client.responses
        assert "owner/repo — enabled" in resp.text
        assert (
            "**SUSPENDED from polling**: gone for this token (`resume-repo owner/repo` once fixed)"
            in resp.text
        )
        assert (
            "owner/other — enabled" in resp.text
            and "backing off after 3 poll failure(s)" in resp.text
        )
