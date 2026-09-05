"""``sbxloop init-repo``: give a repository the labels sbxloop relies on (#630).

Nothing creates the trigger label a human is told to apply, and GitHub
creates the lifecycle labels on first attach with a random color and no
description — a repository the daemon was pointed at but never set up
shows the loop's states as unexplained colored noise. This creates the seven
lifecycle labels (`[daemon] *_label`, with the repository's
`[[github.repos]]` renames applied) and the follow-up label, each with a
color and a description, and leaves existing ones alone.
Idempotent: run it again after renaming a label in config. ``doctor``
stays advisory — its labels row points here.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from sbxloop.config import Config, _valid_repo
from sbxloop.events import EventBus
from sbxloop.gh.labels import EnsureResult, LabelSpec, ensure_label, lifecycle_specs
from sbxloop.log import get_logger
from sbxloop.sbx.cli import SbxCLI

log = get_logger(__name__)


def init_repo(config: Config, cli: SbxCLI, repo: str, *, console: Console) -> bool:
    """Create the labels for ``repo``; print one row per label; return
    whether every label is now present.

    The host never holds the token, so the writes go through a short-lived
    github-ops sandbox — the same one ``doctor --probe`` boots — scoped to
    ``repo`` so a per-repo ``token_env`` applies. A repository that is not
    in the configuration is fine: it gets the daemon-wide labels and the
    daemon-wide credential.
    """
    from sbxloop.daemon.github import DaemonGithub

    if not _valid_repo(repo):
        console.print(f"[bold red]not a repository:[/] {repo!r} — expected owner/name")
        return False
    entry = config.github.find_repo(repo)
    specs = lifecycle_specs(config.labels_for(repo), config.landing.followup_label)
    box = DaemonGithub(
        config,
        cli,
        EventBus(),
        worker_python=config.worker_python,
        name=f"sbxloop-init-{repo.replace('/', '-')}".lower()[:60],
        repo=entry.repo if entry is not None else repo,
    )
    console.print(f"[dim]… booting a github-ops sandbox to create labels on {repo}[/dim]")
    results: list[tuple[LabelSpec, EnsureResult]] = []
    try:
        ops = box.ops()
        for spec in specs:
            results.append((spec, ensure_label(ops, repo, spec)))
    finally:
        box.close()
    table = Table(title=f"sbxloop labels on {repo}")
    for col in ("label", "status", "description"):
        table.add_column(col, overflow="fold")
    for spec, result in results:
        status = {
            "created": "[green]created[/]",
            "present": "[dim]present[/]",
            "failed": "[red]FAILED[/]",
        }[result]
        table.add_row(spec.name, status, spec.description)
    console.print(table)
    failed = [spec.name for spec, result in results if result == "failed"]
    if failed:
        console.print(
            f"[bold red]{len(failed)} label(s) could not be created[/] — the token needs "
            "permission to write issue labels (fine-grained token or GitHub App: Issues → "
            "read and write; classic PAT: `repo`); see the log for GitHub's answer"
        )
        return False
    created = sum(1 for _, result in results if result == "created")
    console.print(
        f"{created} label(s) created, {len(results) - created} already present — "
        f"{repo} is ready for the daemon"
    )
    return True
