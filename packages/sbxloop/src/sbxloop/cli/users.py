"""``sbxloop users``: operator repairs to the collaboration accounts.

Run on the daemon host, as the service user, against the home's own state
database. Like ``sbxloop api``, these commands open the daemon's database
beside a running daemon through their own connection; every change is one
immediate SQLite transaction, so it lands whole or not at all and never
interleaves with the daemon's writes.
"""

from __future__ import annotations

import getpass
import time
from typing import Annotated, Any

import typer
from rich.console import Console

from sbxloop.config import load_config
from sbxloop.daemon.store import DaemonStore

users_app = typer.Typer(help="Collaboration accounts on this installation.", no_args_is_help=True)

console = Console()


def say(line: str) -> None:
    console.print(line, markup=False, soft_wrap=True)


def operator() -> dict[str, Any]:
    """Who is running the command, for the audit trail."""
    try:
        who = getpass.getuser()
    except (KeyError, OSError):  # pragma: no cover - no account name on this host
        who = "unknown"
    return {"kind": "operator", "id": who, "via": "cli"}


@users_app.command("merge")
def merge(
    source: Annotated[
        str,
        typer.Option("--from", help="The account to fold in and deactivate (username or user id)."),
    ],
    target: Annotated[
        str,
        typer.Option("--into", help="The account that keeps everything (username or user id)."),
    ],
    readmit: Annotated[
        bool,
        typer.Option(
            "--readmit",
            help=(
                "Allow the merge when the --into account is no longer a workspace "
                "member; it is brought back in with the merged role."
            ),
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Apply the merge; without it nothing is written.")
    ] = False,
) -> None:
    """Merge one person's second account into their first.

    Everything the --from account made moves to the --into account, its
    provider sign-in then reaches the --into account, and the --from account
    is deactivated with its tokens revoked. Without --yes this prints what
    would move and writes nothing. A deactivated --from account lends none
    of its workspace role, and an --into account that has left the
    workspace is refused unless --readmit says to bring it back in.
    """
    # Imported here, as `sbxloop api` does: the collaboration store is a
    # sizeable module most commands never need.
    from sbxloop.api.collaboration import CollaborationError, CollaborationStore

    path = load_config().paths.state_db
    if not path.exists():
        console.print(f"no state database at {path}", markup=False, style="bold red")
        raise typer.Exit(2)
    dstore = DaemonStore(path)
    try:
        store = CollaborationStore(dstore)
        found = {}
        for flag, selector in (("--from", source), ("--into", target)):
            try:
                user = store.find_user(selector)
            except CollaborationError as exc:
                console.print(
                    f"{flag}: refused ({exc.code}): {exc.message}",
                    markup=False,
                    style="bold red",
                )
                raise typer.Exit(2) from exc
            if user is None:
                console.print(f"{flag}: no user {selector!r}", markup=False, style="bold red")
                raise typer.Exit(2)
            found[flag] = user
        try:
            report = store.merge_users(
                found["--from"].id,
                found["--into"].id,
                time.time(),
                dry_run=not yes,
                readmit=readmit,
                actor=operator(),
            )
        except CollaborationError as exc:
            console.print(f"refused ({exc.code}): {exc.message}", markup=False, style="bold red")
            raise typer.Exit(1) from exc
    finally:
        dstore.close()

    heading = "dry run: nothing written" if report.dry_run else "merged"
    say(
        f"{heading}: {report.source_username} ({report.source_id}) "
        f"-> {report.target_username} ({report.target_id})"
    )
    for kind, rows in report.moved.items():
        say(f"  {kind:<20} {rows}")
    if report.identity is not None:
        issuer, subject = report.identity
        say(f"  provider identity    {issuer} sub={subject} -> {report.target_username}")
    if report.readmitted:
        say(f"  workspace role       re-admitted to the workspace as {report.role}")
    else:
        say(f"  workspace role       {report.previous_role or 'none'} -> {report.role}")
    if report.preference_conflicts:
        say("  preferences kept from the target: " + ", ".join(sorted(report.preference_conflicts)))
    for label, renames in (("team", report.renamed_teams), ("workflow", report.renamed_workflows)):
        for old, new in renames:
            say(f"  {label} {old!r} moves as {new!r} (the target already has one)")
    if report.dry_run:
        say(
            f"{report.source_username} would be deactivated and its tokens revoked. "
            "Run again with --yes to apply."
        )
    else:
        say(f"{report.source_username} is deactivated and its tokens are revoked.")
