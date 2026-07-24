"""The sbxloop CLI: run agentic loops on Docker Sandboxes."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.tree import Tree

import sbxloop
from sbxloop.cli.doctor import run_doctor
from sbxloop.cli.tui import Dashboard, format_event, plain_printer, render_event
from sbxloop.config import Config, load_config, load_config_with_sources, load_dotenv_file
from sbxloop.engine.engine import LoopEngine
from sbxloop.engine.model import RunResult, artifact_files, artifacts_dir
from sbxloop.engine.store import StateStore
from sbxloop.errors import SdxloopError
from sbxloop.events import Event
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import sandbox_name

app = typer.Typer(
    name="sbxloop",
    help="Agentic loop orchestration on Docker Sandboxes with isolated credentials.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
sandbox_app = typer.Typer(help="Manage sbxloop sandboxes.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect configuration.", no_args_is_help=True)
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(config_app, name="config")

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
    # Every command sees ./.env (tokens + SBXLOOP_* settings); real
    # environment variables always take precedence.
    load_dotenv_file()


def _config_with_overrides(**overrides: Any) -> Config:
    config = load_config()
    updates = {k: v for k, v in overrides.items() if v is not None}
    return config.model_copy(update=updates) if updates else config


def _store(config: Config) -> StateStore:
    return StateStore(config.state_dir / "state.db")


def _drive_with_ui(engine: LoopEngine, *, tui: bool, action: Any) -> RunResult:
    """Run start/resume with the scrollback transcript + pinned status, or
    plain event logs (--no-tui).

    Transcript entries print permanently to the terminal's scrollback (via
    ``live.console.print``, which renders above the live region), so the
    full conversation history survives; only the compact status panel at
    the bottom is redrawn in place. Events arrive on the engine thread but
    every terminal write happens here on the main thread, via a queue —
    ordering stays deterministic and rich's Live never interleaves.
    """
    if not tui:
        engine.bus.subscribe(plain_printer(console))
        return action()  # type: ignore[no-any-return]

    dashboard = Dashboard()
    pending: queue.SimpleQueue[Event] = queue.SimpleQueue()
    engine.bus.subscribe(pending.put)
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["result"] = action()
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    with Live(dashboard.renderable(), console=console, refresh_per_second=8) as live:

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

        thread.start()
        while thread.is_alive():
            drain()
            live.update(dashboard.renderable())
            time.sleep(0.15)
        drain()
        live.update(dashboard.renderable())
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]  # type: ignore[no-any-return]


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
    files = artifact_files(target)
    if not files:
        console.print(f"\nartifacts: none produced (workspace: {target})")
        return
    via = "live workspace mount" if result.mounted else "harvested from the sandbox"
    console.print(f"\nartifacts: {len(files)} file(s), {via}")
    console.print(_artifacts_tree(target, files))


def _finish(result: RunResult, config: Config) -> None:
    style = "green" if result.succeeded else "red"
    console.print(f"\nrun [bold cyan]{result.run_id}[/] finished: [bold {style}]{result.state}[/]")
    for task in result.tasks:
        console.print(f"  {task.spec.id}: {task.state}  ({task.spec.title})")
    _print_artifacts_summary(result, config)
    raise typer.Exit(0 if result.succeeded else 1)


@app.command()
def run(
    outcome: Annotated[str, typer.Argument(help="The outcome to achieve.")],
    report: Annotated[
        bool | None,
        typer.Option(
            "--report/--no-report",
            help="Post run progress to the configured [github].repo.",
        ),
    ] = None,
    deliver: Annotated[
        bool | None,
        typer.Option(
            "--deliver/--no-deliver",
            help="Publish the completed run's artifacts as a PR to the configured [github].repo.",
        ),
    ] = None,
    model: Annotated[str | None, typer.Option("--model", help="Copilot model id.")] = None,
    keep_sandboxes: Annotated[
        bool, typer.Option("--keep-sandboxes", help="Do not remove sandboxes at the end.")
    ] = False,
    tui: Annotated[bool, typer.Option("--tui/--no-tui", help="Live dashboard.")] = True,
) -> None:
    """Run an agentic loop for OUTCOME in a fresh sandbox pair."""
    config = _config_with_overrides(model=model, keep_sandboxes=keep_sandboxes or None)
    if report is not None:
        config = config.model_copy(
            update={"github": config.github.model_copy(update={"report": report})}
        )
    if deliver is not None:
        config = config.model_copy(
            update={"github": config.github.model_copy(update={"deliver": deliver})}
        )
    wanted = [
        feature
        for feature, enabled in (
            ("progress reporting (--report)", config.github.report),
            ("PR delivery (--deliver)", config.github.deliver),
        )
        if enabled
    ]
    if wanted and not config.github.enabled:
        console.print(
            f"[bold red]GitHub integration is not configured.[/] {', '.join(wanted)} "
            'needs a repository: set [cyan]\\[github] repo = "owner/repo"[/] in '
            "sbxloop.toml (see `sbxloop init`), then re-run."
        )
        raise typer.Exit(2)
    engine = LoopEngine(config)
    try:
        result = _drive_with_ui(engine, tui=tui, action=lambda: engine.start(outcome))
    except SdxloopError as exc:
        console.print(f"[bold red]run failed:[/] {exc}")
        raise typer.Exit(2) from exc
    _finish(result, config)


@app.command()
def resume(
    run_id: Annotated[str, typer.Argument(help="Run id to resume.")],
    tui: Annotated[bool, typer.Option("--tui/--no-tui")] = True,
) -> None:
    """Resume an unfinished run (fresh sandboxes, persisted state)."""
    config = load_config()
    engine = LoopEngine(config)
    try:
        result = _drive_with_ui(engine, tui=tui, action=lambda: engine.resume(run_id))
    except SdxloopError as exc:
        console.print(f"[bold red]resume failed:[/] {exc}")
        raise typer.Exit(2) from exc
    _finish(result, config)


@app.command()
def cancel(run_id: Annotated[str, typer.Argument()]) -> None:
    """Mark a run cancelled (takes effect at the next phase boundary)."""
    config = load_config()
    engine = LoopEngine(config)
    try:
        engine.cancel(run_id)
    except SdxloopError as exc:
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
        table.add_column("state")
        table.add_column("outcome", max_width=60)
        table.add_column("updated")
        for record in store.list_runs():
            table.add_row(
                record.run_id,
                record.state,
                record.outcome[:60],
                time.strftime("%Y-%m-%d %H:%M", time.localtime(record.updated_at)),
            )
        console.print(table)
        return

    try:
        record = store.get_run(run_id)
    except SdxloopError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc
    console.print(f"run [bold cyan]{record.run_id}[/]  state: [bold]{record.state}[/]")
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


@app.command()
def logs(
    run_id: Annotated[str, typer.Argument()],
    follow: Annotated[bool, typer.Option("--follow", "-f")] = False,
    type_prefix: Annotated[
        str | None, typer.Option("--type", help="Filter by event type prefix.")
    ] = None,
    task: Annotated[str | None, typer.Option("--task", help="Filter by task id.")] = None,
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
        if store.get_run(run_id).state in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.5)


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
    except SdxloopError as exc:
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
    files = artifact_files(target)
    via = "live workspace mount" if record.mounted else "harvested copy"
    console.print(f"run [bold cyan]{run_id}[/]: {len(files)} file(s) ({via}) in [bold]{target}[/]")
    if tree:
        console.print(_artifacts_tree(target, files))
        return
    for file in files:
        size = _human_size(file.stat().st_size)
        console.print(f"  {file.relative_to(target)}  [dim]{size}[/]")


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
        except SdxloopError as exc:
            console.print(f"[yellow]skip {target}:[/] {exc}")


@config_app.command("show")
def config_show() -> None:
    """Show the resolved configuration and where each value came from."""
    try:
        config, sources = load_config_with_sources()
    except SdxloopError as exc:
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


@app.command()
def doctor() -> None:
    """Check that this host is ready to run sbxloop."""
    ok = run_doctor(console)
    raise typer.Exit(0 if ok else 1)


DEFAULT_CONFIG_TOML = """\
# sbxloop configuration. Every key is optional; these are the defaults.
# Precedence: SBXLOOP_* env vars > this file > pyproject [tool.sbxloop].

# Copilot model for agent sessions ("auto" lets the SDK choose).
model = "auto"
# Optional sbx --app-name isolating sbxloop's sandboxes/policies/secrets.
# Empty shares your normal sbx state (your login + balanced policy apply).
# If set, that isolated state needs its own `sbx --app-name <name> login`
# and `sbx --app-name <name> policy init balanced`.
app_name = ""
# Where run state (SQLite) and per-run workspaces live.
state_dir = ".sbxloop"
# Keep sandboxes around after a run (for debugging).
keep_sandboxes = false
# Worker transport: "stream" (default) or "poll".
worker_transport = "stream"
# Secret injection: "proxy" (sbx keychain proxy; recommended) or "plain-env".
secret_strategy = "proxy"

[sandbox]
# Custom sandbox template reference (defaults to the sbx shell template).
# template = "docker.io/you/your-template:v1"
# Extra network allow rules applied to both sandboxes.
extra_allow_domains = []

[github]
# The GitHub integration. Unset (the default) disables GitHub entirely:
# no github sandbox is provisioned, GH_TOKEN is not required, and
# repo-facing features refuse to run. Set `repo` to the ONE repository
# sbxloop may work with; the toggles below act on it.
# repo = "you/your-repo"
# Post run progress (issues/comments) to the configured repo.
# report = false

[deliver]
# Open a PR with the run's artifacts when a run completes ("owner/repo";
# empty disables). GH_TOKEN needs contents:write + pull_requests:write on
# the target. base defaults to the repo's default branch.
# repo = "you/your-repo"
# base = "main"
# draft = false

[budgets]
max_revisions_per_task = 2
max_replans_per_task = 1
max_tasks = 20
max_wall_clock_s = 7200.0
per_job_timeout_s = 900.0
"""


def main() -> None:
    app()


if __name__ == "__main__":
    main()
