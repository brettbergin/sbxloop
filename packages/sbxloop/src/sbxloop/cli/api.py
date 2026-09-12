"""``sbxloop api``: the clients the remote API admits, and its signing key.

Run on the daemon host by the operator. A client's secret is printed once
at creation and never again; the store keeps only its verifier. These
commands write the daemon's own database beside a running daemon the way
the console does — their own connection, no migration — and bring the
schema up themselves only when no database exists yet.
"""

from __future__ import annotations

import getpass
import time
from datetime import UTC, datetime
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from sbxloop.api import MISSING_EXTRA, api_available
from sbxloop.api.auth.store import ApiAuthStore, StandaloneSessions, parse_capabilities
from sbxloop.config import load_config
from sbxloop.daemon.controls.principal import CAPABILITIES
from sbxloop.daemon.store import DaemonStore

api_app = typer.Typer(
    help="Remote API clients and the key their tokens are signed with.", no_args_is_help=True
)
client_app = typer.Typer(help="Registered clients.", no_args_is_help=True)
key_app = typer.Typer(help="The token signing key.", no_args_is_help=True)
api_app.add_typer(client_app, name="client")
api_app.add_typer(key_app, name="key")

console = Console()


def _auth_store() -> tuple[ApiAuthStore, StandaloneSessions]:
    config = load_config()
    path = config.paths.state_db
    if not path.exists():
        # No daemon has run here yet: bring the schema up, then open the
        # way a second process does.
        path.parent.mkdir(parents=True, exist_ok=True)
        DaemonStore(path).close()
    sessions = StandaloneSessions(path, owns_schema=False)
    return ApiAuthStore(sessions), sessions


def _when(ts: float | None) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


@client_app.command("create")
def client_create(
    name: Annotated[str, typer.Argument(help="A name for the client (who or what it is).")],
    cap: Annotated[
        list[str] | None,
        typer.Option(
            "--cap",
            help=f"A capability to grant (repeatable): {', '.join(CAPABILITIES)}.",
        ),
    ] = None,
) -> None:
    """Register a client and print its secret — once."""
    try:
        capabilities = parse_capabilities(cap or [])
    except ValueError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc
    if not capabilities:
        console.print("[bold red]grant at least one capability with --cap[/]")
        raise typer.Exit(2)
    store, sessions = _auth_store()
    try:
        try:
            creator = getpass.getuser()
        except Exception:  # no login identity (containers)
            creator = "operator"
        client, secret = store.create_client(
            name, capabilities, created_by=f"{creator} via sbxloop api", now=time.time()
        )
    finally:
        sessions.close()
    console.print(f"client_id:     {client.id}")
    console.print(f"client_secret: {secret}")
    console.print(f"capabilities:  {', '.join(sorted(client.capabilities))}")
    console.print(
        "[dim]The secret is shown once and never stored; a client that loses it is "
        "revoked and created again.[/]"
    )


@client_app.command("list")
def client_list() -> None:
    """Every registered client, revoked ones included."""
    store, sessions = _auth_store()
    try:
        clients = store.list_clients()
    finally:
        sessions.close()
    if not clients:
        console.print("no clients — `sbxloop api client create NAME --cap runs:read`")
        return
    table = Table(box=None, pad_edge=False)
    for column in ("id", "name", "capabilities", "created", "last used", "state"):
        table.add_column(column)
    for client in clients:
        table.add_row(
            client.id,
            client.name,
            ", ".join(sorted(client.capabilities)),
            _when(client.created_at),
            _when(client.last_used_at),
            "revoked" if client.revoked_at is not None else "active",
        )
    console.print(table)


@client_app.command("revoke")
def client_revoke(
    client_id: Annotated[str, typer.Argument(help="The client id to revoke.")],
) -> None:
    """Revoke a client: no new tokens, its refresh tokens dead, every
    request with a live access token refused from now on."""
    store, sessions = _auth_store()
    try:
        client = store.revoke_client(client_id, time.time())
    finally:
        sessions.close()
    if client is None:
        console.print(f"[bold red]no client {client_id}[/]")
        raise typer.Exit(2)
    console.print(f"{client.id} ({client.name}) revoked")


def _keys() -> Any:
    """The signing-key module, imported here rather than at the top: it
    needs the ``sbxloop[api]`` extra, and ``sbxloop`` as a whole must load
    on an install without it. A missing extra is refused by name."""
    if not api_available():
        # Plain text: rich would read `[api]` in the message as markup.
        console.print(MISSING_EXTRA, markup=False, style="red")
        raise typer.Exit(code=1)
    from sbxloop.api.auth import keys

    return keys


@key_app.command("rotate")
def key_rotate() -> None:
    """Replace the signing key. Tokens signed by the old key still verify
    until they expire; a daemon already running picks the new key up on
    its next start."""
    config = load_config()
    keys = _keys().rotate(config.paths)
    console.print(f"signing key rotated: now {keys.current.kid}")
    if keys.previous is not None:
        console.print(f"[dim]{keys.previous.kid} still verifies the tokens it signed[/]")
    console.print("[dim]restart the daemon for the listener to sign with the new key[/]")


@key_app.command("show")
def key_show() -> None:
    """The current key id (creating the key if there is none)."""
    config = load_config()
    keys = _keys().load_or_create(config.paths)
    console.print(f"kid: {keys.current.kid}")
    if keys.previous is not None:
        console.print(f"previous: {keys.previous.kid}")


@api_app.command("openapi")
def openapi(
    write: Annotated[
        str | None,
        typer.Option("--write", help="Write the document here instead of printing it."),
    ] = None,
    snapshot: Annotated[
        bool,
        typer.Option(
            "--snapshot",
            help="Replace the build's version with a constant, as the committed contract is.",
        ),
    ] = False,
) -> None:
    """The remote API's OpenAPI document, as the listener publishes it —
    without a running daemon. ``--snapshot`` is what ``docs/openapi.json``
    holds; the contract test compares the two."""
    if not api_available():
        console.print(MISSING_EXTRA, markup=False, style="red")
        raise typer.Exit(code=1)
    import json
    from pathlib import Path

    from sbxloop.api.app import openapi_document

    text = json.dumps(openapi_document(snapshot=snapshot), indent=2, sort_keys=True) + "\n"
    if write is None:
        typer.echo(text, nl=False)
        return
    Path(write).write_text(text)
    console.print(f"wrote {write}", markup=False)
