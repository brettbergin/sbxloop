"""The sink helpers (#759): what each sink carries, composed from the
tasks' persisted outputs, and the path check the artifact copy runs."""

from __future__ import annotations

import pytest

from sbxloop.engine import sinks
from sbxloop.engine.model import Published, TaskNeeds, TaskOutput, TaskRecord, TaskSpec


def record(
    id: str,
    *,
    sink: str | None = None,
    repo: str | None = None,
    output: TaskOutput | None = None,
    state: str = "done",
) -> TaskRecord:
    spec = TaskSpec(
        id=id, title=f"task {id}", description="", needs=TaskNeeds(sink=sink, repo=repo)
    )
    return TaskRecord(spec=spec, state=state, output=output)  # type: ignore[arg-type]


def out(text: str, *files: str, more: int = 0) -> TaskOutput:
    return TaskOutput(
        summary=text.splitlines()[0] if text else "", text=text, files=list(files), more_files=more
    )


class TestRouting:
    def test_chat_is_the_default_and_a_task_without_output_is_not_carried(self) -> None:
        tasks = [
            record("a", output=out("did a")),
            record("b", sink="issue", output=out("did b")),
            record("c", sink="chat"),
        ]
        assert [sinks.sink_of(t) for t in tasks] == ["chat", "issue", "chat"]
        assert [t.spec.id for t in sinks.tasks_for(tasks, "chat")] == ["a"]
        assert [t.spec.id for t in sinks.tasks_for(tasks, "issue")] == ["b"]
        assert sinks.sinks_declared(tasks) == ["chat", "issue"]
        assert sinks.PUBLISH_ORDER[-1] == "chat", "the reply names what the others delivered"


class TestPaths:
    @pytest.mark.parametrize("path", ["a.txt", "sub/deep.txt", "./a.txt", "sub//b"])
    def test_relative_paths_pass_normalised(self, path: str) -> None:
        assert sinks.safe_relative(path) is not None

    @pytest.mark.parametrize("path", ["/etc/passwd", "../up.txt", "sub/../../up", ""])
    def test_climbing_and_absolute_paths_are_refused(self, path: str) -> None:
        assert sinks.safe_relative(path) is None

    def test_declared_files_dedupes_and_names_the_offender(self) -> None:
        carried = [
            record("a", output=out("x", "a.txt", "sub/b.txt")),
            record("b", output=out("y", "a.txt", "c.txt")),
        ]
        assert sinks.declared_files(carried) == ["a.txt", "sub/b.txt", "c.txt"]
        bad = [record("z", output=out("y", "../escape"))]
        with pytest.raises(
            sinks.PublishError, match=r"task z declared an unsafe path '\.\./escape'"
        ):
            sinks.declared_files(bad)


class TestText:
    def test_chat_text_is_the_closing_line_then_each_carried_result(self) -> None:
        tasks = [
            record("a", output=out("wrote a\n\nmore on a", "a.txt")),
            record("b", sink="issue", output=out("wrote b")),
        ]
        text = sinks.chat_text(tasks, "Two things", sinks.tasks_for(tasks, "chat"))
        assert text == (
            "Two things — 2/2 task(s) passed the judge\n"
            "a: wrote a (1 file)\n"
            "b: wrote b\n\n"
            "## a: task a\n\n"
            "wrote a\n\nmore on a\n\n"
            "Files: `a.txt`"
        )

    def test_an_empty_result_and_many_files_are_said_so(self) -> None:
        many = [f"f{i}" for i in range(sinks.MAX_LISTED_FILES)]
        tasks = [record("a", output=out("", *many, more=3))]
        text = sinks.chat_text(tasks, None, tasks)
        assert "(no result reported)" in text
        assert text.endswith("`f19` (+3 more)")

    def test_result_title_prefers_the_plan_and_clips(self) -> None:
        assert sinks.result_title("Weekly digest", "anything") == "Weekly digest"
        assert sinks.result_title(None, "\n\nfirst line\nsecond") == "first line"
        assert sinks.result_title("", "") == "workload result"
        long = "x" * 200
        assert sinks.result_title(long, "") == "x" * 119 + "…"

    def test_issue_body_carries_the_footer_and_clips_to_the_budget(self) -> None:
        tasks = [record("a", output=out("wrote a"))]
        body = sinks.issue_body(tasks, "T", tasks, run_id="r1", outcome="do the thing\n")
        assert body.startswith("T — 1/1 task(s) passed the judge\n")
        assert body.endswith("---\n*sbxloop run `r1`*\n\n**Asked:** do the thing\n")
        huge = [record("a", output=out("z" * (sinks.MAX_ISSUE_BODY_CHARS + 10)))]
        clipped = sinks.issue_body(huge, None, huge, run_id="r1", outcome="o")
        assert len(clipped) <= sinks.MAX_ISSUE_BODY_CHARS
        assert "*(clipped: the full result is on the run's tasks)*" in clipped

    def test_pr_body_leaves_room_for_the_delivery_footer(self) -> None:
        """aw/05b: the pull request's description is the result text alone
        — `deliver_workspace` appends the run's footer — clipped so both
        fit under GitHub's cap."""
        tasks = [record("a", output=out("wrote a"))]
        assert sinks.pr_body(tasks, "T", tasks) == sinks.chat_text(tasks, "T", tasks)
        huge = [record("a", output=out("z" * (sinks.MAX_ISSUE_BODY_CHARS + 10)))]
        body = sinks.pr_body(huge, None, huge)
        assert len(body) <= sinks.MAX_ISSUE_BODY_CHARS - sinks.PR_FOOTER_RESERVE
        assert body.endswith("*(clipped: the full result is on the run's tasks)*")

    def test_repo_of_is_the_one_checkout_the_pr_tasks_declared(self) -> None:
        one = [record("a", sink="pr", repo="o/r", output=out("x"))]
        assert sinks.repo_of(one) == "o/r"
        two = [*one, record("b", sink="pr", repo="o/r", output=out("y"))]
        assert sinks.repo_of(two) == "o/r"
        assert sinks.repo_of([record("c", sink="pr", output=out("z"))]) is None
        assert sinks.repo_of([*one, record("d", sink="pr", repo="o/s", output=out("w"))]) is None

    def test_published_line_per_sink(self) -> None:
        assert (
            sinks.published_line(Published(sink="artifact", location="/x/artifacts", files=1))
            == "1 file delivered to /x/artifacts"
        )
        assert (
            sinks.published_line(Published(sink="issue", location="https://gh/i/1", files=2))
            == "result filed as https://gh/i/1"
        )
        assert sinks.published_line(Published(sink="chat", location="chat")) == (
            "result posted to chat"
        )

    def test_result_label_carries_the_descriptor(self) -> None:
        label = sinks.result_label("sbxloop:result")
        assert (label.name, label.color) == ("sbxloop:result", "6f42c1")
