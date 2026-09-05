"""Exact Discord output of the pure formatting layer (no discord.py needed)."""

from __future__ import annotations

from typing import Any

import pytest

from sbxloop.daemon.discord_format import (
    COLOR_FAIL,
    COLOR_OK,
    COLOR_RUNNING,
    COLOR_WARN,
    DISCORD_MAX_MESSAGE,
    EMBED_TOTAL_MAX,
    TOOL_FAIL_OUTPUT_LINES_DEFAULT,
    TOOL_OUTPUT_LINES_DEFAULT,
    EmbedSpec,
    RunStats,
    StatusLine,
    SteerProgress,
    ToolBatcher,
    ToolDigest,
    _clip,
    _fence_state,
    agent_model_label,
    daemon_notice,
    finish_embed,
    finish_text,
    format_for_discord,
    headline_embed,
    headline_text,
    items_lines,
    no_unfurl,
    output_excerpt,
    queue_lines,
    repetitive_streak,
    roster_text,
    split_markdown,
    status_embed,
    strip_json_payload,
    summary_embed,
    summary_text,
)
from sbxloop.daemon.model import DaemonNotice, RunReport, WorkItem
from sbxloop.events import Event


def ev(type: str, **data: Any) -> Event:
    return Event.now(type, "r1", **data)


def texts(chunks: list[Any]) -> list[str]:
    return [c.text for c in chunks]


class TestClip:
    def test_clamps_bad_limits(self) -> None:
        assert _clip("hello", 0) == "…"
        assert _clip("hello", -5) == "…"
        assert len(_clip("x" * 5000, 10_000)) == DISCORD_MAX_MESSAGE
        assert _clip("hi", 2) == "hi"
        assert _clip("hello", 2) == "h…"


class TestSplitMarkdown:
    def test_short_text_is_one_chunk_with_header(self) -> None:
        assert split_markdown("hi", 500, header="**h**") == ["**h**\nhi"]
        assert split_markdown("", 500) == []

    def test_prefers_paragraph_boundaries_and_numbers_continuations(self) -> None:
        para = ("word " * 30).strip()
        parts = split_markdown(
            "\n\n".join([para] * 6), 400, header="**p**", cont="**p** *({i}/{n})*"
        )
        assert len(parts) > 1
        assert parts[0].startswith("**p**\n")
        assert parts[1].startswith(f"**p** *(2/{len(parts)})*\n")
        assert all(len(p) <= 400 for p in parts)
        # no chunk starts or ends mid-paragraph: every chunk boundary is a paragraph
        for p in parts[1:]:
            body = p.split("\n", 1)[1]
            assert body.startswith("word") and body.endswith("word")

    def test_never_splits_inside_a_fence_without_reopening_it(self) -> None:
        code = "```python\n" + "\n".join(f"x{i} = {i}" for i in range(120)) + "\n```"
        parts = split_markdown("intro\n\n" + code + "\n\nafter", 300)
        assert len(parts) > 2
        for p in parts:
            assert len(p) <= 300
            inside, _ = _fence_state(p)
            assert not inside, p
        # continuation chunks re-open with the same language
        assert any(p.startswith("```python\n") for p in parts[1:])
        assert parts[-1].endswith("after")

    def test_oversize_single_line_is_hard_wrapped(self) -> None:
        parts = split_markdown("z" * 1500, 300)
        assert all(len(p) <= 300 for p in parts)
        assert "".join(p.strip("…") for p in parts) == "z" * 1500

    def test_minimum_config_limit(self) -> None:
        text = ("abc " * 200) + "\n```sh\n" + "\n".join(["echo hi"] * 40) + "\n```"
        parts = split_markdown(
            text, 200, header="**planner** · `m`", cont="**planner** *(cont. {i}/{n})*"
        )
        assert all(len(p) <= 200 for p in parts)
        assert all(not _fence_state(p)[0] for p in parts)


class TestStripJsonPayload:
    """The engine's copy of a structured reply never reaches the channel."""

    def test_payload_only_reply_leaves_nothing(self) -> None:
        assert strip_json_payload('```json\n{"verdict": "accept"}\n```') == ""
        # untagged, but the body is a JSON document all the same
        assert strip_json_payload('```\n{"steps": ["a"]}\n```') == ""
        # an unterminated fence still runs to the end of the reply
        assert strip_json_payload('here it is\n```json\n{"a": 1,') == "here it is"

    def test_narration_survives_on_either_side(self) -> None:
        assert (
            strip_json_payload('The executor added a test.\n\n```json\n{"v": "accept"}\n```')
            == "The executor added a test."
        )
        assert strip_json_payload('```json\n{"v": "reject"}\n```\n\nMy ruling.') == "My ruling."
        assert (
            strip_json_payload('a\n```json\n{"x":1}\n```\nb\n```json\n{"y":2}\n```\nc') == "a\nb\nc"
        )

    def test_unfenced_payload_is_stripped_only_when_it_ends_the_reply(self) -> None:
        assert strip_json_payload('Here is the plan:\n{"steps": ["a"]}') == "Here is the plan:"
        # mid-prose braces are prose, not a payload
        text = 'The config uses {"a": 1} inline and continues after.'
        assert strip_json_payload(text) == text

    def test_code_the_agent_is_talking_about_is_not_a_payload(self) -> None:
        shell = "Ran this:\n\n```bash\nuv run pytest\n```\n\nAll green."
        assert strip_json_payload(shell) == shell
        python = '```python\nd = {"a": 1}\n```'
        assert strip_json_payload(python) == python
        checklist = "Done:\n- [x] parser\n- [ ] docs"
        assert strip_json_payload(checklist) == checklist

    def test_empty_and_plain_text(self) -> None:
        assert strip_json_payload("") == ""
        assert strip_json_payload("  \n ") == ""
        assert strip_json_payload("plain prose") == "plain prose"


class TestDecisionCards:
    """What the structured phases decided, rendered for humans — the other
    half of dropping their JSON."""

    def test_roster_lists_every_task_with_its_dependencies(self) -> None:
        text = roster_text(
            {
                "tasks": [
                    {"id": "t1", "title": "Add the parser", "state": "done", "depends_on": []},
                    {"id": "t2", "title": "Wire the CLI", "state": "pending", "depends_on": ["t1"]},
                    {"id": "t3", "title": "Docs", "state": "skipped", "depends_on": ["t1", "t2"]},
                ]
            }
        )
        assert text.splitlines() == [
            "🧩 **3 task(s)**",
            "1. ✅ `t1` Add the parser",
            "2. `t2` Wire the CLI — after `t1`",
            "3. ⏭ `t3` Docs — after `t1`, `t2`",
        ]

    def test_missing_and_misshapen_fields_do_not_break_a_card(self) -> None:
        # Event data is agent-shaped: a renderer must not assume a list.
        assert roster_text({}).splitlines() == ["🧩 **0 task(s)**"]
        assert roster_text({"tasks": "nope"}).splitlines() == ["🧩 **0 task(s)**"]

    def test_events_render_at_every_level(self) -> None:
        for level in ("quiet", "normal", "verbose"):
            roster = format_for_discord(
                ev("run.tasks", tasks=[{"id": "t1", "title": "T", "state": "pending"}]),
                level=level,
            )
            assert texts(roster) == ["🧩 **1 task(s)**\n1. `t1` T"]
            assert roster[0].kind == "block"


class TestFormat:
    def test_agent_message_header_and_split(self) -> None:
        chunks = format_for_discord(
            ev(
                "agent.message",
                content="Hello **there**",
                agent="planner",
                model="claude-sonnet-5",
                backend="claude",
            )
        )
        assert texts(chunks) == ["**planner** · `claude · claude-sonnet-5`\nHello **there**"]
        assert chunks[0].kind == "block" and chunks[0].flush
        many = format_for_discord(
            ev("agent.message", content="para\n\n" * 400, agent="executor"), max_chars=500
        )
        assert len(many) > 1 and many[1].text.startswith(f"**executor** *(cont. 2/{len(many)})*")
        assert format_for_discord(ev("agent.message", content="  ")) == []
        # a structured phase's payload is dropped; its narration is not
        assert (
            format_for_discord(ev("agent.message", content='```json\n{"verdict": "accept"}\n```'))
            == []
        )
        assert texts(
            format_for_discord(
                ev("agent.message", content='Looks good.\n```json\n{"v": 1}\n```', agent="critic")
            )
        ) == ["**critic**\nLooks good."]

    def test_noise_dropped_at_every_level(self) -> None:
        for level in ("quiet", "normal", "verbose"):
            for t in (
                "agent.message_delta",
                "worker.heartbeat",
                "agent.usage",
                "sandbox.resources",
            ):
                assert format_for_discord(ev(t, x=1), level=level) == []

    def test_followups_name_the_issues_disabled_downgrade(self) -> None:
        """#631: a listing forced by the repository (Issues disabled) says
        so; a configured `comment` mode does not."""
        downgraded = texts(
            format_for_discord(
                ev(
                    "run.followups",
                    pr=7,
                    mode="comment",
                    filed=[],
                    listed=["a", "b"],
                    downgraded_from="issues",
                    reason="issues_disabled",
                )
            )
        )
        assert downgraded == [
            "📌 2 follow-up(s) listed on the PR, not filed (Issues are disabled on the "
            "repository): a; b"
        ]
        configured = texts(
            format_for_discord(ev("run.followups", pr=7, mode="comment", filed=[], listed=["a"]))
        )
        assert configured == ["📌 1 follow-up(s) listed on the PR, not filed: a"]

    def test_tool_events_are_not_rendered_here(self) -> None:
        # the pump feeds them to ToolBatcher instead
        assert format_for_discord(ev("agent.tool_start", tool="bash", args="ls")) == []
        assert format_for_discord(ev("agent.tool_end", tool="bash", success=False)) == []

    def test_link_carriers(self) -> None:
        assert format_for_discord(ev("run.report", repo="o/r", issue=3, url="https://x/3")) == []
        pr = format_for_discord(ev("run.deliver", repo="o/r", pr=9, url="https://x/pull/9"))
        assert texts(pr) == ["🔀 PR [#9 · o/r](https://x/pull/9)"] and pr[0].flush
        assert texts(format_for_discord(ev("run.deliver", repo="o/r", error="409 empty"))) == [
            "⚠ **delivery failed:** 409 empty"
        ]
        assert texts(format_for_discord(ev("run.deliver", repo="o/r", created=True))) == [
            "📦 created repository [o/r](https://github.com/o/r)"
        ]
        # The API's own html_url wins when the event carries it (#623).
        assert texts(
            format_for_discord(
                ev("run.deliver", repo="o/r", created=True, url="https://ghe.example.com/o/r")
            )
        ) == ["📦 created repository [o/r](https://ghe.example.com/o/r)"]
        assert texts(
            format_for_discord(
                ev("sandbox.workspace_clone", branch="sbxloop/r1", source="/p", target="/t")
            )
        ) == ["🌿 branch `sbxloop/r1` · clone of `/p`"]
        assert texts(
            format_for_discord(
                ev("sandbox.workspace_clone", branch="b", source="/p", target="/t", reused=True)
            )
        ) == ["🌿 branch `b` · clone of `/p` (reused)"]

    def test_chat_events(self) -> None:
        msg = format_for_discord(ev("chat.message", message_id="m1", text="focus on auth"))
        assert texts(msg) == [
            "> focus on auth\n💬 received — answered at the next checkpoint "
            "(may take a few minutes during a long step)"
        ]
        reply = format_for_discord(
            ev("chat.reply", message_id="m1", reply="Sure.", action="continue")
        )
        assert texts(reply) == ["🧭 **steering reply**\nSure."] and reply[0].flush
        assert texts(
            format_for_discord(ev("chat.reply", message_id="m1", error="worker down"))
        ) == ["⚠ **steering failed:** worker down"]
        assert texts(
            format_for_discord(ev("chat.action", action="steer_task", guidance="focus auth"))
        ) == ["↪ applied `steer_task` — focus auth"]

    def test_phase_end_failed_and_degraded(self) -> None:
        assert texts(
            format_for_discord(
                ev(
                    "phase.end",
                    task_id="t2",
                    phase="verify",
                    status="failed",
                    message="pytest: 1 failed",
                )
            )
        ) == ["✗ **verify** · task `t2` — pytest: 1 failed"]
        # The builder's report excerpt is the plan card's replacement in the
        # chronology; other ok phase-ends stay verbose-only.
        assert texts(
            format_for_discord(
                ev(
                    "phase.end",
                    task_id="t2",
                    phase="build",
                    status="ok",
                    message="added the parser and its tests",
                )
            )
        ) == ["🔨 **build** · task `t2` — added the parser and its tests"]
        assert format_for_discord(ev("phase.end", task_id="t2", phase="verify", status="ok")) == []
        # A workload's operator (#756) reports the way the builder does; the
        # judge's verdict is its own line, so a passing judge phase is quiet.
        assert texts(
            format_for_discord(
                ev("phase.end", task_id="t1", phase="execute", status="ok", message="wrote it")
            )
        ) == ["🛠 **execute** · task `t1` — wrote it"]
        assert (
            format_for_discord(
                ev("phase.end", task_id="t1", phase="judge", status="ok", message="all met")
            )
            == []
        )
        assert texts(
            format_for_discord(
                ev("phase.end", task_id="t1", phase="judge", status="failed", message="unmet: x")
            )
        ) == ["✗ **judge** · task `t1` — unmet: x"]
        # An advisory failure (#682) blocked nothing, and the human is who it
        # is evidence for: a warning line at every level.
        advisory = ev(
            "phase.end",
            task_id="t2",
            phase="verify",
            status="advisory",
            message="verify command failed: `pytest` (exit 1) (advisory, not blocking)",
        )
        assert texts(format_for_discord(advisory)) == [
            "⚠ **verify** · task `t2` — verify command failed: `pytest` (exit 1) "
            "(advisory, not blocking)"
        ]
        assert texts(format_for_discord(advisory, level="quiet")) == texts(
            format_for_discord(advisory)
        )
        # a skip under ci-only stays verbose-only, like any other non-failure
        skipped = ev("phase.end", task_id="t2", phase="verify", status="skipped", message="not run")
        assert format_for_discord(skipped) == []
        assert texts(format_for_discord(skipped, level="verbose")) == [
            "· verify · task `t2` — not run"
        ]

    def test_newly_surfaced_events_and_levels(self) -> None:
        err = format_for_discord(ev("worker.error", message="job died"))
        assert texts(err) == ["🛑 **worker error:** job died"] and err[0].kind == "block"
        assert format_for_discord(ev("worker.error", message="x"), level="quiet")  # all levels
        denied = ev("agent.permission_denied", kind="write", feedback="read-only critic")
        assert texts(format_for_discord(denied)) == ["🚫 `write`: read-only critic"]
        assert format_for_discord(denied, level="quiet") == []
        deny = ev("policy.deny", domain="evil.io", reason="not justified")
        assert texts(format_for_discord(deny)) == ["⛔ egress denied `evil.io` (not justified)"]
        assert format_for_discord(deny, level="quiet") == []
        warn = ev("sandbox.tooling_warning", message="node missing")
        assert texts(format_for_discord(warn)) == ["⚠ tooling: node missing"]
        assert format_for_discord(warn, level="quiet") == []
        services = ev(
            "verify.services_detected",
            evidence=["docker-compose.yml (compose file)", "uv.lock mentions testcontainers"],
            hint="set verify_mode",
        )
        assert texts(format_for_discord(services)) == [
            "⚠ verify: the suite may need services the sandbox does not have "
            "(`docker-compose.yml (compose file)`, `uv.lock mentions testcontainers`) "
            "— set verify_mode"
        ]
        assert format_for_discord(services, level="quiet") == []
        cap = ev("agent.tool_cap", cap=40)
        assert texts(format_for_discord(cap)) == [
            "⛔ tool-call ceiling (40) reached — further calls are turned away; the agent was "
            "told to wrap up and report"
        ]
        assert format_for_discord(cap, level="quiet") == []

    def test_lifecycle_absorbed_unless_verbose(self) -> None:
        for t, data in (
            ("task.state", {"task_id": "t1", "state": "done"}),
            ("task.start", {"task_id": "t1", "title": "T"}),
            ("task.end", {"task_id": "t1", "title": "T", "state": "done"}),
            ("run.state", {"state": "running"}),
            ("policy.allow", {"domain": "pypi.org"}),
        ):
            assert format_for_discord(ev(t, **data)) == []
            assert format_for_discord(ev(t, **data), level="verbose") != []
        assert texts(
            format_for_discord(ev("task.state", task_id="t1", state="done"), level="verbose")
        ) == ["· task t1 → done"]


class TestToolDigest:
    def test_one_line_grows_with_the_burst(self) -> None:
        d = ToolDigest()
        assert d.render() == "" and not d.dirty
        d.add_start("bash", "ls  -la")
        assert d.dirty
        assert d.render() == "⚙ 1 tool call (bash) — last: `ls -la`"
        assert not d.dirty
        for i in range(20):
            d.add_start("bash", f"grep -n x file{i}.py")
        d.add_start("view", "README.md")
        d.add_start("view", "docs/x.md")
        d.add_start("bash", ".venv/bin/pytest -q")
        assert d.render() == ("⚙ 24 tool calls (bash x22, view x2) — last: `.venv/bin/pytest -q`")
        # a failure counts in the line AND yields its own detail block
        detail = d.add_end("bash", success=False, exit_code=1, detail="FAILED a\n1 failed")
        assert detail is not None and detail.text.startswith("✗ `bash` failed (exit 1)")
        assert d.add_end("bash", success=True, exit_code=0, detail="") is None
        assert d.render().endswith("`.venv/bin/pytest -q` · ✗ 1 failed")

    def test_backticks_and_long_args_are_tamed(self) -> None:
        d = ToolDigest()
        d.add_start("bash", "echo `x` " + "a" * 400)
        text = d.render()
        assert "`echo 'x'" in text and len(text) < 200

    def test_repetition_collapses_and_nudges_the_human(self) -> None:
        d = ToolDigest(cancel_hint="!sbx cancel")
        for i in range(5):
            d.add_start("bash", f"grep -F 'exit {i}' /tmp/out | od -c")
        assert d.repetitive == 0  # below the window
        d.add_start("bash", "grep -E 'exit 9' /tmp/out | od -c | head")
        assert d.repetitive == 6
        assert d.render() == (
            "⚙ bash x6 similar commands — last: `grep -E 'exit 9' /tmp/out | od -c | head`\n"
            "⚠ the last 6 bash calls are near-identical — the agent may be stuck; "
            "`!sbx cancel` stops the run"
        )
        # a burst that started differently keeps the full count and adds the warning
        d2 = ToolDigest()
        d2.add_start("view", "a.py")
        d2.add_start("bash", "pytest -q")
        for i in range(6):
            d2.add_start("bash", f"grep -n 'exit {i}' /tmp/out")
        assert d2.render().startswith("⚙ 8 tool calls (bash x7, view) — last:")
        assert "the last 6 bash calls are near-identical" in d2.render()

    def test_repetitive_streak_needs_same_head_and_similar_text(self) -> None:
        alternating = [("bash", "grep -n a f"), ("bash", "od -c f")] * 4
        assert repetitive_streak(alternating) == 0
        prefixed = [("bash", f"cd /w && LC_ALL=C grep -n 'a{i}' f") for i in range(7)]
        assert repetitive_streak(prefixed) == 7
        different_tool = [*prefixed[:-1], ("view", "grep.txt")]
        assert repetitive_streak(different_tool) == 0
        assert repetitive_streak(prefixed[:3]) == 0

    def test_long_spiral_collapses_past_the_bounded_tail(self) -> None:
        # The digest used to keep only a bounded tail of commands, so a 17-call
        # spiral capped the streak at 13 and never rendered as "x17 similar".
        d = ToolDigest()
        for i in range(17):
            d.add_start("bash", f"grep -n 'exit {i}' /tmp/out | od -c")
        assert d.repetitive == 17
        assert d.render().startswith("⚙ bash x17 similar commands — last:")
        assert "the last 17 bash calls are near-identical" in d.render()

    def test_streak_uses_the_window_mean_not_every_adjacent_pair(self) -> None:
        # One dissimilar neighbour inside an otherwise near-identical window
        # does not veto it: the mean over the six-command window is what counts.
        run = [("bash", f"grep -n 'exit {i}' /tmp/out") for i in range(5)]
        run.append(("bash", "grep -rIl --include='*.py' 'zzzzzzzzzzzzzzzzzzzzzz' /somewhere/else"))
        run.extend(("bash", f"grep -n 'exit {i}' /tmp/out") for i in range(5, 9))
        assert repetitive_streak(run) == 10
        # ...whereas a run that drifts one flag at a time can keep every adjacent
        # pair similar while the window's mean falls under the threshold.
        drift = [("bash", "a" * 6 + "b" * (2 * i)) for i in range(1, 12)]
        assert repetitive_streak(drift) == 0
        # a slice whose mean fails ends the streak (0, not the run length); once a
        # later slice qualifies again the streak restarts from that window rather
        # than reaching back over the break.
        same = [("bash", "grep -n 'x' f")] * 6
        unrelated = [("bash", f"grep {c * 60}") for c in "qzy"]
        assert repetitive_streak(same + unrelated) == 0
        assert repetitive_streak(same + unrelated + same) == 8  # < len(run) == 15


class TestToolBatcher:
    def test_batch_renders_one_line_per_call_with_outcome(self) -> None:
        b = ToolBatcher(max_lines=8)
        b.add_start("bash", "ls -la", "c1")
        assert len(b) == 0  # a start emits nothing
        assert (
            b.add_end("bash", "c1", success=True, exit_code=0, detail="", duration_ms=1200) is None
        )
        assert len(b) == 1
        b.add_start("bash", "pytest -q", "c2")
        detail = b.add_end("bash", "c2", success=False, exit_code=1, detail="FAILED a\n1 failed\n")
        assert detail is not None
        assert detail.text == "✗ `bash` failed (exit 1)\n```text\nFAILED a\n1 failed\n```"
        text = b.flush().text  # type: ignore[union-attr]
        assert text == "```text\n$ bash  ls -la  ✓ 1.2s\n$ bash  pytest -q  ✗ exit 1\n```"
        assert b.flush() is None

    def test_concurrent_calls_ending_out_of_order_pair_by_id(self) -> None:
        b = ToolBatcher(max_lines=8)
        b.add_start("bash", "ruff check .", "c1")
        b.add_start("bash", "mypy packages", "c2")
        assert len(b) == 0
        b.add_end("bash", "c2", success=True, exit_code=0, detail="", duration_ms=500)
        b.add_end("bash", "c1", success=False, exit_code=2, detail="boom", duration_ms=2000)
        lines = b.flush().text.strip("`").strip().splitlines()[1:]  # type: ignore[union-attr]
        assert len(lines) == 2
        assert lines[0] == "$ bash  mypy packages  ✓ 500ms"
        assert lines[1] == "$ bash  ruff check .  ✗ exit 2 · 2.0s"
        assert "mypy" not in lines[1] and "ruff" not in lines[0]

    def test_unmatched_end_renders_from_its_own_args(self) -> None:
        b = ToolBatcher()
        assert b.add_end("bash", "zz", success=True, exit_code=0, detail="", args="echo hi") is None
        assert "$ bash  echo hi  ✓" in b.flush().text  # type: ignore[union-attr]

    def test_unfinished_start_is_flushed_as_running_only_when_final(self) -> None:
        b = ToolBatcher()
        b.add_start("bash", "sleep 100", "c1")
        assert b.flush() is None  # routine flush: in-flight calls stay pending
        text = b.flush(final=True).text  # type: ignore[union-attr]
        assert text == "```text\n$ bash  sleep 100  … running\n```"
        assert b.flush(final=True) is None

    def test_call_in_flight_at_flush_renders_exactly_once(self) -> None:
        # The PR #420 review repro: a failed sibling forces a mid-batch
        # flush; the call still in flight must not appear as `… running`
        # then again on completion.
        b = ToolBatcher()
        b.add_start("bash", "uv run mypy", "c1")
        b.add_start("bash", "uv run pytest -q", "c2")
        b.add_end("bash", "c2", success=False, exit_code=1, detail="1 failed")
        first = b.flush()  # the failure triggers an immediate flush
        assert first is not None
        assert "pytest" in first.text and "mypy" not in first.text
        b.add_end("bash", "c1", success=True, exit_code=0, detail="", duration_ms=500)
        second = b.flush(final=True)
        assert second is not None
        assert second.text == "```text\n$ bash  uv run mypy  ✓ 500ms\n```"
        assert "running" not in second.text

    def test_full_and_quiet(self) -> None:
        b = ToolBatcher(max_lines=2)
        b.add_start("read_file", "a.py", "c1")
        b.add_end("read_file", "c1", success=True, exit_code=0, detail="")
        assert not b.full
        b.add_start("read_file", "b.py", "c2")
        b.add_end("read_file", "c2", success=True, exit_code=0, detail="")
        assert b.full and len(b) == 2
        q = ToolBatcher(quiet=True)
        q.add_start("bash", "ls", "c1")
        assert len(q) == 0 and q.flush() is None
        # failures still surface in quiet mode
        assert q.add_end("bash", "c1", success=False, exit_code=2, detail="") is not None
        assert len(q) == 0 and q.flush() is None

    def test_long_args_are_elided_and_fences_neutralised(self) -> None:
        b = ToolBatcher()
        b.add_start("bash", "echo " + "a" * 500 + " ```", "c1")
        b.add_end("bash", "c1", success=True, exit_code=0, detail="")
        text = b.flush().text  # type: ignore[union-attr]
        assert "…" in text and text.count("```") == 2  # only the block fence itself
        assert "$ bash  echo " in text  # the verb always survives

    def test_run_path_prefix_collapses(self) -> None:
        b = ToolBatcher()
        args = (
            "cd /home/x/.local/state/sbxloop/sbxloop-work/runs/rfxm7ad23/workspace"
            " && git diff -- README.md"
        )
        b.add_start("bash", args, "c1")
        b.add_end("bash", "c1", success=True, exit_code=0, detail="")
        text = b.flush().text  # type: ignore[union-attr]
        assert "cd $RUN && git diff" in text


class TestStatusLine:
    def test_progression(self) -> None:
        s = StatusLine()
        assert s.render() == "⏳ decomposing"
        s.observe(ev("task.state", task_id="t1", title="Add tests", state="pending", revisions=0))
        s.observe(ev("task.state", task_id="t2", title="Wire CLI", state="pending", revisions=0))
        assert s.render() == "⏳ 2 task(s) planned"
        s.observe(ev("task.start", task_id="t1", title="Add tests"))
        assert s.render() == "⏳ task 1/2 · **Add tests**"
        s.observe(ev("phase.end", task_id="t1", phase="verify", status="failed", message="x"))
        s.observe(ev("task.state", task_id="t1", state="executing", revisions=1))
        assert s.render() == "⏳ task 1/2 · **Add tests** · verify · rev 1"
        s.observe(ev("task.end", task_id="t1", title="Add tests", state="done"))
        s.observe(ev("task.start", task_id="t2", title="Wire CLI"))
        assert s.render() == "⏳ task 2/2 · **Wire CLI**\n✅ 1 done"
        s.observe(ev("task.end", task_id="t2", title="Wire CLI", state="failed"))
        s.finish("failed")
        assert s.render() == "❌ finished · 1/2 tasks done · ✅ 1 done · ❌ 1 failed"

    def test_dirty_flag(self) -> None:
        s = StatusLine()
        assert not s.dirty
        s.observe(ev("task.start", task_id="t1", title="T"))
        assert s.dirty
        s.render()
        assert not s.dirty
        s.observe(ev("agent.message", content="ignored"))
        assert not s.dirty


class TestSteerProgress:
    def test_where_the_agent_is(self) -> None:
        p = SteerProgress(cap=40)
        assert p.render() == "⏳ steer queued; answered at the next checkpoint"
        p.observe(ev("task.start", task_id="t2", title="Wire CLI"))
        assert p.render() == (
            "⏳ steer queued — agent is on `t2` · Wire CLI; answered at the next checkpoint"
        )
        p.observe(ev("task.state", task_id="t2", state="executing", revisions=0))
        for _ in range(12):
            p.observe(ev("agent.tool_start", tool="bash", args="ls"))
        assert p.render() == (
            "⏳ steer queued — agent is mid-**build** on `t2` · Wire CLI "
            "(12/40 tool calls so far); answered at the next checkpoint"
        )
        # a phase boundary is a checkpoint: the count restarts with the new job
        p.observe(ev("task.state", task_id="t2", state="verifying", revisions=0))
        assert "mid-**verify** on `t2` · Wire CLI;" in p.render()
        p.observe(ev("task.state", task_id="t2", state="executing", revisions=1))
        p.observe(ev("agent.tool_start", tool="bash", args="ls"))
        p.observe(ev("agent.tool_cap", cap=40, calls=40, tool="bash"))
        assert "(1/40 tool calls — ceiling reached)" in p.render()
        p.observe(ev("task.end", task_id="t2", title="Wire CLI", state="done"))
        assert p.render() == "⏳ steer queued; answered at the next checkpoint"

    def test_a_workload_names_its_own_phases(self) -> None:
        """The same task states under the operator's names (#756): the run's
        kind, carried on `run.start`, picks the vocabulary."""
        p = SteerProgress(cap=40)
        p.observe(ev("run.start", outcome="o", kind="workload"))
        p.observe(ev("task.state", task_id="t1", state="executing", revisions=0))
        p.observe(ev("task.start", task_id="t1", title="Count"))
        assert "mid-**execute** on `t1` · Count" in p.render()
        p.observe(ev("task.state", task_id="t1", state="verifying", revisions=0))
        assert "mid-**judge** on `t1` · Count" in p.render()
        code = SteerProgress(cap=40)
        code.observe(ev("run.start", outcome="o"))
        code.observe(ev("task.state", task_id="t1", state="executing", revisions=0))
        assert "mid-**build**" in code.render()

    def test_production_event_order_keeps_the_build_phase(self) -> None:
        # LoopEngine._run_task emits task.state=executing BEFORE task.start
        # (and the persisted phase first on resume); the start must not
        # wipe the phase already observed for the same task.
        p = SteerProgress(cap=40)
        p.observe(ev("task.state", task_id="t1", state="executing", revisions=0))
        p.observe(ev("task.start", task_id="t1", title="Build it"))
        assert p.render() == (
            "⏳ steer queued — agent is mid-**build** on `t1` · Build it; "
            "answered at the next checkpoint"
        )
        p.observe(ev("task.state", task_id="t3", state="verifying", revisions=0))
        p.observe(ev("agent.tool_start", tool="bash", args="ls"))
        p.observe(ev("task.start", task_id="t3", title="Resumed"))
        assert "mid-**verify** on `t3` · Resumed (1/40 tool calls so far)" in p.render()
        # a start for a DIFFERENT task still resets the phase and counters
        p.observe(ev("task.start", task_id="t4", title="Next"))
        assert p.render() == (
            "⏳ steer queued — agent is on `t4` · Next; answered at the next checkpoint"
        )

    def test_unbounded_cap_and_terminal_states(self) -> None:
        p = SteerProgress(cap=0)  # 0 = unbounded in [budgets]
        p.observe(ev("task.state", task_id="t1", state="executing", revisions=0))
        p.observe(ev("agent.tool_start", tool="bash", args="ls"))
        assert "(1 tool call so far)" in p.render()
        p.observe(ev("agent.tool_start", tool="bash", args="ls"))
        assert "(2 tool calls so far)" in p.render()
        assert p.render(state="answering") == "🧭 steer picked up — the agent is answering now"
        assert p.render(state="answered") == "✅ steer answered"
        assert p.render(state="failed").startswith("⚠ steer failed")
        assert p.render(state="unanswered") == "⚠ steer not answered — the run ended first"

    def test_dirty_flag(self) -> None:
        p = SteerProgress()
        assert not p.dirty
        p.observe(ev("agent.message", content="ignored"))
        assert not p.dirty
        p.observe(ev("agent.tool_start", tool="bash", args="ls"))
        assert p.dirty
        p.render()
        assert not p.dirty


class TestEmbeds:
    def test_clamped_respects_limits(self) -> None:
        spec = EmbedSpec(
            title="t" * 300,
            description="d" * 5000,
            fields=tuple((f"n{i}", "v" * 2000, True) for i in range(30)),
            footer="f" * 3000,
        ).clamped()
        assert len(spec.title or "") == 256
        assert len(spec.fields) <= 25 and all(len(v) <= 1024 for _, v, _ in spec.fields)
        assert len(spec.footer or "") == 2048
        total = (
            len(spec.title or "")
            + len(spec.description or "")
            + len(spec.footer or "")
            + sum(len(n) + len(v) for n, v, _ in spec.fields)
        )
        assert total <= EMBED_TOTAL_MAX

    def test_headline_card_by_state(self) -> None:
        item = WorkItem(item_id="gh:issue:4", source_key="4", title="Fix login", url="https://x/4")
        running = headline_embed(item, "r1", hostname="db")
        assert running.title == "Fix login" and running.url == "https://x/4"
        assert running.color == COLOR_RUNNING and running.footer == "sbxloop · db"
        assert {n: v for n, v, _ in running.fields} == {
            "Source": "[issue #4](https://x/4)",
            "Item": "`gh:issue:4`",
            "Run": "`r1`",
            "State": "running",
        }
        done = headline_embed(
            item,
            "r1",
            "completed",
            branch="sbxloop/r1",
            pr=(34, "https://x/pull/34"),
            summary="3/3 tasks done",
            requested_by="4242",
            hostname="db",
        )
        names = [n for n, _, _ in done.fields]
        assert names == [
            "Source",
            "Item",
            "Run",
            "State",
            "Branch",
            "PR",
            "Tasks",
            "Requested by",
        ]
        assert done.color == COLOR_OK
        assert {n: v for n, v, _ in done.fields}["Requested by"] == "<@4242>"
        assert headline_embed(item, "r1", "merged", hostname="db").color == COLOR_OK
        assert headline_embed(item, "r1", "failed", hostname="db").color == COLOR_FAIL
        assert headline_embed(item, "r1", "blocked", hostname="db").color == COLOR_WARN
        assert headline_text(item, "r2", "merged").startswith("🎉 run `r2` — **Fix login**")
        assert "`gh:issue:4`" in headline_text(item, "r2", "merged")

    def test_typed_ids_on_cards_and_listings(self) -> None:
        """Legacy `gh:<n>` items still render, but always in the typed form."""
        legacy = WorkItem(item_id="gh:4", source_key="4", title="Fix login", url="https://x/4")
        card = headline_embed(legacy, "r1", hostname="db")
        assert {n: v for n, v, _ in card.fields}["Item"] == "`gh:issue:4`"
        assert "`gh:issue:4`" in headline_text(legacy, "r1")
        assert "`gh:issue:4`" in queue_lines([legacy])
        assert "`gh:issue:4`" in items_lines([legacy])
        report = RunReport("r1", "cancelled", "1/3 tasks done", cancelled_by="ops")
        cancelled = finish_embed(legacy, report, "cancelled")
        note = {n: v for n, v, _ in cancelled.fields}["Cancelled"]
        assert "!sbx retry gh:issue:4" in note

    def test_finish_card_and_text(self) -> None:
        item = WorkItem(item_id="gh:issue:4", source_key="4", title="Fix login")
        report = RunReport("r1", "merged", "3/3 tasks done", pr=(34, "https://x/pull/34"), rounds=2)
        assert finish_text("merged", report) == "**finished: merged** — 3/3 tasks done"
        card = finish_embed(item, report, "merged", unanswered=1)
        assert card.title == "🎉 finished: merged" and card.description == "3/3 tasks done"
        assert [n for n, _, _ in card.fields] == ["PR", "Fix rounds", "Steering"]
        assert card.color == COLOR_OK
        blocked = finish_embed(
            item,
            RunReport("r1", "blocked", "x", pr=(34, "https://x/pull/34"), reason="405 refused"),
            "blocked",
        )
        assert blocked.color == COLOR_WARN
        assert {n: v for n, v, _ in blocked.fields}["Reason"] == "405 refused"
        # A merged run's reason (none) is not a field.
        assert "Reason" not in {n for n, _, _ in card.fields}

    def test_finish_card_for_operator_cancel_says_who_and_how_to_continue(self) -> None:
        """#246: a cancel is not a failure; the card must name the requester
        and tell the human the run is resumable (or already re-queued)."""
        item = WorkItem(item_id="gh:issue:8", source_key="8", title="Demo")
        report = RunReport("r1", "cancelled", "1/3 tasks done", cancelled_by="Discord user `b`")
        text = finish_text("cancelled", report)
        assert "cancelled by Discord user `b`" in text and "`sbxloop resume r1`" in text
        card = finish_embed(item, report, "cancelled")
        assert card.title == "⏹ finished: cancelled"
        assert card.fields[0][0] == "Cancelled"
        assert "`sbxloop resume r1`" in card.fields[0][1]
        assert "!sbx retry gh:issue:8" in card.fields[0][1]
        requeued = finish_embed(item, report._replace(requeued=True), "cancelled")
        assert "re-queued" in requeued.fields[0][1] and "resume" not in requeued.fields[0][1]

    def test_status_card_and_queue(self) -> None:
        card = status_embed(
            {
                "current": {"run_id": "r1", "title": "Fix"},
                "queued": 2,
                "runs_today": 1,
                "max_runs_per_day": 12,
                "run_cap_timezone": "UTC",
                "breaker_open": False,
                "paused": True,
            }
        )
        assert {n: v for n, v, _ in card.fields} == {
            "Current": "`r1` — Fix",
            "Queued": "2",
            "Runs today (UTC)": "1/12 · resets 00:00 UTC",
            "Breaker": "closed",
            "Paused": "yes",
        }
        assert card.color == COLOR_WARN
        idle = status_embed({"current": None, "breaker_open": True})
        assert idle.fields[0][1] == "idle" and idle.color == COLOR_FAIL
        assert queue_lines([]) == "queue is empty."
        items = [
            WorkItem(
                item_id=f"gh:{i}",
                source_key=str(i),
                title=f"T{i}",
                url=f"https://x/{i}",
            )
            for i in range(1, 4)
        ]
        assert (
            queue_lines(items, limit=2)
            == "• `gh:issue:1` [T1](https://x/1)\n• `gh:issue:2` [T2](https://x/2)\n… and 1 more"
        )

    def test_daemon_notice_masks_urls(self) -> None:
        assert no_unfurl("PR https://x/pull/9 done") == "PR <https://x/pull/9> done"
        assert (
            no_unfurl("already <https://x> and [t](https://y)")
            == "already <https://x> and [t](https://y)"
        )
        assert daemon_notice("✅ gh:issue:8 done · PR https://x/pull/9", thread_id=77) == (
            "✅ gh:issue:8 done · PR <https://x/pull/9> · <#77>"
        )
        notice = DaemonNotice("run.done", "🎉 gh:issue:8 merged · PR https://x/pull/9", run_id="r1")
        assert (
            daemon_notice(notice, thread_id=77)
            == "🎉 gh:issue:8 merged · PR <https://x/pull/9> · <#77>"
        )
        warn = DaemonNotice("run.failed", "gh:issue:8 failed", level="warning")
        assert daemon_notice(warn) == "⚠ gh:issue:8 failed"
        error = DaemonNotice("run.blocked", "🚧 gh:issue:8 blocked", level="error")
        assert daemon_notice(error) == "🛑 🚧 gh:issue:8 blocked"

    def test_pipeline_events_render_one_line_each(self) -> None:
        assert texts(format_for_discord(ev("run.state", state="reviewing"))) == ["🔍 **reviewing**"]
        assert texts(format_for_discord(ev("run.state", state="awaiting_ci"))) == [
            "⏳ **awaiting ci**"
        ]
        assert format_for_discord(ev("run.state", state="building")) == []
        # A workload's own stages (#755) — its graph stages render like a
        # code run's: nothing, the task lines carry that.
        assert texts(format_for_discord(ev("run.state", state="judging"))) == ["⚖️ **judging**"]
        assert texts(format_for_discord(ev("run.state", state="publishing"))) == [
            "📤 **publishing**"
        ]
        assert format_for_discord(ev("run.state", state="planning")) == []
        assert format_for_discord(ev("run.state", state="executing")) == []
        verdict = format_for_discord(
            ev("review.verdict", round=2, verdict="request_changes", findings=3, url="https://r")
        )
        assert texts(verdict) == [
            "🔍 review round 2: **requested changes** · 3 finding(s) · [review](https://r)"
        ]
        assert verdict[0].flush
        assert texts(
            format_for_discord(ev("review.verdict", round=3, verdict="approve", findings=0))
        ) == ["🔍 review round 3: **approved** · 0 finding(s)"]
        # The judge's verdicts (#756): the first unmet criterion is the
        # line, since it is the next attempt's whole brief.
        passed = format_for_discord(
            ev("judge.verdict", task_id="t1", attempt=1, passed=True, unmet=[], notes="fine")
        )
        assert texts(passed) == ["⚖️ judge: task `t1` **passed**"] and passed[0].flush
        failed = format_for_discord(
            ev(
                "judge.verdict",
                task_id="t2",
                attempt=2,
                passed=False,
                unmet=["the file exists — it does not", "the count matches"],
                notes="",
            )
        )
        assert texts(failed) == [
            "⚖️ judge: task `t2` **failed** (attempt 2) · 2 unmet — the file exists — it does not"
        ]
        degraded = format_for_discord(
            ev("judge.degraded", task_id="t2", attempt=1, error="produced invalid output twice")
        )
        assert texts(degraded) == [
            "🛑 judge: task `t2` — no usable verdict twice; the task fails closed "
            "(produced invalid output twice)"
        ]
        assert degraded[0].flush
        reconciled = format_for_discord(
            ev(
                "review.reconciled",
                round=2,
                addressed=2,
                refuted=1,
                unanswered=0,
                replied=3,
                resolved=2,
                comment_url="https://c",
            )
        )
        assert texts(reconciled) == [
            "🧾 reconciled round 2: 2 addressed · 1 refuted · 0 unanswered · "
            "3 repl(ies), 2 thread(s) resolved · [body-only](https://c)"
        ]
        assert reconciled[0].flush
        assert texts(
            format_for_discord(
                ev("fix.round", round=1, kind="ci", budget="1/2", why="mdformat, security failed")
            )
        ) == ["🛠 fix round 1 (ci, budget 1/2) — mdformat, security failed"]
        assert texts(
            format_for_discord(ev("ci.status", state="red", failed=["lint", "test (3.13)"]))
        ) == ["❌ CI red — lint, test (3.13)"]
        assert texts(format_for_discord(ev("ci.status", state="green", total=7))) == [
            "✅ CI green · 7 check(s)"
        ]
        assert format_for_discord(ev("ci.status", state="pending")) == []
        assert texts(format_for_discord(ev("land.undraft", pr=9))) == [
            "🚀 PR #9 taken out of draft"
        ]
        assert texts(format_for_discord(ev("land.update", pr=9, attempt=1))) == [
            "🚀 PR #9 updated from its base (attempt 1)"
        ]
        assert texts(format_for_discord(ev("land.enqueued", pr=9, position=2, resumed=False))) == [
            "🚀 PR #9 entered the merge queue at position 2"
        ]
        assert texts(
            format_for_discord(ev("land.enqueued", pr=9, position=None, resumed=True))
        ) == ["🚀 PR #9 already in the merge queue"]
        dequeued = format_for_discord(
            ev("land.dequeued", pr=9, reason="CI_FAILED", failed=["integration"])
        )
        assert texts(dequeued) == [
            "🚧 PR #9 removed from the merge queue (CI_FAILED) — failing: integration"
        ]
        assert dequeued[0].flush
        assert texts(format_for_discord(ev("land.dequeued", pr=9, reason="", failed=[]))) == [
            "🚧 PR #9 removed from the merge queue"
        ]
        merged = format_for_discord(ev("run.merged", pr=9, url="https://x/pull/9"))
        assert texts(merged) == ["🎉 **merged** PR [#9](https://x/pull/9)"] and merged[0].flush
        assert texts(
            format_for_discord(ev("run.merged", pr=9, url="https://x/pull/9", by_human=True))
        ) == ["🎉 **merged** PR [#9](https://x/pull/9) (by a human)"]
        assert texts(
            format_for_discord(ev("run.blocked", pr=9, url="https://x/pull/9", why="405"))
        ) == ["🚧 **blocked:** 405 · PR [#9](https://x/pull/9) — a human needs to look"]
        assert texts(
            format_for_discord(ev("run.deliver", repo="o/r", pr=9, url="https://x/pull/9", round=2))
        ) == ["🔀 PR [#9 · o/r](https://x/pull/9) (round 2)"]
        waiting = format_for_discord(
            ev(
                "run.awaiting_review",
                pr=9,
                url="https://x/pull/9",
                approvals_required=2,
                code_owners=True,
            )
        )
        assert texts(waiting) == [
            "👀 **awaiting review** PR [#9](https://x/pull/9) — the base requires 2 approving "
            "review(s) from a code owner (0/2 so far); waiting for a reviewer on GitHub"
        ]
        assert waiting[0].flush
        drafted = format_for_discord(ev("land.held_by_draft", pr=9, head="abc"))
        assert texts(drafted) == [
            "✋ PR #9 was converted to draft by a person — holding until it is marked ready "
            "for review"
        ]
        assert drafted[0].flush
        held = format_for_discord(
            ev(
                "run.awaiting_review",
                pr=9,
                url="https://x/pull/9",
                approvals_required=0,
                code_owners=False,
                draft=True,
            )
        )
        assert texts(held) == [
            "✋ **held in draft** PR [#9](https://x/pull/9) — a person converted it to draft; "
            "waiting for it to be marked ready for review"
        ]
        assert held[0].flush


def tev(ts: float, type: str, **data: Any) -> Event:
    return Event(ts=ts, run_id="r1", type=type, data=data)


class TestRunSummary:
    def _folded(self) -> RunStats:
        stats = RunStats()
        for event in (
            tev(100.0, "run.start", outcome="fix the bug"),
            tev(101.0, "run.tasks", tasks=[{"id": "t1"}, {"id": "t2"}]),
            tev(110.0, "agent.usage", input_tokens=1000, output_tokens=200),
            tev(111.0, "agent.tool_start", tool="bash"),
            tev(112.0, "agent.tool_start", tool="bash"),
            tev(
                130.0,
                "phase.end",
                task_id="t2",
                phase="verify",
                status="failed",
                message="verify command failed: `pytest -q` (exit 1)",
            ),
            tev(140.0, "agent.usage", input_tokens=2000, output_tokens=300),
            tev(150.0, "chat.message", message_id="m1", text="go faster"),
            tev(151.0, "chat.reply", message_id="m1", reply="ok", action="continue"),
            tev(160.0, "policy.deny", op="push"),
            tev(161.0, "task.state", task_id="t1", state="done", revisions=0),
            tev(162.0, "task.end", task_id="t2", state="done", revisions=1),
            tev(220.0, "run.end", state="completed"),
        ):
            stats.observe(event)
        return stats

    def test_stats_fold_the_event_stream(self) -> None:
        stats = self._folded()
        assert stats.duration_s == 120.0
        assert stats.turns == 2 and stats.tool_calls == 2
        assert stats.input_tokens == 3000 and stats.output_tokens == 500
        assert stats.rework == [
            ("t2", "verify", "failed", "verify command failed: `pytest -q` (exit 1)")
        ]
        assert stats.steers == 1 and stats.steers_answered == 1 and stats.steers_failed == 0
        assert stats.denies == 1
        assert stats.task_counts() == (2, 2)

    def test_summary_text_leads_with_the_headline_numbers(self) -> None:
        stats = self._folded()
        assert summary_text(stats, "completed") == (
            "📊 **run summary** — 2m 00s · 2 turn(s) · 2 tool call(s) · 3,000 in / 500 out tokens"
        )
        assert summary_text(None, "completed") == "📊 **run summary**"

    def test_summary_card_stats_and_both_ledgers(self) -> None:
        stats = self._folded()
        report = RunReport("r1", "merged", "2/2 tasks done", pr=(34, "https://x/pull/34"))
        card = summary_embed(stats, report, "merged")
        assert card.title == "📊 run summary" and card.color == COLOR_OK
        assert card.description == "**merged** — 2/2 tasks done in 2m 00s"
        assert card.footer == "run r1"
        fields = {n: v for n, v, _ in card.fields}
        assert list(fields) == ["Stats", "Went well", "Needed work"]
        assert "turns 2 · tool calls 2" in fields["Stats"]
        assert "tokens 3,000 in / 500 out" in fields["Stats"]
        assert "steering 1 asked / 1 answered" in fields["Stats"]
        well = fields["Went well"]
        assert "merged PR [#34](https://x/pull/34)" in well
        assert "all 2 task(s) completed" in well
        assert "answered all 1 steering message(s)" in well
        work = fields["Needed work"]
        assert "• `t2` verify: **failed** — verify command failed: `pytest -q` (exit 1)" in work
        assert "• 1 policy denial(s)" in work

    def test_summary_card_degrades_without_stats(self) -> None:
        """A run the pump never saw events for still gets a summary; unknown
        numbers are omitted rather than shown as zero."""
        report = RunReport("r1", "failed", "no tasks ran")
        card = summary_embed(None, report, "failed")
        assert card.color == COLOR_FAIL and card.description == "**failed** — no tasks ran"
        fields = {n: v for n, v, _ in card.fields}
        assert "Stats" not in fields
        assert fields["Went well"] == "nothing stood out"
        assert fields["Needed work"] == "no setbacks observed"

    def test_summary_card_flags_setbacks_and_resume(self) -> None:
        stats = RunStats()
        stats.observe(tev(100.0, "run.start", resumed=True))
        stats.observe(tev(101.0, "agent.tool_cap", cap=40))
        stats.observe(tev(102.0, "chat.message", message_id="m1", text="x"))
        stats.observe(tev(103.0, "chat.reply", message_id="m1", error="worker died"))
        stats.observe(tev(104.0, "task.state", task_id="t1", state="failed"))
        report = RunReport("r1", "failed", "0/1 tasks done", reason="409 conflict", rounds=1)
        card = summary_embed(stats, report, "failed", unanswered=1)
        assert "_stats cover the run since the daemon last picked it up_" in (
            card.description or ""
        )
        work = {n: v for n, v, _ in card.fields}["Needed work"]
        assert "1 task(s) failed" in work
        assert "failed — 409 conflict" in work and "1 fix round(s) spent" in work
        assert "1 steering message(s) went unanswered" in work
        assert "1 steer(s) errored" in work
        assert "hit the per-phase tool-call ceiling" in work

    def test_usage_never_reported_is_not_zero(self) -> None:
        stats = RunStats()
        stats.observe(tev(100.0, "agent.usage"))
        assert stats.turns == 1
        assert stats.input_tokens is None
        assert summary_text(stats, "completed") == "📊 **run summary** — 0s · 1 turn(s)"
        card = summary_embed(stats, RunReport("r1", "completed", "x"), "completed")
        assert {n: v for n, v, _ in card.fields}["Stats"] == "turns 1"

    def test_per_turn_cost_is_never_accumulated_or_rendered(self) -> None:
        """The backend's per-turn ``cost`` is a constant of unknown unit, so
        summing it invents a currency figure (#430). Nothing may read it and
        no summary path may print a ``$`` number."""
        stats = RunStats()
        for _ in range(3):
            stats.observe(tev(100.0, "agent.usage", cost=15.0, total_cost=15.0))
        assert not any("cost" in name or "spend" in name for name in vars(stats))
        text = summary_text(stats, "completed")
        card = summary_embed(stats, RunReport("r1", "completed", "x"), "completed")
        rendered = text + "\n".join(f"{n}{v}" for n, v, _ in card.fields)
        assert "$" not in rendered
        assert "45" not in rendered


def test_embed_converter_roundtrip() -> None:
    pytest.importorskip("discord")
    from sbxloop.daemon.discord import _allowed_mentions_none, _to_embed

    spec = EmbedSpec(
        title="T",
        description="D",
        url="https://x",
        color=COLOR_OK,
        fields=(("a", "b", True),),
        footer="f",
    )
    embed = _to_embed(spec)
    assert embed is not None
    assert embed.title == "T" and embed.description == "D" and embed.url == "https://x"
    assert embed.colour.value == COLOR_OK
    assert [(f.name, f.value, f.inline) for f in embed.fields] == [("a", "b", True)]
    assert embed.footer.text == "f"
    assert _allowed_mentions_none() is not None


class TestOutputExcerpt:
    """Bounded, informative output excerpts for completed tool calls."""

    def _big(self, n: int = 10_000, width: int = 100) -> str:
        return "\n".join(f"L{i} " + "x" * width for i in range(n))

    def test_pathological_output_stays_under_discord_limit(self) -> None:
        big = self._big()
        assert len(big) > 1_000_000 - 1
        fail = output_excerpt("bash", 1, big, success=False, output_lines=10_000)
        assert fail is not None
        assert len(fail.text) <= DISCORD_MAX_MESSAGE
        ok = output_excerpt("bash", 0, big, success=True, max_lines=5)
        assert ok is not None
        assert len(ok.text) <= DISCORD_MAX_MESSAGE
        assert ok.text.count("```") == 2

    def test_single_huge_line_is_clipped(self) -> None:
        chunk = output_excerpt("bash", 1, "y" * 1_000_000, success=False)
        assert chunk is not None
        assert len(chunk.text) <= DISCORD_MAX_MESSAGE
        assert chunk.text.endswith("```")

    def test_elided_marker_counts_omitted_lines(self) -> None:
        detail = "\n".join(f"L{i}" for i in range(100))
        chunk = output_excerpt("bash", 2, detail, success=False, max_lines=20)
        assert chunk is not None
        assert "… 80 lines elided …" in chunk.text
        body = chunk.text.split("```text\n")[1].rsplit("\n```", 1)[0].splitlines()
        assert body[0] == "L0" and body[9] == "L9"
        assert body[10] == "… 80 lines elided …"
        assert body[-1] == "L99"

    def test_elided_count_uses_event_output_lines(self) -> None:
        # The stored text was itself truncated upstream: the marker must
        # report the true omitted count, not what happens to be present.
        detail = "\n".join(f"L{i}" for i in range(30))
        chunk = output_excerpt("bash", 1, detail, success=False, max_lines=20, output_lines=5000)
        assert chunk is not None
        assert "… 4980 lines elided …" in chunk.text

    def test_no_marker_when_nothing_omitted(self) -> None:
        chunk = output_excerpt("bash", 1, "a\nb\nc", success=False, max_lines=20)
        assert chunk is not None
        assert "elided" not in chunk.text

    def test_failure_surfaces_stderr_and_exit_status(self) -> None:
        chunk = output_excerpt("bash", 2, "ModuleNotFoundError: sbxloop", success=False)
        assert chunk is not None
        assert "✗" in chunk.text and "(exit 2)" in chunk.text
        assert "ModuleNotFoundError: sbxloop" in chunk.text

    def test_success_budget_is_smaller_than_failure(self) -> None:
        detail = "\n".join(f"L{i}" for i in range(50))
        ok = output_excerpt("bash", 0, detail, success=True, max_lines=3)
        bad = output_excerpt("bash", 1, detail, success=False, max_lines=20)
        assert ok is not None and bad is not None
        assert len(ok.text) < len(bad.text)
        assert "✓" in ok.text and "(exit 0)" in ok.text
        assert ok.text.rstrip("`\n").endswith("L49")

    def test_success_with_zero_budget_renders_nothing(self) -> None:
        assert output_excerpt("bash", 0, "hello", success=True, max_lines=0) is None

    def test_failure_with_no_output_still_reports(self) -> None:
        chunk = output_excerpt("bash", 3, "", success=False)
        assert chunk is not None
        assert chunk.text == "✗ `bash` failed (exit 3)"

    def test_fences_escaped_and_balanced(self) -> None:
        chunk = output_excerpt("bash", 1, "before\n```\ninside\n```\nafter", success=False)
        assert chunk is not None
        assert chunk.text.count("```") == 2
        assert "'''" in chunk.text

    def test_redacted_upstream_text_stays_redacted(self) -> None:
        # The renderer reads only the (already redacted) payload text; it
        # must not resurrect anything, and must not invent a new source.
        chunk = output_excerpt("bash", 1, "token=***REDACTED***", success=False)
        assert chunk is not None
        assert "***REDACTED***" in chunk.text

    def test_batcher_uses_configured_budgets(self) -> None:
        detail = "\n".join(f"L{i}" for i in range(50))
        b = ToolBatcher(max_lines=8, output_lines=2, fail_output_lines=6)
        ok = b.add_end("bash", "c1", success=True, exit_code=0, detail=detail)
        assert ok is not None and ok.text.endswith("L48\nL49\n```")
        assert "… 48 lines elided …" in ok.text
        bad = b.add_end("bash", "c2", success=False, exit_code=1, detail=detail, output_lines=50)
        assert bad is not None and "… 44 lines elided …" in bad.text

    def test_batcher_success_quiet_by_default(self) -> None:
        b = ToolBatcher(max_lines=8)
        assert b.add_end("bash", "c1", success=True, exit_code=0, detail="out") is None

    def test_digest_success_quiet_failure_loud(self) -> None:
        d = ToolDigest()
        d.add_start("bash", "ls", "c1")
        assert d.add_end("bash", "c1", success=True, exit_code=0, detail="x") is None
        d.add_start("bash", "ls", "c2")
        chunk = d.add_end("bash", "c2", success=False, exit_code=1, detail="boom", output_lines=1)
        assert chunk is not None and "boom" in chunk.text

    def test_config_defaults_match_constants(self) -> None:
        from sbxloop.config import DiscordConfig

        cfg = DiscordConfig()
        assert cfg.tool_output_lines == TOOL_OUTPUT_LINES_DEFAULT
        assert cfg.tool_fail_output_lines == TOOL_FAIL_OUTPUT_LINES_DEFAULT


# Concrete credential literals: nothing here may survive into a chunk.
PAT = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
FINE_PAT = "github_pat_11ABCDEFG0" + "abcdefghijklmnopqrstuvwxyz012345"
BEARER = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"
APIKEY = "API_KEY=sk-live-abcdef0123456789"
SECRETS = (PAT, FINE_PAT, BEARER.split()[-1], APIKEY.split("=", 1)[1])
SECRET_TEXT = f"exporting {APIKEY}\ncurl -H '{BEARER}'\nusing {PAT} and {FINE_PAT}\n"


def _assert_clean(text: str) -> None:
    for literal in SECRETS:
        assert literal not in text, literal
    assert "***" in text


class TestRenderRedaction:
    """Everything published to a thread is scrubbed at the render seam (#403 t6)."""

    def test_failure_excerpt_is_redacted(self) -> None:
        chunk = output_excerpt("bash", 1, SECRET_TEXT, success=False)
        assert chunk is not None
        _assert_clean(chunk.text)

    def test_success_excerpt_is_redacted(self) -> None:
        chunk = output_excerpt("bash", 0, SECRET_TEXT, success=True, max_lines=5)
        assert chunk is not None
        _assert_clean(chunk.text)

    def test_batcher_line_and_detail_are_redacted(self) -> None:
        b = ToolBatcher(max_lines=8, fail_output_lines=10)
        detail = b.add_end(
            "bash",
            "c1",
            success=False,
            exit_code=1,
            detail=SECRET_TEXT,
            args=f"curl -H '{BEARER}' && {APIKEY} gh auth login --with-token {PAT}",
        )
        assert detail is not None
        _assert_clean(detail.text)
        batch = b.flush()
        assert batch is not None
        _assert_clean(batch.text)

    def test_batcher_running_line_is_redacted(self) -> None:
        b = ToolBatcher(max_lines=8)
        b.add_start("bash", f"gh auth login --with-token {PAT} && {APIKEY} run", "c1")
        chunk = b.flush(final=True)
        assert chunk is not None
        _assert_clean(chunk.text)
        assert "running" in chunk.text

    def test_digest_render_and_detail_are_redacted(self) -> None:
        d = ToolDigest(fail_output_lines=10)
        d.add_start("bash", f"curl -H '{BEARER}' {APIKEY} {PAT}", "c1")
        _assert_clean(d.render())
        chunk = d.add_end("bash", "c1", success=False, exit_code=1, detail=SECRET_TEXT)
        assert chunk is not None
        _assert_clean(chunk.text)

    def test_redaction_is_idempotent_and_preserves_upstream_marker(self) -> None:
        from sbxloop.log import redact_text

        once = redact_text(SECRET_TEXT)
        assert redact_text(once) == once
        chunk = output_excerpt("bash", 1, "token=***REDACTED***", success=False)
        assert chunk is not None
        assert "***REDACTED***" in chunk.text

    def test_ordinary_command_survives_untouched(self) -> None:
        b = ToolBatcher(max_lines=8)
        b.add_end("bash", "c1", success=True, exit_code=0, detail="", args="uv run pytest -q")
        chunk = b.flush()
        assert chunk is not None
        assert "uv run pytest -q" in chunk.text


class TestOldWorkerEventCompatibility:
    """A worker that predates `output_lines`/`duration_ms`/`tool_call_id`
    still renders: the new fields are additive and optional (#403 t7)."""

    def test_batcher_without_any_new_fields(self) -> None:
        b = ToolBatcher(max_lines=8)
        # No tool_call_id, no duration_ms, no output_lines: exactly what an
        # old worker's agent.tool_start/agent.tool_end carry.
        b.add_start("bash", "uv run pytest -q", None)
        assert b.add_end("bash", None, success=True, exit_code=0, detail="") is None
        chunk = b.flush()
        assert chunk is not None
        # One line, command visible, no duration and no crash.
        body = chunk.text.strip("`").strip().splitlines()[1:]
        assert body == ["$ bash  uv run pytest -q  ✓"]

    def test_batcher_old_failure_without_new_fields(self) -> None:
        b = ToolBatcher(max_lines=8, fail_output_lines=4)
        b.add_start("bash", "uv run mypy", None)
        detail = b.add_end("bash", None, success=False, exit_code=1, detail="error: bad type")
        assert detail is not None and "error: bad type" in detail.text
        assert "✗ `bash` failed (exit 1)" in detail.text
        chunk = b.flush()
        assert chunk is not None and "$ bash" in chunk.text and "✗ exit 1" in chunk.text

    def test_digest_without_call_id_or_output_lines(self) -> None:
        d = ToolDigest()
        d.add_start("bash", "ls")
        assert d.add_end("bash", success=True, exit_code=0, detail="ok") is None
        assert d.count == 1
        chunk = d.add_end("bash", success=False, exit_code=1, detail="boom")
        assert chunk is not None and "boom" in chunk.text
        assert "⚙" in d.render()

    def test_rich_form_shows_duration_and_elision(self) -> None:
        b = ToolBatcher(max_lines=8, fail_output_lines=6)
        b.add_start("bash", "uv run pytest -q", "c1")
        detail = b.add_end(
            "bash",
            "c1",
            success=False,
            exit_code=1,
            detail="\n".join(f"L{i}" for i in range(40)),
            duration_ms=1500,
            output_lines=40,
        )
        assert detail is not None and "… 34 lines elided …" in detail.text
        chunk = b.flush()
        assert chunk is not None
        assert "$ bash  uv run pytest -q  ✗ exit 1 · 1.5s" in chunk.text

    @pytest.mark.parametrize("duration", [None, "", "not-a-number"])
    def test_bad_duration_values_are_ignored(self, duration: Any) -> None:
        b = ToolBatcher(max_lines=8)
        b.add_end(
            "bash", "c1", success=True, exit_code=0, detail="", args="ls", duration_ms=duration
        )
        chunk = b.flush()
        assert chunk is not None and "$ bash  ls  ✓" in chunk.text

    def test_excerpt_never_exceeds_discord_limit(self) -> None:
        chunk = output_excerpt(
            "bash", 1, "x" * 200000 + "\n" + "y\n" * 5000, success=False, max_lines=200
        )
        assert chunk is not None and len(chunk.text) <= DISCORD_MAX_MESSAGE


class TestNoFabricatedSpend:
    """#430: the backend's per-turn ``cost`` is a constant of unknown unit
    (15.0 on every turn of run rrhb28j7n), so summing it across a run and
    printing it as ``$`` fabricates a spend figure. Nothing in the summary
    may read it."""

    TURNS = 147
    COST = 15.0

    def _stats(self) -> RunStats:
        stats = RunStats()
        stats.observe(tev(100.0, "run.start", outcome="fix the bug"))
        for i in range(self.TURNS):
            stats.observe(
                tev(
                    101.0 + i,
                    "agent.usage",
                    cost=self.COST,
                    input_tokens=1000,
                    output_tokens=200,
                )
            )
        return stats

    def test_no_cost_attribute_holds_the_sum(self) -> None:
        stats = self._stats()
        total = self.TURNS * self.COST
        for name in ("cost", "total_cost", "spend", "cost_usd", "usd"):
            assert not hasattr(stats, name)
        for value in vars(stats).values():
            assert value != total
            assert value != self.COST

    def test_summary_text_has_no_currency(self) -> None:
        stats = self._stats()
        for state in ("done", "completed", "failed"):
            text = summary_text(stats, state)
            assert "$" not in text
            assert "2205" not in text

    def test_summary_embed_has_no_currency(self) -> None:
        card = summary_embed(self._stats(), RunReport("r1", "completed", "x"), "completed")
        parts = [card.title or "", card.description or "", card.footer or ""]
        for name, value, _inline in card.fields:
            parts += [name, value]
        blob = "\n".join(parts)
        assert "$" not in blob
        assert "2205" not in blob

    def test_token_totals_still_accumulate(self) -> None:
        stats = self._stats()
        assert stats.turns == self.TURNS
        assert stats.input_tokens == 1000 * self.TURNS
        assert stats.output_tokens == 200 * self.TURNS
        assert "147 turn(s)" in summary_text(stats, "done")
        assert "147,000 in / 29,400 out tokens" in summary_text(stats, "done")


class TestStatusLineStages:
    """After the task graph the status line follows the pipeline, not the
    (finished) task roster: a watcher reads *which stage* the run is in."""

    def _built(self) -> StatusLine:
        s = StatusLine()
        s.observe(ev("task.state", task_id="t1", title="Add tests", state="pending", revisions=0))
        s.observe(ev("task.start", task_id="t1", title="Add tests"))
        s.observe(ev("task.end", task_id="t1", title="Add tests", state="done"))
        return s

    def test_stage_progression(self) -> None:
        s = self._built()
        assert s.render() == "⏳ 1 task(s) planned\n✅ 1 done"
        s.observe(ev("run.state", state="gating"))
        assert s.render() == "🚦 gate · running the project's own check\n✅ 1 done"
        s.observe(ev("run.state", state="delivering"))
        assert s.render().startswith("🔀 delivering")
        s.observe(ev("run.state", state="reviewing"))
        assert s.render().startswith("🔍 review round 1")
        s.observe(ev("run.state", state="fixing"))
        s.observe(ev("fix.round", round=1, kind="review", task_id="fix-1", budget="1/3"))
        s.observe(ev("task.state", task_id="fix-1", title="Make it acceptable", state="pending"))
        s.observe(ev("task.start", task_id="fix-1", title="Make it acceptable"))
        s.observe(ev("phase.end", task_id="fix-1", phase="build", status="ok", message="x"))
        assert s.render().startswith("🛠 fix round 1 (review, budget 1/3) · build")
        s.observe(ev("task.end", task_id="fix-1", state="done"))
        s.observe(ev("run.state", state="gating"))
        s.observe(ev("run.state", state="delivering"))
        s.observe(ev("run.state", state="reviewing"))
        assert s.render().startswith("🔍 review round 2")
        s.observe(ev("run.state", state="awaiting_ci"))
        assert s.render().startswith("⏳ CI\n")
        s.observe(ev("ci.status", state="pending", pending=["lint", "test"], failed=[]))
        assert s.render().startswith("⏳ CI · 2 pending")
        s.observe(ev("ci.status", state="green", pending=[], failed=[], total=2))
        assert s.render().startswith("✅ CI green")
        s.observe(ev("run.state", state="landing"))
        assert s.render().startswith("🚀 landing · merging")
        s.observe(ev("land.undraft", pr=9))
        assert s.render().startswith("🚀 landing · out of draft")
        s.observe(ev("land.held_by_draft", pr=9, head="abc"))
        assert s.render().startswith("🚀 landing · held in draft by a person")
        s.observe(ev("land.update", pr=9, attempt=1, accepted=True))
        assert "updating from base (attempt 1)" in s.render()
        s.observe(ev("run.state", state="merged"))
        assert s.render().startswith("🎉 finished · 2/2 tasks done")

    def test_workload_stages(self) -> None:
        """A workload (#755) walks executing → judging → publishing: the
        graph stage clears the line, the two after it name themselves."""
        s = self._built()
        s.observe(ev("run.state", state="executing"))
        assert s.render() == "⏳ 1 task(s) planned\n✅ 1 done"
        s.observe(ev("run.state", state="judging"))
        assert s.render().startswith("⚖️ judging · re-running every task's check")
        s.observe(ev("run.state", state="publishing"))
        assert s.render().startswith("📤 publishing")
        s.observe(ev("run.state", state="completed"))
        assert s.render().startswith("✅ finished · 1/1 tasks done")

    def test_ci_red_and_blocked(self) -> None:
        s = self._built()
        s.observe(ev("run.state", state="awaiting_ci"))
        s.observe(ev("ci.status", state="red", pending=[], failed=["test"]))
        assert s.render().startswith("❌ CI red")
        s.observe(ev("run.state", state="blocked"))
        assert s.render().startswith("🚧 finished")

    def test_stage_events_mark_dirty(self) -> None:
        s = self._built()
        s.render()
        s.observe(ev("run.state", state="reviewing"))
        assert s.dirty


class TestSteerProgressStages:
    def test_wait_stages_answer_at_once(self) -> None:
        p = SteerProgress()
        p.observe(ev("run.state", state="awaiting_ci"))
        assert p.render() == "⏳ steer queued — the run is waiting on CI; answered now"
        p.observe(ev("run.state", state="landing"))
        assert "landing the pull request; answered now" in p.render()

    def test_agent_stages_wait_for_a_checkpoint(self) -> None:
        p = SteerProgress()
        p.observe(ev("run.state", state="reviewing"))
        assert p.render() == (
            "⏳ steer queued — the run is reviewing its own pull request; "
            "answered at the next checkpoint"
        )
        # a fix task in flight is reported like any task, not as a stage
        p.observe(ev("run.state", state="fixing"))
        p.observe(ev("task.start", task_id="fix-1", title="Make it acceptable"))
        assert "agent is on `fix-1`" in p.render()
        p.observe(ev("run.state", state="building"))
        p.observe(ev("task.end", task_id="fix-1", state="done"))
        assert p.render() == "⏳ steer queued; answered at the next checkpoint"


class TestNoUnfurl:
    """Bare URLs get angle brackets; everything already safe is untouched."""

    def test_wraps_bare_urls(self) -> None:
        out = no_unfurl("see https://example.com/x and http://a.b/c now")
        assert out == "see <https://example.com/x> and <http://a.b/c> now"

    def test_is_idempotent(self) -> None:
        once = no_unfurl("run at https://example.com/run/1")
        assert no_unfurl(once) == once

    def test_leaves_bracketed_and_masked_links(self) -> None:
        text = "<https://example.com/a> and [run](https://example.com/b)"
        assert no_unfurl(text) == text

    def test_leaves_inline_code(self) -> None:
        text = "curl `https://example.com/a` please"
        assert no_unfurl(text) == text

    def test_code_spans_are_byte_identical(self) -> None:
        text = (
            "```sh\ncurl -sS https://example.com/install.sh | sh\n```\n"
            "inline `curl https://example.com/a` too"
        )
        assert no_unfurl(text) == text
        assert no_unfurl(no_unfurl(text)) == text

    def test_idempotent_on_bracketed_and_link_targets(self) -> None:
        text = "<https://example.com/a> [run](https://example.com/b) https://example.com/c"
        once = no_unfurl(text)
        assert (
            once == "<https://example.com/a> [run](https://example.com/b) <https://example.com/c>"
        )
        assert no_unfurl(once) == once

    def test_leaves_fenced_code(self) -> None:
        text = "before\n```sh\ncurl https://example.com/a\n```\nafter https://example.com/b"
        assert no_unfurl(text) == (
            "before\n```sh\ncurl https://example.com/a\n```\nafter <https://example.com/b>"
        )


class TestAgentModelLabel:
    def test_known_backends_pair_with_model(self) -> None:
        assert agent_model_label("copilot", "gpt-5") == "copilot · gpt-5"
        assert agent_model_label("claude", "claude-sonnet-5") == "claude · claude-sonnet-5"
        assert agent_model_label("COPILOT", "gpt-5") == "copilot · gpt-5"

    def test_missing_or_unknown_backend_reads_unknown(self) -> None:
        for backend in (None, "", "   ", "wat"):
            assert agent_model_label(backend, "gpt-5") == "unknown · gpt-5"

    def test_missing_model_still_renders(self) -> None:
        assert agent_model_label("copilot", None) == "copilot · unknown"
        assert agent_model_label(None, None) == "unknown · unknown"

    def test_agent_message_without_model_shows_backend(self) -> None:
        chunks = format_for_discord(
            ev("agent.message", content="hi", agent="planner", backend="copilot")
        )
        assert texts(chunks) == ["**planner** · `copilot · unknown`\nhi"]

    def test_agent_message_historical_event_has_no_backend(self) -> None:
        chunks = format_for_discord(ev("agent.message", content="hi", agent="p", model="gpt-5"))
        assert texts(chunks) == ["**p** · `unknown · gpt-5`\nhi"]

    def test_agent_message_without_model_or_backend_renders(self) -> None:
        chunks = format_for_discord(ev("agent.message", content="hi", agent="p"))
        assert texts(chunks) == ["**p**\nhi"]


class TestHeadlineAgentField:
    def test_headline_names_backend_and_model(self) -> None:
        item = WorkItem(item_id="gh:issue:7", source_key="7", title="T")
        card = headline_embed(item, "r1", backend="copilot", model="gpt-5", hostname="h")
        assert ("Agent", "`copilot · gpt-5`", True) in card.fields
        assert "`copilot · gpt-5`" in headline_text(item, "r1", backend="copilot", model="gpt-5")

    def test_headline_unknown_backend(self) -> None:
        item = WorkItem(item_id="gh:issue:7", source_key="7", title="T")
        card = headline_embed(item, "r1", backend="", model="gpt-5", hostname="h")
        assert ("Agent", "`unknown · gpt-5`", True) in card.fields

    def test_headline_without_agent_info_is_unchanged(self) -> None:
        item = WorkItem(item_id="gh:issue:7", source_key="7", title="T")
        card = headline_embed(item, "r1", hostname="h")
        assert not any(name == "Agent" for name, _v, _i in card.fields)
