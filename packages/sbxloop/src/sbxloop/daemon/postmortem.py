"""Post-mortems the daemon files for its own failures.

When an item is abandoned (or completes but cannot deliver), the daemon
already knows the most about why — the plan, every verify transcript, the
failure events. The auditor that will investigate runs in a fresh clone of
the repository and cannot read the daemon's state DB, so the evidence has to
travel *in the issue*. ``build_dossier`` assembles it, clipped so a
pathological run cannot produce a megabyte issue body, and the daemon files
it as an audit-lane charter (``sbxloop:audit``): the discovery lane then
turns the failure into evidenced backlog items — bug plus proposed fix.

Pure over the two stores; nothing here talks to GitHub.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from sbxloop.cli.tui import format_event
from sbxloop.daemon.model import WorkItem
from sbxloop.engine.store import StateStore
from sbxloop.errors import SbxloopError

POSTMORTEM_MARKER = "<!-- sbxloop-postmortem"
DOSSIER_MAX_CHARS = 20_000
_TRANSCRIPT_TAIL_LINES = 40
_EVENTS_TAIL = 30
_NOISE = ("agent.message_delta", "worker.heartbeat", "agent.usage", "sandbox.resources", "gh.op_")


def postmortem_marker(run_id: str) -> str:
    return f"{POSTMORTEM_MARKER} {run_id} -->"


def _tail(text: str, lines: int) -> str:
    rows = [r for r in str(text).splitlines() if r.strip()]
    if len(rows) <= lines:
        return "\n".join(rows)
    return f"… ({len(rows) - lines} earlier lines omitted)\n" + "\n".join(rows[-lines:])


def _run_section(store: StateStore, run_id: str) -> list[str]:
    out = [f"## Run `{run_id}`"]
    try:
        record = store.get_run(run_id)
    except SbxloopError:
        out.append("_no persisted run (it died before create_run)_")
        return out
    out.append(f"State: **{record.state}**")
    tasks = store.get_tasks(run_id)
    for task in tasks:
        spec = task.spec
        out.append(
            f"### Task `{spec.id}` — {spec.title} — **{task.state}** "
            f"(revisions {task.revisions}, replans {task.replans})"
        )
        if spec.verify_commands:
            out.append("Verify commands (decomposer-authored, run under `sh -c`):")
            out.append("```sh")
            out.extend(spec.verify_commands)
            out.append("```")
        if task.last_feedback:
            out.append("Last feedback:")
            out.append("```text")
            out.append(_tail(task.last_feedback, 20))
            out.append("```")
        # The last verify transcript is usually the whole story.
        row = store.latest_phase_attempt(run_id, spec.id, "verify")
        if row is not None:
            try:
                payload = json.loads(row["output_json"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            transcript = payload.get("results") or payload.get("feedback") or ""
            if transcript:
                out.append(f"Last verify attempt ({row['status']}, attempt {row['attempt']}):")
                out.append("```text")
                out.append(_tail(str(transcript), _TRANSCRIPT_TAIL_LINES))
                out.append("```")
    # Failure-shaped events, then the tail of everything else that is not noise.
    failures = []
    recent = []
    for _seq, event in store.events(run_id):
        if any(event.type.startswith(n) for n in _NOISE):
            continue
        line = format_event(event)
        if event.type in ("worker.error", "run.deliver") or (
            event.type == "phase.end" and event.data.get("status") in ("failed", "degraded")
        ):
            failures.append(line)
        recent.append(line)
    if failures:
        out.append("Failure events:")
        out.append("```text")
        out.extend(failures[-20:])
        out.append("```")
    if recent:
        out.append(f"Last {min(_EVENTS_TAIL, len(recent))} events:")
        out.append("```text")
        out.extend(recent[-_EVENTS_TAIL:])
        out.append("```")
    return out


def build_dossier(
    store: StateStore,
    item: WorkItem,
    run_ids: Sequence[str],
    reason: str,
    *,
    state_dir: str | None = None,
) -> str:
    """The Markdown body of a post-mortem issue for ``item``.

    Sections: what the item was and how it ended; per run, the plan, the
    last verify transcript, failure events and the recent event tail; and
    the exact CLI a human needs to dig deeper. Clipped to
    ``DOSSIER_MAX_CHARS`` from the middle-out (older runs go first).
    """
    head = [
        f"# Post-mortem: {item.title.strip()}",
        "",
        f"Item `{item.item_id}` ({item.url or item.source_key}) ended **{reason}** after "
        f"{item.attempts} attempt(s).",
        "",
        "Charter: read the evidence below and decide where the fault is — the code, the "
        "plan (a wrong or unrunnable verify command), the prompts, or a lint gap that let "
        "a bad plan through. File ONE finding per distinct root cause with a concrete "
        "proposal (a lint rule, a prompt line, a code fix, a test). If the failure was "
        "transient infrastructure with nothing to fix, say so and file nothing.",
        "",
    ]
    if state_dir:
        head.append(
            "For a human: `SBXLOOP_STATE_DIR="
            f"{state_dir} sbxloop logs <run>` / `sbxloop status <run>` replay the full "
            "transcript."
        )
        head.append("")
    body: list[str] = []
    for run_id in run_ids:
        body.extend(_run_section(store, run_id))
        body.append("")
    text = "\n".join(head + body).rstrip()
    if len(text) > DOSSIER_MAX_CHARS:
        keep = DOSSIER_MAX_CHARS - 200
        text = (
            text[: keep // 3]
            + "\n\n… _(dossier clipped; the middle was cut — see the CLI line above)_ …\n\n"
            + text[-(keep - keep // 3) :]
        )
    return text
