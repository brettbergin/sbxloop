"""Follow-up issues from a landed run (#517).

The reviewer routinely sees things that are real but out of scope for the
PR under review — and, correctly, keeps them out of ``findings`` so they do
not cost a fix round. Until now they were prose in a review body that
nobody reads once the PR merges (run rfxja288b left two, both worth issues,
both filed by hand). A fix round's ``deferred:`` answers (#522) are the same
shape: acknowledged, not in this PR.

This module is the pure half: gather the follow-ups a run produced across
its review rounds, merge duplicates (round 2 usually repeats round 1), and
render the issue bodies and the PR checklist. The engine files them — after
the run **lands**, never before, so a failed or blocked run litters nothing —
and never with the trigger label: a human promotes a follow-up to work.

That last rule is load-bearing. The 1.0 cutover removed every path by which
the loop filed its own work, because issues used to force the loop forward
had become a spiral (#498). A follow-up is filed with its own label, capped
per run, deduplicated by title within the run and by marker against the
repository, and left for a person.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Literal, NamedTuple

from sbxloop.engine.review import (
    Followup,
    ReviewRound,
    prior_findings,
    reconcile_rounds,
)

FollowupMode = Literal["issues", "comment", "off"]

# The stamp a filed follow-up carries in its body, so a resume between filing
# and recording — or a retry of the whole landing — finds it on the repository
# instead of filing it twice. The key is the follow-up's normalised title.
_MARKER_RE = re.compile(
    r"<!--\s*sbxloop-followup\s+run=(?P<run>\S+)\s+key=(?P<key>[0-9a-f]{16})\s*-->"
)


def followup_key(title: str) -> str:
    """The dedup key of a follow-up: its title with case, punctuation and
    spacing folded away ("micro-VM" and "microVM" are one note)."""
    folded = re.sub(r"[^a-z0-9]+", "", title.lower())
    return hashlib.sha256(folded.encode()).hexdigest()[:16]


def followup_marker(run_id: str, key: str) -> str:
    return f"<!-- sbxloop-followup run={run_id} key={key} -->"


def marker_key(body: str) -> tuple[str, str] | None:
    """``(run_id, key)`` from a filed follow-up's body, or None."""
    match = _MARKER_RE.search(body or "")
    return (match.group("run"), match.group("key")) if match else None


class Candidate(NamedTuple):
    """One follow-up to file, with where it came from."""

    key: str
    followup: Followup
    round: int
    source: Literal["review", "deferred"]


def collect_followups(rounds: Sequence[ReviewRound]) -> list[Candidate]:
    """Every follow-up the run's reviews produced, deduplicated by title.

    Two sources: the reviewer's own ``followups`` (any round), and findings
    a fix round ``deferred:`` with a reason (#522) — the latter become
    follow-ups titled by the finding, with the fixer's reason in the body.
    The first occurrence wins; a later round's restatement is dropped.
    """
    out: dict[str, Candidate] = {}
    for entry in rounds:
        for item in entry.verdict.followups:
            key = followup_key(item.title)
            out.setdefault(key, Candidate(key, item, entry.round, "review"))
    fates = reconcile_rounds([r for r in rounds if r.response.strip()])
    findings = prior_findings(rounds)
    for anchor, fate in fates.items():
        if fate.status != "deferred" or anchor not in findings:
            continue
        finding = findings[anchor]
        title = " ".join(finding.body.split())
        title = title[:100].rstrip(" .,;:") if len(title) > 100 else title.rstrip(" .")
        key = followup_key(title)
        if key in out:
            continue
        reason = fate.text or "deferred by the fix round"
        body = (
            f"Raised by the loop's review as a `{finding.severity}` finding and deferred "
            f"by the fix round: {reason}."
        )
        if finding.repro.strip():
            body += f"\n\nRepro: {' '.join(finding.repro.split())}"
        round_no = next(
            (r.round for r in rounds if any(f.anchor == anchor for f in r.verdict.findings)), 0
        )
        out[key] = Candidate(
            key,
            Followup(title=title, body=body, path=finding.path, line=finding.line),
            round_no,
            "deferred",
        )
    return list(out.values())


def issue_body(
    candidate: Candidate,
    *,
    run_id: str,
    repo: str,
    pr_number: int,
    pr_url: str,
    closes: int | None,
    trigger_label: str | None = None,
) -> str:
    """The follow-up issue's body: the note, then where it came from.

    ``trigger_label`` is the daemon's trigger for this repository when a
    daemon dispatched the run; the "add the trigger label" instruction is
    only written then (#631) — on a repository nothing polls, it would
    point at a label that does nothing.
    """
    lines = [candidate.followup.body.strip() or candidate.followup.title.strip(), ""]
    if candidate.followup.anchor:
        lines.append(f"Where: `{candidate.followup.anchor}`")
        lines.append("")
    origin = (
        f"Out of scope for [PR #{pr_number}]({pr_url})"
        if pr_url
        else f"Out of scope for PR #{pr_number}"
    )
    if closes is not None:
        origin += f" (issue #{closes})"
    how = (
        f"noted by the review in round {candidate.round}"
        if candidate.source == "review"
        else f"a review finding of round {candidate.round} the fix round deferred"
    )
    lines.append(f"{origin}, {how}; run `{run_id}` on `{repo}`.")
    queued = "Filed by sbxloop after that pull request merged. It is **not** queued for the loop"
    if trigger_label:
        queued += f": add the `{trigger_label}` label if you want it run."
    else:
        queued += "."
    lines.append(queued)
    lines.append("")
    lines.append(followup_marker(run_id, candidate.key))
    return "\n".join(lines)


def checklist_comment(
    candidates: Sequence[Candidate],
    *,
    run_id: str,
    filed: Sequence[tuple[str, str]] = (),
    reason: str | None = None,
) -> str:
    """The PR comment listing follow-ups — the whole record when filing is
    off (``[landing] followups = "comment"``), or a pointer to the issues
    when they were filed (``filed`` is ``(title, url)``). ``reason`` names
    why nothing was filed when it is not the configuration — Issues
    disabled on the repository (#631)."""
    lines = ["## Follow-ups", ""]
    if filed:
        lines.append("Real but out of scope here; filed as issues (not queued for the loop):")
        lines.append("")
        lines.extend(f"- [{title}]({url})" for title, url in filed)
    else:
        why = reason or '`[landing] followups = "comment"`'
        lines.append(f"Real but out of scope here. Not filed as issues ({why}); a human may:")
        lines.append("")
        lines.extend(f"- [ ] {c.followup.render()[2:]}" for c in candidates)
    lines.append("")
    lines.append(f"<!-- sbxloop-followups run={run_id} -->")
    return "\n".join(lines)
