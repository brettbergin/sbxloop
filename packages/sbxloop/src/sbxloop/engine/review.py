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
the refutation is wrong. :class:`ReviewGuard` backs that rule mechanically:
a verdict whose every finding sits on a refuted anchor is sent back once
with the history quoted. Together with the round budgets that is what stops
a run arguing with itself; the old loop had no such memory and re-filed the
same findings run after run.

Findings carry their reproduction (#521). The reviewer reproduces a defect
against the tree before filing it; that repro used to reach the fixer only
as prose, and the fixer wrote tests for the *shape the finding named* — so
rounds converged one adjacent case at a time (run rfxja288b: four real
findings on one migration, one per round, the budget spent one line short).
A blocking/major finding now carries a structured ``repro``, the fix brief
makes it a regression test that must fail first, asks for the neighbourhood
the same path sees, and shows the fixer the earlier rounds too.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
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
    """One concrete problem the reviewer found, anchored to the diff.

    ``repro`` is the reviewer's reproduction — the minimal setup, what
    happens, what should happen — in a form the fixer can turn into a test
    that fails on the current tree (#521). Asked for on every blocking or
    major finding; a nit needs none.
    """

    path: str
    line: int | None = Field(default=None, ge=1)
    body: str
    severity: Severity = "major"
    repro: str = ""

    @property
    def anchor(self) -> str:
        """``path:line`` — how a finding is matched across rounds."""
        return f"{self.path}:{self.line or 0}"

    @property
    def blocking(self) -> bool:
        return self.severity in BLOCKING_SEVERITIES

    @property
    def needs_repro(self) -> bool:
        """A blocking/major finding filed without a reproduction."""
        return self.blocking and not self.repro.strip()

    def render(self, *, repro: bool = True) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        text = f"- `{where}` [{self.severity}] {self.body}"
        if repro and self.repro.strip():
            steps = " ".join(self.repro.split())
            text += f"\n  Repro: {steps}"
        return text

    def comment(self) -> ReviewComment | None:
        """The inline comment this finding posts, or None when it has no
        line to anchor to (it is then listed in the review body)."""
        if self.line is None:
            return None
        body = f"[{self.severity}] {self.body}"
        if self.repro.strip():
            body += f"\n\n**Repro:** {' '.join(self.repro.split())}"
        return ReviewComment(path=self.path, line=self.line, body=body)


CarriedStatus = Literal["confirmed_fixed", "still_open"]


class CarriedVerdict(_Model):
    """Round *n+1*'s verdict on one finding an earlier round raised.

    Keyed by the earlier finding's ``path:line`` anchor, which is how the
    engine finds the thread that finding opened: the verdict is posted as a
    reply *there* rather than restated in a new review body (#520 step 4).
    """

    anchor: str
    status: CarriedStatus
    note: str = ""

    @property
    def fixed(self) -> bool:
        return self.status == "confirmed_fixed"

    def finding(self, severity: Severity = "major") -> ReviewFinding:
        """The still-open carried finding this verdict stands for, so a
        round that only confirms old problems still drives a fix round."""
        path, _, tail = self.anchor.rpartition(":")
        line = int(tail) if path and tail.isdigit() and int(tail) > 0 else None
        return ReviewFinding(
            path=path if line is not None else self.anchor,
            line=line,
            body=self.note.strip() or "still open after the fix round.",
            severity=severity,
        )


class ReviewVerdict(_Model):
    """REVIEW's output: the call on the PR, in the reviewer's words and as
    anchored findings."""

    verdict: Verdict
    summary: str
    findings: list[ReviewFinding] = Field(default_factory=list)
    # Round n+1 only: the verdict on each carried-over finding, keyed by its
    # anchor. Posted in that finding's thread, never in this review's body.
    confirmations: list[CarriedVerdict] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> ReviewVerdict:
        if not self.summary.strip():
            raise ValueError("`summary` must say what the reviewer looked at and concluded")
        if (
            self.verdict == "request_changes"
            and not any(f.blocking for f in self.findings)
            and not self.still_open
        ):
            raise ValueError(
                "`request_changes` needs at least one finding of severity `blocking` "
                "or `major`; minor findings and nits do not block a pull request — "
                "either raise the severity of a real problem or `approve`"
            )
        return self

    @property
    def still_open(self) -> list[CarriedVerdict]:
        """Carried-over findings this round says are *not* fixed."""
        return [c for c in self.confirmations if not c.fixed]

    @property
    def confirmed_fixed(self) -> list[CarriedVerdict]:
        return [c for c in self.confirmations if c.fixed]

    @property
    def blocking(self) -> list[ReviewFinding]:
        return [f for f in self.findings if f.blocking]

    @property
    def event(self) -> ReviewEvent:
        return "APPROVE" if self.verdict == "approve" else "REQUEST_CHANGES"

    def comments(self) -> list[ReviewComment]:
        anchored = [c for c in (f.comment() for f in self.findings) if c is not None]
        return anchored[:MAX_INLINE_COMMENTS]

    def carried_forward(self, prior: Mapping[str, ReviewFinding]) -> list[ReviewFinding]:
        """The still-open carried findings, as findings a fix round can act on.

        The earlier round's finding is reused when it is known (its severity
        and its own words are the record); the reviewer's note is appended so
        the fixer sees why the previous attempt did not settle it.
        """
        out: list[ReviewFinding] = []
        for carried in self.still_open:
            note = " ".join(carried.note.split()).strip()
            original = prior.get(carried.anchor)
            if original is None:
                out.append(carried.finding())
                continue
            body = original.body.rstrip()
            if note:
                body = f"{body}\n\nStill open after the fix round: {note}"
            out.append(original.model_copy(update={"body": body}))
        return out


class ReviewRound(NamedTuple):
    """One earlier round: what the reviewer found and what the fixer said."""

    round: int
    verdict: ReviewVerdict
    # The fix task's build report — where the fixer lists, per finding,
    # `addressed` or `refuted: <reason>`. Empty when no fix ran.
    response: str


def prior_findings(rounds: Sequence[ReviewRound]) -> dict[str, ReviewFinding]:
    """Every earlier round's findings by anchor — the carried-over set.

    A later round's wording wins for the same anchor: it is the most recent
    statement of the same problem.
    """
    out: dict[str, ReviewFinding] = {}
    for entry in rounds:
        for finding in entry.verdict.findings:
            out[finding.anchor] = finding
    return out


def split_carried(
    verdict: ReviewVerdict, prior: Mapping[str, ReviewFinding]
) -> tuple[ReviewVerdict, list[CarriedVerdict]]:
    """Separate round *n+1*'s word on old findings from its genuinely new ones.

    Two things land in the carried set: the reviewer's own ``confirmations``
    (anchor-keyed, as ``prompts/review.md`` asks for), and any finding it
    filed on an anchor an earlier round already raised — that is a restated
    carried finding, and it belongs in the existing thread, not in a fresh
    review body. Findings on anchors nobody raised before are left alone, so
    a first-round review (empty ``prior``) is returned untouched.
    """
    if not prior:
        return verdict, []
    carried: dict[str, CarriedVerdict] = {}
    for item in verdict.confirmations:
        if item.anchor in prior:
            carried[item.anchor] = item
    fresh: list[ReviewFinding] = []
    for finding in verdict.findings:
        if finding.anchor not in prior:
            fresh.append(finding)
            continue
        # A restated old finding is by definition still open.
        existing = carried.get(finding.anchor)
        note = finding.body.strip()
        if existing is not None and existing.fixed:
            # The reviewer both confirmed it fixed and re-filed it; the
            # re-filing is the more specific claim, so it wins.
            carried[finding.anchor] = CarriedVerdict(
                anchor=finding.anchor, status="still_open", note=note
            )
        elif existing is None:
            carried[finding.anchor] = CarriedVerdict(
                anchor=finding.anchor, status="still_open", note=note
            )
    ordered = [carried[a] for a in dict.fromkeys(list(carried))]
    body_verdict = verdict.model_copy(update={"findings": fresh, "confirmations": ordered})
    return body_verdict, ordered


VERDICT_LABEL: dict[str, str] = {"approve": "approve", "request_changes": "changes requested"}


def verdict_line(verdict: ReviewVerdict, *, round: int) -> str:
    """The standing verdict, said in words: a ``COMMENT`` review or a PR
    comment carries no review state a human can see, so the body says it."""
    return f"**Review verdict: {VERDICT_LABEL[verdict.verdict]}** (round {round})"


def review_body(
    verdict: ReviewVerdict,
    *,
    run_id: str,
    round: int,
    anchored: bool = True,
    in_body: Sequence[ReviewFinding] | None = None,
) -> str:
    """The review body posted to the PR: the verdict in words, the
    reviewer's summary, any finding that had no line to anchor an inline
    comment to, the overflow the comment cap dropped, and provenance.

    ``anchored=False`` renders *every* finding into the body instead — the
    shape for a review posted without inline comments, which is what a
    reviewer that anchored a finding to a line outside the diff gets
    (GitHub 422s the whole review otherwise, and losing the findings over
    an anchor would be the worst outcome). ``in_body`` names exactly the
    findings that got no thread of their own — the single-identity review
    (#513), which posts findings one comment at a time and knows per anchor
    which ones GitHub refused.
    """
    parts = [verdict_line(verdict, round=round), verdict.summary.strip()]
    if in_body is not None:
        if in_body:
            parts.append(
                "Findings without a thread of their own:\n" + "\n".join(f.render() for f in in_body)
            )
        parts.append(f"<sub>sbxloop review round {round} of run `{run_id}`</sub>")
        return "\n\n".join(parts)
    if not anchored:
        if verdict.findings:
            parts.append("Findings:\n" + "\n".join(f.render() for f in verdict.findings))
        parts.append(f"<sub>sbxloop review round {round} of run `{run_id}`</sub>")
        return "\n\n".join(parts)
    unanchored = [f for f in verdict.findings if f.line is None]
    if unanchored:
        parts.append(
            "Findings without a line anchor:\n" + "\n".join(f.render() for f in unanchored)
        )
    anchored_findings = [f for f in verdict.findings if f.line is not None]
    dropped = max(0, len(anchored_findings) - MAX_INLINE_COMMENTS)
    if dropped:
        parts.append(
            f"_{dropped} further inline comment(s) were not posted "
            f"(cap {MAX_INLINE_COMMENTS}); the ones above are the review's own order._"
        )
    parts.append(f"<sub>sbxloop review round {round} of run `{run_id}`</sub>")
    return "\n\n".join(parts)


_REFUTED_LINE = re.compile(r"refut", re.IGNORECASE)
_ADDRESSED_LINE = re.compile(r"address", re.IGNORECASE)
# Leading list markers/emphasis the fixer's report often wraps the line in.
_LEAD = re.compile(r"^[\s>*_`-]+")
# The separator between the anchor and the note: em dash, en dash, ASCII
# hyphen, or a colon — the brief asks for an em dash, models write all four.
_SEP = re.compile(r"\s*[\u2014\u2013:-]+\s*")

ReconcileStatus = Literal["addressed", "refuted", "unanswered"]


class Reconciliation(NamedTuple):
    """What the fixer said about one finding: its status, its own words,
    and — for an addressed finding — the regression test it named (#521)."""

    status: ReconcileStatus
    note: str
    test: str = ""

    @property
    def text(self) -> str:
        """The note with the named test appended — what a reply says."""
        if not self.test:
            return self.note
        return f"{self.note} (test: `{self.test}`)" if self.note else f"test: `{self.test}`"


# `addressed: <anchor> — what changed; test: tests/unit/test_x.py::test_y`
_TEST_TAIL = re.compile(r"[;,(]?\s*\btest(?:s)?\s*:\s*(?P<test>[^;)]+)\)?\s*$", re.IGNORECASE)


def split_test(note: str) -> tuple[str, str]:
    """The ``test: <id>`` tail of a fixer's note, separated from the note."""
    match = _TEST_TAIL.search(note)
    if match is None:
        return note.strip(" ."), ""
    return note[: match.start()].strip(" .;,("), match.group("test").strip(" `'\".")


def _note_after(line: str, *, path: str, anchor: str) -> str:
    """The fixer's 'what changed' / 'why' from one report line.

    Everything after the anchor (or, failing that, after the status word),
    with the separator and any list decoration stripped. Prose lines that
    carry no note at all yield "".
    """
    text = _LEAD.sub("", line.strip()).rstrip()
    lowered = text.lower()
    cut = -1
    for token in (anchor, path):
        idx = lowered.find(token.lower())
        if idx >= 0:
            cut = max(cut, idx + len(token))
    if cut < 0:
        match = _REFUTED_LINE.search(text) or _ADDRESSED_LINE.search(text)
        cut = match.end() if match else 0
    rest = text[cut:]
    rest = re.sub(r"^[`'\"\s]+", "", rest)
    return _SEP.sub("", rest, count=1).strip(" `'\".") if _SEP.match(rest) else rest.strip(" `'\".")


def _names(line: str, finding: ReviewFinding) -> bool:
    """Does this report line name the finding?

    An exact ``path:line`` anchor wins; otherwise the path alone is enough —
    a loose match on purpose: the cost of a miss is one more review turn,
    the cost of a false positive is a reviewer forced to restate a real
    problem, and both are bounded.
    """
    return _names_path(line, finding.path, finding.line)


def _names_path(line: str, path: str, lineno: int | None) -> bool:
    if not path:
        return False
    if lineno is not None and f"{path}:{lineno}" in line:
        return True
    if path not in line:
        return False
    # Another finding on the same path claimed this line by exact anchor.
    return not re.search(re.escape(path) + r":\d+", line)


def _status_lines(report: str) -> tuple[list[str], list[str]]:
    """The report's ``refuted`` and ``addressed`` lines, in that order."""
    refuted: list[str] = []
    addressed: list[str] = []
    for line in report.splitlines():
        if not line.strip():
            continue
        if _REFUTED_LINE.search(line):
            refuted.append(line)
        elif _ADDRESSED_LINE.search(line):
            addressed.append(line)
    return refuted, addressed


def reconcile_anchor(report: str, anchor: str) -> Reconciliation:
    """The fixer's answer to one ``path:line`` (or bare ``path``) anchor.

    The same parsing :func:`reconcile` does per finding, exposed for
    objections that are not findings of a review round — a human's inline
    review comment, most of all (#520). An anchor no line names, and an
    empty anchor (a review *body* objection, which has no path at all), come
    back ``unanswered`` unless the report is a single overall statement; the
    caller decides what to do with that.
    """
    path, _, tail = anchor.rpartition(":")
    if not path:
        path, lineno = anchor, None
    else:
        lineno = int(tail) if tail.isdigit() else None
        if lineno is None:
            path = anchor
    refuted, addressed = _status_lines(report)
    for status, lines in (("refuted", refuted), ("addressed", addressed)):
        hit = next((line for line in lines if _names_path(line, path, lineno)), None)
        if hit is not None:
            note, test = split_test(_note_after(hit, path=path, anchor=anchor))
            return Reconciliation(status, note, test)  # type: ignore[arg-type]
    return Reconciliation("unanswered", "")


def reconcile(round: ReviewRound) -> dict[str, Reconciliation]:
    """Per-finding reconciliation of one review round against the fixer's
    report — ``addressed`` / ``refuted`` / ``unanswered``, with the note.

    The fixer's report is prose; ``fix_brief`` asks it to end with one line
    per finding in the form ``addressed: <path:line> — what changed`` or
    ``refuted: <path:line> — why``. Parsing tolerates the dash variants,
    stray whitespace, list markers and case; lines naming anchors that are
    not findings of this round are ignored. A finding no line names is
    ``unanswered`` — the round said nothing about it, which is not the same
    as leaving it alone deliberately.
    """
    refuted_lines, addressed_lines = _status_lines(round.response)
    out: dict[str, Reconciliation] = {}
    for f in round.verdict.findings:
        for status, lines in (("refuted", refuted_lines), ("addressed", addressed_lines)):
            hit = next((line for line in lines if _names(line, f)), None)
            if hit is not None:
                note, test = split_test(_note_after(hit, path=f.path, anchor=f.anchor))
                out[f.anchor] = Reconciliation(status, note, test)  # type: ignore[arg-type]
                break
        else:
            out[f.anchor] = Reconciliation("unanswered", "")
    return out


def reconcile_rounds(rounds: Sequence[ReviewRound]) -> dict[str, Reconciliation]:
    """:func:`reconcile` across rounds; a later round's word wins, except
    that an ``unanswered`` never overwrites a status already given."""
    out: dict[str, Reconciliation] = {}
    for entry in rounds:
        for anchor, item in reconcile(entry).items():
            if item.status == "unanswered" and anchor in out:
                continue
            out[anchor] = item
    return out


def refuted_anchors(rounds: Sequence[ReviewRound]) -> set[str]:
    """Anchors of findings the fixer refuted in an earlier round."""
    return {
        anchor
        for entry in rounds
        for anchor, item in reconcile(entry).items()
        if item.status == "refuted"
    }


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


def render_fix_history(rounds: Sequence[ReviewRound]) -> str:
    """The earlier rounds as the *next fixer* should see them (#521).

    A fixer is a fresh session with no memory of why the previous fixer
    chose what it chose; without this, round 3's fixer re-derives — or
    contradicts — round 2's decision. Per round: the findings, then each
    one's fate in the fixer's own words (and the regression test it
    named), which is the reconciliation the engine already computes, not
    the raw report.
    """
    answered = [r for r in rounds if r.response.strip()]
    if not answered:
        return ""
    blocks: list[str] = []
    for entry in answered:
        items = reconcile(entry)
        lines: list[str] = []
        for finding in entry.verdict.findings:
            item = items.get(finding.anchor, Reconciliation("unanswered", ""))
            fate = f"{item.status} — {item.text}" if item.text else str(item.status)
            lines.append(f"{finding.render(repro=False)}\n  → {fate}")
        blocks.append(
            f"### Round {entry.round} — {entry.verdict.verdict}\n\n"
            + ("\n".join(lines) or "- (no findings)")
        )
    return "\n\n".join(blocks)


class ReviewGuard:
    """Send a verdict back to the reviewer, once, for two shapes of defect.

    Used as the ``check`` of the review phase's JSON acceptance. A
    ``request_changes`` built entirely on refuted findings, or a blocking /
    major finding filed without its ``repro`` (#521), goes back with the
    rule quoted; the next verdict is accepted whatever it says — the
    reviewer has now been told and insists, and a run that fails on that
    disagreement would be worse than one that spends a fix round on it.
    One trip in total: the acceptance path retries exactly once.
    """

    def __init__(self, refuted: set[str]) -> None:
        self.refuted = refuted
        self.tripped = False

    def check(self, verdict: ReviewVerdict) -> None:
        if self.tripped:
            return
        blocking = verdict.blocking
        if (
            verdict.verdict == "request_changes"
            and self.refuted
            and blocking
            and all(f.anchor in self.refuted for f in blocking)
        ):
            self.tripped = True
            anchors = ", ".join(sorted({f.anchor for f in blocking}))
            raise ValueError(
                "every blocking finding sits on a finding the fixer already refuted "
                f"in an earlier round ({anchors}). Do not re-raise a refuted finding "
                "unless you can say specifically why the refutation is wrong — put "
                "that reason in the finding's body — otherwise drop it, and approve "
                "if nothing else blocks."
            )
        missing = [f.anchor for f in verdict.findings if f.needs_repro]
        if missing:
            self.tripped = True
            raise ValueError(
                "these blocking/major findings carry no `repro` "
                f"({', '.join(missing)}). You reproduced each one before filing it: "
                "put that reproduction in the finding's `repro` — the minimal setup, "
                "what happens, what should happen — concrete enough to become a test "
                "that fails on this tree. A finding you cannot reproduce is `minor` "
                "at most."
            )


# The old name, kept for callers that predate the repro rule.
RefutedGuard = ReviewGuard


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
    conflicts: Sequence[str] = (),
    history: str = "",
) -> str:
    """What one fix round is for, concretely.

    Named failures rather than "make the PR acceptable": the round is one
    task whose acceptance criteria are exactly these, and a vague brief is
    what turns a small fix back into a full investigation. Everything the
    fixer needs is quoted here — its sandbox holds no GitHub credential, so
    telling it to read the PR itself hands it a tool that cannot work.
    ``pr_number`` is None before the first delivery (a red gate).

    Each finding's ``repro`` becomes a required regression test — written
    first, failing on the current tree, then made to pass — and the brief
    asks for the neighbourhood: the other inputs the same code path sees.
    Both target the field pattern of fix rounds that settle exactly the
    case the finding named and leave the adjacent one for the next round
    (#521). ``history`` is the earlier rounds as :func:`render_fix_history`
    renders them, so this fixer knows what its predecessors chose and why.
    """
    what = _pr_label(pr_number)
    parts = [
        f"{what[0].upper()}{what[1:]} is not yet acceptable (fix round {round}, {kind}): {why}.",
        "The work is already here in the working tree, on its own branch. "
        "Change only what is needed to clear the problems below — do not "
        "restructure or redo the existing work, and do not start over.",
    ]
    if history.strip():
        parts.append(
            "Earlier fix rounds on this pull request, with each finding's fate in "
            "the previous fixer's words — build on those decisions rather than "
            "re-deriving or silently reversing them:\n\n" + history.strip()
        )
    if findings:
        with_repro = [f for f in findings if f.repro.strip()]
        parts.append(
            "The review's findings:\n" + "\n".join(f.render() for f in findings) + "\n\n"
            "Address each one. A finding you believe is wrong may be refuted, "
            "but only with a specific reason."
        )
        if with_repro:
            parts.append(
                "Each finding's **Repro** is how the reviewer reproduced it against "
                "this tree. Reproduce it first, as a test that fails on the current "
                "tree, then make it pass — the test is the deliverable that proves "
                "the finding is closed, and it stays in the suite. Build the test's "
                "setup the way the repro describes it (a raw row, a stored value, a "
                "malformed input), not through the code path under test: a test "
                "that constructs its fixture with the very function being fixed "
                "cannot see the bug."
            )
        parts.append(
            "Before you finish, list the other inputs this same code path sees — "
            "the row states, id forms, config shapes or error paths adjacent to "
            "the one the finding names — and cover the ones your change affects. "
            "Rounds are spent one finding at a time when a fix settles exactly the "
            "named case and leaves its neighbour for the next review."
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
    if conflicts:
        parts.append(
            "The base branch was merged into this tree and left conflict markers in:\n"
            + "\n".join(f"- `{path}`" for path in conflicts)
            + "\n\nResolve every marker so the file is correct against the *current* "
            "base, then complete the merge with `git add -A && git commit --no-edit` "
            "before you finish — the delivery diffs against the merged base."
        )
    parts.append(
        "End your summary with one line per finding or objection above, in the form "
        "`addressed: <path:line> — what changed; test: <the regression test you added, "
        "e.g. tests/test_x.py::test_y>` or `refuted: <path:line> — why it "
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
