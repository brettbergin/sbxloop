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
    EmbedSpec,
    RunStats,
    StatusLine,
    SteerProgress,
    ToolBatcher,
    ToolDigest,
    _clip,
    _fence_state,
    charter_skipped_notice,
    daemon_notice,
    filed_lines,
    filed_notice,
    findings_summary,
    finish_embed,
    finish_text,
    format_for_discord,
    headline_embed,
    headline_text,
    issue_url,
    mask_urls,
    plan_text,
    queue_lines,
    ref_link,
    refs_text,
    repetitive_streak,
    roster_text,
    split_markdown,
    status_embed,
    strip_json_payload,
    summary_embed,
    summary_text,
    verdict_text,
)
from sbxloop.daemon.model import RunReport, WorkItem
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

    def test_plan_numbers_its_steps_and_names_its_promises(self) -> None:
        text = plan_text(
            {
                "task_id": "t2",
                "attempt": 1,
                "steps": ["write it", "test it"],
                "expected_artifacts": ["out.txt"],
                "verify_commands": ["uv run pytest -q"],
                "egress": [{"domain": "pypi.org", "reason": "install deps"}],
            }
        )
        assert text.splitlines() == [
            "🗺 **plan** · task `t2`",
            "1. write it",
            "2. test it",
            "**expects:** `out.txt`",
            "**verify:** `uv run pytest -q`",
            "**egress:** `pypi.org` — install deps",
        ]

    def test_replan_says_which_attempt_it_is(self) -> None:
        text = plan_text({"task_id": "t2", "attempt": 3, "steps": ["try again"]})
        assert text.splitlines() == ["🗺 **plan** · task `t2` *(replan 3)*", "1. try again"]

    def test_verdict_carries_issues_and_the_feedback_verbatim(self) -> None:
        text = verdict_text(
            {
                "task_id": "t1",
                "phase": "scrutinize",
                "verdict": "revise",
                "issues": [
                    {"severity": "high", "detail": "quoted fields are ignored"},
                    {"severity": "low", "detail": "no empty-file test"},
                ],
                "feedback": "Handle quotes.\nThen add the test.",
            }
        )
        assert text.splitlines() == [
            "♻ **scrutinize: revise** · task `t1`",
            "🔴 **high** — quoted fields are ignored",
            "⚪ **low** — no empty-file test",
            "> Handle quotes.",
            "> Then add the test.",
        ]

    def test_a_clean_verdict_is_one_line(self) -> None:
        assert verdict_text(
            {
                "task_id": "t1",
                "phase": "validate",
                "verdict": "accept",
                "issues": [],
                "feedback": "",
            }
        ) == ("✅ **validate: accept** · task `t1`")

    def test_missing_and_misshapen_fields_do_not_break_a_card(self) -> None:
        # Event data is agent-shaped: a renderer must not assume a list.
        assert roster_text({}).splitlines() == ["🧩 **0 task(s)**"]
        assert roster_text({"tasks": "nope"}).splitlines() == ["🧩 **0 task(s)**"]
        assert plan_text({"task_id": "t1", "steps": []}).splitlines() == [
            "🗺 **plan** · task `t1`",
            "· (no steps)",
        ]
        assert verdict_text({}).splitlines() == ["🔎 **critic: ** · task ``"]

    def test_events_render_at_every_level(self) -> None:
        for level in ("quiet", "normal", "verbose"):
            roster = format_for_discord(
                ev("run.tasks", tasks=[{"id": "t1", "title": "T", "state": "pending"}]),
                level=level,
            )
            assert texts(roster) == ["🧩 **1 task(s)**\n1. `t1` T"]
            assert roster[0].kind == "block"
            plan = format_for_discord(ev("phase.plan", task_id="t1", steps=["go"]), level=level)
            assert texts(plan) == ["🗺 **plan** · task `t1`\n1. go"]
            verdict = format_for_discord(
                ev("phase.verdict", task_id="t1", phase="validate", verdict="reject"), level=level
            )
            assert texts(verdict) == ["❌ **validate: reject** · task `t1`"]

    def test_a_long_plan_is_split_not_clipped(self) -> None:
        chunks = format_for_discord(
            ev("phase.plan", task_id="t1", steps=[f"step {i} " + "x" * 120 for i in range(40)]),
            max_chars=400,
        )
        assert len(chunks) > 1
        assert all(len(c.text) <= 400 for c in chunks)
        assert chunks[1].text.startswith(f"🗺 *(cont. 2/{len(chunks)})*")
        assert "step 39" in chunks[-1].text


class TestFormat:
    def test_agent_message_header_and_split(self) -> None:
        chunks = format_for_discord(
            ev("agent.message", content="Hello **there**", agent="planner", model="claude-sonnet-5")
        )
        assert texts(chunks) == ["**planner** · `claude-sonnet-5`\nHello **there**"]
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

    def test_tool_events_are_not_rendered_here(self) -> None:
        # the pump feeds them to ToolBatcher instead
        assert format_for_discord(ev("agent.tool_start", tool="bash", args="ls")) == []
        assert format_for_discord(ev("agent.tool_end", tool="bash", success=False)) == []

    def test_link_carriers(self) -> None:
        assert texts(
            format_for_discord(ev("run.report", repo="o/r", issue=3, url="https://x/3"))
        ) == ["📋 tracking issue [#3](https://x/3)"]
        pr = format_for_discord(ev("run.deliver", repo="o/r", pr=9, url="https://x/pull/9"))
        assert texts(pr) == ["🔀 PR [#9 · o/r](https://x/pull/9)"] and pr[0].flush
        assert texts(format_for_discord(ev("run.deliver", repo="o/r", error="409 empty"))) == [
            "⚠ **delivery failed:** 409 empty"
        ]
        assert texts(format_for_discord(ev("run.deliver", repo="o/r", created=True))) == [
            "📦 created repository [o/r](https://github.com/o/r)"
        ]
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
        assert texts(
            format_for_discord(
                ev("phase.end", task_id="t2", phase="critic", status="degraded", message="skipped")
            )
        ) == ["⚠ **critic degraded** · task `t2` — skipped"]
        assert texts(
            format_for_discord(
                ev(
                    "phase.end",
                    task_id="t2",
                    phase="scrutinize",
                    status="verify_suspect",
                    message="wrong od layout",
                )
            )
        ) == ["🔎 **scrutinize suspects the check** · task `t2` — wrong od layout"]
        assert format_for_discord(ev("phase.end", task_id="t2", phase="plan", status="ok")) == []

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
    def test_batch_renders_one_block_and_marks_failures(self) -> None:
        b = ToolBatcher(max_lines=8)
        b.add_start("bash", "ls -la", "c1")
        assert b.add_end("bash", "c1", success=True, exit_code=0, detail="") is None
        b.add_start("bash", "pytest -q", "c2")
        detail = b.add_end("bash", "c2", success=False, exit_code=1, detail="FAILED a\n1 failed\n")
        assert detail is not None
        assert detail.text == "✗ `bash` failed (exit 1)\n```text\nFAILED a\n1 failed\n```"
        assert b.flush().text == "```text\n$ bash  ls -la\n$ bash  pytest -q   ✗ exit 1\n```"  # type: ignore[union-attr]
        assert b.flush() is None

    def test_full_and_quiet(self) -> None:
        b = ToolBatcher(max_lines=2)
        b.add_start("read_file", "a.py", "c1")
        assert not b.full
        b.add_start("read_file", "b.py", "c2")
        assert b.full and len(b) == 2
        q = ToolBatcher(quiet=True)
        q.add_start("bash", "ls", "c1")
        assert len(q) == 0 and q.flush() is None
        # failures still surface in quiet mode
        assert q.add_end("bash", "c1", success=False, exit_code=2, detail="") is not None

    def test_long_args_are_middle_elided_and_fences_neutralised(self) -> None:
        b = ToolBatcher()
        b.add_start("bash", "echo " + "a" * 500 + " ```", "c1")
        text = b.flush().text  # type: ignore[union-attr]
        assert "…" in text and text.count("```") == 2  # only the block fence itself


class TestStatusLine:
    def test_progression(self) -> None:
        s = StatusLine()
        assert s.render() == "⏳ planning"
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
            "⏳ steer queued — agent is mid-**execute** on `t2` · Wire CLI "
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

    def test_production_event_order_keeps_the_planning_phase(self) -> None:
        # LoopEngine._run_task emits task.state=planning BEFORE task.start
        # (and the persisted phase first on resume); the start must not
        # wipe the phase already observed for the same task.
        p = SteerProgress(cap=40)
        p.observe(ev("task.state", task_id="t1", state="planning", revisions=0))
        p.observe(ev("task.start", task_id="t1", title="Plan it"))
        assert p.render() == (
            "⏳ steer queued — agent is mid-**plan** on `t1` · Plan it; "
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
        p.observe(ev("task.state", task_id="t1", state="planning", revisions=0))
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
        item = WorkItem(
            item_id="gh:4", source="github", source_key="4", title="Fix login", url="https://x/4"
        )
        running = headline_embed(item, "r1", hostname="db")
        assert running.title == "Fix login" and running.url == "https://x/4"
        assert running.color == COLOR_RUNNING and running.footer == "sbxloop · db"
        assert {n: v for n, v, _ in running.fields} == {
            "Source": "[issue #4](https://x/4)",
            "Run": "`r1`",
            "State": "running",
        }
        done = headline_embed(
            item,
            "r1",
            "completed",
            branch="sbxloop/r1",
            tracking=(12, "https://x/12"),
            pr=(34, "https://x/pull/34"),
            summary="3/3 tasks done",
            hostname="db",
        )
        names = [n for n, _, _ in done.fields]
        assert names == ["Source", "Run", "State", "Branch", "Tracking issue", "PR", "Tasks"]
        assert done.color == COLOR_OK
        assert headline_embed(item, "r1", "failed", hostname="db").color == COLOR_FAIL
        assert headline_embed(item, "r1", "delivery_failed", hostname="db").color == COLOR_WARN
        inbox = WorkItem(item_id="inbox:a.md", source="inbox", source_key="a.md", title="A")
        assert headline_text(inbox, "r2") == "▶ run `r2` — **A** · inbox `a.md`"

    def test_finish_card_and_text(self) -> None:
        item = WorkItem(item_id="gh:4", source="github", source_key="4", title="Fix login")
        report = RunReport(
            "r1",
            "completed",
            "3/3 tasks done",
            tracking_issue=(12, "https://x/12"),
            delivery=(34, "https://x/pull/34"),
        )
        assert finish_text("completed", report) == "**finished: completed** — 3/3 tasks done"
        card = finish_embed(item, report, "completed", unanswered=1)
        assert card.title == "✅ finished: completed" and card.description == "3/3 tasks done"
        assert [n for n, _, _ in card.fields] == ["Tracking issue", "PR", "Steering"]
        failed = finish_embed(
            item, RunReport("r1", "completed", "x", delivery_error="409"), "delivery_failed"
        )
        assert failed.color == COLOR_WARN and failed.fields[0][1].startswith("⚠ 409")

    def test_finish_card_shows_what_the_run_filed(self) -> None:
        """An audit's deliverable is its findings: they belong on the card
        next to the PR, linked, with upstream/noted ones told apart."""
        audit = WorkItem(item_id="gh:9", source="github", source_key="9", title="A", kind="audit")
        report = RunReport(
            "r1",
            "completed",
            "2/2 tasks done",
            filed=("gh:12", "gh:13"),
            tool_filed=("brettbergin/sbxloop#5",),
            tool_noted=("X",),
        )
        card = finish_embed(audit, report, "completed", repo="o/r")
        assert {n: v for n, v, _ in card.fields} == {
            "Filed": "[#12](https://github.com/o/r/issues/12), [#13](https://github.com/o/r/issues/13)",
            "Upstream": "[brettbergin/sbxloop#5](https://github.com/brettbergin/sbxloop/issues/5)",
            "Noted": (
                "1 finding(s) about sbxloop noted, not filed — "
                "set `[daemon] tool_repo` to route them upstream"
            ),
        }
        empty = finish_embed(audit, RunReport("r1", "completed", "x"), "completed", repo="o/r")
        assert empty.fields == (("Filed", "no findings", True),)
        patch = WorkItem(item_id="gh:4", source="github", source_key="4", title="P")
        assert finish_embed(patch, RunReport("r1", "completed", "x"), "completed").fields == ()
        # No repo known (bridge without a github section): plain #n, never a broken link.
        assert finish_embed(patch, report, "completed").fields[0][1] == "#12, #13"

    def test_finish_card_for_operator_cancel_says_who_and_how_to_continue(self) -> None:
        """#246: a cancel is not a failure; the card must name the requester
        and tell the human the run is resumable (or already re-queued)."""
        item = WorkItem(item_id="gh:8", source="github", source_key="8", title="Demo")
        report = RunReport("r1", "cancelled", "1/3 tasks done", cancelled_by="Discord user `b`")
        text = finish_text("cancelled", report)
        assert "cancelled by Discord user `b`" in text and "`sbxloop resume r1`" in text
        card = finish_embed(item, report, "cancelled")
        assert card.title == "⏹ finished: cancelled"
        assert card.fields[0][0] == "Cancelled"
        assert "`sbxloop resume r1`" in card.fields[0][1]
        assert "!sbx retry gh:8" in card.fields[0][1]
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
                source="github",
                source_key=str(i),
                title=f"T{i}",
                url=f"https://x/{i}",
            )
            for i in range(3)
        ]
        assert (
            queue_lines(items, limit=2)
            == "• `gh:0` [T0](https://x/0)\n• `gh:1` [T1](https://x/1)\n… and 1 more"
        )
        audit = WorkItem(
            item_id="gh:9",
            source="github",
            source_key="9",
            title="A",
            url="https://x/9",
            kind="audit",
        )
        assert queue_lines([audit]) == "• `gh:9` 🔎 audit · [A](https://x/9)"

    def test_daemon_notice_masks_urls(self) -> None:
        assert mask_urls("PR https://x/pull/9 done") == "PR <https://x/pull/9> done"
        assert (
            mask_urls("already <https://x> and [t](https://y)")
            == "already <https://x> and [t](https://y)"
        )
        assert daemon_notice("✅ gh:8 done · PR https://x/pull/9", thread_id=77) == (
            "✅ gh:8 done · PR <https://x/pull/9> · <#77>"
        )


def tev(ts: float, type: str, **data: Any) -> Event:
    return Event(ts=ts, run_id="r1", type=type, data=data)


class TestRunSummary:
    def _folded(self) -> RunStats:
        stats = RunStats()
        for event in (
            tev(100.0, "run.start", outcome="fix the bug"),
            tev(101.0, "run.tasks", tasks=[{"id": "t1"}, {"id": "t2"}]),
            tev(110.0, "agent.usage", input_tokens=1000, output_tokens=200, cost=0.10),
            tev(111.0, "agent.tool_start", tool="bash"),
            tev(112.0, "agent.tool_start", tool="bash"),
            tev(120.0, "phase.verdict", task_id="t1", phase="verify", verdict="pass"),
            tev(
                130.0,
                "phase.verdict",
                task_id="t2",
                phase="scrutinize",
                verdict="revise",
                issues=[{"severity": "major", "detail": "tests missing"}],
                feedback="add tests",
            ),
            tev(140.0, "agent.usage", input_tokens=2000, output_tokens=300, cost=0.15),
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
        assert stats.cost == pytest.approx(0.25)
        assert stats.verdict_passes == 1
        assert stats.rework == [("t2", "scrutinize", "revise", "tests missing")]
        assert stats.steers == 1 and stats.steers_answered == 1 and stats.steers_failed == 0
        assert stats.denies == 1
        assert stats.task_counts() == (2, 2)

    def test_summary_text_leads_with_the_headline_numbers(self) -> None:
        stats = self._folded()
        assert summary_text(stats, "completed") == (
            "📊 **run summary** — 2m 00s · 2 turn(s) · 2 tool call(s) · "
            "3,000 in / 500 out tokens · $0.25"
        )
        assert summary_text(None, "completed") == "📊 **run summary**"

    def test_summary_card_stats_and_both_ledgers(self) -> None:
        stats = self._folded()
        report = RunReport("r1", "completed", "2/2 tasks done", delivery=(34, "https://x/pull/34"))
        card = summary_embed(stats, report, "completed")
        assert card.title == "📊 run summary" and card.color == COLOR_OK
        assert card.description == "**completed** — 2/2 tasks done in 2m 00s"
        assert card.footer == "run r1"
        fields = {n: v for n, v, _ in card.fields}
        assert list(fields) == ["Stats", "Went well", "Needed work"]
        assert "turns 2 · tool calls 2" in fields["Stats"]
        assert "tokens 3,000 in / 500 out · cost $0.25" in fields["Stats"]
        assert "steering 1 asked / 1 answered" in fields["Stats"]
        well = fields["Went well"]
        assert "delivered PR [#34](https://x/pull/34)" in well
        assert "all 2 task(s) completed" in well
        assert "answered all 1 steering message(s)" in well
        work = fields["Needed work"]
        assert "• `t2` scrutinize: **revise** — tests missing" in work
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
        report = RunReport("r1", "failed", "0/1 tasks done", delivery_error="409 conflict")
        card = summary_embed(stats, report, "failed", unanswered=1)
        assert "_stats cover the run since the daemon last picked it up_" in (
            card.description or ""
        )
        work = {n: v for n, v, _ in card.fields}["Needed work"]
        assert "1 task(s) failed" in work
        assert "delivery failed — 409 conflict" in work
        assert "1 steering message(s) went unanswered" in work
        assert "1 steer(s) errored" in work
        assert "hit the per-phase tool-call ceiling" in work

    def test_usage_never_reported_is_not_zero(self) -> None:
        stats = RunStats()
        stats.observe(tev(100.0, "agent.usage"))
        assert stats.turns == 1
        assert stats.input_tokens is None and stats.cost is None
        assert summary_text(stats, "completed") == "📊 **run summary** — 0s · 1 turn(s)"
        card = summary_embed(stats, RunReport("r1", "completed", "x"), "completed")
        assert {n: v for n, v, _ in card.fields}["Stats"] == "turns 1"


class TestFiledRefs:
    """Refs the daemon files (``gh:n``, ``owner/name#n``) rendered the way
    every other link in the bridge is: masked, or plain when no URL is known."""

    def test_ref_link_and_refs_text(self) -> None:
        assert issue_url("gh:12", "o/r") == "https://github.com/o/r/issues/12"
        assert issue_url("gh:12") is None
        assert issue_url("o/x#5") == "https://github.com/o/x/issues/5"
        assert issue_url("gh:existing", "o/r") is None
        assert ref_link("gh:12", "o/r") == "[#12](https://github.com/o/r/issues/12)"
        assert ref_link("gh:12") == "#12"
        assert ref_link("o/x#5") == "[o/x#5](https://github.com/o/x/issues/5)"
        assert ref_link("inbox:foo.md") == "`inbox:foo.md`"
        assert ref_link("gh:existing") == "`gh:existing`"
        assert refs_text(["gh:1", "gh:2"], "o/r") == (
            "[#1](https://github.com/o/r/issues/1), [#2](https://github.com/o/r/issues/2)"
        )
        assert refs_text([f"gh:{i}" for i in range(8)], limit=6) == "#0, #1, #2, #3, #4, #5, … +2"

    def test_filed_notice(self) -> None:
        assert filed_notice(
            "audit", "gh:701", repo="o/r", target="charter `flakes`", detail="audit: flakes"
        ) == (
            "🔎 audit [#701](https://github.com/o/r/issues/701) filed for charter `flakes`"
            " · audit: flakes"
        )
        assert filed_notice("post-mortem", "gh:901", target="gh:4", detail="abandoned: boom") == (
            "🔎 post-mortem #901 filed for gh:4 · abandoned: boom"
        )
        assert filed_notice("review", "gh:801") == "🔎 review #801 filed"

    def test_findings_summary_and_filed_lines(self) -> None:
        none = RunReport("r1", "completed", "x")
        assert findings_summary(none) == ""
        assert findings_summary(none, kind="audit") == "no findings"
        assert filed_lines(none) == []
        report = RunReport(
            "r1", "completed", "x", filed=("gh:12",), tool_filed=("o/x#5",), tool_noted=("A", "B")
        )
        assert findings_summary(report, repo="o/r") == (
            "filed [#12](https://github.com/o/r/issues/12)"
            " · upstream [o/x#5](https://github.com/o/x/issues/5)"
            " · noted 2 finding(s) about sbxloop — set `[daemon] tool_repo` to file them upstream"
        )
        assert filed_lines(report, repo="o/r") == [
            "🔎 filed #12 <https://github.com/o/r/issues/12>",
            "🔎 filed upstream o/x#5 <https://github.com/o/x/issues/5>",
            "⚠ 2 finding(s) about sbxloop noted, not filed — "
            "set `[daemon] tool_repo` to route them upstream",
        ]
        assert filed_lines(report._replace(tool_filed=(), tool_noted=())) == ["🔎 filed #12"]
        # Masked links survive the control-channel URL masking.
        assert daemon_notice(findings_summary(report, repo="o/r")) == findings_summary(
            report, repo="o/r"
        )

    def test_charter_skipped_notice(self) -> None:
        assert charter_skipped_notice(
            "a/bad.md: charter body is empty", ".github/sbxloop/audits"
        ) == (
            "⚠ audit charter skipped: a/bad.md: charter body is empty"
            " · fix or remove it under `.github/sbxloop/audits`"
        )


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
