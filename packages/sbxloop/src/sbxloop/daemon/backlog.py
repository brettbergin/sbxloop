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

from sbxloop.daemon.sources import WorkSource, parse_markdown_item
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import RunRecord

logger = logging.getLogger(__name__)

BACKLOG_SUBDIR = Path(".sbxloop") / "backlog"

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
