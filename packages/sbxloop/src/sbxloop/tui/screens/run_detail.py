"""One run: the header card, then Thread / Tasks / Phases / Landing /
Artifacts / Events, each fed from the store's read-only handle off the
UI thread."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Input, TabbedContent, TabPane, Tree
from textual.worker import get_current_worker

from sbxloop.cli.tui import TASK_STATE_STYLES
from sbxloop.config import TUI_CONTROL_CHANNEL
from sbxloop.daemon.usage import usage_lines
from sbxloop.engine.model import TERMINAL_RUN_STATES, artifacts_dir, scan_artifacts
from sbxloop.sbx.provision import sandbox_name
from sbxloop.tui import actions
from sbxloop.tui.data import ConsoleState, EventTail, RunDetail, build_run_detail
from sbxloop.tui.format import (
    SPEND_NOT_REPORTED,
    age,
    clock,
    duration,
    run_title,
    state_label,
    to_rich,
    tokens,
)
from sbxloop.tui.screens.base import ConsoleScreen
from sbxloop.tui.screens.modals import TextPromptScreen
from sbxloop.tui.widgets.chat_input import ChatInput
from sbxloop.tui.widgets.chronology import ChronologyLog
from sbxloop.tui.widgets.panel import TextPanel
from sbxloop.tui.widgets.tables import ConsoleTable
from sbxloop.tui.widgets.thread import ThreadView
from sbxloop_worker.protocol import Event

_TREE_MAX_FILES = 500


class RunDetailScreen(ConsoleScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", "Back"),
        Binding("f", "follow", "Follow events"),
        Binding("slash", "event_filter", "Event type filter"),
        Binding("v", "toggle_view", "Transcript/lines", show=False),
        Binding("r", "reply_or_refresh", "Reply / refresh", show=False),
        Binding("i", "compose", "Type", show=False),
        Binding("c", "cancel", "Cancel"),
        Binding("C", "cancel_retry", "Cancel + retry", show=False),
        Binding("R", "resume", "Resume / retry"),
        Binding("u", "requeue", "Requeue", show=False),
        Binding("A", "abandon", "Abandon", show=False),
        Binding("m", "merge", "Approve merge", show=False),
        Binding("w", "resume_review", "Check review now", show=False),
        Binding("plus", "grant_rounds", "Grant rounds", show=False),
        Binding("s", "shell_agent", "Shell (agent)"),
        Binding("S", "shell_github", "Shell (github)", show=False),
    ]
    DEFAULT_CSS = """
    RunDetailScreen #header { height: auto; max-height: 8; padding: 0 1; border: round $primary; }
    RunDetailScreen TabbedContent { height: 1fr; }
    RunDetailScreen ChronologyLog { height: 1fr; }
    RunDetailScreen #thread-box { height: 1fr; display: none; }
    """

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id
        self.detail: RunDetail | None = None
        self.follow = True
        self._thread_tail: EventTail | None = None
        self._events_tail: EventTail | None = None
        self._artifacts_seen: tuple[Path, ...] | None = None
        # The artifacts scan walks a workspace and may fork git: once on
        # mount, on `r`, and when the tab is opened — not every tick.
        self._scan_artifacts_next = True
        # The run's local-bridge thread, once the run has one; until then
        # the tab shows the chronology derived from the persisted events.
        self._thread_view: ThreadView | None = None
        # Events that land while follow is off wait for it.
        self._held: list[tuple[int, Event]] = []
        # Bumped by every reset of a tail: a load still in flight from
        # before the reset lands with the old number and is dropped, so
        # the reset view never loses rows to a stale delta.
        self._generation = 0

    def compose(self) -> ComposeResult:
        yield from self.compose_frame()
        with Vertical(id="body"):
            yield TextPanel(Text(f"run {self.run_id}", style="bold cyan"), id="header")
            with TabbedContent(id="tabs"):
                with TabPane("Thread", id="thread"):
                    yield Vertical(id="thread-box")
                    yield ChronologyLog("transcript", id="thread-log", max_lines=4000)
                with TabPane("Tasks", id="tasks"):
                    yield ConsoleTable(
                        "task",
                        "title",
                        "state",
                        "rev/replan",
                        "suspect",
                        "last feedback",
                        id="tasks-table",
                    )
                with TabPane("Phases", id="phases"):
                    yield ConsoleTable(
                        "task",
                        "phase",
                        "attempt",
                        "status",
                        "started",
                        "duration",
                        "in",
                        "out",
                        "cache r/w",
                        "turns",
                        id="phases-table",
                    )
                    yield TextPanel(id="usage")
                with TabPane("Landing", id="landing"):
                    yield TextPanel(id="landing-body")
                with TabPane("Artifacts", id="artifacts"):
                    yield Tree("artifacts", id="artifacts-tree")
                with TabPane("Events", id="events"):
                    yield Input(placeholder="event type prefix, e.g. policy.", id="event-filter")
                    yield ChronologyLog("lines", id="events-log", max_lines=4000)
        yield from self.compose_footer()

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#event-filter", Input).display = False
        self.query_one("#header", TextPanel).border_title = self.run_id
        mailbox = self.console_app.mailbox
        self._thread_tail = EventTail(mailbox, self.run_id)
        self._events_tail = EventTail(mailbox, self.run_id)
        self.load()

    # -- data ------------------------------------------------------------------

    def refresh_data(self, state: ConsoleState) -> None:
        super().refresh_data(state)
        self.load()

    @work(thread=True, exclusive=True, group="run-detail")
    def load(self) -> None:
        mailbox = self.console_app.mailbox
        generation = self._generation
        scan = self._scan_artifacts_next
        self._scan_artifacts_next = False
        try:
            detail = build_run_detail(mailbox, self.run_id, previous=self.detail)
            thread = (
                self._thread_tail.pull() if self._thread_tail and self._thread_view is None else []
            )
            events = self._events_tail.pull() if self._events_tail else []
            artifacts = self._scan_artifacts(detail) if scan else None
        except Exception as exc:
            self._scan_artifacts_next = self._scan_artifacts_next or scan
            self.app.call_from_thread(self.app.notify, f"read failed: {exc}", severity="error")
            return
        # A superseded or shut-down worker keeps running to here (a thread
        # cannot be cancelled); its rows are not applied.
        if get_current_worker().is_cancelled:
            return
        self.app.call_from_thread(self._apply, detail, thread, events, artifacts, generation)

    def _scan_artifacts(self, detail: RunDetail | None) -> tuple[Path | None, list[Path], str]:
        if detail is None:
            return None, [], ""
        config = self.console_app.config
        target = artifacts_dir(detail.record, config.state_dir)
        if target is None or not target.is_dir():
            return target, [], ""
        scan = scan_artifacts(target, config.artifacts.exclude)
        return target, list(scan.files), scan.excluded_note or ""

    def _apply(
        self,
        detail: RunDetail | None,
        thread: list[tuple[int, Event]],
        events: list[tuple[int, Event]],
        artifacts: tuple[Path | None, list[Path], str] | None,
        generation: int = -1,
    ) -> None:
        if generation != -1 and generation != self._generation:
            return  # a load from before a reset: its rows belong to the old view
        try:
            header = self.query_one("#header", TextPanel)
        except NoMatches:
            return  # the screen was closed while the load ran
        if self._thread_tail is not None:
            self._thread_tail.commit(thread)
        if self._events_tail is not None:
            self._events_tail.commit(events)
        if detail is None:
            header.update(Text(f"run {self.run_id} is not in the store", style="red"))
            return
        self.detail = detail
        header.update(self._header(detail))
        self._thread_tab(detail)
        if self._thread_view is None:
            self.query_one("#thread-log", ChronologyLog).feed(thread)
        # Events that land while follow is off wait, so toggling it back on
        # catches up rather than leaving a gap.
        self._held.extend(events)
        if self.follow:
            self.query_one("#events-log", ChronologyLog).feed(self._held)
            self._held = []
        self._tasks(detail)
        self._phases(detail)
        self.query_one("#landing-body", TextPanel).update(self._landing(detail))
        if artifacts is not None:
            self._artifacts(*artifacts)

    def _thread_tab(self, detail: RunDetail) -> None:
        if self._thread_view is not None:
            self._thread_view.pull()
            return
        # With `thread_per_run = false` the run "thread" is the control
        # channel itself: that is the Chat screen, not this run's thread.
        if detail.thread is None or detail.thread.thread_id == TUI_CONTROL_CHANNEL:
            return
        view = ThreadView(detail.thread.thread_id, thread=True, run_id=self.run_id)
        self._thread_view = view
        self.query_one("#thread-log", ChronologyLog).display = False
        box = self.query_one("#thread-box", Vertical)
        box.display = True
        box.mount(view)

    # -- rendering ---------------------------------------------------------------

    def _header(self, detail: RunDetail) -> Text:
        r = detail.record
        emoji = self.console_app.emoji
        text = Text()
        text.append(r.run_id, style="bold cyan")
        if detail.item is not None:
            text.append(f" · {detail.item.item_id}", style="dim")
            if detail.item.repo:
                text.append(f" · {detail.item.repo}", style="dim")
        text.append(" · ")
        text.append_text(state_label(r.state, r.reason, emoji=emoji))
        if r.stage:
            text.append(f" · stage {r.stage}", style="dim")
        text.append("\n")
        text.append(run_title(r))
        text.append("\n")
        bits = []
        if r.pr_number:
            bits.append(f"PR #{r.pr_number}")
        if r.branch:
            bits.append(r.branch)
        if r.head_sha:
            bits.append(r.head_sha[:8])
        bits.append(f"rounds review {r.review_rounds} · ci {r.ci_rounds}")
        if r.exhausted:
            bits.append(f"exhausted: {r.exhausted} (+{r.granted_rounds} granted)")
        done = sum(1 for t in detail.tasks if t.state == "done")
        bits.append(f"tasks {done}/{len(detail.tasks)}")
        bits.append(f"last event {age(detail.last_event_ts or r.updated_at)}")
        text.append(" · ".join(bits), style="dim")
        if detail.gate is not None:
            mark = "⏸ " if emoji else ""
            text.append(f"\n{mark}ready to merge — waiting for approval", style="bold yellow")
        if detail.hold is not None:
            mark = "👀 " if emoji else ""
            text.append(f"\n{mark}{detail.hold.state}: waiting on a reviewer", style="yellow")
        if not self.console_app.read_only:
            text.append("\n" + " · ".join(self._offers(detail)), style="dim")
        return text

    def _is_current(self, detail: RunDetail) -> bool:
        daemon = self.console_app.state.daemon
        return daemon is not None and daemon.live and daemon.current_run == detail.record.run_id

    def _offers(self, detail: RunDetail) -> list[str]:
        """The verbs that apply to this run, as the header's key hints —
        the resume/cancel table docs/tui.md carries."""
        r = detail.record
        offers: list[str] = []
        if self._is_current(detail):
            offers += ["c cancel", "C cancel+retry"]
        elif r.state not in TERMINAL_RUN_STATES:
            offers.append("c cancel")
        if detail.item is not None:
            if detail.item.state in ("failed", "blocked", "cancelled", "queued"):
                offers.append("R retry")
            if detail.item.state in ("running", "queued"):
                offers.append("u requeue")
            if detail.item.state not in ("done", "failed", "cancelled"):
                offers.append("A abandon")
            if detail.gate is not None:
                offers.append("m approve merge")
            if detail.hold is not None:
                offers.append("w check review now")
            if r.exhausted:
                offers.append("+ grant rounds")
        elif r.state not in TERMINAL_RUN_STATES and not self._is_current(detail):
            offers.append("R resume here")
        offers += ["s shell", "S github shell"]
        return offers

    def _tasks(self, detail: RunDetail) -> None:
        rows = []
        for t in detail.tasks:
            feedback = " ".join((t.last_feedback or "").split())[:80]
            rows.append(
                (
                    t.spec.id,
                    (
                        t.spec.id,
                        t.spec.title[:60],
                        Text(t.state, style=TASK_STATE_STYLES.get(t.state, "")),
                        f"{t.revisions}/{t.replans}",
                        ("⚠" if self.console_app.emoji else "suspect") if t.verify_suspect else "",
                        feedback,
                    ),
                )
            )
        self.query_one("#tasks-table", ConsoleTable).replace_rows(rows)

    def _phases(self, detail: RunDetail) -> None:
        rows = []
        totals = {"in": 0, "out": 0, "cr": 0, "cw": 0, "turns": 0}
        for row in detail.phases:
            started = row["started_at"]
            ended = row["ended_at"]
            for key, col in (
                ("in", "input_tokens"),
                ("out", "output_tokens"),
                ("cr", "cache_read_tokens"),
                ("cw", "cache_write_tokens"),
                ("turns", "turns"),
            ):
                value = row[col]
                if value is not None:
                    totals[key] += int(value)
            rows.append(
                (
                    str(row["id"]),
                    (
                        row["task_id"] or "—",
                        row["phase"],
                        str(row["attempt"]),
                        row["status"],
                        clock(started),
                        duration((ended - started) if started and ended else None),
                        tokens(row["input_tokens"]),
                        tokens(row["output_tokens"]),
                        f"{tokens(row['cache_read_tokens'])}/{tokens(row['cache_write_tokens'])}",
                        str(row["turns"] if row["turns"] is not None else "—"),
                    ),
                )
            )
        self.query_one("#phases-table", ConsoleTable).replace_rows(rows)
        usage = Text()
        usage.append(
            f"{len(detail.phases)} attempt(s) · {totals['turns']} turn(s) · "
            f"{tokens(totals['in'])} in / {tokens(totals['out'])} out · "
            f"cache {tokens(totals['cr'])} read / {tokens(totals['cw'])} write\n",
            style="bold",
        )
        if detail.usage is not None and detail.usage.recorded:
            # The block the concierge's run_usage tool prints, verbatim.
            usage.append(f"models: {detail.usage.model_line}\n", style="dim")
            usage.append("\n".join(usage_lines(detail.usage)))
        else:
            usage.append(SPEND_NOT_REPORTED, style="dim")
        self.query_one("#usage", TextPanel).update(usage)

    def _landing(self, detail: RunDetail) -> Any:
        r = detail.record
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold", no_wrap=True)
        table.add_column()
        table.add_row("PR", f"#{r.pr_number} {r.pr_url or ''}".strip() if r.pr_number else "—")
        table.add_row("title", r.pr_title or "—")
        table.add_row("branch", f"{r.branch or '—'} @ {(r.head_sha or '')[:12] or '—'}")
        table.add_row("stage", r.stage or "—")
        table.add_row(
            "rounds",
            f"review {r.review_rounds} · ci {r.ci_rounds} · update attempts {r.update_attempts}"
            + (f" · exhausted {r.exhausted}, granted {r.granted_rounds}" if r.exhausted else ""),
        )
        table.add_row("last verdict", r.last_verdict or "—")
        if detail.item is not None:
            i = detail.item
            table.add_row(
                "item",
                f"{i.item_id} · {i.state} · attempts {i.attempts}"
                + (f" · last error: {i.last_error[:80]}" if i.last_error else ""),
            )
        if detail.gate is not None:
            g = detail.gate
            table.add_row(
                "merge gate",
                Text(f"{g.state} since {age(g.created_at)} · PR #{g.pr_number}", style="yellow"),
            )
        if detail.hold is not None:
            h = detail.hold
            waiting = (
                "held in draft"
                if h.held_by_draft
                else f"{h.approvals_required} approval(s) required"
            )
            table.add_row(
                "review hold",
                Text(
                    f"{h.state} · {waiting} · polls {h.polls} · next {age(h.next_poll_at)}",
                    style="yellow",
                ),
            )
        for event in detail.landing_events:
            when = clock(event.ts, seconds=False)
            summary = ", ".join(
                f"{k}={v}"
                for k, v in list(event.data.items())[:6]
                if not isinstance(v, list | dict)
            )
            table.add_row(event.type, to_rich(f"{when} {summary}"))
        return table

    def _artifacts(self, target: Path | None, files: list[Path], note: str) -> None:
        seen = tuple(files)
        if seen == self._artifacts_seen:
            return
        self._artifacts_seen = seen
        tree = self.query_one("#artifacts-tree", Tree)
        tree.clear()
        if target is None:
            tree.root.set_label("no artifacts: the run never provisioned a workspace")
            return
        tree.root.set_label(f"{target} ({len(files)} file(s))")
        nodes: dict[Path, Any] = {target: tree.root}
        for path in files[:_TREE_MAX_FILES]:
            rel = path.relative_to(target)
            parent = target
            for part in rel.parts[:-1]:
                child = parent / part
                if child not in nodes:
                    nodes[child] = nodes[parent].add(f"{part}/")
                parent = child
            try:
                size = path.lstat().st_size
            except OSError:
                size = 0
            nodes[parent].add_leaf(f"{rel.name}  ({size} B)")
        if len(files) > _TREE_MAX_FILES:
            tree.root.add_leaf(f"… +{len(files) - _TREE_MAX_FILES} more")
        if note:
            tree.root.add_leaf(note)
        tree.root.expand()

    # -- actions -----------------------------------------------------------------

    # -- run verbs ---------------------------------------------------------------

    def action_cancel(self) -> None:
        detail = self.detail
        if detail is None:
            return
        if detail.record.state in TERMINAL_RUN_STATES and not self._is_current(detail):
            self.app.notify(f"run is already {detail.record.state}", severity="warning")
            return
        self.console_app.perform(
            actions.cancel_run(
                self.console_app.deps, detail.record, current=self._is_current(detail)
            )
        )

    def action_cancel_retry(self) -> None:
        detail = self.detail
        if detail is None:
            return
        if not self._is_current(detail):
            self.app.notify(
                "cancel + retry applies to the daemon's current run", severity="warning"
            )
            return
        self.console_app.perform(
            actions.cancel_run(self.console_app.deps, detail.record, current=True, retry=True)
        )

    def action_resume(self) -> None:
        """An item's run is retried through the daemon; a run with no item
        resumes as a detached process on this host."""
        detail = self.detail
        if detail is None:
            return
        deps = self.console_app.deps
        if detail.item is not None:
            self.console_app.perform(actions.retry(deps, detail.item.item_id))
            return
        if detail.record.state in TERMINAL_RUN_STATES:
            self.app.notify(f"run is {detail.record.state}; nothing to resume", severity="warning")
            return
        self.console_app.perform(actions.resume_run(deps, detail.record.run_id))

    def action_requeue(self) -> None:
        detail = self.detail
        if detail is None or detail.item is None:
            self.app.notify("this run has no work item", severity="warning")
            return
        self.console_app.perform(actions.requeue(self.console_app.deps, detail.item.item_id))

    def action_abandon(self) -> None:
        detail = self.detail
        if detail is None or detail.item is None:
            self.app.notify("this run has no work item", severity="warning")
            return
        self.console_app.perform(actions.abandon(self.console_app.deps, detail.item.item_id))

    def action_merge(self) -> None:
        detail = self.detail
        if detail is None or detail.gate is None:
            self.app.notify("no open merge gate on this run", severity="warning")
            return
        self.console_app.perform(actions.merge(self.console_app.deps, detail.gate.item_id))

    def action_resume_review(self) -> None:
        detail = self.detail
        if detail is None or detail.hold is None:
            self.app.notify("this run is not waiting for a review", severity="warning")
            return
        self.console_app.perform(actions.resume_review(self.console_app.deps, detail.hold.item_id))

    def action_grant_rounds(self) -> None:
        detail = self.detail
        if detail is None:
            return
        run_id = detail.record.run_id

        def submitted(value: str | None) -> None:
            if not value:
                return
            if not value.isdigit() or int(value) < 1:
                self.app.notify("a whole number of rounds, 1 or more", severity="warning")
                return
            self.console_app.perform(
                actions.grant_rounds(self.console_app.deps, run_id, int(value))
            )

        self.app.push_screen(
            TextPromptScreen(
                "grant rounds", f"How many more fix rounds for {run_id}?", placeholder="2"
            ),
            submitted,
        )

    def _shell(self, role: str) -> None:
        name = sandbox_name(self.run_id, "agent" if role == "agent" else "github")
        self.console_app.perform(actions.shell(self.console_app.deps, name))

    def action_shell_agent(self) -> None:
        self._shell("agent")

    def action_shell_github(self) -> None:
        self._shell("github")

    def action_back(self) -> None:
        """``Esc``: clear an open event filter first; then leave the run."""
        box = self.query_one("#event-filter", Input)
        if box.display:
            box.value = ""
            box.display = False
            self.query_one("#events-log", ChronologyLog).focus()
            return
        self.app.pop_screen()

    def action_refresh_run(self) -> None:
        self._scan_artifacts_next = True
        self.load()
        self.console_app.action_refresh()

    def action_reply_or_refresh(self) -> None:
        if self._thread_view is not None:
            self._thread_view.reply_to_focused()
        else:
            self.action_refresh_run()

    def action_compose(self) -> None:
        if self._thread_view is not None:
            self._thread_view.query_one(ChatInput).focus()

    def action_follow(self) -> None:
        self.follow = not self.follow
        self.app.notify(f"events follow {'on' if self.follow else 'off'}")
        if self.follow and self._held:
            self.query_one("#events-log", ChronologyLog).feed(self._held)
            self._held = []

    def action_toggle_view(self) -> None:
        if self._thread_view is not None:
            return  # the tab shows the run's thread; nothing to re-render
        log = self.query_one("#thread-log", ChronologyLog)
        log.reset("lines" if log.view == "transcript" else "transcript")
        if self._thread_tail:
            self._thread_tail.reset()
        self._generation += 1
        self.load()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "artifacts":
            self._scan_artifacts_next = True
            self.load()

    def action_event_filter(self) -> None:
        box = self.query_one("#event-filter", Input)
        box.display = True
        box.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "event-filter":
            return
        prefix = event.value.strip() or None
        log = self.query_one("#events-log", ChronologyLog)
        log.reset()
        self._held = []
        if self._events_tail:
            self._events_tail.reset(type_prefix=prefix)
        self._generation += 1
        event.input.display = False
        self.load()
