"""The sbxloop CLI: run agentic loops on Docker Sandboxes."""

from __future__ import annotations

import os
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
from sbxloop.sbx.bake import DEFAULT_TEMPLATE_REF, bake_template
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import sandbox_name
from sbxloop.sbx.prune import classify_sandboxes, format_age, remove_sandbox
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
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(config_app, name="config")
app.add_typer(secrets_app, name="secrets")

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
    if result.kept_sandboxes:
        console.print(f"\n[bold yellow]sandboxes kept:[/] {', '.join(result.kept_sandboxes)}")
        console.print(f"  inspect: [cyan]sbxloop shell {result.run_id}[/] (--role github)")
        console.print(f"  remove:  [cyan]sbxloop sandbox rm --run {result.run_id}[/]")
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
    keep_on_failure: Annotated[
        bool | None,
        typer.Option(
            "--keep-on-failure/--no-keep-on-failure",
            help="Keep the sandbox pair alive when the run fails (inspect with `sbxloop shell`).",
        ),
    ] = None,
    tui: Annotated[bool, typer.Option("--tui/--no-tui", help="Live dashboard.")] = True,
) -> None:
    """Run an agentic loop for OUTCOME in a fresh sandbox pair."""
    config = _config_with_overrides(
        model=model,
        keep_sandboxes=keep_sandboxes or None,
        keep_on_failure=keep_on_failure,
    )
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
    except SdxloopError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc
    cli = SbxCLI(app_name=config.app_name or None)
    name = sandbox_name(run_id, "agent" if role == "agent" else "github")
    try:
        live = any(info.name == name for info in cli.ls())
    except SdxloopError as exc:
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
    except SdxloopError as exc:
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
    except SdxloopError as exc:
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
        if config.secret_strategy == "plain-env":
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
    except SdxloopError as exc:
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
    except SdxloopError as exc:
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
            remove_sandbox(cli, v.name)
        except SdxloopError as exc:
            failures += 1
            console.print(f"[yellow]skip {v.name}:[/] {exc}")
            continue
        console.print(f"removed {v.name}")
        # A pruned kept run is no longer kept; keep the DB marker honest.
        if v.kept_reason is not None and v.run_id is not None:
            store.set_run_kept(v.run_id, None)
    if failures:
        raise typer.Exit(1)


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


@config_app.command("policy")
def config_policy() -> None:
    """Show the effective per-phase network egress policy."""
    from sbxloop.policy import PROMPT_ADVERTISED_DOMAINS
    from sbxloop.sbx.provision import AGENT_ALLOW_DOMAINS, GITHUB_ALLOW_DOMAINS

    try:
        config = load_config()
    except SdxloopError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc

    extra = list(config.sandbox.extra_allow_domains)
    baseline = ", ".join([*AGENT_ALLOW_DOMAINS, *extra])
    advertised = ", ".join(PROMPT_ADVERTISED_DOMAINS)

    table = Table(title="agent sandbox: effective egress per phase")
    table.add_column("phase", no_wrap=True)
    table.add_column("policy", overflow="fold")
    table.add_row("decompose / plan", "baseline")
    table.add_row(
        "execute",
        "baseline + plan-declared grants (auto-granted just before execute, "
        "within the [policy] bounds below; every grant/refusal is event-logged)",
    )
    table.add_row(
        "scrutinize / verify / validate",
        "baseline + grants already made — sbx has no policy revocation, so "
        "grants persist for the sandbox's lifetime (sandboxes are removed at "
        "run end; grants never outlive a run)",
    )
    console.print(table)
    console.print(f"baseline (provisioned per-sandbox): {baseline}")
    console.print(f"advertised by the user's balanced preset: {advertised}")

    bounds = Table(title="[policy] bounds for plan-declared grants")
    bounds.add_column("bound", no_wrap=True)
    bounds.add_column("patterns", overflow="fold")
    bounds.add_row(
        "allow", ", ".join(config.policy.allow) or "(empty — plans may only use the baseline)"
    )
    bounds.add_row("deny", ", ".join(config.policy.deny) or "(none)")
    console.print(bounds)

    if config.github.enabled:
        gh_domains = ", ".join([*GITHUB_ALLOW_DOMAINS, *extra])
        console.print(f"github sandbox (all phases, no plan grants): {gh_domains}")
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
    except SdxloopError as exc:
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
) -> None:
    """Check that this host is ready to run sbxloop."""
    ok = run_doctor(console, deep=deep)
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
# baseline: the Copilot/GitHub hosts, PyPI, and apt mirrors.
# See `sbxloop config policy` for the effective per-phase policy.
allow = []
deny = []

[github]
# The GitHub integration. Unset (the default) disables GitHub entirely:
# no github sandbox is provisioned, GH_TOKEN is not required, and
# repo-facing features refuse to run. Set `repo` to the ONE repository
# sbxloop may work with; the toggles below act on it.
# repo = "you/your-repo"
# Post run progress (issues/comments) to the configured repo.
# report = false
# Open a PR with the run's artifacts when a run completes (or `--deliver`).
# GH_TOKEN needs contents:write + pull_requests:write on the repo.
# deliver = false
# deliver_base = "main"   # base branch; unset uses the repo's default
# deliver_draft = false

[budgets]
max_revisions_per_task = 2
max_replans_per_task = 1
max_tasks = 20
max_wall_clock_s = 7200.0
per_job_timeout_s = 900.0

[limits]
# Sandbox resource guardrails (percent used; 0 disables). Sampled in-VM on
# the worker heartbeat and shown as a gauge in the TUI status panel.
# Crossing disk_abort fails the current task with an explicit
# "sandbox disk exhausted" error.
disk_warn = 85.0
disk_abort = 95.0
mem_warn = 90.0
"""


def main() -> None:
    app()


if __name__ == "__main__":
    main()
