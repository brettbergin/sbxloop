"""The run's own review of its pull request, and the fix rounds it drives.

Review is a *stage of the run*, not a separate work item: a fresh read-only
agent session reads the PR's whole diff adversarially and returns a verdict,
which the engine acts on directly — a ``request_changes`` becomes a fix task
appended to the run's task table, built and verified like any other task,
re-delivered onto the same branch and reviewed again. The verdict is also
posted to the PR for the record (as a COMMENT review when GitHub refuses the
loop's identity as a reviewer, which it does for a PR's own author), but the
engine never reads it back from GitHub: **our verdict is authoritative**.

Findings carry forward. Every round sees the earlier rounds' findings and
what the fixer said about each — ``addressed`` or ``refuted: <reason>`` — and
is told not to re-raise a refuted finding unless it can say specifically why
the refutation is wrong. :class:`RefutedGuard` backs that rule mechanically:
a verdict whose every finding sits on a refuted anchor is sent back once
with the history quoted. Together with the round budgets that is what stops
a run arguing with itself; the old loop had no such memory and re-filed the
same findings run after run.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sbxloop.engine.model import FixKind, TaskSpec
from sbxloop.gh.ops import FailedCheck, ReviewComment, ReviewEvent

# How many inline comments one posted review may carry. A reviewer that
# anchors a hundred nits is not reviewing, and GitHub rejects oversized
# review bodies; the overflow is summarised in the body rather than dropped
# silently.
MAX_INLINE_COMMENTS = 25

# A fix round is ONE task, seeded rather than decomposed. The failures are
# already the acceptance criteria: asking an agent to decompose "mdformat
# failed" costs a whole session to rediscover a structure the engine already
# knows.
FIX_TASK_TITLE = "Make the pull request acceptable"
FIX_TASK_PREFIX = "fix-"

Severity = Literal["blocking", "major", "minor", "nit"]
Verdict = Literal["approve", "request_changes"]

# Severities that justify asking for changes. A review may note minor
# findings and nits and still approve; it may not block a PR on them.
BLOCKING_SEVERITIES: frozenset[str] = frozenset({"blocking", "major"})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewFinding(_Model):
    """One concrete problem the reviewer found, anchored to the diff."""

    path: str
    line: int | None = Field(default=None, ge=1)
    body: str
    severity: Severity = "major"

    @property
    def anchor(self) -> str:
        """``path:line`` — how a finding is matched across rounds."""
        return f"{self.path}:{self.line or 0}"

    @property
    def blocking(self) -> bool:
        return self.severity in BLOCKING_SEVERITIES

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"- `{where}` [{self.severity}] {self.body}"

    def comment(self) -> ReviewComment | None:
        """The inline comment this finding posts, or None when it has no
        line to anchor to (it is then listed in the review body)."""
        if self.line is None:
            return None
        return ReviewComment(path=self.path, line=self.line, body=f"[{self.severity}] {self.body}")


class ReviewVerdict(_Model):
    """REVIEW's output: the call on the PR, in the reviewer's words and as
    anchored findings."""

    verdict: Verdict
    summary: str
    findings: list[ReviewFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> ReviewVerdict:
        if not self.summary.strip():
            raise ValueError("`summary` must say what the reviewer looked at and concluded")
        if self.verdict == "request_changes" and not any(f.blocking for f in self.findings):
            raise ValueError(
                "`request_changes` needs at least one finding of severity `blocking` "
                "or `major`; minor findings and nits do not block a pull request — "
                "either raise the severity of a real problem or `approve`"
            )
        return self

    @property
    def blocking(self) -> list[ReviewFinding]:
        return [f for f in self.findings if f.blocking]

    @property
    def event(self) -> ReviewEvent:
        return "APPROVE" if self.verdict == "approve" else "REQUEST_CHANGES"

    def comments(self) -> list[ReviewComment]:
        anchored = [c for c in (f.comment() for f in self.findings) if c is not None]
        return anchored[:MAX_INLINE_COMMENTS]


class ReviewRound(NamedTuple):
    """One earlier round: what the reviewer found and what the fixer said."""

    round: int
    verdict: ReviewVerdict
    # The fix task's build report — where the fixer lists, per finding,
    # `addressed` or `refuted: <reason>`. Empty when no fix ran.
    response: str


def review_body(verdict: ReviewVerdict, *, run_id: str, round: int) -> str:
    """The review body posted to the PR: the reviewer's summary, any finding
    that had no line to anchor an inline comment to, the overflow the
    comment cap dropped, and provenance."""
    parts = [verdict.summary.strip()]
    unanchored = [f for f in verdict.findings if f.line is None]
    if unanchored:
        parts.append(
            "Findings without a line anchor:\n" + "\n".join(f.render() for f in unanchored)
        )
    anchored = [f for f in verdict.findings if f.line is not None]
    dropped = max(0, len(anchored) - MAX_INLINE_COMMENTS)
    if dropped:
        parts.append(
            f"_{dropped} further inline comment(s) were not posted "
            f"(cap {MAX_INLINE_COMMENTS}); the ones above are the review's own order._"
        )
    parts.append(f"<sub>sbxloop review round {round} of run `{run_id}`</sub>")
    return "\n\n".join(parts)


_REFUTED_LINE = re.compile(r"refut", re.IGNORECASE)


def refuted_anchors(rounds: Sequence[ReviewRound]) -> set[str]:
    """Anchors of findings the fixer refuted in an earlier round.

    The fixer's report is prose; the brief asks it to end with one line per
    finding saying ``addressed`` or ``refuted: <reason>``. A finding counts
    as refuted when a line of the response mentions refuting and names the
    finding's path — a loose match on purpose: the cost of a miss is one
    more review turn, the cost of a false positive is a reviewer forced to
    restate a real problem, and both are bounded.
    """
    refuted: set[str] = set()
    for entry in rounds:
        lines = [line for line in entry.response.splitlines() if _REFUTED_LINE.search(line)]
        if not lines:
            continue
        for finding in entry.verdict.findings:
            if any(finding.path in line for line in lines):
                refuted.add(finding.anchor)
    return refuted


def render_review_history(rounds: Sequence[ReviewRound]) -> str:
    """The earlier rounds as the next reviewer should see them."""
    if not rounds:
        return "(first review of this pull request)"
    blocks: list[str] = []
    for entry in rounds:
        findings = "\n".join(f.render() for f in entry.verdict.findings) or "- (no findings)"
        response = entry.response.strip() or "(no fix round ran after this review)"
        blocks.append(
            f"### Round {entry.round} — {entry.verdict.verdict}\n\n"
            f"{entry.verdict.summary.strip()}\n\nFindings:\n{findings}\n\n"
            f"The fixer's response:\n\n{response}"
        )
    return "\n\n".join(blocks)


class RefutedGuard:
    """Reject, once, a ``request_changes`` built entirely on refuted findings.

    Used as the ``check`` of the review phase's JSON acceptance. The first
    such verdict goes back to the reviewer with the rule quoted; a second
    one is accepted — the reviewer has now been told and insists, and a run
    that fails on a disagreement about refutations would be worse than one
    that spends a fix round on it.
    """

    def __init__(self, refuted: set[str]) -> None:
        self.refuted = refuted
        self.tripped = False

    def check(self, verdict: ReviewVerdict) -> None:
        if verdict.verdict != "request_changes" or not self.refuted or self.tripped:
            return
        blocking = verdict.blocking
        if blocking and all(f.anchor in self.refuted for f in blocking):
            self.tripped = True
            anchors = ", ".join(sorted({f.anchor for f in blocking}))
            raise ValueError(
                "every blocking finding sits on a finding the fixer already refuted "
                f"in an earlier round ({anchors}). Do not re-raise a refuted finding "
                "unless you can say specifically why the refutation is wrong — put "
                "that reason in the finding's body — otherwise drop it, and approve "
                "if nothing else blocks."
            )


def _pr_label(pr_number: int | None) -> str:
    return f"pull request #{pr_number}" if pr_number is not None else "the work in this tree"


def fix_brief(
    *,
    pr_number: int | None,
    kind: FixKind,
    why: str,
    round: int,
    findings: Sequence[ReviewFinding] = (),
    failed_checks: Sequence[FailedCheck] = (),
    objections: str = "",
) -> str:
    """What one fix round is for, concretely.

    Named failures rather than "make the PR acceptable": the round is one
    task whose acceptance criteria are exactly these, and a vague brief is
    what turns a small fix back into a full investigation. Everything the
    fixer needs is quoted here — its sandbox holds no GitHub credential, so
    telling it to read the PR itself hands it a tool that cannot work.
    ``pr_number`` is None before the first delivery (a red gate).
    """
    what = _pr_label(pr_number)
    parts = [
        f"{what[0].upper()}{what[1:]} is not yet acceptable (fix round {round}, {kind}): {why}.",
        "The work is already here in the working tree, on its own branch. "
        "Change only what is needed to clear the problems below — do not "
        "restructure or redo the existing work, and do not start over.",
    ]
    if findings:
        parts.append(
            "The review's findings:\n" + "\n".join(f.render() for f in findings) + "\n\n"
            "Address each one. A finding you believe is wrong may be refuted, "
            "but only with a specific reason."
        )
    if failed_checks:
        blocks = []
        for check in failed_checks:
            excerpt = check.excerpt.strip() or "(no log output was available)"
            blocks.append(f"#### `{check.name}` ({check.conclusion})\n\n```\n{excerpt}\n```")
        parts.append(
            "Failing checks, with their log output:\n\n"
            + "\n\n".join(blocks)
            + "\n\nMake these pass; run the project's own gate here before you finish."
        )
    if objections:
        parts.append(
            "Review comments a human left on the PR, quoted verbatim:\n\n"
            + objections
            + "\n\nAddress each one — with a change, or with a reasoned explanation."
        )
    parts.append(
        "End your summary with one line per finding or objection above, in the form "
        "`addressed: <path:line> — what changed` or `refuted: <path:line> — why it "
        "is not a problem`. The next review reads that list; a finding you refuted "
        "with a stated reason will not be raised again without a rebuttal."
    )
    return "\n\n".join(parts)


def fix_task(
    *,
    round: int,
    pr_number: int | None,
    brief: str,
    verify_commands: Sequence[str],
    failed_checks: Sequence[FailedCheck] = (),
) -> TaskSpec:
    """The seeded task for a fix round — deliberately one task.

    ``verify_commands`` is the mechanical exam the round must still pass:
    the union of the decomposer's verify commands plus the project gate,
    host-assembled so the fixer never writes its own exam. The task never
    depends on anything and is appended after the graph, so the scheduler
    never sees it; the engine drives it directly.
    """
    criteria = [
        f"PR #{pr_number}'s checks pass" if pr_number is not None else "the project gate passes",
        "every finding is addressed or refuted",
    ]
    criteria += [f"the `{check.name}` check passes" for check in failed_checks]
    return TaskSpec(
        id=f"{FIX_TASK_PREFIX}{round}",
        title=FIX_TASK_TITLE,
        description=brief,
        acceptance_criteria=criteria,
        verify_commands=list(dict.fromkeys(verify_commands)),
    )


def is_fix_task(task_id: str) -> bool:
    return task_id.startswith(FIX_TASK_PREFIX)
