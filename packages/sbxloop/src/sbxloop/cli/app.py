"""The sbxloop CLI: run agentic loops on Docker Sandboxes."""

from __future__ import annotations

import contextlib
import json
import math
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Annotated, Any, NoReturn, cast, get_args

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.live import Live
from rich.markup import escape as rich_escape
from rich.table import Table
from rich.tree import Tree

import sbxloop
from sbxloop.cli.doctor import run_doctor
from sbxloop.cli.tui import ChatInput, Dashboard, format_event, plain_printer, render_event
from sbxloop.config import (
    Config,
    DaemonConfig,
    DiscordConfig,
    GithubConfig,
    RepoConfig,
    load_config,
    load_config_with_sources,
    load_dotenv_file,
)
from sbxloop.daemon.control import DEFAULT_TIMEOUT_S
from sbxloop.daemon.store import DaemonStore
from sbxloop.daemon.versions import VersionProbe, start_drift_check
from sbxloop.engine.engine import LoopEngine
from sbxloop.engine.model import TERMINAL_RUN_STATES, RunResult, artifacts_dir, scan_artifacts
from sbxloop.engine.store import StateStore
from sbxloop.errors import SbxloopError
from sbxloop.events import Event, EventBus, HostEventTypes
from sbxloop.gc import DAY_S, format_bytes, prune_run_dirs
from sbxloop.ghids import normalize_item_id, try_parse_gh_id
from sbxloop.log import configure_logging, get_logger
from sbxloop.sbx.bake import DEFAULT_TEMPLATE_REF, bake_template
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxRole
from sbxloop.sbx.pair import cleanup_registry
from sbxloop.sbx.provision import sandbox_name
from sbxloop.sbx.prune import (
    classify_sandboxes,
    format_age,
    remove_run_sandbox,
    remove_sandbox,
)
from sbxloop.sbx.secretstate import (
    COPILOT_TOKEN_ENV,
    SANDBOX_SCOPE_PREFIX,
    assess,
    inspect_custom_secret,
    removal_ladder,
    replace_registration,
    tracked_custom_secrets,
    verify_secret_visibility,
)

app = typer.Typer(
    name="sbxloop",
    help="Agentic loop orchestration on Docker Sandboxes with isolated credentials.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
sandbox_app = typer.Typer(help="Manage sbxloop sandboxes.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect configuration.", no_args_is_help=True)
secrets_app = typer.Typer(
    help="Manage the sbx custom-secret registrations sbxloop owns.", no_args_is_help=True
)
# `sbxloop daemon` runs the loop; `sbxloop daemon items|abandon|retry|requeue`
# are the operator's item controls (#229) and `sbxloop daemon ctl` drives a
# running daemon (#232), so the group's callback IS the daemon and only
# defers when a subcommand was named.
daemon_app = typer.Typer(invoke_without_command=True)
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(config_app, name="config")
app.add_typer(secrets_app, name="secrets")
app.add_typer(daemon_app, name="daemon")

console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"sbxloop {sbxloop.__version__}")
        raise typer.Exit()


@app.callback()
def _main_callback(
    version: Annotated[
        bool, typer.Option("--version", callback=_version_callback, is_eager=True)
    ] = False,
) -> None:
    """sbxloop — agentic loops on Docker Sandboxes."""
    # Library warnings (provisioning rollbacks, worker install fallbacks)
    # render through the same pipeline as the daemon's instead of falling
    # through to logging's bare last-resort handler. The daemon re-configures
    # with its own level/format once its config is loaded.
    configure_logging("WARNING")
    # Every command sees ./.env (tokens + SBXLOOP_* settings); real
    # environment variables always take precedence.
    load_dotenv_file()


def _config_with_overrides(**overrides: Any) -> Config:
    config = load_config()
    updates = {k: v for k, v in overrides.items() if v is not None}
    return config.model_copy(update=updates) if updates else config


def _store(config: Config) -> StateStore:
    return StateStore(config.state_dir / "state.db")


def _resolve_repo(config: Config, selector: str | None) -> RepoConfig:
    """The repository a command acts on.

    ``selector`` may be a full ``owner/name`` or an unambiguous bare name;
    with no selector the sole configured repository is used. Anything else —
    no repository at all, an unknown selector, or several configured repos
    with nothing to choose between them — is a clear CLI error rather than a
    silent pick of the first entry.
    """
    entries = config.github.repo_list()
    if not entries:
        console.print(
            "[bold red]no GitHub repository is configured.[/] Add "
            '[cyan]\\[github] repo = "owner/name"[/] or a [cyan]\\[\\[github.repos]][/] '
            "entry to sbxloop.toml, or pass [cyan]--repo owner/name[/]."
        )
        raise typer.Exit(2)
    entry = config.github.find_repo(selector)
    if entry is not None:
        return entry
    known = ", ".join(r.repo for r in entries)
    if selector is None:
        console.print(
            "[bold red]several repositories are configured[/] — pass "
            f"[cyan]--repo owner/name[/] to choose one of: {known}"
        )
    else:
        console.print(f"[bold red]unknown repository[/] {selector!r} — configured: {known}")
    raise typer.Exit(2)


def _run_repo(store: StateStore, run_id: str) -> str:
    """The repository a run targeted, from the config persisted at its
    creation. Empty when the run predates config persistence or had no
    GitHub repository (a local, GitHub-less run)."""
    try:
        raw = store.get_run_config(run_id)
    except SbxloopError:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    github = data.get("github") if isinstance(data, dict) else None
    if not isinstance(github, dict):
        return ""
    return str(github.get("repo") or "")


def _item_repo(item: Any) -> str:
    """The repository a work item came from: its recorded repo, else the
    repo its id is qualified with (legacy ids carry neither)."""
    repo = getattr(item, "repo", None)
    if repo:
        return str(repo)
    parsed = try_parse_gh_id(getattr(item, "item_id", "") or "")
    return parsed.repo or "" if parsed is not None else ""


# How long a Ctrl-C waits for the engine thread to reach a phase boundary
# and unwind before sandbox cleanup proceeds regardless.
_INTERRUPT_JOIN_S = 10.0


def _exit_interrupted(run_id: str | None) -> NoReturn:
    """Finish a Ctrl+C cleanly: exit 130 with a resume hint, no traceback.

    Runs after the engine was quiesced; the registry's signal handler
    normally tore the sandboxes down already, and the ``cleanup_all`` here
    covers environments where the handlers could not install. The run's
    persisted state is untouched (interrupted states are all resumable),
    so `sbxloop resume` picks the run back up with fresh sandboxes. A
    second Ctrl+C during teardown force-quits and leaves any remaining
    sandboxes to `sbxloop sandbox prune`.
    """
    console.print("\n[bold yellow]interrupted[/] — removing sandboxes (Ctrl+C again to force quit)")
    try:
        cleanup_registry.cleanup_all()
    except KeyboardInterrupt:
        console.print(
            "[bold red]force quit[/] — sandboxes may be left behind; "
            "clean up with [cyan]sbxloop sandbox prune --force[/]"
        )
        # atexit would re-enter the same blocking cleanup; skip it.
        os._exit(130)
    if run_id is not None:
        console.print(
            f"run [bold cyan]{run_id}[/] interrupted — resume with [cyan]sbxloop resume {run_id}[/]"
        )
    raise typer.Exit(130)


def _drive_with_ui(engine: LoopEngine, *, tui: bool, chat: bool = True, action: Any) -> RunResult:
    """Run start/resume with the scrollback transcript + pinned status, or
    plain event logs (--no-tui).

    Transcript entries print permanently to the terminal's scrollback (via
    ``live.console.print``, which renders above the live region), so the
    full conversation history survives; only the compact status panel at
    the bottom is redrawn in place. Events arrive on the engine thread but
    every terminal write happens here on the main thread, via a queue —
    ordering stays deterministic and rich's Live never interleaves.

    With ``chat`` on (and stdin a TTY), the user can type messages to the
    running agent: the TUI captures keystrokes in cbreak mode and renders
    the input line inside the pinned panel; --no-tui falls back to plain
    line input. Messages queue on the engine and are absorbed at the next
    phase boundary, where the agent pauses, replies, and applies any course
    change before continuing.

    Ctrl-C/SIGTERM quiesce the engine before sandbox teardown: the
    registry's signal handler (and, when handlers could not install, the
    KeyboardInterrupt path here) signals the engine's cancel flag —
    checked at phase boundaries — and briefly joins the daemon thread, so
    cleanup doesn't race an engine still mid-``sbx exec``. After teardown
    a Ctrl-C exits 130 with a `sbxloop resume` hint instead of a
    traceback; a second Ctrl-C force-quits (see ``_exit_interrupted``).
    """
    # The TUI runs the engine on a background thread, where pair
    # registration cannot install signal handlers — install them here,
    # on the main thread, so SIGTERM/SIGINT still clean up the sandboxes.
    cleanup_registry.install_handlers()
    seen: dict[str, str] = {}

    def remember(event: Event) -> None:
        seen.setdefault("run_id", event.run_id)

    engine.bus.subscribe(remember)
    if not tui:
        engine.bus.subscribe(plain_printer(console))
        if chat and sys.stdin.isatty():
            threading.Thread(target=_stdin_chat_reader, args=(engine,), daemon=True).start()
        try:
            return action()  # type: ignore[no-any-return]
        except KeyboardInterrupt:
            # The pair context manager already cleaned up while unwinding.
            _exit_interrupted(seen.get("run_id"))

    dashboard = Dashboard()
    pending: queue.SimpleQueue[Event] = queue.SimpleQueue()
    engine.bus.subscribe(pending.put)
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["result"] = action()
        except BaseException as exc:
            outcome["error"] = exc

    def submit(text: str) -> None:
        dashboard.post_chat(engine.post_user_message(text), text)

    chat_input = ChatInput(submit) if chat and ChatInput.available() else None

    thread = threading.Thread(target=target, daemon=True)

    def quiesce() -> None:
        # Idempotent: the signal handler runs it before cleanup, and the
        # KeyboardInterrupt fallback below may run it again after.
        engine.request_cancel()
        if thread.is_alive():
            thread.join(timeout=_INTERRUPT_JOIN_S)

    cleanup_registry.set_quiesce(quiesce)
    try:
        with contextlib.ExitStack() as stack:
            live = stack.enter_context(
                Live(dashboard.renderable(), console=console, refresh_per_second=8)
            )
            if chat_input is not None:
                stack.enter_context(chat_input)

            def drain() -> None:
                while True:
                    try:
                        event = pending.get_nowait()
                    except queue.Empty:
                        return
                    dashboard.on_event(event)
                    rendered = render_event(event)
                    if rendered is not None:
                        live.console.print(rendered)

            def refresh(*, now: bool = False) -> None:
                line = chat_input.renderable() if chat_input is not None else None
                # `now` bypasses Live's refresh_per_second throttle so a
                # keystroke echoes the instant it lands, not on the next tick.
                live.update(dashboard.renderable(line), refresh=now)

            thread.start()
            try:
                while thread.is_alive():
                    drain()
                    refresh()
                    if chat_input is not None:
                        if chat_input.pump(0.15):
                            refresh(now=True)
                    else:
                        time.sleep(0.15)
            except KeyboardInterrupt:
                # Fallback for environments where the signal handlers could
                # not install (no-op re-quiesce when they did).
                quiesce()
                raise
            drain()
            refresh()
    except KeyboardInterrupt:
        # The signal handler (or the fallback above) already quiesced the
        # engine and tore the sandboxes down; finish with exit 130, a
        # resume hint, and no traceback.
        _exit_interrupted(dashboard.run_id or seen.get("run_id"))
    finally:
        cleanup_registry.set_quiesce(None)
    if dashboard.chat_pending:
        console.print(
            "[yellow]chat message(s) the run ended before answering:[/] "
            + "; ".join(dashboard.chat_pending.values())
        )
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]  # type: ignore[no-any-return]


def _stdin_chat_reader(engine: LoopEngine) -> None:
    """--no-tui chat: plain line input (terminal echo shows the typing)."""
    with contextlib.suppress(ValueError, OSError):  # stdin closed mid-run
        for line in sys.stdin:
            text = line.strip()
            if text:
                engine.post_user_message(text)


_TREE_MAX_FILES = 50


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024 or unit == "MB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")  # pragma: no cover


def _artifacts_tree(root: Path, files: list[Path], cap: int = _TREE_MAX_FILES) -> Tree:
    tree = Tree(f"[bold]{root}[/]")
    nodes: dict[Path, Tree] = {root: tree}
    for path in files[:cap]:
        rel = path.relative_to(root)
        parent = root
        for part in rel.parts[:-1]:
            child = parent / part
            if child not in nodes:
                nodes[child] = nodes[parent].add(f"{part}/")
            parent = child
        nodes[parent].add(f"{rel.name} [dim]({_human_size(path.stat().st_size)})[/]")
    if len(files) > cap:
        tree.add(f"[dim]… +{len(files) - cap} more[/]")
    return tree


def _print_artifacts_summary(result: RunResult, config: Config) -> None:
    target = artifacts_dir(result, config.state_dir)
    if target is None or not target.is_dir():
        return
    scan = scan_artifacts(target, config.artifacts.exclude)
    if not scan.files:
        console.print(f"\nartifacts: none produced (workspace: {target})")
        if scan.excluded_note:
            console.print(f"  [dim]{scan.excluded_note}[/]")
        return
    via = "live workspace mount" if result.mounted else "harvested from the sandbox"
    console.print(f"\nartifacts: {len(scan.files)} file(s), {via}")
    console.print(_artifacts_tree(target, scan.files))
    if scan.excluded_note:
        console.print(f"  [dim]{scan.excluded_note}[/]")


def _print_github_summary(result: RunResult, config: Config) -> None:
    """What the run did on GitHub, mined from its persisted event stream.

    The run.deliver/review/merge lines scroll away with the transcript; the
    repo, the PR and how it ended are the outputs the user actually came
    for, so the finish summary restates them (#67-adjacent: outcomes must be
    surfaced, never left implicit in scrollback).
    """
    created = False
    repo: str | None = None
    lines: list[str] = []
    try:
        events = list(_store(config).events(result.run_id, type_prefix="r"))
    except SbxloopError:
        return
    rounds = 0
    for _seq, event in events:
        data = event.data
        if event.type == HostEventTypes.RUN_DELIVER:
            repo = str(data.get("repo") or repo or "")
            if data.get("created"):
                created = True
            elif data.get("error"):
                lines.append(f"delivery [bold red]failed[/]: {data['error']}")
            elif data.get("url"):
                rounds = int(data.get("round") or 1)
                lines = [f"PR [bold]#{data.get('pr')}[/]  {data['url']} (delivery {rounds})"]
        elif event.type == HostEventTypes.REVIEW_VERDICT:
            lines.append(
                f"review round {data.get('round')}: [bold]{data.get('verdict')}[/] "
                f"({data.get('blocking', 0)} blocking finding(s))"
            )
        elif event.type == HostEventTypes.RUN_MERGED:
            who = "by a human" if data.get("by_human") else "by sbxloop"
            lines.append(f"[bold green]merged[/] {who}: {str(data.get('sha') or '')[:12]}")
        elif event.type == HostEventTypes.RUN_BLOCKED:
            lines.append(f"[bold yellow]blocked[/]: {data.get('why')}")
    if not repo and not result.pr_url:
        return
    suffix = " [dim](created this run)[/]" if created else ""
    console.print(f"\ngithub: [bold]{repo or config.github.repo}[/]{suffix}")
    for line in lines:
        console.print(f"  {line}")


def _print_workspace_clone_summary(result: RunResult, config: Config) -> None:
    """Where an isolated run's results live, mined from the persisted
    sandbox.workspace_clone event — the transcript line scrolls away, and
    "how do I get the changes back into my checkout" is the first question
    an isolated run raises."""
    try:
        events = list(_store(config).events(result.run_id, type_prefix="sandbox.workspace_clone"))
    except SbxloopError:
        return
    if not events:
        return
    data = events[-1][1].data
    source, target, branch = data.get("source"), data.get("target"), data.get("branch")
    commit = str(data.get("commit") or "")
    console.print(f"\nworkspace: cloned from [bold]{source}[/] (HEAD {commit[:12]})")
    if result.mounted:
        console.print(f"  results are on branch [bold]{branch}[/] in {target}")
        console.print(f"  fetch into your checkout: [cyan]git fetch {target} {branch}[/]")
    else:
        console.print(f"  harvested changes are uncommitted in {target} (branch {branch})")
    _print_retention_note(config)


def _print_retention_note(config: Config) -> None:
    """The run directory is the only copy of the work until it is fetched or
    delivered — say how long it stays: the daemon sweeps on that window and
    `sbxloop gc` uses it as the default."""
    days = config.daemon.prune_runs_after_days
    if days <= 0:
        return
    console.print(
        f"  [dim]retention: run dirs older than {days:g}d are removed by the daemon / "
        "[cyan]sbxloop gc[/] — fetch results before then[/]"
    )


def _finish(result: RunResult, config: Config) -> None:
    style = "green" if result.succeeded else ("yellow" if result.state == "blocked" else "red")
    console.print(f"\nrun [bold cyan]{result.run_id}[/] finished: [bold {style}]{result.state}[/]")
    if result.reason:
        console.print(f"  reason: {result.reason}")
    for task in result.tasks:
        console.print(f"  {task.spec.id}: {task.state}  ({task.spec.title})")
    _print_github_summary(result, config)
    _print_artifacts_summary(result, config)
    _print_workspace_clone_summary(result, config)
    if result.kept_sandboxes:
        console.print(f"\n[bold yellow]sandboxes kept:[/] {', '.join(result.kept_sandboxes)}")
        console.print(f"  inspect: [cyan]sbxloop shell {result.run_id}[/] (--role github)")
        console.print(f"  remove:  [cyan]sbxloop sandbox rm --run {result.run_id}[/]")
    raise typer.Exit(0 if result.succeeded else 1)


@app.command()
def run(
    outcome: Annotated[str, typer.Argument(help="The outcome to achieve.")],
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo",
            help='GitHub repository ("owner/name") the run delivers to and merges into, '
            "overriding [github].repo from sbxloop.toml. Without one the run stops "
            "after its gate with the work in the workspace.",
        ),
    ] = None,
    deliver_base: Annotated[
        str | None,
        typer.Option(
            "--deliver-base",
            help="Base branch for the pull request (default: the repo's default branch).",
        ),
    ] = None,
    create_repo: Annotated[
        bool | None,
        typer.Option(
            "--create-repo/--no-create-repo",
            help="Create the --repo repository if it does not exist (private "
            "unless --create-public). Without this, a missing repo fails the "
            "run up front.",
        ),
    ] = None,
    create_public: Annotated[
        bool | None,
        typer.Option(
            "--create-public/--no-create-public",
            help="Make a repository created via --create-repo public.",
        ),
    ] = None,
    model: Annotated[str | None, typer.Option("--model", help="Copilot model id.")] = None,
    keep_sandboxes: Annotated[
        bool | None,
        typer.Option(
            "--keep-sandboxes/--no-keep-sandboxes",
            help="Do not remove sandboxes at the end (either flag overrides config).",
        ),
    ] = None,
    keep_on_failure: Annotated[
        bool | None,
        typer.Option(
            "--keep-on-failure/--no-keep-on-failure",
            help="Keep the sandbox pair alive when the run fails (inspect with `sbxloop shell`).",
        ),
    ] = None,
    tui: Annotated[bool, typer.Option("--tui/--no-tui", help="Live dashboard.")] = True,
    chat: Annotated[
        bool,
        typer.Option(
            "--chat/--no-chat",
            help="Interactive chat: type a message + Enter to pause the agent at the "
            "next checkpoint, get an answer, and steer the run (needs a TTY).",
        ),
    ] = True,
) -> None:
    """Run an agentic loop for OUTCOME in a fresh sandbox pair.

    With a GitHub repository configured the run carries its work all the
    way: a draft pull request, its own review, fix rounds, CI, and the merge.
    """
    config = _config_with_overrides(
        model=model,
        keep_sandboxes=keep_sandboxes,
        keep_on_failure=keep_on_failure,
    )
    github_overrides = {
        key: value
        for key, value in (
            ("repo", repo),
            ("deliver_base", deliver_base),
            ("create_repo", create_repo),
            ("create_public", create_public),
        )
        if value is not None
    }
    if github_overrides:
        # model_copy skips validation, so rebuild the section instead: an
        # ill-formed --repo must fail here, not as a mid-run GitHub error.
        try:
            base = config.github.model_dump()
            if "repo" in github_overrides:
                # --repo names the one repository this run targets, replacing
                # any configured repo list — unless it selects one of them.
                selected = config.github.find_repo(repo)
                if selected is not None:
                    github_overrides["repo"] = selected.repo
                    base["repos"] = [selected.model_dump()]
                else:
                    base["repos"] = []
            github = GithubConfig.model_validate({**base, **github_overrides})
        except ValidationError as exc:
            console.print(f"[bold red]invalid GitHub option:[/] {exc.errors()[0]['msg']}")
            raise typer.Exit(2) from exc
        config = config.model_copy(update={"github": github})
    elif len(config.github.repo_list()) > 1:
        # Several repositories are configured but a run targets exactly one:
        # default to the sole enabled repo, else make the operator choose.
        entry = _resolve_repo(config, None)
        config = config.model_copy(
            update={
                "github": config.github.for_repo(
                    entry.repo, workspace=config.workspace_for_repo(entry.repo)
                )
            }
        )
    if (deliver_base or create_repo or create_public) and not config.github.enabled:
        console.print(
            "[bold red]GitHub integration is not configured.[/] Those options need a "
            "repository: pass [cyan]--repo owner/repo[/] or set "
            '[cyan]\\[github] repo = "owner/repo"[/] in sbxloop.toml '
            "(see `sbxloop init`), then re-run."
        )
        raise typer.Exit(2)
    engine = LoopEngine(config)
    try:
        result = _drive_with_ui(engine, tui=tui, chat=chat, action=lambda: engine.start(outcome))
    except SbxloopError as exc:
        console.print(f"[bold red]run failed:[/] {exc}")
        raise typer.Exit(2) from exc
    _finish(result, config)


@app.command()
def resume(
    run_id: Annotated[str, typer.Argument(help="Run id to resume.")],
    tui: Annotated[bool, typer.Option("--tui/--no-tui")] = True,
    chat: Annotated[
        bool,
        typer.Option("--chat/--no-chat", help="Interactive chat (see `sbxloop run --help`)."),
    ] = True,
) -> None:
    """Resume an unfinished run (fresh sandboxes, persisted state and config)."""
    config = load_config()
    engine = LoopEngine(config)
    try:
        result = _drive_with_ui(engine, tui=tui, chat=chat, action=lambda: engine.resume(run_id))
    except SbxloopError as exc:
        console.print(f"[bold red]resume failed:[/] {exc}")
        raise typer.Exit(2) from exc
    # engine.config is the run's rehydrated config, which is what drove the run.
    _finish(result, engine.config)


@app.command()
def cancel(run_id: Annotated[str, typer.Argument()]) -> None:
    """Mark a run cancelled (takes effect at the next phase boundary)."""
    config = load_config()
    engine = LoopEngine(config)
    try:
        engine.cancel(run_id)
    except SbxloopError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc
    console.print(f"run {run_id} cancelled")


@app.command()
def status(
    run_id: Annotated[str | None, typer.Argument(help="Run id for details.")] = None,
) -> None:
    """List runs, or show one run's tasks and phase history."""
    config = load_config()
    store = _store(config)
    if run_id is None:
        table = Table(title="sbxloop runs")
        table.add_column("run")
        table.add_column("repo")
        table.add_column("state", max_width=60)
        table.add_column("outcome", max_width=60)
        table.add_column("updated")
        for record in store.list_runs():
            # Reconciled/cancelled runs carry *why* they are terminal (#374);
            # showing it here is what makes `status` agree with the daemon.
            state = f"{record.state} — [dim]{record.reason}[/]" if record.reason else record.state
            table.add_row(
                record.run_id,
                _run_repo(store, record.run_id) or "[dim]—[/]",
                state,
                record.outcome[:60],
                time.strftime("%Y-%m-%d %H:%M", time.localtime(record.updated_at)),
            )
        console.print(table)
        return

    try:
        record = store.get_run(run_id)
    except SbxloopError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc
    console.print(f"run [bold cyan]{record.run_id}[/]  state: [bold]{record.state}[/]")
    repo = _run_repo(store, record.run_id)
    if repo:
        console.print(f"repo: [bold]{repo}[/]")
    if record.reason:
        console.print(f"reason: {record.reason}")
    console.print(f"outcome: {record.outcome}")
    table = Table(title="tasks")
    for column in ("task", "title", "state", "revisions", "replans"):
        table.add_column(column)
    for task in store.get_tasks(run_id):
        table.add_row(
            task.spec.id,
            task.spec.title,
            task.state,
            str(task.revisions),
            str(task.replans),
        )
    console.print(table)
    attempts = store.phase_attempts(run_id)
    console.print(f"{len(attempts)} phase attempts recorded")
    # The pair names, so debugging a live run needs no by-hand
    # `sbxloop-<run>-agent` reconstruction.
    console.print("sandboxes:")
    roles: tuple[SandboxRole, ...] = ("agent", "github")
    try:
        live = {info.name for info in SbxCLI(app_name=config.app_name or None).ls()}
    except SbxloopError:
        for role in roles:
            console.print(
                f"  {sandbox_name(run_id, role)}  [dim](liveness unknown: sbx ls failed)[/]"
            )
        return
    any_live = False
    for role in roles:
        name = sandbox_name(run_id, role)
        any_live = any_live or name in live
        state_note = "[green]running[/]" if name in live else "[dim]not running[/]"
        console.print(f"  {name}  {state_note}")
    if any_live:
        console.print(f"  inspect: [cyan]sbxloop shell {run_id}[/] (--role github)")


@app.command()
def logs(
    run_id: Annotated[str, typer.Argument()],
    follow: Annotated[bool, typer.Option("--follow", "-f")] = False,
    type_prefix: Annotated[
        str | None, typer.Option("--type", help="Filter by event type prefix.")
    ] = None,
    task: Annotated[str | None, typer.Option("--task", help="Filter by task id.")] = None,
    stale_after: Annotated[
        float,
        typer.Option(
            "--stale-after",
            help="With --follow: exit once a non-terminal run has shown no "
            "activity (events or state changes) for this many minutes; "
            "0 follows forever.",
        ),
    ] = 10.0,
) -> None:
    """Replay (or tail) a run's event stream from the state store."""
    config = load_config()
    store = _store(config)
    store.get_run(run_id)  # validates existence
    last_seq = 0
    while True:
        for seq, event in store.events(run_id, after_seq=last_seq, type_prefix=type_prefix):
            last_seq = seq
            if task and event.data.get("task_id") != task:
                continue
            console.print(format_event(event), highlight=False)
        if not follow:
            break
        record = store.get_run(run_id)
        if record.state in TERMINAL_RUN_STATES:
            break
        # A run whose driving process died hard stays non-terminal in the DB
        # forever; without this, --follow would spin indefinitely.
        last_activity = max(record.updated_at, store.last_event_ts(run_id) or 0.0)
        if stale_after > 0 and time.time() - last_activity > stale_after * 60.0:
            console.print(
                f"[yellow]run {run_id} is {record.state} but has shown no activity "
                f"for over {stale_after:g} minutes[/] — its process may be dead. "
                f"Exiting; resume with [cyan]sbxloop resume {run_id}[/] or keep "
                "waiting with [cyan]--stale-after 0[/]."
            )
            break
        time.sleep(0.5)


# Prefer bash when the template has it, fall back to POSIX sh. `sbx exec`
# has no documented -it flags; terminal attachment is inherited stdio.
_INTERACTIVE_SHELL = ("sh", "-c", "command -v bash >/dev/null && exec bash -l; exec sh -l")


@app.command()
def shell(
    run_id: Annotated[str, typer.Argument(help="Run id.")],
    role: Annotated[
        str, typer.Option("--role", help="Which sandbox of the pair: agent or github.")
    ] = "agent",
    command: Annotated[
        str | None,
        typer.Option(
            "--command", "-c", help="Run one shell command instead of an interactive shell."
        ),
    ] = None,
) -> None:
    """Open a shell inside a run's sandbox (kept, in-flight, or leaked).

    Attaching to an in-flight run is meant as observation: the worker owns
    its env files and workspace, so avoid mutating them mid-phase.
    """
    if role not in ("agent", "github"):
        console.print(f"[bold red]invalid --role {role!r}:[/] must be agent or github")
        raise typer.Exit(2)
    config = load_config()
    store = _store(config)
    try:
        store.get_run(run_id)
    except SbxloopError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc
    cli = SbxCLI(app_name=config.app_name or None)
    name = sandbox_name(run_id, "agent" if role == "agent" else "github")
    try:
        live = any(info.name == name for info in cli.ls())
    except SbxloopError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc
    if not live:
        console.print(
            f"[bold red]sandbox {name} is not running.[/] Sandboxes are removed at run end "
            "unless kept (keep_on_failure, --keep-sandboxes), and kept ones may have been "
            "pruned since."
        )
        raise typer.Exit(2)
    argv = ("sh", "-lc", command) if command else _INTERACTIVE_SHELL
    raise typer.Exit(cli.exec_interactive(name, argv))


@app.command()
def artifacts(
    run_id: Annotated[str, typer.Argument(help="Run id.")],
    path: Annotated[
        bool, typer.Option("--path", help="Print only the artifacts directory (for scripting).")
    ] = False,
    tree: Annotated[
        bool, typer.Option("--tree/--list", help="Render a file tree instead of a flat list.")
    ] = False,
) -> None:
    """Show where a run's artifacts live on the host, and what is in there."""
    config = load_config()
    store = _store(config)
    try:
        record = store.get_run(run_id)
    except SbxloopError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc
    target = artifacts_dir(record, config.state_dir)
    if target is None:
        console.print(
            f"[bold red]run {run_id} has no artifacts:[/] it never provisioned a workspace "
            f"(state: {record.state})"
        )
        raise typer.Exit(2)
    if path:
        # bare path on stdout, nothing else — `cd $(sbxloop artifacts R --path)`
        typer.echo(str(target))
        return
    if not target.is_dir():
        console.print(f"[bold red]artifacts directory is gone:[/] {target}")
        raise typer.Exit(2)
    scan = scan_artifacts(target, config.artifacts.exclude)
    files = scan.files
    via = "live workspace mount" if record.mounted else "harvested copy"
    console.print(f"run [bold cyan]{run_id}[/]: {len(files)} file(s) ({via}) in [bold]{target}[/]")
    if tree:
        console.print(_artifacts_tree(target, files))
    else:
        for file in files:
            size = _human_size(file.stat().st_size)
            console.print(f"  {file.relative_to(target)}  [dim]{size}[/]")
    if scan.excluded_note:
        console.print(f"  [dim]{scan.excluded_note}[/]")


@sandbox_app.command("ls")
def sandbox_ls() -> None:
    """List sbxloop-managed sandboxes."""
    config = load_config()
    cli = SbxCLI(app_name=config.app_name or None)
    table = Table(title="sbxloop sandboxes")
    for column in ("name", "agent", "status", "workspace"):
        table.add_column(column)
    for info in cli.ls():
        if info.name.startswith("sbxloop-"):
            table.add_row(info.name, info.agent or "", info.status or "", info.workspace or "")
    console.print(table)


@sandbox_app.command("rm")
def sandbox_rm(
    name: Annotated[str | None, typer.Argument(help="Sandbox name.")] = None,
    run_id: Annotated[str | None, typer.Option("--run", help="Remove a run's pair.")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Remove all sbxloop sandboxes.")] = False,
) -> None:
    """Remove sbxloop sandboxes by name, by run, or all of them."""
    config = load_config()
    cli = SbxCLI(app_name=config.app_name or None)
    targets: list[str] = []
    if name:
        targets.append(name)
    if run_id:
        targets += [sandbox_name(run_id, "agent"), sandbox_name(run_id, "github")]
    if all_:
        targets += [i.name for i in cli.ls() if i.name.startswith("sbxloop-")]
    if not targets:
        console.print("nothing to remove: pass a NAME, --run, or --all")
        raise typer.Exit(2)
    for target in dict.fromkeys(targets):
        try:
            cli.rm(target)
            console.print(f"removed {target}")
        except SbxloopError as exc:
            console.print(f"[yellow]skip {target}:[/] {exc}")


_STATUS_STYLES = {"ok": "[green]ok[/]", "warn": "[yellow]warn[/]", "unknown": "[dim]?[/]"}


def _secrets_context() -> tuple[Config, SbxCLI, set[str]]:
    """Config, an sbx handle, and the live sbxloop sandbox names (for
    telling in-use registration scopes from stale ones)."""
    config = load_config()
    cli = SbxCLI(app_name=config.app_name or None)
    live = {i.name for i in cli.ls() if i.name.startswith(SANDBOX_SCOPE_PREFIX)}
    return config, cli, live


@secrets_app.command("list")
def secrets_list(
    probe: Annotated[
        bool,
        typer.Option(
            "--probe/--no-probe",
            help="When `sbx secret ls` cannot answer, detect registrations by "
            "transiently registering (and immediately removing) a sentinel — "
            "the collision error names the real owner.",
        ),
    ] = True,
) -> None:
    """Show sbxloop's custom-secret registrations across scopes.

    Flags registrations that no longer match what provisioning would
    register (stale scopes, wrong host bindings) — the pre-collision
    warnings. The built-in `github` service secret is sbx-managed and never
    touched by these commands.
    """
    try:
        config, cli, live = _secrets_context()
        table = Table(title="sbxloop custom-secret registrations")
        for column in ("env", "expected", "actual", "status", "note"):
            table.add_column(column)
        warned = False
        for env, host in tracked_custom_secrets(config):
            state = inspect_custom_secret(cli, env, host=host, probe=probe)
            judgement = assess(state, canonical_host=host, live_sandboxes=live)
            warned = warned or judgement.status == "warn"
            if state.exists:
                actual = f"scope {state.scope or '(unknown)'}"
                if state.hosts:
                    actual += f" @ {', '.join(state.hosts)}"
            elif state.exists is None:
                actual = "(undetermined)"
            else:
                actual = "not registered"
            table.add_row(
                env,
                f"custom @ {host} (per-run scope)",
                actual,
                _STATUS_STYLES[judgement.status],
                judgement.note,
            )
        console.print(table)
        console.print(
            "[dim]GH_TOKEN uses sbx's built-in `github` service secret; "
            "it is never managed here.[/]"
        )
        if warned:
            console.print(
                "\n[yellow]warnings above are pre-collision state[/] — "
                "`sbxloop secrets clean` removes the stale entries"
            )
    except SbxloopError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc


@secrets_app.command("clean")
def secrets_clean(
    apply: Annotated[
        bool, typer.Option("--apply", help="Actually remove (default is a dry run).")
    ] = False,
    all_: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Also remove healthy sbxloop-owned registrations (global with the "
            "canonical binding, live-sandbox scopes), not just stale ones.",
        ),
    ] = False,
) -> None:
    """Remove stale sbxloop-owned custom-secret registrations (dry-run by default).

    Only touches registrations sbxloop itself created (sbxloop-* sandbox
    scopes and global entries for its tracked env vars) — never foreign
    scopes and never the built-in `github` service secret.
    """
    try:
        config, cli, live = _secrets_context()
        failed = False
        removed_any = False
        for env, host in tracked_custom_secrets(config):
            state = inspect_custom_secret(cli, env, host=host)
            judgement = assess(state, canonical_host=host, live_sandboxes=live)
            if not (judgement.stale or (all_ and judgement.owned)):
                console.print(f"{env}: nothing to clean ({judgement.note})")
                continue
            where = f"scope {state.scope or '(unknown)'}"
            if not apply:
                console.print(f"{env}: would remove the registration in {where} — {judgement.note}")
                removed_any = True
                continue
            if any(rm() for rm in removal_ladder(cli, state, host=host)):
                console.print(f"[green]{env}: removed the registration in {where}[/]")
                removed_any = True
            else:
                console.print(f"[bold red]{env}: sbx rejected every removal for {where}[/]")
                failed = True
        if not apply and removed_any:
            console.print("\ndry run — re-run with [cyan]--apply[/] to remove")
        if failed:
            raise typer.Exit(1)
    except SbxloopError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc


@secrets_app.command("rotate")
def secrets_rotate(
    prompt: Annotated[
        bool,
        typer.Option(
            "--prompt",
            help="Read the new token from a hidden interactive prompt instead of "
            f"the {COPILOT_TOKEN_ENV} environment variable / ./.env.",
        ),
    ] = False,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify/--no-verify",
            help="Boot a throwaway sandbox to report which secret strategy "
            "(proxy vs plain-env fallback) the next run will use.",
        ),
    ] = True,
) -> None:
    """Rotate the Copilot token's sbx registration in one step.

    Replaces the existing registration (wherever its scope) with a global
    one carrying the canonical host binding — the rm + set-custom dance
    provisioning would otherwise perform mid-run. The token is read from
    the environment/.env or an interactive prompt, never from argv.
    """
    if prompt:
        token = typer.prompt(f"new {COPILOT_TOKEN_ENV}", hide_input=True)
    else:
        token = os.environ.get(COPILOT_TOKEN_ENV, "")
        if not token:
            console.print(
                f"[bold red]{COPILOT_TOKEN_ENV} is not set.[/] Export the new token "
                "(or put it in ./.env), or pass [cyan]--prompt[/] to type it — "
                "it is never accepted as a command-line argument."
            )
            raise typer.Exit(2)
    try:
        config, cli, live = _secrets_context()
        for env, host in tracked_custom_secrets(config):
            replace_registration(cli, env=env, host=host, token=token)
            console.print(f"[green]rotated:[/] {env} registered @ {host} (global scope)")
        if live:
            console.print(
                f"[yellow]live sbxloop sandboxes exist ({', '.join(sorted(live))})[/] — "
                "they may still hold the old token in their in-VM env file; "
                "remove them with `sbxloop sandbox rm --all`"
            )
        if prompt:
            console.print(
                f"[yellow]runs read {COPILOT_TOKEN_ENV} from the environment at "
                "provision time[/] — update your export / ./.env with the new value too"
            )
        if config.secret_strategy == "plain-env":  # nosec B105 - strategy label
            console.print(
                "next run: [bold]plain-env[/] strategy (configured) — the token is "
                "written to the in-VM env file from your environment"
            )
        elif verify:
            workspace = config.state_dir / "secretcheck"
            workspace.mkdir(parents=True, exist_ok=True)
            visible = verify_secret_visibility(
                cli,
                env=COPILOT_TOKEN_ENV,
                workspace=workspace,
                template=config.sandbox.template,
            )
            if visible is True:
                console.print(
                    "next run: [bold green]proxy[/] strategy — the token stays out of the VM"
                )
            elif visible is False:
                console.print(
                    "next run: [bold yellow]plain-env fallback[/] — sbx's proxy secret is "
                    "invisible to exec sessions, so provisioning will write the in-VM env file"
                )
            else:
                console.print("[yellow]could not verify secret visibility[/] (see logs)")
    except SbxloopError as exc:
        console.print(f"[bold red]rotate failed:[/] {exc}")
        raise typer.Exit(2) from exc


@sandbox_app.command("prune")
def sandbox_prune(
    force: Annotated[
        bool,
        typer.Option("--force", "--yes", help="Actually remove (default is a dry run)."),
    ] = False,
    min_age: Annotated[
        float,
        typer.Option(
            "--min-age",
            help="Hours a run must be inactive before its sandboxes count as orphaned.",
        ),
    ] = 1.0,
    include_kept: Annotated[
        bool,
        typer.Option("--include-kept", help="Also prune kept-for-debugging sandboxes."),
    ] = False,
) -> None:
    """Garbage-collect orphaned sbxloop sandboxes (crashed hosts, killed runs).

    Cross-references `sbx ls` against this working copy's state DB. Dry-run
    by default: prints the classification and removes nothing without
    --force.
    """
    config = load_config()
    store = _store(config)
    cli = SbxCLI(app_name=config.app_name or None)
    try:
        verdicts = classify_sandboxes(
            cli.ls(), store, min_age_s=min_age * 3600.0, include_kept=include_kept
        )
    except SbxloopError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc
    if not verdicts:
        console.print("no sbxloop sandboxes found")
        return

    table = Table(title="sbxloop sandbox prune")
    for column in ("sandbox", "run", "run state", "age", "verdict"):
        table.add_column(column)
    for v in verdicts:
        table.add_row(
            v.name,
            v.run_id or "",
            v.run_state or "[dim]unknown[/]",
            format_age(v.age_s),
            ("[red]orphan[/] — " if v.orphan else "[green]keep[/] — ") + v.reason,
        )
    console.print(table)
    console.print(
        "[dim]note: the state DB is per working copy — 'unknown' sandboxes may "
        "belong to another checkout's runs on this sbx host[/]"
    )

    orphans = [v for v in verdicts if v.orphan]
    if not orphans:
        console.print("nothing to prune")
        return
    if not force:
        console.print(f"dry run: {len(orphans)} orphan candidate(s); re-run with --force to remove")
        return
    failures = 0
    for v in orphans:
        try:
            if v.role in ("agent", "github"):
                # A pruned run sandbox takes its secret registrations with
                # it; otherwise a later run under the same name (resume)
                # cannot replace them and comes up with the proxy sentinel.
                remove_run_sandbox(cli, v.name, v.role)  # type: ignore[arg-type]
            else:
                remove_sandbox(cli, v.name)
        except SbxloopError as exc:
            failures += 1
            console.print(f"[yellow]skip {v.name}:[/] {exc}")
            continue
        console.print(f"removed {v.name}")
        # A pruned kept run is no longer kept; keep the DB marker honest.
        if v.kept_reason is not None and v.run_id is not None:
            store.set_run_kept(v.run_id, None)
    if failures:
        raise typer.Exit(1)


@app.command()
def gc(
    older_than: Annotated[
        float | None,
        typer.Option(
            "--older-than",
            help="Days a finished run must be untouched before its directory is removed "
            "(default: [daemon] prune_runs_after_days).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Classify and report; remove nothing."),
    ] = False,
) -> None:
    """Remove old run directories (workspace clones, harvested artifacts).

    Same policy as the daemon's daily sweep: only runs that are terminal
    (completed/failed/cancelled), past the retention window, not
    kept-for-debugging and whose delivery did not fail. Run rows in the
    state DB — the audit trail — are never removed.
    """
    config = load_config()
    days = config.daemon.prune_runs_after_days if older_than is None else older_than
    # typer parses "nan" and "inf" as valid floats; NaN compares false
    # against everything, which would make EVERY terminal run "past
    # retention" — the one input this destructive command must not accept.
    if not math.isfinite(days) or days < 0:
        console.print("[bold red]--older-than must be a finite number of days >= 0[/]")
        raise typer.Exit(2)
    store = _store(config)
    result = prune_run_dirs(store, config.state_dir, older_than_s=days * DAY_S, dry_run=dry_run)
    if not result.verdicts:
        console.print(f"no run directories under {config.state_dir / 'runs'}")
        return
    table = Table(title="sbxloop gc" + (" (dry run)" if dry_run else ""))
    for column in ("run", "state", "age", "size", "verdict"):
        table.add_column(column)
    for v in result.verdicts:
        table.add_row(
            v.run_id,
            v.run_state or "[dim]unknown[/]",
            format_age(v.age_s),
            format_bytes(v.size_bytes) if v.prunable else "",
            ("[red]prune[/] — " if v.prunable else "[green]keep[/] — ") + v.reason,
        )
    console.print(table)
    candidates = result.candidates
    if not candidates:
        console.print(f"nothing to prune (retention {days:g}d)")
        return
    if dry_run:
        console.print(
            f"dry run: {len(candidates)} run dir(s), {format_bytes(result.bytes_freed)}; "
            "re-run without --dry-run to remove",
            highlight=False,
        )
        return
    console.print(
        f"removed {len(result.pruned)} run dir(s), freed {format_bytes(result.bytes_freed)}",
        highlight=False,
    )
    for run_id in result.failed:
        console.print(f"[yellow]could not remove {run_id}[/] (see log)")
    if result.failed:
        raise typer.Exit(1)


@config_app.command("show")
def config_show() -> None:
    """Show the resolved configuration and where each value came from."""
    try:
        config, sources = load_config_with_sources()
    except SbxloopError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc
    table = Table(title="sbxloop configuration")
    table.add_column("key")
    table.add_column("value")
    table.add_column("source")
    flat: dict[str, Any] = {}

    def flatten(prefix: str, data: dict[str, Any]) -> None:
        for key, value in data.items():
            dotted = f"{prefix}{key}"
            if isinstance(value, dict):
                flatten(f"{dotted}.", value)
            else:
                flat[dotted] = value

    flatten("", config.model_dump(mode="json"))
    for dotted in sorted(flat):
        table.add_row(dotted, repr(flat[dotted]), sources.get(dotted, "default"))
    console.print(table)


@config_app.command("repos")
def config_repos(
    repo: Annotated[
        str | None,
        typer.Option("--repo", help="Show only this repository (owner/name or bare name)."),
    ] = None,
) -> None:
    """List the configured repositories with their enabled state and base branch."""
    config = load_config()
    entries = [_resolve_repo(config, repo)] if repo is not None else config.github.repo_list()
    if not entries:
        console.print(
            "no GitHub repository configured — add [cyan]\\[github] repo[/] or "
            "[cyan]\\[\\[github.repos]][/] to sbxloop.toml"
        )
        return
    table = Table(title="sbxloop repositories")
    for col in ("repo", "enabled", "base", "token env", "trigger label"):
        table.add_column(col)
    for entry in entries:
        effective = config.github.effective_repo(entry.repo) or entry
        table.add_row(
            entry.repo,
            "yes" if entry.enabled else "no",
            effective.deliver_base or "(repo default)",
            entry.token_env or "GH_TOKEN",
            entry.trigger_label or config.daemon.trigger_label,
        )
    console.print(table)


@config_app.command("policy")
def config_policy() -> None:
    """Show the effective per-phase network egress policy."""
    from sbxloop.policy import (
        APT_MIRROR_DOMAINS,
        BASELINE_REGISTRY_DOMAINS,
        WELL_KNOWN_REGISTRY_DOMAINS,
        baseline_allows,
    )
    from sbxloop.sbx.provision import AGENT_ALLOW_DOMAINS, GITHUB_ALLOW_DOMAINS

    try:
        config = load_config()
    except SbxloopError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc

    extra = list(config.sandbox.extra_allow_domains)
    baseline = ", ".join([*AGENT_ALLOW_DOMAINS, *extra])
    # What provisioning actually seeds, deny applied — an operator reading
    # this needs the effective set, not the constant.
    registries = ", ".join(baseline_allows(BASELINE_REGISTRY_DOMAINS, config.policy.deny))
    mirrors = ", ".join(baseline_allows(APT_MIRROR_DOMAINS, config.policy.deny))

    table = Table(title="agent sandbox: effective egress per phase")
    table.add_column("phase", no_wrap=True)
    table.add_column("policy", overflow="fold")
    table.add_row("decompose", "baseline")
    table.add_row(
        "build",
        "baseline + task-declared grants (auto-granted just before build, "
        "within the [policy] bounds below; every grant/refusal is event-logged)",
    )
    table.add_row(
        "verify",
        "baseline + grants already made — sbx has no policy revocation, so "
        "grants persist for the sandbox's lifetime (sandboxes are removed at "
        "run end; grants never outlive a run)",
    )
    console.print(table)
    console.print(f"baseline (provisioned per-sandbox): {baseline}")
    console.print(f"language registry baseline (always reachable, no declaration): {registries}")
    console.print(f"distro mirrors (always reachable, no declaration): {mirrors}")
    console.print(
        "well-known registries (declarable without [policy] allow): "
        + (
            ", ".join(WELL_KNOWN_REGISTRY_DOMAINS)
            or "(none — every supported language's registry is in the baseline above)"
        )
    )

    bounds = Table(title="[policy] bounds for task-declared grants")
    bounds.add_column("bound", no_wrap=True)
    bounds.add_column("patterns", overflow="fold")
    bounds.add_row(
        "allow",
        ", ".join(config.policy.allow)
        or "(empty — tasks may only use the baseline and well-known registries)",
    )
    bounds.add_row("deny", ", ".join(config.policy.deny) or "(none)")
    console.print(bounds)

    if config.github.enabled:
        gh_domains = ", ".join([*GITHUB_ALLOW_DOMAINS, *extra])
        console.print(f"github sandbox (all phases, no task grants): {gh_domains}")
    console.print("audit trail: [cyan]sbxloop logs RUN_ID --type policy.[/]")


@app.command()
def init(
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a commented sbxloop.toml with the default configuration."""
    path = Path("sbxloop.toml")
    if path.exists() and not force:
        console.print("sbxloop.toml already exists (use --force to overwrite)")
        raise typer.Exit(2)
    path.write_text(DEFAULT_CONFIG_TOML)
    console.print(f"wrote {path}")


@daemon_app.callback()
def daemon(
    ctx: typer.Context,
    repo: Annotated[
        str | None,
        typer.Option("--repo", help='GitHub repository ("owner/name") to poll for labeled issues.'),
    ] = None,
    max_runs_per_day: Annotated[
        int | None,
        int | None,
        typer.Option(
            "--max-runs-per-day",
            help="Calendar-day run cap; resets at midnight in run_cap_timezone (default UTC).",
        ),
    ] = None,
    poll_interval: Annotated[
        float | None, typer.Option("--poll-interval", help="Seconds between polls.")
    ] = None,
    discord_channel: Annotated[
        int | None,
        typer.Option("--discord-channel", help="Discord control channel id (enables the bridge)."),
    ] = None,
    once: Annotated[
        bool, typer.Option("--once", help="Recover, run one tick, exit (cron / smoke tests).")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Poll and print candidates; claim and run nothing.")
    ] = False,
    log_level: Annotated[
        str | None,
        typer.Option(
            "--log-level",
            help="Daemon log level: DEBUG|INFO|WARNING|ERROR (also SBXLOOP_DAEMON__LOG_LEVEL).",
        ),
    ] = None,
    log_format: Annotated[
        str | None,
        typer.Option("--log-format", help="Daemon log rendering: console|json."),
    ] = None,
) -> None:
    """Run the always-on outer loop: claim labeled GitHub issues, run each
    one through to a merged pull request, report back on the issue, and
    mirror the chronology to Discord. Subcommands inspect and steer
    individual work items; `sbxloop daemon ctl CMD` talks to the running
    daemon instead."""
    if ctx.invoked_subcommand is not None:
        return
    from sbxloop.daemon.agentbox import DaemonAgent
    from sbxloop.daemon.concierge import Concierge
    from sbxloop.daemon.control import ControlServer
    from sbxloop.daemon.discord import DiscordBridge
    from sbxloop.daemon.github import DaemonGithub
    from sbxloop.daemon.logsink import event_log_subscriber
    from sbxloop.daemon.loop import DaemonLoop
    from sbxloop.daemon.model import DaemonNotice, WorkItem
    from sbxloop.daemon.paths import resolve_state_dir
    from sbxloop.daemon.sources import GitHubLabels, build_github_source

    log = get_logger("sbxloop.daemon")
    started_at = time.monotonic()
    config, config_sources = load_config_with_sources()
    # The daemon's state lives at an absolute path outside the workspace
    # (#255): a relative `.sbxloop` inside the checkout it works on would
    # accrete a per-run clone there forever.
    state_choice = resolve_state_dir(
        config, config_sources, cwd=Path.cwd(), env=os.environ, home=Path.home()
    )
    config = config.model_copy(update={"state_dir": state_choice.path})
    daemon_overrides = {
        k: v
        for k, v in (
            ("max_runs_per_day", max_runs_per_day),
            ("poll_interval_s", poll_interval),
            ("log_level", log_level),
            ("log_format", log_format),
        )
        if v is not None
    }
    try:
        if daemon_overrides:
            daemon_cfg = DaemonConfig.model_validate(
                {**config.daemon.model_dump(), **daemon_overrides}
            )
            config = config.model_copy(update={"daemon": daemon_cfg})
        if repo is not None:
            github_cfg = GithubConfig.model_validate(
                {**config.github.model_dump(), "repos": [], "repo": repo}
            )
            config = config.model_copy(update={"github": github_cfg})
        if discord_channel is not None:
            discord_cfg = DiscordConfig.model_validate(
                {**config.discord.model_dump(), "channel_id": discord_channel}
            )
            config = config.model_copy(update={"discord": discord_cfg})
    except ValidationError as exc:
        # Before the pipeline is configured with the daemon's own settings:
        # the WARNING-level default from the app callback carries this.
        log.error("daemon.invalid_option", error=exc.errors()[0]["msg"])
        raise typer.Exit(2) from exc
    configure_logging(config.daemon.log_level, fmt=config.daemon.log_format)

    if not config.github.enabled:
        log.error(
            "daemon.no_repository",
            hint="set --repo owner/name (or [github] repo / [[github.repos]]): the "
            "daemon's work is the labeled issues of the configured repositories",
        )
        raise typer.Exit(2)
    if not config.github.enabled_repos():
        log.error(
            "daemon.no_enabled_repository",
            hint="every configured repository is disabled — set enabled = true on at "
            "least one [[github.repos]] entry",
        )
        raise typer.Exit(2)

    # A repository whose workspace is another repository's checkout would
    # have its runs built from the wrong tree (#526). Refuse to start.
    from sbxloop.cli.doctor import workspace_origin_mismatches

    mismatches = workspace_origin_mismatches(config)
    if mismatches:
        for mismatch in mismatches:
            log.error(
                "daemon.workspace_origin_mismatch",
                repo=mismatch.repo,
                workspace=str(mismatch.path),
                origin_repo=mismatch.origin_repo,
                hint=mismatch.message,
            )
        raise typer.Exit(2)

    db_path = config.state_dir / "state.db"
    # A pre-1.0 state database carries the old daemon lanes' tables and item
    # kinds; it is moved aside rather than migrated, before the engine store
    # opens the file (both stores share it).
    archived = DaemonStore.archive_legacy(db_path)
    store = _store(config)
    dstore = DaemonStore(db_path)
    # Rows a single-repo daemon wrote carry no repository. Name it now, from
    # the config, rather than letting whichever repository is polled first
    # adopt them. With several repos configured there is no sole owner to
    # attribute them to, so the non-terminal ones are dropped instead of
    # left to double-queue or mis-route; the next poll re-creates them
    # repo-qualified against the repository they actually came from.
    configured = config.github.repo_list()
    stranded: list[WorkItem] = []
    if len(configured) == 1:
        dstore.backfill_repo(configured[0].repo)
    else:
        repos = [r.repo for r in configured]
        # Most repo-less rows name their repository in their issue URL, so
        # they are attributed exactly rather than guessed at or discarded.
        dstore.attribute_repoless(repos)
        dropped = dstore.drop_repoless()
        if dropped:
            log.info(
                "daemon.repoless_items_dropped",
                rows=dropped,
                repos=repos,
                hint="work items written before multi-repo carry no repository and "
                "could not be attributed to one of several configured repos; they "
                "were never claimed, so they will be rediscovered, repo-qualified, "
                "on the next poll",
            )
        # What is left is claimed or in flight: claiming swapped the trigger
        # label for the in-progress one, so discovery can never re-create
        # these. They are failed (not deleted) before recover() runs, so
        # their pinned runs reconcile against a real item, and an operator
        # is told which issues are left carrying the in-progress label.
        stranded = dstore.strand_repoless(
            "repository could not be determined after upgrading to multi-repo "
            f"(configured: {', '.join(repos)}); settle the issue by hand",
            time.time(),
        )
        if stranded:
            log.error(
                "daemon.repoless_items_stranded",
                rows=len(stranded),
                repos=repos,
                items=[i.item_id for i in stranded],
                urls=[i.url for i in stranded],
                hint="these items were already claimed before the multi-repo "
                "upgrade, so their issues still carry the in-progress label and "
                "cannot be rediscovered; remove the label (and re-add the trigger "
                "label) by hand to run them again",
            )
    sbx = SbxCLI(app_name=config.app_name or None)
    bus = EventBus()
    bus.subscribe(event_log_subscriber)
    github = DaemonGithub(config, sbx, bus, worker_python=config.worker_python)
    github.remove_stale()
    labels = GitHubLabels(
        config.daemon.trigger_label,
        config.daemon.in_progress_label,
        config.daemon.failed_label,
        config.daemon.completed_label,
        config.daemon.blocked_label,
    )
    # Every enabled configured repository is polled; the daemon-wide
    # guardrails below (run cap, retry cap, breaker, one-run-at-a-time)
    # stay global across all of them.
    source = build_github_source(
        github.ops,
        config.github.enabled_repos(),
        labels,
        on_failure=github.note_failure,
    )

    # One line an operator can read back from the journal to know exactly
    # what this daemon is: where its state went (with the anchored default,
    # `sbxloop status` in the runner dir shows nothing unless
    # SBXLOOP_STATE_DIR points here), what it polls, and every guardrail.
    log.info(
        "daemon.starting",
        version=sbxloop.__version__,
        pid=os.getpid(),
        state_dir=str(config.state_dir),
        state_dir_reason=state_choice.reason,
        archived_state=str(archived) if archived else None,
        repo=config.github.repo,
        repos=[r.repo for r in config.github.enabled_repos()],
        trigger_label=config.daemon.trigger_label,
        poll_interval_s=config.daemon.poll_interval_s,
        max_runs_per_day=config.daemon.max_runs_per_day,
        max_attempts_per_item=config.daemon.max_attempts_per_item,
        max_resumes_per_item=config.daemon.max_resumes_per_item,
        retry_backoff_s=config.daemon.retry_backoff_s,
        max_consecutive_failures=config.daemon.max_consecutive_failures,
        breaker_cooldown_s=config.daemon.breaker_cooldown_s,
        workspace_isolation=config.daemon.workspace_isolation,
        refresh_workspace=config.daemon.refresh_workspace,
        landing="on",
        max_review_rounds=config.landing.max_review_rounds,
        max_ci_rounds=config.landing.max_ci_rounds,
        merge_method=config.landing.merge_method,
        discord=("on" if config.discord.enabled else "off"),
        discord_channel=config.discord.channel_id if config.discord.enabled else None,
        concierge=("on" if config.discord.enabled and config.concierge.enabled else "off"),
        log_level=config.daemon.log_level,
        log_format=config.daemon.log_format,
        once=once,
        dry_run=dry_run,
    )

    if dry_run:
        code = 0
        try:
            found = 0
            for item in source.poll():
                found += 1
                # The listing IS this command's output: stdout, so it
                # pipes and greps; the log keeps the structured record.
                console.print(
                    f"[cyan]{item.item_id}[/]  {item.title}" + (f"  {item.url}" if item.url else "")
                )
                log.debug(
                    "daemon.dry_run_candidate",
                    item=item.item_id,
                    title=item.title,
                    url=item.url or None,
                )
            log.info("daemon.dry_run_polled", candidates=found)
        except SbxloopError as exc:
            log.error("daemon.poll_failed", error=str(exc), exc_info=True)
            code = 1
        finally:
            github.close()
        raise typer.Exit(code)

    loop = DaemonLoop(config, store=store, dstore=dstore, source=source, sbx=sbx)
    # One probe, shared: the startup drift check below warms its PyPI memo, so
    # the concierge's first `version_status` answers without a network call.
    versions = VersionProbe(sbx=sbx)
    bridge: DiscordBridge | None = None
    concierge: Concierge | None = None
    if config.discord.enabled:
        try:
            bridge = DiscordBridge(config, dstore, loop_ref=loop)
            bridge.start()
        except SbxloopError as exc:
            log.error("discord.bridge_failed", error=str(exc), exc_info=True)
            raise typer.Exit(2) from exc
        loop.frontend = bridge
        if archived is not None:
            bridge.daemon_notice(
                DaemonNotice(
                    kind="daemon.state_archived",
                    text=f"pre-1.0 daemon state moved aside to {archived}; starting fresh",
                    level="warning",
                )
            )
        if stranded:
            listed = "\n".join(f"- {i.item_id} {i.url}".rstrip() for i in stranded)
            bridge.daemon_notice(
                DaemonNotice(
                    kind="daemon.repoless_items_stranded",
                    text=(
                        f"{len(stranded)} in-flight work item(s) could not be "
                        "attributed to a configured repository after the multi-repo "
                        "upgrade and were failed. Their issues still carry the "
                        "in-progress label and will not be rediscovered — clear it "
                        f"by hand:\n{listed}"
                    ),
                    level="warning",
                )
            )
        if config.concierge.enabled and not once:
            # The control channel's agent: its own event bus (the log sink
            # sees its turns like any agent session) and a long-lived agent
            # sandbox provisioned in the background, so the first mention
            # does not pay the microVM boot. Built after the bridge so a
            # missing bot token exits before any sandbox work starts.
            concierge_bus = EventBus()
            concierge_bus.subscribe(event_log_subscriber)
            concierge = Concierge(
                config,
                loop=loop,
                dstore=dstore,
                store_factory=lambda: _store(config),
                github=github,
                host=DaemonAgent(config, sbx, concierge_bus, worker_python=config.worker_python),
                bus=concierge_bus,
                versions=versions,
                on_watch=bridge.on_watch,
            )
            bridge.concierge = concierge
            concierge.warm_up()

    if not once:
        # Merges to main auto-release a patch; deploying here is manual, so a
        # long-lived daemon drifts behind silently. Check once in the
        # background (never on the startup path) and narrate it only when
        # behind — nobody has to remember to ask. `sbx_control`'s concierge
        # tool `version_status` answers the same question on demand.
        start_drift_check(
            versions,
            (
                (
                    lambda text: bridge.daemon_notice(
                        DaemonNotice(kind="daemon.version_drift", text=text, level="warning")
                    )
                )
                if bridge is not None
                else None
            ),
        )

    ctl = ControlServer(loop, config.state_dir)
    cleanup_registry.install_handlers()
    cleanup_registry.set_quiesce(loop.quiesce)
    stop_reason = "finished"
    try:
        loop.recover()
        # Only now: an `abandon`/`requeue` served while recover() is still
        # settling the item it snapshotted would be overwritten by recovery's
        # own verdict. Requests submitted before this point are refused as
        # stale, never executed.
        ctl.start()
        if once:
            result = loop.tick()
            # --once is a smoke/cron probe: its one-line verdict stays on
            # stdout for the human or script that invoked it.
            console.print(f"tick: {result}")
            log.info(
                "daemon.tick",
                discovered=result.discovered,
                dispatched=result.dispatched,
                outcome=result.outcome,
                idle=result.idle_kind,
                idle_detail=result.idle_detail,
            )
        else:
            loop.run_forever()
    except KeyboardInterrupt:
        stop_reason = "interrupted"
        log.warning("daemon.interrupted", hint="KeyboardInterrupt; shutting down")
    except BaseException as exc:
        stop_reason = type(exc).__name__
        raise
    finally:
        cleanup_registry.set_quiesce(None)
        log.debug("daemon.shutdown", step="control server")
        ctl.close()
        if bridge is not None:
            log.debug("daemon.shutdown", step="discord bridge")
            bridge.close()
        if concierge is not None:
            # Forgets the handle; the concierge sandbox itself is kept for
            # the next daemon process (conversation memory lives in it).
            log.debug("daemon.shutdown", step="concierge")
            concierge.close()
        log.debug("daemon.shutdown", step="github sandbox")
        github.close()
        dstore.close()
        log.info(
            "daemon.stopped",
            reason=stop_reason,
            uptime_s=round(time.monotonic() - started_at, 1),
        )


def _daemon_state_dir() -> Path:
    # Same resolution as `sbxloop daemon` itself (#255): with the anchored
    # default the daemon's queue and control queue are not under the runner
    # dir's `.sbxloop`, so the operator commands must follow the daemon's
    # rule, not `_store`'s.
    from sbxloop.daemon.paths import resolve_state_dir

    config, sources = load_config_with_sources()
    return resolve_state_dir(config, sources, cwd=Path.cwd(), env=os.environ, home=Path.home()).path


def _daemon_store() -> DaemonStore:
    return DaemonStore(_daemon_state_dir() / "state.db")


_ITEM_CONTROL_NOTE = (
    "[dim]a live daemon notices the change within a second: an in-flight run for "
    "this item is cancelled and the issue is told on its next "
    "tick; with no daemon running, the next daemon start reports it and closes the "
    "dead run. The item's next dispatch, if any, starts a fresh run.[/]"
)


@daemon_app.command("items")
def daemon_items(
    state: Annotated[
        list[str] | None,
        typer.Option("--state", "-s", help="Only these states (repeatable)."),
    ] = None,
) -> None:
    """List work items with attempts, pinned run and last error."""
    from sbxloop.daemon.model import ItemState

    states = tuple(state or ())
    bad = [s for s in states if s not in get_args(ItemState)]
    if bad:
        console.print(f"[bold red]unknown item state:[/] {', '.join(bad)}")
        raise typer.Exit(2)
    dstore = _daemon_store()
    try:
        items = dstore.items(cast(Any, states) or None)
    finally:
        dstore.close()
    if not items:
        console.print("no work items")
        return
    table = Table(box=None, pad_edge=False)
    for col in ("item", "repo", "state", "attempts", "run", "title", "last error"):
        table.add_column(col)
    for i in items:
        table.add_row(
            i.item_id,
            _item_repo(i) or "—",
            i.state,
            str(i.attempts),
            i.run_id or "",
            i.title[:60],
            (i.last_error or "").splitlines()[0][:80] if i.last_error else "",
        )
    console.print(table)


@daemon_app.command("abandon")
def daemon_abandon(
    item_id: Annotated[str, typer.Argument(help="Work item id (e.g. gh:issue:12).")],
    reason: Annotated[str | None, typer.Option("--reason", help="Recorded as last error.")] = None,
) -> None:
    """Give up on a queued or running item; its run will not be resumed."""
    _item_control("abandon", item_id, reason)


@daemon_app.command("retry")
def daemon_retry(
    item_id: Annotated[str, typer.Argument(help="Work item id (e.g. gh:issue:12).")],
) -> None:
    """Re-queue a failed/blocked/cancelled item: attempts reset, fresh run (not a resume)."""
    _item_control("retry", item_id, None)


@daemon_app.command("requeue")
def daemon_requeue(
    item_id: Annotated[str, typer.Argument(help="Work item id (e.g. gh:issue:12).")],
) -> None:
    """Unpin a running item from its run so the next dispatch starts fresh (attempts kept)."""
    _item_control("requeue", item_id, None)


def _item_control(action: str, item_id: str, reason: str | None) -> None:
    # The legacy bare `gh:<n>` spelling is accepted from the command line and
    # normalised, so lookups and every message below show the typed id.
    item_id = normalize_item_id(item_id)
    dstore = _daemon_store()
    try:
        now = time.time()
        if action == "abandon":
            item = dstore.abandon(item_id, reason or "abandoned by operator", now)
        elif action == "retry":
            item = dstore.retry(item_id, now, "re-queued by operator (CLI)")
        else:
            item = dstore.requeue(item_id, now)
    except KeyError:
        console.print(f"[bold red]unknown work item:[/] {item_id}")
        raise typer.Exit(2) from None
    except ValueError as exc:
        console.print(f"[bold red]{action} refused:[/] {exc}")
        raise typer.Exit(2) from None
    finally:
        dstore.close()
    # highlight=False: rich would otherwise wrap the attempt count and run id
    # in colour codes, splitting the plain text a script (or test) greps for.
    console.print(
        f"{item.item_id}: [bold]{item.state}[/] (attempts {item.attempts}"
        + (f", run {item.run_id}" if item.run_id else "")
        + ")",
        highlight=False,
    )
    console.print(_ITEM_CONTROL_NOTE, highlight=False)


# `--retry` belongs to the daemon's `cancel` verb, not to this command: pass
# unknown options through as words so the CLI and Discord spell it the same.
@daemon_app.command("ctl", context_settings={"ignore_unknown_options": True})
def daemon_ctl(
    command: Annotated[
        list[str],
        typer.Argument(
            help="status | pause | resume | cancel [--retry] | queue | items | abandon <item> "
            "[reason] | retry <item> | requeue <item> (the Discord !sbx verbs)."
        ),
    ],
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            help="Seconds to wait for the daemon's reply. A request no daemon picks up in "
            "time is withdrawn (exit 2); one the daemon has already taken keeps "
            "executing and is reported as pending (exit 1).",
        ),
    ] = DEFAULT_TIMEOUT_S,
) -> None:
    """Send a command to the daemon running against this state_dir — the
    programmatic twin of Discord's `!sbx`, for scripts, cron and remote
    operators (the bot ignores its own messages by design)."""
    from sbxloop.daemon.control import ControlClient, plain

    state_dir = _daemon_state_dir()
    reply = ControlClient(state_dir).submit(" ".join(command), timeout_s=timeout)
    if reply is None:
        console.print(
            f"[bold red]no reply from the daemon[/] within {timeout:g}s — is "
            f"[cyan]sbxloop daemon[/] running with state dir {state_dir}?"
        )
        raise typer.Exit(2)
    if reply.pending:
        console.print(f"pending: {plain(reply.text)}", markup=False, highlight=False)
        raise typer.Exit(1)
    console.print(plain(reply.text), markup=False, highlight=False)
    if not reply.ok:
        raise typer.Exit(1)


@app.command()
def bake(
    ref: Annotated[
        str, typer.Option("--ref", help="Template reference to save (name:tag).")
    ] = DEFAULT_TEMPLATE_REF,
    base_template: Annotated[
        str | None,
        typer.Option("--from", help="Base template to bake from (default: the sbx base)."),
    ] = None,
    runtime_cache: Annotated[
        bool,
        typer.Option(
            "--runtime-cache/--no-runtime-cache",
            help="Pre-cache the Copilot runtime into the template.",
        ),
    ] = True,
    keep: Annotated[
        bool, typer.Option("--keep", help="Keep the scratch sandbox for debugging.")
    ] = False,
) -> None:
    """Bake a sandbox template with the worker preinstalled.

    Runs the full worker install once in a scratch sandbox and saves it
    via `sbx template save`. With `[sandbox] template` pointing at the
    saved ref, runs verify the baked worker with fast probes instead of
    reinstalling it on every provision (and fall back to the normal
    install if the template goes stale).
    """
    config = load_config()
    cli = SbxCLI(app_name=config.app_name or None)
    try:
        record = bake_template(
            cli,
            config,
            ref=ref,
            base_template=base_template,
            cache_runtime=runtime_cache,
            keep=keep,
            progress=lambda message: console.print(f"[dim]… {message}[/dim]", highlight=False),
        )
    except SbxloopError as exc:
        console.print(f"[bold red]bake failed:[/] {exc}")
        raise typer.Exit(2) from exc
    runtime = "cached" if record.runtime_cached else "[yellow]not cached[/]"
    console.print(
        f"baked [bold cyan]{record.ref}[/] "
        f"(worker {record.worker_version}, copilot runtime {runtime})"
    )
    if config.sandbox.template == record.ref:
        console.print("`[sandbox] template` already points at this ref — runs will use it.")
    else:
        console.print(
            f'set [cyan]\\[sandbox] template = "{record.ref}"[/] in sbxloop.toml to use it.'
        )


@app.command("list-models")
def list_models(
    json_output: Annotated[
        bool, typer.Option("--json", help="Machine-readable JSON on stdout (for scripting).")
    ] = False,
    timeout_s: Annotated[
        float,
        typer.Option("--timeout", help="Seconds to wait for the Copilot runtime and API."),
    ] = 60.0,
) -> None:
    """List the models the GitHub Copilot SDK gives this host access to.

    Queries the SDK directly on the host (no sandbox) with the same auth
    chain agent sessions use, so the ids shown here are valid values for
    `model` in sbxloop.toml and `sbxloop run --model`.
    """
    from sbxloop.cli.models import fetch_models, format_context, format_efforts, model_row

    config = load_config()
    try:
        rows = [model_row(info) for info in fetch_models(timeout_s=timeout_s)]
    except SbxloopError as exc:
        # escape(): the install hint (`sbxloop[copilot]`) and arbitrary SDK
        # error text must not be parsed as rich markup.
        console.print(f"[bold red]list-models failed:[/] {rich_escape(str(exc))}")
        raise typer.Exit(2) from exc
    if json_output:
        # bare JSON on stdout, nothing else — `sbxloop list-models --json | jq`
        typer.echo(json.dumps([row.raw or {"id": row.id, "name": row.name} for row in rows]))
        return
    table = Table(title="copilot models")
    for column in ("model", "name", "billing", "context", "vision", "reasoning", "policy"):
        table.add_column(column)
    for row in rows:
        configured = row.id == config.model
        # SDK-provided text is escaped: a model name with brackets must not
        # be parsed as rich markup.
        table.add_row(
            f"[bold cyan]{rich_escape(row.id)}[/] ◀" if configured else rich_escape(row.id),
            rich_escape(row.name),
            f"{row.multiplier:g}x" if row.multiplier is not None else "",
            format_context(row.context_window),
            "yes" if row.vision else "",
            format_efforts(row),
            row.policy_state or "",
        )
    console.print(table)
    if not rows:
        console.print(
            "[yellow]the SDK returned no models[/] — the subscription may have "
            "no model access, or model policy blocks them all"
        )
    marker = (
        f"◀ = configured model ({config.model})"
        if any(row.id == config.model for row in rows)
        else f"configured model: {config.model}"
        + (" (the SDK picks one per session)" if config.model == "auto" else " — not in this list!")
    )
    console.print(f"[dim]{marker}; * = default reasoning effort[/]")


@app.command()
def doctor(
    deep: Annotated[
        bool,
        typer.Option(
            "--deep",
            help="Also run the live-sandbox sbx conformance probes (boots one "
            "scratch sandbox) and refresh the version-keyed verdict cache.",
        ),
    ] = False,
    fail_on_drift: Annotated[
        bool,
        typer.Option(
            "--fail-on-drift",
            help="Exit 1 when any sbx conformance probe drifted, errored, or is "
            "unprobed for the installed sbx version (CI gate; drift is otherwise "
            "a warning).",
        ),
    ] = False,
) -> None:
    """Check that this host is ready to run sbxloop."""
    ok = run_doctor(console, deep=deep, fail_on_drift=fail_on_drift)
    raise typer.Exit(0 if ok else 1)


DEFAULT_CONFIG_TOML = """\
# sbxloop configuration. Every key is optional; these are the defaults.
# Precedence: SBXLOOP_* env vars > this file > pyproject [tool.sbxloop]
# > ~/.config/sbxloop/sbxloop.toml (user-level defaults).

# Copilot model for agent sessions ("auto" lets the SDK choose).
model = "auto"
# Optional sbx --app-name isolating sbxloop's sandboxes/policies/secrets.
# Empty shares your normal sbx state (your login + balanced policy apply).
# If set, that isolated state needs its own `sbx --app-name <name> login`
# and `sbx --app-name <name> policy init balanced`.
app_name = ""
# Where run state (SQLite) and per-run workspaces live. Per-user by default
# so status/logs see the same runs from any directory; a relative path
# (e.g. ".sbxloop") scopes state to this project instead.
state_dir = "~/.sbxloop"
# Keep sandboxes around after a run (for debugging).
keep_sandboxes = false
# Keep the pair alive only when a run fails; inspect with `sbxloop shell <run>`.
keep_on_failure = false
# Worker transport: "stream" (default) or "poll".
worker_transport = "stream"
# Secret injection: "proxy" (sbx keychain proxy; recommended) or "plain-env".
secret_strategy = "proxy"

[sandbox]
# Custom sandbox template reference (defaults to the sbx shell template).
# `sbxloop bake` builds one with the worker preinstalled, cutting the
# per-run install out of provisioning.
# template = "sbxloop-baked:latest"
# Where runs execute. Unset (the default) gives every run a fresh directory
# under state_dir. Point it at an existing project to work on that code.
# workspace = "path/to/checkout"
# When `workspace` is a git checkout: "auto" (default) isolates each run in
# a per-run clone on branch sbxloop/<run_id> — the checkout and its branches
# are never touched, and a dirty tree refuses the run (uncommitted changes
# would silently not travel). "clone" also isolates but runs from committed
# HEAD even when dirty (and requires a git workspace). "in-place" mutates
# the workspace directly (the pre-0.6 behavior).
# workspace_isolation = "auto"
# Extra network allow rules applied to both sandboxes.
extra_allow_domains = []

[policy]
# Bounds for plan-declared egress. At PLAN time the agent may declare extra
# domains a task needs during EXECUTE (each with a justification); they are
# auto-granted to the agent sandbox just before EXECUTE only when they match
# `allow` and no `deny` pattern, and every grant/refusal is logged as a run
# event (`sbxloop logs RUN --type policy.`). Patterns: exact domains,
# "*.example.com" (the domain and all subdomains), or "*" (everything).
# Empty `allow` (the default) means plans may only use the always-reachable
# baseline (the Copilot/GitHub hosts, the supported languages' package
# registries, and apt mirrors) plus the well-known package registries plans
# may declare without any configuration here. `deny` wins over both,
# including the always-reachable baseline.
# See `sbxloop config policy` for the effective per-phase policy.
allow = []
deny = []

[github]
# The GitHub integration. Unset (the default) disables GitHub entirely:
# no github sandbox is provisioned, GH_TOKEN is not required, and a run
# stops after its gate with the work in the workspace. Set `repo` to the
# ONE repository sbxloop works with: every run that passes its gate opens a
# pull request there and carries it through review, CI and merge (see
# [landing]). GH_TOKEN needs contents:write + pull_requests:write on it.
# `sbxloop run --repo owner/repo` overrides this per run.
# repo = "you/your-repo"
# deliver_base = "main"   # base branch; unset uses the repo's default
# Create `repo` when it does not exist (probed up front, so a typo'd repo
# fails the run before any work; needs a token allowed to create repos).
# create_repo = false
# create_public = false   # created repos are private unless flipped
#
# Several repositories: declare them as an array of tables instead of the
# single `repo` above (the two forms are mutually exclusive — migrate by
# moving `repo` and its delivery settings into one entry; a lone `[github]
# repo` keeps working unchanged). The daemon polls every enabled entry for
# the trigger label and routes each run — clone, branch, PR, review, CI,
# merge, issue comments — to the repository its work item came from.
# Everything here is PER REPOSITORY; the [daemon] guardrails (daily run
# cap, per-item attempt cap, circuit breaker, one run at a time) stay
# daemon-wide and are shared across all of them.
# [[github.repos]]
# repo = "you/one"
# deliver_base = "main"        # base branch; unset uses the repo's default
# [[github.repos]]
# repo = "you/two"
# enabled = false              # registered but not polled
# token_env = "GH_TOKEN_TWO"   # unset uses the daemon-wide GH_TOKEN
# trigger_label = "sbxloop:go" # unset uses [daemon] trigger_label
# labels = ["team:core"]       # extra labels for issues/PRs in this repo

[landing]
# What happens after the tasks are built: the PR opens as a draft, the run
# reviews its own diff, spends bounded fix rounds on what the review, CI or
# the base branch object to, un-drafts and merges. Merging is not optional —
# a run that cannot land its PR ends `blocked` with the PR left open for a
# human. On a repo whose merges publish, every merged run is a release.
# deliver_draft = true
# max_review_rounds = 3      # times the review may request changes
# max_ci_rounds = 2          # red gate / red CI / conflict / human objection rounds
# ci_poll_interval_s = 60
# ci_settle_s = 90           # "no check runs yet" must persist this long to mean "no CI"
# ci_timeout_s = 3600        # per wait; exceeding it ends the run blocked
# merge_method = "squash"    # squash | merge | rebase
# delete_branch_on_merge = true
# merge_update_attempts = 3  # update-branch calls when protection wants "up to date"

[artifacts]
# Path components excluded from artifact listings, harvest and delivery,
# matched at any depth (a bare name each, no slashes). Setting this replaces
# the default list below rather than adding to it. The default drops run/VCS
# state plus regenerable dependency and build trees — dot-path artifacts like
# .github/ or .gitignore are kept, and so are the ambiguous generic names
# (bin, build, dist, out, lib, vendor), which you can add if you want them
# dropped. Exclusions are counted and surfaced, never silent.
exclude = [
  ".git", ".sbxloop",
  ".mypy_cache", ".nox", ".pytest_cache", ".ruff_cache", ".tox",
  ".venv", "venv", "__pycache__", "*.egg-info",   # Python (globs allowed)
  "node_modules",                            # JavaScript / TypeScript
  "target",                                  # Rust (cargo), Java (Maven)
  ".gradle",                                 # Java (Gradle)
  "obj",                                     # C# / .NET
  ".bundle",                                 # Ruby (bundler)
  "CMakeFiles",                              # C / C++
]

[budgets]
# Sized for a small greenfield project. A large existing repo (thousands of
# tests, a multi-package tree to orient in) wants a bigger wall clock and
# tool cap — see contrib/presets/large-repo.toml.
max_revisions_per_task = 2
max_replans_per_task = 1
max_tasks = 20
max_wall_clock_s = 7200.0
per_job_timeout_s = 1800.0
max_tool_calls_per_phase = 40   # 0 = unbounded; past it the agent is told to wrap up

[limits]
# Sandbox resource guardrails (percent used; 0 disables). Sampled in-VM on
# the worker heartbeat and shown as a gauge in the TUI status panel.
# Crossing disk_abort fails the current task with an explicit
# "sandbox disk exhausted" error; mem_abort does the same for memory (off by
# default: a parallel test run legitimately spikes memory for a heartbeat).
disk_warn = 85.0
disk_abort = 95.0
mem_warn = 90.0
mem_abort = 0.0

[daemon]
# `sbxloop daemon` — the always-on outer loop. Polls every configured,
# enabled [github] repository for
# issues carrying trigger_label; each one becomes ONE run that builds the
# work, opens a draft PR, reviews and fixes it, waits for CI and merges it
# (the landing knobs live under [landing]). The issue closes with
# completed_label when the PR merges, gets failed_label when the run gave
# up, blocked_label when GitHub would not let the loop finish. The daemon
# never files work of its own; a label alone starts a run, so the guardrails
# below are what stand between a mislabeled issue and your budget. All of
# them are DAEMON-WIDE: with several repositories configured, the daily run
# cap, the per-item attempt/resume caps, the consecutive-failure circuit
# breaker and one-run-at-a-time are shared across every repository.
# poll_interval_s = 60.0
# trigger_label = "sbxloop:run"    # issue label that queues work
# in_progress_label = "sbxloop:in-progress"
# failed_label = "sbxloop:failed"
# completed_label = "sbxloop:completed"  # applied when the PR merges
# blocked_label = "sbxloop:blocked"      # the loop could not land the PR; a human looks
# max_runs_per_day = 12           # calendar-day cap, persisted across restarts
# run_cap_timezone = "UTC"        # day boundary for the run cap (resets at 00:00 there)
# max_attempts_per_item = 2
# max_resumes_per_item = 2         # interrupted runs resumed at most this often per item
# retry_backoff_s = 900.0          # times the attempt number
# max_consecutive_failures = 3     # circuit breaker ...
# breaker_cooldown_s = 3600.0      # ... and how long it stays open
# shutdown_grace_s = 60.0          # keep below systemd TimeoutStopSec
# Retention for .sbxloop/runs/<run>/ (workspace clones, harvested artifacts):
# swept on daemon start and daily; `sbxloop gc` for non-daemon use. 0 disables.
# prune_runs_after_days = 14
# Unattended workspace posture. Point [sandbox] workspace at a dedicated clone
# nobody edits; before each run the daemon `git fetch`es it and fast-forwards
# to origin (never merges/rebases), and daemon runs use `clone` isolation so a
# dirty tree proceeds from HEAD with a warning instead of `auto`'s refusal.
# workspace_isolation = "clone"    # clone | auto | in-place, for daemon runs
# refresh_workspace = true
# Daemon state lives OUTSIDE the workspace, at an absolute path. Unset:
# $XDG_STATE_HOME/sbxloop/<runner-dir-name> (~/.local/state/...), unless the
# top-level state_dir is set or a legacy ./.sbxloop/state.db already exists.
# `sbxloop daemon items|abandon|retry|requeue` follow the same rule;
# `sbxloop status`/`logs`/`gc` need SBXLOOP_STATE_DIR pointed there.
# state_dir = "~/.local/state/sbxloop/my-project"

[discord]
# The daemon's human channel: a gateway bot posts each run's chronology
# (agent messages, tool lines, issue/PR links) into a thread under a control
# channel and relays replies typed in the thread to the running agent as
# steering. Needs `pip install 'sbxloop[discord]'` and DISCORD_BOT_TOKEN in
# the environment / .env (never here). Anyone who can post in the channel can
# steer — restrict the channel. Unset channel_id = Discord off.
# channel_id = 123456789012345678
# command_prefix = "!sbx"          # !sbx status|pause|resume|cancel|queue
# thread_per_run = true
# chronology_level = "normal"      # quiet (lifecycle+links+chat) | normal (tool bursts
#                                  # digested into one edited line) | verbose (every call)
# max_message_chars = 1900
# embeds = true                    # headline / finished / status as embed cards
# status_line = true               # one per-run message edited as tasks progress
# tool_batch_lines = 8             # verbose: consecutive tool calls per code block
# tool_output_lines = 0            # tail output lines echoed for a successful call (0 = none)
# tool_fail_output_lines = 20      # head+tail output lines echoed for a failed call

[concierge]
# The control channel's agent: @mention the bot (or reply to it) to ask
# about runs, PRs and diffs, operate the daemon (every !sbx verb) or queue
# new work in plain language. Runs as a Copilot session in a long-lived
# agent sandbox and reaches the daemon only through host tools; needs
# COPILOT_GITHUB_TOKEN on the daemon host. Acts with the same authority as
# !sbx. Effective only when [discord] is enabled.
# enabled = true
# model = ""                       # empty = the top-level model
# timeout_s = 180                  # one message's wall-clock budget
# max_tool_calls = 16
# session_turns = 40               # rotate the resumed SDK session after N turns
# github_tools = true              # PR/issue/diff/file reads via the github-ops sandbox
# create_issues = true             # file (queued at once)/list/comment/label/close issues
#                                  # (a close needs your yes)
"""


def main() -> None:
    app()


if __name__ == "__main__":
    main()
