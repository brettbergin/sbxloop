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
    StatusLine,
    ToolBatcher,
    ToolDigest,
    _clip,
    _fence_state,
    daemon_notice,
    finish_embed,
    finish_text,
    format_for_discord,
    headline_embed,
    headline_text,
    mask_urls,
    queue_lines,
    repetitive_streak,
    split_markdown,
    status_embed,
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
                "breaker_open": False,
                "paused": True,
            }
        )
        assert {n: v for n, v, _ in card.fields} == {
            "Current": "`r1` — Fix",
            "Queued": "2",
            "Runs today": "1/12",
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

    def test_daemon_notice_masks_urls(self) -> None:
        assert mask_urls("PR https://x/pull/9 done") == "PR <https://x/pull/9> done"
        assert (
            mask_urls("already <https://x> and [t](https://y)")
            == "already <https://x> and [t](https://y)"
        )
        assert daemon_notice("✅ gh:8 done · PR https://x/pull/9", thread_id=77) == (
            "✅ gh:8 done · PR <https://x/pull/9> · <#77>"
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
