"""Agent-filed backlog: follow-up work the inner agent discovered.

The agent sandbox holds no GH_TOKEN, so an agent cannot file issues; it
writes ``.sbxloop/backlog/*.md`` files in the run workspace instead, and
the daemon files them after the run through whichever source ``--backlog``
names. ``.sbxloop`` sits in the default artifact excludes, which is exactly
what keeps these files out of harvest and out of the delivery PR — and also
why collection reads the mounted workspace directly (unmounted runs are
skipped with a loud warning).

Filed items land in triage by default (backlog label / inbox ``triage/``);
``backlog_auto_trigger`` puts them straight into the queue. Fingerprints
dedup re-collection on resume, and ``max_items`` caps how much queue one
run may generate — an agent enumerating infinite future work must not
flood the daemon.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import NamedTuple

from sbxloop.daemon.sources import WorkSource, parse_markdown_item
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import RunRecord

logger = logging.getLogger(__name__)

BACKLOG_SUBDIR = Path(".sbxloop") / "backlog"

# The audit lane's contract (discovery on GitHub): the deliverable is issues.
AUDIT_INSTRUCTIONS = (
    "This is an AUDIT: your deliverable is a set of well-evidenced findings, not "
    "code. Do not modify the tree except to write findings under "
    "`.sbxloop/backlog/` in the workspace — one markdown file per finding, first "
    "line `# <short, specific title>`, then these sections: **Evidence** (file:line "
    "references and quoted lines that show the problem), **Repro / how to observe** "
    "(a command, a test, or the exact scenario), **Proposal** (a one-line remedy), "
    "**Size** (small | medium | large), **Kind** (bug | test | docs | refactor). "
    "No finding without evidence. At most 5 findings — pick the ones that matter. "
    "If the charter turns up nothing real, write no files and say so in your "
    "summary: an empty result is a valid, honest outcome. Each file becomes a "
    "GitHub issue for a human to triage. ROUTING: findings about THIS PROJECT's "
    "code go directly under `.sbxloop/backlog/`. Findings about sbxloop itself — "
    "the tool that ran you: its planner, prompts, verify lint, delivery, daemon — "
    "go under `.sbxloop/backlog/tool/` instead; they are filed to the tool's own "
    "tracker when the operator configured one, otherwise only noted in the closing "
    "comment, never filed as issues of this project. VERIFICATION: an audit changes "
    "no code, so its tasks need NO verify_commands (leave the list empty) — or at "
    "most a check on the findings you wrote (e.g. `ls .sbxloop/backlog/*.md`). Never "
    "the project's test suite, build or lint: an audit whose charter is to find a "
    "failing test cannot also promise the suite is green (field failure rakvqn6fr, "
    "where exactly that verify command sank the audit and its findings)."
)

TOOL_SUBDIR = BACKLOG_SUBDIR / "tool"


class ToolFindings(NamedTuple):
    filed: list[str]  # refs filed upstream (tool_repo configured)
    unfiled: list[str]  # titles noted only (no tool_repo)


def collect_tool_findings(
    run: RunRecord,
    *,
    dstore: DaemonStore,
    source: WorkSource,
    max_items: int,
    now: float,
) -> ToolFindings:
    """Findings the run addressed to the TOOL (``.sbxloop/backlog/tool/``):
    filed to the tool's tracker via ``source.file_tool_backlog`` when the
    operator configured ``[daemon] tool_repo``, otherwise returned as
    titles for the closing comment — never as issues of the project."""
    if not run.mounted or run.workspace is None:
        return ToolFindings([], [])
    folder = run.workspace / TOOL_SUBDIR
    if not folder.is_dir():
        return ToolFindings([], [])
    filer = getattr(source, "file_tool_backlog", None)
    filed: list[str] = []
    unfiled: list[str] = []
    for path in sorted(folder.glob("*.md")):
        try:
            title, body = parse_markdown_item(path.read_text(), path.stem)
        except OSError:
            continue
        if filer is None:
            unfiled.append(title)
            continue
        fp = fingerprint("tool:" + title, body)
        if dstore.backlog_seen(fp):
            continue
        if len(filed) >= max_items:
            unfiled.append(title)
            continue
        try:
            ref = filer(title, body, run.run_id)
        except Exception:
            logger.warning(
                "tool finding %r from run %s could not be filed", title, run.run_id, exc_info=True
            )
            unfiled.append(title)
            continue
        if ref is None:
            unfiled.append(title)
            continue
        dstore.backlog_record(fp, run.run_id, ref, now)
        filed.append(ref)
    return ToolFindings(filed, unfiled)


BACKLOG_INSTRUCTIONS = (
    "If you identify follow-up work that is OUT OF SCOPE for this outcome, do "
    "not expand scope. Write each such item as its own markdown file under "
    "`.sbxloop/backlog/` in the workspace — first line `# <short title>`, then a "
    "paragraph describing the work and why it matters. These are triaged and "
    "filed automatically after the run."
)


def fingerprint(title: str, body: str) -> str:
    return hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()


def collect_backlog(
    run: RunRecord,
    *,
    dstore: DaemonStore,
    source: WorkSource,
    max_items: int,
    trigger: bool,
    now: float,
) -> list[str]:
    """File the run's backlog items via ``source``; returns the refs filed."""
    if not run.mounted or run.workspace is None:
        logger.warning(
            "backlog skipped for run %s: workspace not mounted (agent-filed backlog "
            "requires the mounted-workspace mode)",
            run.run_id,
        )
        return []
    folder = run.workspace / BACKLOG_SUBDIR
    if not folder.is_dir():
        return []
    filed: list[str] = []
    skipped = 0
    for path in sorted(folder.glob("*.md")):
        try:
            title, body = parse_markdown_item(path.read_text(), path.stem)
        except OSError:
            continue
        fp = fingerprint(title, body)
        if dstore.backlog_seen(fp):
            continue
        if len(filed) >= max_items:
            skipped += 1
            continue
        try:
            ref = source.file_backlog(title, body, run.run_id, trigger=trigger)
        except Exception:
            logger.warning(
                "backlog: filing %r from run %s failed", title, run.run_id, exc_info=True
            )
            continue
        dstore.backlog_record(fp, run.run_id, ref, now)
        filed.append(ref)
    if skipped:
        logger.warning(
            "backlog: run %s produced %d item(s) beyond the per-run cap of %d; deferred "
            "(not fingerprint-recorded, so the next collection pass files them)",
            run.run_id,
            skipped,
            max_items,
        )
    return filed
