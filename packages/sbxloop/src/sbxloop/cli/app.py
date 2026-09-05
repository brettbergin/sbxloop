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
from importlib import resources
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
from sbxloop.backends import backend_for
from sbxloop.cli.doctor import run_doctor
from sbxloop.cli.tui import ChatInput, Dashboard, format_event, plain_printer, render_event
from sbxloop.config import (
    ChatConfig,
    Config,
    DaemonConfig,
    DiscordConfig,
    GithubConfig,
    RepoConfig,
    SlackConfig,
    load_config,
    load_config_with_sources,
    load_dotenv_file,
)
from sbxloop.daemon.control import DEFAULT_TIMEOUT_S
from sbxloop.daemon.store import DaemonStore, apply_item_verb
from sbxloop.daemon.versions import VersionProbe, start_drift_check
from sbxloop.engine.engine import LoopEngine
from sbxloop.engine.model import (
    TERMINAL_RUN_STATES,
    RunResult,
    TaskRecord,
    artifacts_dir,
    scan_artifacts,
    workload_summary,
)
from sbxloop.engine.sinks import published_line
from sbxloop.engine.store import StateStore
from sbxloop.errors import SbxloopError
from sbxloop.events import Event, EventBus, HostEventTypes
from sbxloop.gc import DAY_S, format_bytes, prune_run_dirs
from sbxloop.ghids import normalize_item_id, try_parse_gh_id
from sbxloop.log import configure_logging, get_logger
from sbxloop.sbx.bake import DEFAULT_TEMPLATE_REF, bake_template
from sbxloop.sbx.cli import INTERACTIVE_SHELL_ARGV, SbxCLI
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
    clean_secrets,
    rotate_registrations,
    secret_rows,
    secrets_context,
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
    config = _run_config()
    updates = {k: v for k, v in overrides.items() if v is not None}
    return config.model_copy(update=updates) if updates else config


def _run_config() -> Config:
    """Config for a command that reads or writes *runs*, with ``state_dir``
    pointing where this project's runs actually live.

    On a daemon host the daemon anchors its state away from the top-level
    default (#255), so a command that trusts ``state_dir`` verbatim reports
    an unrelated — usually stale, often empty — world: `sbxloop status` in
    the runner directory answers about neither the daemon's runs nor
    anything else current. ``sbxloop daemon`` and its ``ctl`` subcommands
    already resolve this way (``_daemon_state_dir``); the run commands were
    left behind.

    Stamped onto the config rather than applied at each read, because a run
    directory is derived from ``state_dir`` too (``LoopEngine``): resolving
    only the store would file a run's rows in one place and its workspace
    and artifacts in another. See ``paths.resolve_cli_state_dir`` for when
    the redirect fires at all — never on a host with no daemon store.
    """
    from sbxloop.daemon.paths import resolve_cli_state_dir

    config, sources = load_config_with_sources()
    resolved = resolve_cli_state_dir(
        config, sources, cwd=Path.cwd(), env=os.environ, home=Path.home()
    ).path
    if resolved == config.state_dir:
        return config
    return config.model_copy(update={"state_dir": resolved})


def _store(config: Config) -> StateStore:
    return StateStore(config.state_dir / "state.db")


def _resolve_run_workspace(
    config: Config, flag: Path | None, *, cwd: Path
) -> tuple[Config, Path | None, str]:
    """The checkout a one-shot ``sbxloop run`` works on, and where it came from.

    In order: ``--workspace``; whatever the config already resolves for the
    run's repository (or configures for another — that run clones from its
    remote instead, see ``Provisioner._resolve_workspace_source``); the git
    checkout enclosing ``cwd``. A run from inside a checkout used to operate
    on an *empty* per-run directory and report success (#670) — the
    enclosing checkout is what the person typing the command means.

    None with no checkout anywhere is harvest mode, kept as is: the agent
    starts from nothing and the output is collected as artifacts.
    """
    from sbxloop import hostgit

    if flag is not None:
        chosen = flag.expanduser()
        if not chosen.is_dir():
            raise SbxloopError(f"{chosen} is not a directory")
        return _pin_workspace(config, chosen.resolve()), chosen.resolve(), "--workspace"
    source = config.workspace_source(config.github.repo)
    if source == "configured":
        return config, config.workspace_for_repo(config.github.repo), source
    if source == "remote":
        # Configured for some other repository: the provisioner clones this
        # one from its remote rather than borrowing that checkout.
        return config, None, source
    root = hostgit.repo_toplevel(cwd)
    if root is None:
        return config, None, "none"
    return _pin_workspace(config, root), root, "cwd-checkout"


_WORKSPACE_SOURCE_TEXT = {
    "--workspace": "from --workspace",
    "configured": "configured",
    "cwd-checkout": "the git checkout enclosing the current directory",
}


def _pin_workspace(config: Config, workspace: Path) -> Config:
    """Make ``workspace`` the one checkout this run works on.

    Set on the ``[sandbox]`` section and on every repository entry the run
    still carries (at most one after ``run`` narrows), so
    ``Config.workspace_for_repo`` resolves to it whichever path it takes and
    the provisioner's origin check still guards a checkout of the wrong
    repository.
    """
    sandbox = config.sandbox.model_copy(update={"workspace": workspace})
    repos = [entry.model_copy(update={"workspace": workspace}) for entry in config.github.repos]
    github = config.github.model_copy(update={"repos": repos})
    return config.model_copy(update={"sandbox": sandbox, "github": github})


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
        nodes[parent].add(f"{rel.name} [dim]({_human_size(path.lstat().st_size)})[/]")
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
    if result.kind != "code":
        console.print(f"  kind: {result.kind}")
    if result.reason:
        console.print(f"  reason: {result.reason}")
    if result.summary:
        # A workload's closing line (#757); the per-task lines follow.
        console.print(f"  summary: {rich_escape(result.summary.splitlines()[0])}")
    for task in result.tasks:
        line = f"  {task.spec.id}: {task.state}  ({rich_escape(task.spec.title)})"
        if task.output is not None:
            # A workload task's own result line (#757).
            line += f"\n      {_output_cell(task)}"
        console.print(line)
    for entry in result.published:
        # Where a workload's result went (#759), one line per sink.
        console.print(f"  published: {rich_escape(published_line(entry))}")
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
    kind: Annotated[
        str,
        typer.Option(
            "--kind",
            help="What the run is for: `code` (the developer loop: a task graph that "
            "ends in a pull request) or `workload` (the operator persona: plan, "
            "execute, judge, publish — in its own data directory, no repository).",
        ),
    ] = "code",
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="The [[workloads]] profile a `--kind workload` run is bounded by "
            "(default: [workload] default; none at all lets the plan declare no needs).",
        ),
    ] = None,
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
    workspace: Annotated[
        Path | None,
        typer.Option(
            "--workspace",
            "-w",
            help="Repository checkout to work on. Default: [sandbox] workspace (or the "
            "repo entry's), then the git checkout enclosing the current directory.",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Model id for the configured [agent] backend (`sbxloop list-models`).",
        ),
    ] = None,
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
    if kind not in ("code", "workload"):
        console.print(f"[bold red]invalid --kind:[/] {kind!r} (expected `code` or `workload`)")
        raise typer.Exit(2)
    if profile is not None and kind != "workload":
        console.print(
            "[bold red]--profile cannot be combined with --kind code:[/] a workload "
            "profile bounds what a workload's plan may ask for"
        )
        raise typer.Exit(2)
    if kind == "workload":
        # A workload works in its own data directory on the agent sandbox
        # alone (#755); a checkout or a repository is a code run's, and
        # what a workload may do with either comes with its config (#758).
        refused = [
            flag
            for flag, value in (
                ("--repo", repo),
                ("--deliver-base", deliver_base),
                ("--create-repo", create_repo),
                ("--create-public", create_public),
                ("--workspace", workspace),
            )
            if value is not None
        ]
        if refused:
            console.print(
                f"[bold red]{', '.join(refused)} cannot be combined with --kind workload:[/] "
                "a workload runs in its own data directory and delivers to no repository"
            )
            raise typer.Exit(2)
        console.print(
            "workspace: a per-run data directory — the agent starts from an empty "
            "directory and the run's output is harvested as artifacts"
        )
        try:
            chosen_profile = config.workload_profile(profile)
        except SbxloopError as exc:
            console.print(f"[bold red]{exc}[/]")
            raise typer.Exit(2) from exc
        console.print(
            f"profile: {chosen_profile.name}"
            if chosen_profile is not None
            else "profile: none (no needs can be granted)"
        )
        engine = LoopEngine(config)
        try:
            result = _drive_with_ui(
                engine,
                tui=tui,
                chat=chat,
                action=lambda: engine.start(outcome, kind="workload", profile=profile),
            )
        except SbxloopError as exc:
            console.print(f"[bold red]run failed:[/] {exc}")
            raise typer.Exit(2) from exc
        _finish(result, config)
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
    try:
        config, chosen, source = _resolve_run_workspace(config, workspace, cwd=Path.cwd())
    except SbxloopError as exc:
        console.print(f"[bold red]invalid --workspace:[/] {exc}")
        raise typer.Exit(2) from exc
    if chosen is not None:
        console.print(f"workspace: {chosen} ({_WORKSPACE_SOURCE_TEXT[source]})")
    elif source == "remote":
        console.print(
            f"workspace: a fresh clone of {config.github.repo} (the configured checkout "
            "belongs to another repository)"
        )
    else:
        console.print(
            "workspace: none — not inside a git checkout and none configured; the "
            "agent starts from an empty directory and the run's output is harvested "
            "as artifacts (pass [cyan]--workspace PATH[/] to work on a checkout)"
        )
    engine = LoopEngine(config)
    try:
        result = _drive_with_ui(
            engine,
            tui=tui,
            chat=chat,
            action=lambda: engine.start(outcome, workspace_source=source),
        )
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
    grant_rounds: Annotated[
        int,
        typer.Option(
            "--grant-rounds",
            min=0,
            help="Give a run that exhausted its fix rounds this many more before resuming.",
        ),
    ] = 0,
) -> None:
    """Resume an unfinished run (fresh sandboxes, persisted state and config)."""
    config = _run_config()
    engine = LoopEngine(config)
    try:
        if grant_rounds:
            total = engine.store.grant_rounds(run_id, grant_rounds)
            console.print(
                f"run {run_id}: {grant_rounds} more fix round(s) granted ({total} in all)"
            )
        result = _drive_with_ui(engine, tui=tui, chat=chat, action=lambda: engine.resume(run_id))
    except SbxloopError as exc:
        console.print(f"[bold red]resume failed:[/] {exc}")
        raise typer.Exit(2) from exc
    # engine.config is the run's rehydrated config, which is what drove the run.
    _finish(result, engine.config)


@app.command()
def cancel(run_id: Annotated[str, typer.Argument()]) -> None:
    """Mark a run cancelled (takes effect at the next phase boundary)."""
    config = _run_config()
    engine = LoopEngine(config)
    try:
        engine.cancel(run_id)
    except SbxloopError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc
    console.print(f"run {run_id} cancelled")


def _output_cell(task: TaskRecord) -> str:
    """One task's output as the status table shows it (#757)."""
    if task.output is None:
        return "[dim]—[/]"
    cell = rich_escape(task.output.summary) or "[dim](no result reported)[/]"
    if count := task.output.file_count:
        cell += f" [dim]({count} file{'s' if count != 1 else ''})[/]"
    return cell


@app.command()
def status(
    run_id: Annotated[str | None, typer.Argument(help="Run id for details.")] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="With a run id: the run, its tasks and their outputs as one JSON "
            "object on stdout, for scripts.",
        ),
    ] = False,
) -> None:
    """List runs, or show one run's tasks, outputs and phase history."""
    config = _run_config()
    store = _store(config)
    if run_id is None:
        if json_output:
            console.print("[bold red]--json needs a run id.[/]")
            raise typer.Exit(2)
        table = Table(title="sbxloop runs")
        table.add_column("run")
        table.add_column("kind")
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
                record.kind,
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
    tasks = store.get_tasks(run_id)
    if json_output:
        # Bare JSON on stdout, nothing else — `sbxloop status <run> --json | jq`.
        # A workload's tasks carry their outputs (#757); a code run's are null.
        typer.echo(
            json.dumps(
                {
                    "run": record.model_dump(mode="json"),
                    "summary": (
                        workload_summary(tasks, record.pr_title)
                        if record.kind == "workload"
                        else None
                    ),
                    "tasks": [task.model_dump(mode="json") for task in tasks],
                }
            )
        )
        return
    console.print(f"run [bold cyan]{record.run_id}[/]  state: [bold]{record.state}[/]")
    console.print(f"kind: {record.kind}")
    repo = _run_repo(store, record.run_id)
    if repo:
        console.print(f"repo: [bold]{repo}[/]")
    if record.reason:
        console.print(f"reason: {record.reason}")
    console.print(f"outcome: {record.outcome}")
    for entry in record.published:
        console.print(f"published: {rich_escape(published_line(entry))}")
    table = Table(title="tasks")
    columns: tuple[str, ...] = ("task", "title", "state", "revisions", "replans")
    if record.kind == "workload":
        columns += ("output",)
    for column in columns:
        table.add_column(column)
    for task in tasks:
        row = [
            task.spec.id,
            task.spec.title,
            task.state,
            str(task.revisions),
            str(task.replans),
        ]
        if record.kind == "workload":
            row.append(_output_cell(task))
        table.add_row(*row)
    console.print(table)
    attempts = store.phase_attempts(run_id)
    console.print(f"{len(attempts)} phase attempts recorded")
    # The pair names, so debugging a live run needs no by-hand
    # `sbxloop-<run>-agent` reconstruction.
    console.print("sandboxes:")
    # The service sandbox (#765) exists only for a run granted credentials;
    # the github sandbox never for a workload (#755).
    roles: tuple[SandboxRole, ...] = (
        ("agent",) if record.kind == "workload" else ("agent", "github")
    )
    if record.credentials:
        roles += ("service",)
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
    config = _run_config()
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
    if role not in ("agent", "github", "service"):
        console.print(f"[bold red]invalid --role {role!r}:[/] must be agent, github or service")
        raise typer.Exit(2)
    config = _run_config()
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
    argv = ("sh", "-lc", command) if command else INTERACTIVE_SHELL_ARGV
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
    config = _run_config()
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
            # lstat: a symlink lists as itself, even one that does not resolve
            size = _human_size(file.lstat().st_size)
            console.print(f"  {file.relative_to(target)}  [dim]{size}[/]")
    if scan.excluded_note:
        console.print(f"  [dim]{scan.excluded_note}[/]")


@sandbox_app.command("ls")
def sandbox_ls() -> None:
    """List sbxloop-managed sandboxes."""
    config = _run_config()
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
    config = _run_config()
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


def _secrets_context(config: Config | None = None) -> tuple[Config, SbxCLI, set[str]]:
    """Config, an sbx handle, and the live sbxloop sandbox names (for
    telling in-use registration scopes from stale ones)."""
    config = load_config() if config is None else config
    cli, live = secrets_context(config)
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
        for row in secret_rows(config, cli, live, probe=probe):
            warned = warned or row.judgement.status == "warn"
            table.add_row(
                row.env,
                row.expected,
                row.actual,
                _STATUS_STYLES[row.judgement.status],
                row.judgement.note,
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
        for outcome in clean_secrets(config, cli, live, apply=apply, all_=all_):
            if outcome.failed:
                console.print(f"[bold red]{outcome.env}: {outcome.message}[/]")
                failed = True
            elif outcome.removed and apply:
                console.print(f"[green]{outcome.env}: {outcome.message}[/]")
                removed_any = True
            else:
                console.print(f"{outcome.env}: {outcome.message}")
                removed_any = removed_any or outcome.removed
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
            "the configured agent backend's environment variable "
            "(COPILOT_GITHUB_TOKEN, or ANTHROPIC_API_KEY under the claude backend) / ./.env.",
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
    """Rotate the agent credential's sbx registration in one step.

    Which credential follows `[agent] backend` (the Copilot token by
    default, the Anthropic key under the claude backend). Replaces the
    existing registration (wherever its scope) with a global one carrying
    the canonical host binding — the rm + set-custom dance provisioning
    would otherwise perform mid-run. The token is read from the
    environment/.env or an interactive prompt, never from argv.
    """
    try:
        config = load_config()
        token_env = backend_for(config).token_env
        if prompt:
            token = typer.prompt(f"new {token_env}", hide_input=True)
        else:
            token = os.environ.get(token_env, "")
            if not token:
                console.print(
                    f"[bold red]{token_env} is not set.[/] Export the new token "
                    "(or put it in ./.env), or pass [cyan]--prompt[/] to type it — "
                    "it is never accepted as a command-line argument."
                )
                raise typer.Exit(2)
        config, cli, live = _secrets_context(config)
        styles = {"ok": "[green]{}[/]", "warn": "[yellow]{}[/]", "note": "{}"}
        for kind, line in rotate_registrations(config, cli, live, token=token):
            if kind == "warn" and "update your export" in line and not prompt:
                continue  # the token came from the environment: it is current there
            console.print(styles[kind].format(line))
        # Under plain-env the strategy line above already says it all.
        if config.secret_strategy != "plain-env" and verify:  # nosec B105 - strategy label
            workspace = config.state_dir / "secretcheck"
            workspace.mkdir(parents=True, exist_ok=True)
            visible = verify_secret_visibility(
                cli,
                env=token_env,
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
    config = _run_config()
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
            if v.role in ("agent", "github", "service"):
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
    config = _run_config()
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

    dumped = config.model_dump(mode="json")
    # The catalogue and the profiles are lists of tables: shown by name
    # below rather than as one repr each. A credential's value is never in
    # the model — only whether its env var is set is worth showing.
    dumped.pop("credentials", None)
    dumped.pop("workloads", None)
    flatten("", dumped)
    for dotted in sorted(flat):
        table.add_row(dotted, repr(flat[dotted]), sources.get(dotted, "default"))
    console.print(table)
    if config.credentials:
        creds = Table(title="credentials (values never shown)")
        for col in ("name", "env", "present", "host", "description"):
            creds.add_column(col)
        for entry in config.credentials:
            present = "set" if os.environ.get(entry.env) else "[yellow]unset[/]"
            creds.add_row(entry.name, entry.env, present, entry.host, entry.description)
        console.print(creds)
    if config.workloads:
        profiles = Table(title="workload profiles")
        for col in ("name", "egress", "credentials", "sinks", "repo", "publish", "budgets"):
            profiles.add_column(col)
        for prof in config.workloads:
            overrides = prof.budgets.model_dump(exclude_none=True)
            profiles.add_row(
                prof.name + (" (default)" if prof.name == config.workload.default else ""),
                ", ".join(prof.egress) or "-",
                ", ".join(prof.credentials) or "-",
                ", ".join(prof.sinks) or "-",
                "yes" if prof.repo else "no",
                prof.publish,
                ", ".join(f"{k}={v}" for k, v in overrides.items()) or "-",
            )
        console.print(profiles)


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
            config.labels_for(entry.repo).trigger,
        )
    console.print(table)


@config_app.command("policy")
def config_policy() -> None:
    """Show the effective per-phase network egress policy."""
    from sbxloop.cli.policyview import policy_view

    try:
        config = load_config()
    except SbxloopError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc
    view = policy_view(config)

    table = Table(title="agent sandbox: effective egress per phase")
    table.add_column("phase", no_wrap=True)
    table.add_column("policy", overflow="fold")
    for phase, policy in view.phases:
        table.add_row(phase, policy)
    console.print(table)
    console.print(f"baseline (provisioned per-sandbox): {view.baseline}")
    console.print(
        f"language registry baseline (always reachable, no declaration): {view.registries}"
    )
    console.print(f"distro mirrors (always reachable, no declaration): {view.mirrors}")
    console.print(f"well-known registries (declarable without [policy] allow): {view.well_known}")

    bounds = Table(title="[policy] bounds for task-declared grants")
    bounds.add_column("bound", no_wrap=True)
    bounds.add_column("patterns", overflow="fold")
    bounds.add_row("allow", view.allow)
    bounds.add_row("deny", view.deny)
    console.print(bounds)

    if view.github is not None:
        console.print(f"github sandbox (all phases, no task grants): {view.github}")
    if view.service is not None:
        console.print(
            "service sandbox (fetches from the credentialed registries; the agent "
            f"reaches none of them): {view.service}"
        )
    console.print(f"audit trail: [cyan]{view.audit}[/]")


@app.command()
def init(
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
    to_stdout: Annotated[
        bool,
        typer.Option("--stdout", help="Print the template instead of writing sbxloop.toml."),
    ] = False,
    preset: Annotated[
        str | None,
        typer.Option(
            "--preset",
            help=(
                "Append a packaged preset's live sections to the template, e.g. "
                "`large-repo` for a repository whose gate takes minutes."
            ),
        ),
    ] = None,
) -> None:
    """Write a commented sbxloop.toml with the default configuration.

    The template is `sbxloop.toml.example`, shipped as package data and
    published at the repository root — one source of truth, so the example
    file and this command cannot drift. `--preset NAME` appends the packaged
    `presets/NAME.toml` (live sections, every table in the template is
    commented out) so the result is one self-contained file.
    """
    try:
        text = render_config_template(preset)
    except KeyError:
        available = ", ".join(sorted(config_presets())) or "none"
        console.print(f"unknown preset {preset!r} (available: {available})")
        raise typer.Exit(2) from None
    if to_stdout:
        # bare TOML on stdout, nothing else — `sbxloop init --stdout > f.toml`
        sys.stdout.write(text)
        return
    path = Path("sbxloop.toml")
    if path.exists() and not force:
        console.print("sbxloop.toml already exists (use --force to overwrite)")
        raise typer.Exit(2)
    path.write_text(text)
    console.print(f"wrote {path}")


@app.command("init-repo")
def init_repo_command(
    repo: Annotated[str, typer.Argument(help="The repository, owner/name.")],
) -> None:
    """Create the labels sbxloop relies on in a repository (idempotent).

    The seven lifecycle labels (with this repository's renames from
    `[[github.repos]]` applied) and the follow-up label, each with a color
    and a description; existing labels are left alone. Boots one github-ops
    sandbox for the writes, as `doctor --probe` does.
    """
    from sbxloop.cli.initrepo import init_repo

    config = load_config()
    ok = init_repo(config, SbxCLI(app_name=config.app_name or None), repo, console=console)
    raise typer.Exit(0 if ok else 1)


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
        typer.Option(
            "--discord-channel", help="Discord control channel id (selects the Discord bridge)."
        ),
    ] = None,
    slack_channel: Annotated[
        str | None,
        typer.Option(
            "--slack-channel", help="Slack control channel id, C…, (selects the Slack bridge)."
        ),
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
    mirror the chronology to the chat backend (Discord or Slack).
    Subcommands inspect and steer
    individual work items; `sbxloop daemon ctl CMD` talks to the running
    daemon instead."""
    if ctx.invoked_subcommand is not None:
        return
    from sbxloop.daemon.agentbox import DaemonAgent
    from sbxloop.daemon.concierge import Concierge
    from sbxloop.daemon.control import ControlServer
    from sbxloop.daemon.fanout import FanoutFrontend, build_frontend
    from sbxloop.daemon.github import DaemonGithub
    from sbxloop.daemon.logsink import event_log_subscriber
    from sbxloop.daemon.loop import DaemonLoop
    from sbxloop.daemon.model import DaemonNotice, WorkItem
    from sbxloop.daemon.paths import resolve_state_dir
    from sbxloop.daemon.sources import (
        REPO_HEALTH_KEY,
        ChatSource,
        CompositeSource,
        GitHubLabels,
        MultiRepoIssueSource,
        ScheduleSource,
        WorkSource,
        build_github_source,
    )

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
        if discord_channel is not None and slack_channel is not None:
            raise ValidationError.from_exception_data(
                "daemon",
                [
                    {
                        "type": "value_error",
                        "loc": ("chat",),
                        "input": None,
                        "ctx": {
                            "error": "--discord-channel and --slack-channel are exclusive: "
                            "the daemon has one chat backend"
                        },
                    }
                ],
            )
        if discord_channel is not None:
            discord_cfg = DiscordConfig.model_validate(
                {**config.discord.model_dump(), "channel_id": discord_channel}
            )
            # The option names the backend too: a Slack section in the file
            # must not make the switch ambiguous.
            config = config.model_copy(
                update={"discord": discord_cfg, "chat": ChatConfig(backend="discord")}
            )
        if slack_channel is not None:
            slack_cfg = SlackConfig.model_validate(
                {**config.slack.model_dump(), "channel_id": slack_channel}
            )
            config = config.model_copy(
                update={"slack": slack_cfg, "chat": ChatConfig(backend="slack")}
            )
    except ValidationError as exc:
        # Before the pipeline is configured with the daemon's own settings:
        # the WARNING-level default from the app callback carries this.
        log.error("daemon.invalid_option", error=exc.errors()[0]["msg"])
        raise typer.Exit(2) from exc
    configure_logging(config.daemon.log_level, fmt=config.daemon.log_format)

    # Work comes from the labelled issues of the configured repositories,
    # or (#760) from workloads asked for in chat — a daemon with a chat
    # backend and no `[github]` runs on those alone.
    # (`--once` skips the concierge but still runs what one already queued.)
    chat_intake = config.chat_backend is not None and bool(config.concierge.enabled)
    if not config.github.enabled and not chat_intake and not config.schedules:
        log.error(
            "daemon.no_repository",
            hint="set --repo owner/name (or [github] repo / [[github.repos]]): the "
            "daemon's work is the labeled issues of the configured repositories — or "
            "configure a chat backend with the concierge on, and workloads asked for "
            "in chat are its work — or declare [[schedules]], and their ticks are",
        )
        raise typer.Exit(2)
    if config.github.enabled and not config.github.enabled_repos():
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
    # Per-repository polling health (#516) is persisted so `doctor` in
    # another process can show a suspended repository; a new daemon process
    # starts every repository fresh (a config edit is the usual reason for
    # the restart, and a still-broken repo re-suspends on its own).
    dstore.clear_prefix(REPO_HEALTH_KEY)
    github: DaemonGithub | None = None
    source: WorkSource
    if config.github.enabled:
        github = DaemonGithub(config, sbx, bus, worker_python=config.worker_python)
        github.remove_stale()
        labels = GitHubLabels(
            config.daemon.trigger_label,
            config.daemon.in_progress_label,
            config.daemon.failed_label,
            config.daemon.completed_label,
            config.daemon.blocked_label,
            config.daemon.gated_label,
            config.daemon.workload_label,
        )

        def persist_repo_health(repo: str, data: dict[str, Any] | None) -> None:
            dstore.set_value(f"{REPO_HEALTH_KEY}{repo}", json.dumps(data) if data else None)

        # Every enabled configured repository is polled; the daemon-wide
        # guardrails below (run cap, retry cap, breaker, one-run-at-a-time)
        # stay global across all of them.
        source = build_github_source(
            github.ops,
            config.github.enabled_repos(),
            labels,
            on_failure=github.note_failure,
            stale_after_s=config.daemon.claim_stale_after_s,
            poll_interval_s=config.daemon.poll_interval_s,
            suspend_after=config.daemon.repo_suspend_after,
            persist=persist_repo_health,
        )
        if chat_intake or config.schedules:
            # Chat-started (#760) and scheduled (#761) workloads ride the
            # same queue; the composite routes each item back to where it
            # came from.
            source = CompositeSource(
                source,
                ChatSource() if chat_intake else None,
                ScheduleSource() if config.schedules else None,
            )
    else:
        source = CompositeSource(
            None,
            ChatSource() if chat_intake else None,
            ScheduleSource() if config.schedules else None,
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
        source=source.name,
        trigger_label=config.daemon.trigger_label,
        workload_label=config.daemon.workload_label,
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
        merge_gate=config.landing.merge_gate,
        chat=config.chat_backend or "off",
        chat_channel=(config.chat_settings.channel_ref if config.chat_settings else None),
        tui="on",
        concierge=("on" if concierge_wanted(config, once=once) else "off"),
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
            if github is not None:
                github.close()
        raise typer.Exit(code)

    loop = DaemonLoop(config, store=store, dstore=dstore, source=source, sbx=sbx, github=github)
    polled = source.github if isinstance(source, CompositeSource) else source
    if isinstance(polled, MultiRepoIssueSource):
        polled.notify = loop.source_notice
    # One probe, shared: the startup drift check below warms its PyPI memo, so
    # the concierge's first `version_status` answers without a network call.
    versions = VersionProbe(
        sbx=sbx,
        check_pypi=config.daemon.version_check,
        upgrade_command=config.daemon.upgrade_command,
    )
    concierge: Concierge | None = None
    # Every bridge at once: the [chat] backend's when one is configured, and
    # always the operator console's local one (`sbxloop tui`).
    frontend: FanoutFrontend = build_frontend(config, dstore, loop_ref=loop)
    try:
        frontend.start()
    except SbxloopError as exc:
        log.error("chat.bridge_failed", error=str(exc), exc_info=True)
        raise typer.Exit(2) from exc
    loop.frontend = frontend
    if archived is not None:
        frontend.daemon_notice(
            DaemonNotice(
                kind="daemon.state_archived",
                text=f"pre-1.0 daemon state moved aside to {archived}; starting fresh",
                level="warning",
            )
        )
    if stranded:
        listed = "\n".join(f"- {i.item_id} {i.url}".rstrip() for i in stranded)
        frontend.daemon_notice(
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
    if concierge_wanted(config, once=once):
        # The control channel's agent: its own event bus (the log sink
        # sees its turns like any agent session) and a long-lived agent
        # sandbox provisioned in the background, so the first mention
        # does not pay the microVM boot. Built after the bridges so a
        # missing bot token exits before any sandbox work starts. It is
        # built whenever it is enabled: the local bridge always exists,
        # so a headless daemon's console can talk to it too.
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
            on_watch=frontend.on_watch,
            thread_link=frontend.thread_link,
        )
        frontend.set_concierge(concierge)
        concierge.warm_up()

    if not once and not config.daemon.version_check:
        # #641: the operator switched the PyPI half off — no request leaves
        # the host for it, and no advice is given that a pipeline or a mirror
        # pin would contradict. The concierge's `version_status` still
        # answers with the installed half.
        log.info("versions.check_disabled")
    elif not once:
        # sbxloop's releases ship often while upgrading a host is an
        # operator's step, so a long-lived daemon drifts behind silently.
        # Check once in the background (never on the startup path) and
        # narrate it only when behind — nobody has to remember to ask.
        # `sbx_control`'s concierge tool `version_status` answers the same
        # question on demand.
        start_drift_check(
            versions,
            lambda text: frontend.daemon_notice(
                DaemonNotice(kind="daemon.version_drift", text=text, level="warning")
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
        log.debug("daemon.shutdown", step="chat bridges")
        frontend.close()
        if concierge is not None:
            # Forgets the handle; the concierge sandbox itself is kept for
            # the next daemon process (conversation memory lives in it).
            log.debug("daemon.shutdown", step="concierge")
            concierge.close()
        if github is not None:
            log.debug("daemon.shutdown", step="github sandbox")
            github.close()
        dstore.close()
        log.info(
            "daemon.stopped",
            reason=stop_reason,
            uptime_s=round(time.monotonic() - started_at, 1),
        )


@app.command()
def tui(
    run: Annotated[
        str | None, typer.Option("--run", help="Open this run's screen straight away.")
    ] = None,
    read_only: Annotated[
        bool, typer.Option("--read-only", help="Observe only: no daemon commands, no chat.")
    ] = False,
    state_dir: Annotated[
        Path | None,
        typer.Option(
            "--state-dir", help="The daemon's state directory (default: the daemon's own rule)."
        ),
    ] = None,
    unit: Annotated[
        str | None,
        typer.Option(
            "--unit", help="The daemon's systemd --user unit (default: [tui] daemon_unit)."
        ),
    ] = None,
) -> None:
    """The operator console: watch, steer and administer the daemon on this host.

    Reads the daemon's state.db read-only, drives the daemon through the
    same `ctl` queue `sbxloop daemon ctl` uses, and (from the chat screens)
    speaks to the daemon's local chat bridge with the same experience a
    Discord or Slack channel gets.
    """
    import getpass

    from sbxloop.tui.app import build_app

    config = load_config()
    resolved = state_dir if state_dir is not None else _daemon_state_dir()
    # The daemon stamps its resolved state dir on its config before it
    # harvests; the console reads artifacts through the same value.
    config = config.model_copy(update={"state_dir": resolved})
    operator = config.tui.operator_id or getpass.getuser()
    try:
        console_app = build_app(
            config,
            resolved,
            operator_id=operator,
            read_only=read_only,
            initial_run=run,
            unit=unit,
        )
    except SbxloopError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc
    console_app.run()


def concierge_wanted(config: Config, *, once: bool) -> bool:
    """Whether this daemon builds the concierge: whenever it is enabled and
    the daemon is long-lived. The console's local bridge always exists, so
    a headless daemon has a surface for it too — a host without a chat
    service now boots the concierge sandbox at start and needs the agent
    credential (`sbxloop doctor` shows the row)."""
    return bool(config.concierge.enabled) and not once


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
        else:
            item = apply_item_verb(dstore, action, item_id, now=now, by="operator (CLI)")
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
            help="status | pause [--hold NAME] | resume [--hold NAME|--all] | cancel "
            "[--retry] | queue | items | abandon <item> [reason] | retry <item> | "
            "requeue <item> | grant-rounds <run> <n> | log [--tail N] [--level L] [--grep T] "
            "| stop (the chat !sbx verbs)."
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
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="With `status`: print the daemon's status as one JSON object (current, "
            "claiming, holds, paused, …) instead of prose, for scripts.",
        ),
    ] = False,
) -> None:
    """Send a command to the daemon running against this state_dir — the
    programmatic twin of Discord's `!sbx`, for scripts, cron and remote
    operators (the bot ignores its own messages by design)."""
    from sbxloop.daemon.control import ControlClient, plain

    if as_json and (not command or command[0].lower() != "status"):
        console.print("[bold red]--json applies to[/] [cyan]ctl status[/] only.")
        raise typer.Exit(2)
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
    if as_json:
        if reply.ok and reply.status is None:
            # A daemon from before #639 answered with prose only. Say so
            # rather than print prose to a jq: exit 1, not 2, so a script
            # can tell "answered, too old" from "no daemon".
            console.print(
                "[bold red]the daemon answered without a structured status[/] — it predates "
                "`ctl status --json`; upgrade and restart it, then retry."
            )
            raise typer.Exit(1)
        if reply.ok:
            typer.echo(json.dumps(reply.status, sort_keys=True))
            return
    console.print(plain(reply.text), markup=False, highlight=False)
    if not reply.ok:
        raise typer.Exit(1)


@daemon_app.command("notify")
def daemon_notify(
    text: Annotated[
        str,
        typer.Argument(help="The notice, in the chat's Markdown (bold, `code`, [label](url))."),
    ],
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds to wait for the chat service.")
    ] = 30.0,
) -> None:
    """Post one message to the daemon's control channel through the configured
    `[chat] backend` — from the host, without the daemon, for deploy scripts
    and cron. The bot token comes from the environment / .env, as for the
    daemon; nothing else about the channel is read outside sbxloop.toml."""
    from sbxloop.daemon.notify import post_notice

    try:
        config = load_config()
        posted = post_notice(config, text, timeout_s=timeout)
    except SbxloopError as exc:
        console.print(f"[bold red]notify failed:[/] {exc}")
        raise typer.Exit(2) from exc
    console.print(f"posted to {posted.backend} channel {posted.channel_id}", highlight=False)


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
        typer.Option("--timeout", help="Seconds to wait for the backend's runtime and API."),
    ] = 60.0,
) -> None:
    """List the models the configured [agent] backend gives this host access to.

    Queries the backend directly on the host (no sandbox) with the same
    credential agent sessions use — the Copilot SDK by default, the
    Anthropic Models API under `[agent] backend = "claude"` — so the ids
    shown here are valid values for `model` in sbxloop.toml and
    `sbxloop run --model`.
    """
    from sbxloop.cli.models import (
        fetch_backend_rows,
        format_context,
        format_efforts,
        table_columns,
    )

    config = load_config()
    backend = backend_for(config)
    try:
        rows = fetch_backend_rows(backend, timeout_s=timeout_s)
    except SbxloopError as exc:
        # escape(): the install hint (`sbxloop[copilot]`) and arbitrary SDK
        # error text must not be parsed as rich markup.
        console.print(f"[bold red]list-models failed:[/] {rich_escape(str(exc))}")
        raise typer.Exit(2) from exc
    if json_output:
        # bare JSON on stdout, nothing else — `sbxloop list-models --json | jq`
        typer.echo(json.dumps([row.raw or {"id": row.id, "name": row.name} for row in rows]))
        return
    table = Table(title=f"{backend.label} models")
    columns = table_columns(backend)
    for column in columns:
        table.add_column(column)
    for row in rows:
        configured = row.id == config.model
        # SDK-provided text is escaped: a model name with brackets must not
        # be parsed as rich markup.
        cells = {
            "model": f"[bold cyan]{rich_escape(row.id)}[/] ◀"
            if configured
            else rich_escape(row.id),
            "name": rich_escape(row.name),
            "billing": f"{row.multiplier:g}x" if row.multiplier is not None else "",
            "context": format_context(row.context_window),
            "vision": "yes" if row.vision else "",
            "reasoning": format_efforts(row),
            "policy": row.policy_state or "",
            "created": row.created or "",
        }
        table.add_row(*(cells[column] for column in columns))
    console.print(table)
    if not rows:
        console.print(
            f"[yellow]{backend.models_source} returned no models[/] — the subscription "
            "may have no model access, or model policy blocks them all"
        )
    marker = (
        f"◀ = configured model ({config.model})"
        if any(row.id == config.model for row in rows)
        else f"configured model: {config.model}"
        + (" (the SDK picks one per session)" if config.model == "auto" else " — not in this list!")
    )
    footnote = "; * = default reasoning effort" if "reasoning" in columns else ""
    console.print(f"[dim]{marker}{footnote}[/]")


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
    probe: Annotated[
        bool,
        typer.Option(
            "--probe/--no-probe",
            help="Ask GitHub about each configured repository (reachability, token "
            "permissions) from a github-ops sandbox — one microVM per distinct "
            "credential. Off by default; --deep implies it.",
        ),
    ] = False,
) -> None:
    """Check that this host is ready to run sbxloop."""
    ok = run_doctor(console, deep=deep, fail_on_drift=fail_on_drift, probe=probe)
    raise typer.Exit(0 if ok else 1)


# The commented default configuration lives in one file — `sbxloop.toml.example`,
# shipped as package data and published at the repository root — so `sbxloop
# init` and the example a reader copies can never drift apart.
DEFAULT_CONFIG_TOML = (
    resources.files("sbxloop.data").joinpath("sbxloop.toml.example").read_text(encoding="utf-8")
)


def config_presets() -> dict[str, str]:
    """The packaged `init --preset` fragments by name, from `sbxloop/data/presets`.

    Package data, not a checkout path, so `sbxloop init --preset` works from a
    wheel (#636) and nothing `init` writes points outside the user's project.
    """
    folder = resources.files("sbxloop.data").joinpath("presets")
    return {
        entry.name.removesuffix(".toml"): entry.read_text(encoding="utf-8")
        for entry in folder.iterdir()
        if entry.name.endswith(".toml")
    }


def render_config_template(preset: str | None = None) -> str:
    """The template `sbxloop init` writes, with a preset's sections appended.

    Every table in the template is commented out, so appending a preset's
    live `[budgets]`/`[limits]` yields valid TOML. Raises KeyError for a
    preset name the package does not ship.
    """
    if preset is None:
        return DEFAULT_CONFIG_TOML
    fragment = config_presets()[preset]
    return DEFAULT_CONFIG_TOML.rstrip("\n") + "\n\n" + fragment


def main() -> None:
    app()


if __name__ == "__main__":
    main()
