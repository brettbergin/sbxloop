"""Where a workload's result goes (#759): the sinks the publishing stage
writes to, as pure text and path helpers the engine drives.

A task's output goes to the sink its plan declared in ``needs.sink``;
``chat`` — a reply where the run was asked for, the terminal for a CLI
run — is the default and always granted, the others (``issue``,
``artifact``, ``pr``) only under a profile that names them (#758). The
engine's :meth:`LoopEngine._stage_publish` calls these to compose what
each sink carries; nothing here touches a sandbox or GitHub.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath

from sbxloop.config import SinkName
from sbxloop.engine.model import Published, TaskOutput, TaskRecord, workload_summary
from sbxloop.gh.labels import LabelSpec

# The sink a task publishes to when its plan named none.
DEFAULT_SINK: SinkName = "chat"
# The order the publishing stage works the sinks: the durable ones first,
# the chat reply last so it can name what they delivered.
PUBLISH_ORDER: tuple[SinkName, ...] = ("artifact", "pr", "issue", "chat")

# The label a result issue carries, so a repository can tell the loop's
# results from its work queue (`[workload] result_label`).
RESULT_LABEL_DESCRIPTOR = ("6f42c1", "a result an sbxloop workload delivered")

# GitHub caps an issue or pull request body at 65536 characters; the
# closing note and the footer around the result must fit under it.
MAX_ISSUE_BODY_CHARS = 60_000
# What `deliver.deliver_workspace` appends under a body it is handed.
PR_FOOTER_RESERVE = 200
# How many of a task's files the text names before counting the rest.
MAX_LISTED_FILES = 20


class PublishError(Exception):
    """A result the sink cannot take as declared — an unsafe path, a task
    row the stage cannot read. The run fails at publishing, named."""


def result_label(name: str) -> LabelSpec:
    return LabelSpec(name, *RESULT_LABEL_DESCRIPTOR)


def sink_of(task: TaskRecord) -> str:
    """The sink a task's output goes to: the one its plan declared, else
    the default."""
    return task.spec.needs.sink or DEFAULT_SINK


def tasks_for(tasks: Sequence[TaskRecord], sink: str) -> list[TaskRecord]:
    """The tasks whose output the sink carries: those that declared it
    (or none, for the default sink) and produced an output."""
    return [t for t in tasks if sink_of(t) == sink and t.output is not None]


def sinks_declared(tasks: Sequence[TaskRecord]) -> list[str]:
    """Every sink the plan's tasks publish to, first declaration first —
    the default sink included for a task that named none."""
    return list(dict.fromkeys(sink_of(t) for t in tasks))


def safe_relative(path: str) -> PurePosixPath | None:
    """A task-declared data-directory path as something the host may copy:
    relative, normalised, never climbing out. None refuses it. The lists
    come from the engine's own ``find`` (#757), so a refusal here is a
    corrupted row rather than a hostile operator — but the copy runs on the
    host, and the check costs nothing."""
    pure = PurePosixPath(path)
    if pure.is_absolute() or not pure.parts or any(part in ("..", "") for part in pure.parts):
        return None
    return pure


def declared_files(carried: Sequence[TaskRecord]) -> list[str]:
    """The paths the artifact sink copies: every file the carried tasks
    declared, each checked by :func:`safe_relative`, in declaration order
    without repeats. Raises :class:`PublishError` on the first unsafe one."""
    files: list[str] = []
    for task in carried:
        assert task.output is not None
        for name in task.output.files:
            rel = safe_relative(name)
            if rel is None:
                raise PublishError(f"task {task.spec.id} declared an unsafe path {name!r}")
            files.append(str(rel))
    return list(dict.fromkeys(files))


def published_line(entry: Published) -> str:
    """One line on where a result landed, for the record and the thread."""
    count = f"{entry.files} file{'s' if entry.files != 1 else ''}"
    if entry.sink == "artifact":
        return f"{count} delivered to {entry.location}"
    if entry.sink == "issue":
        return f"result filed as {entry.location}"
    if entry.sink == "pr":
        return f"result delivered as {entry.location}"
    return f"result posted to {entry.sink}"


def _files_line(output: TaskOutput) -> str:
    if not output.file_count:
        return ""
    listed = ", ".join(f"`{f}`" for f in output.files[:MAX_LISTED_FILES])
    rest = output.file_count - min(len(output.files), MAX_LISTED_FILES)
    if rest:
        listed += f" (+{rest} more)"
    return f"Files: {listed}"


def _task_section(task: TaskRecord) -> str:
    assert task.output is not None
    head = f"## {task.spec.id}: {task.spec.title}"
    body = task.output.text.strip() or "(no result reported)"
    files = _files_line(task.output)
    return "\n\n".join(part for part in (head, body, files) if part)


def chat_text(tasks: Sequence[TaskRecord], title: str | None, carried: Sequence[TaskRecord]) -> str:
    """What the chat sink posts: the run's closing line over every task,
    then each carried task's own result text."""
    parts = [workload_summary(tasks, title)]
    parts.extend(_task_section(t) for t in carried)
    return "\n\n".join(parts)


def repo_of(carried: Sequence[TaskRecord]) -> str | None:
    """The one repository the pr sink delivers to: the checkout the
    carried tasks declared in ``needs.repo``. None when they named none
    or more than one — the grant refuses a pr task without a repo and a
    run's `[github]` is narrowed to one repository, so publishing never
    sees either, but the sink checks rather than assumes."""
    repos = list(dict.fromkeys(t.spec.needs.repo for t in carried if t.spec.needs.repo))
    return repos[0] if len(repos) == 1 else None


def clipped(text: str, reserve: int) -> str:
    """``text`` under GitHub's body cap with ``reserve`` characters left
    for what follows it, the cut marked."""
    note = "\n\n*(clipped: the full result is on the run's tasks)*"
    budget = MAX_ISSUE_BODY_CHARS - reserve - len(note)
    if len(text) <= budget:
        return text
    return text[:budget].rstrip() + note


def pr_body(tasks: Sequence[TaskRecord], title: str | None, carried: Sequence[TaskRecord]) -> str:
    """The result pull request's description: the closing line and each
    carried task's result — delivery adds the run's own footer."""
    return clipped(chat_text(tasks, title, carried), PR_FOOTER_RESERVE)


def result_title(title: str | None, outcome: str) -> str:
    """The result issue's or pull request's title: the plan's own line,
    else the outcome's first line, clipped the way GitHub would wrap it
    anyway."""
    line = (title or "").strip() or next(
        (ln.strip() for ln in outcome.splitlines() if ln.strip()), "workload result"
    )
    return line if len(line) <= 120 else line[:119] + "…"


def issue_body(
    tasks: Sequence[TaskRecord],
    title: str | None,
    carried: Sequence[TaskRecord],
    *,
    run_id: str,
    outcome: str,
) -> str:
    """The result issue's body: the closing line, each carried task's
    result, and a footer naming the run and what it was asked."""
    footer = f"---\n*sbxloop run `{run_id}`*\n\n**Asked:** {outcome.strip()}"
    text = clipped(chat_text(tasks, title, carried), len(footer) + 3)
    return f"{text}\n\n{footer}\n"
