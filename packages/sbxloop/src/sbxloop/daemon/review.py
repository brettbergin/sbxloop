"""A review of a delivered PR, posted to that PR.

The loop used to review its own work by filing GitHub issues: one charter
issue per delivered PR, and then one *backlog issue per finding* the review
turned up. In the field that produced

    PR #389 → #391 (charter) → #392, #393, #394, #395, #396 (findings)
    PR #375 → #378 (charter) → #379 to #383, two of them duplicates

— every one of them feedback about a diff, filed where the diff is not, with
nothing to converge on and a human left to triage the pile.

The findings belong on the pull request. The agent sandbox holds no
``GH_TOKEN`` and cannot post them itself, so it writes its review to
``.sbxloop/review.json`` in the run workspace — the same shape the backlog
lane uses (``.sbxloop`` is in the default artifact excludes, which keeps it
out of harvest and out of the delivery PR) — and the daemon posts it through
the github-ops sandbox afterwards.

The verdict is deliberately the *GitHub* one. ``REQUEST_CHANGES`` and
``APPROVE`` are states GitHub itself tracks and, under branch protection,
enforces; "the review was accepted" then means something a human and the
loop can both read off the PR rather than something sbxloop merely believes.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, NamedTuple

from sbxloop.engine.model import RunRecord, TaskSpec
from sbxloop.gh.ops import GithubOps, ReviewComment, ReviewEvent, SubmittedReview
from sbxloop.log import get_logger

log = get_logger(__name__)

REVIEW_FILE = Path(".sbxloop") / "review.json"

# How many inline comments one review may carry. A reviewer that anchors a
# hundred nits is not reviewing, and GitHub rejects oversized review bodies;
# the overflow is summarised in the body rather than dropped silently.
MAX_INLINE_COMMENTS = 25

# The review lane's contract, injected into the review run's outcome. The
# deliverable is a verdict on *this* PR, not issues: anything genuinely out
# of scope goes in the summary as prose for a human to file, because a
# reviewer that can open issues will, and that is the behaviour being
# replaced.
REVIEW_INSTRUCTIONS = (
    "This is a REVIEW of a pull request. Your deliverable is a verdict on that "
    "PR, written to `.sbxloop/review.json` in the workspace — not code, and not "
    "issues. Do not modify the tree except to write that one file.\n\n"
    "Check out the PR's branch and run the project's own gate (its `make check` "
    "or equivalent) — a PR that does not pass what CI enforces is not "
    "approvable, whatever the code looks like.\n\n"
    'The file is JSON: `verdict` is `"approve"` or `"request_changes"`; '
    "`summary` is markdown prose for the review body; `comments` is a list of "
    "`{path, line, body}` anchored to lines the PR actually changed. Anchor "
    "every concrete problem as a comment; keep the summary for what the "
    "reviewer would say out loud. If the PR is fine, `approve` with a summary "
    "saying why and no comments — a clean review is a valid result.\n\n"
    "Anything you notice that is real but out of scope for this PR goes in the "
    "summary as prose. Do not file it anywhere: a human decides whether it "
    "becomes work."
)


# A fix round is ONE task, seeded rather than decomposed. The failures are
# already the acceptance criteria: asking an agent to decompose "mdformat
# failed" costs a whole session to rediscover a structure the caller already
# knows, and a normal run is ~270 turns and the better part of an hour.
FIX_TASK_TITLE = "Make the pull request acceptable"


def fix_brief(pr_number: int, why: str, failed: Sequence[str] = ()) -> str:
    """What one fix round is for, concretely.

    Named failures rather than "make the PR acceptable": the round is one
    task whose acceptance criteria are exactly these, and a vague brief is
    what turns a small fix back into a full investigation.
    """
    parts = [
        f"Pull request #{pr_number} is not yet acceptable: {why}.",
        "You are working on that PR's own branch and its work is already "
        "here. Change only what is needed to clear the problems below — do "
        "not restructure or redo the existing work, and do not start over.",
    ]
    if failed:
        parts.append(
            "Failing checks: "
            + ", ".join(failed)
            + ". Run the project's own gate here and make it pass before you finish."
        )
    parts.append(
        "Any unresolved review comments on the PR say what a reviewer "
        "objected to; read them (`gh pr view --comments`) and address each one."
    )
    return "\n\n".join(parts)


def fix_tasks(pr_number: int, why: str, failed: Sequence[str] = ()) -> list[TaskSpec]:
    """The seeded task graph for a fix round — deliberately one task."""
    criteria = [f"PR #{pr_number}'s checks pass", "the review's objections are addressed"]
    criteria += [f"the `{name}` check passes" for name in failed]
    return [
        TaskSpec(
            id="fix",
            title=FIX_TASK_TITLE,
            description=fix_brief(pr_number, why, failed),
            acceptance_criteria=criteria,
        )
    ]


class ReviewResult(NamedTuple):
    """A parsed agent review, ready to post."""

    event: ReviewEvent
    summary: str
    comments: tuple[ReviewComment, ...]
    # Comments dropped by MAX_INLINE_COMMENTS, so the body can say so rather
    # than the reviewer appearing to have missed them.
    dropped: int = 0


def _comment(entry: Any) -> ReviewComment | None:
    """One inline comment, or None when the agent's entry is unusable.

    Defensive because this is agent-authored JSON: a comment missing its
    anchor cannot be posted, and one bad entry must not cost the review.
    """
    if not isinstance(entry, dict):
        return None
    path = str(entry.get("path") or "").strip()
    body = str(entry.get("body") or "").strip()
    raw_line = entry.get("line")
    try:
        line = int(raw_line)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not path or not body or line < 1:
        return None
    side: Literal["LEFT", "RIGHT"] = (
        "LEFT" if str(entry.get("side") or "").upper() == "LEFT" else "RIGHT"
    )
    return ReviewComment(path=path, line=line, body=body, side=side)


def parse_review(text: str) -> ReviewResult | None:
    """Parse ``.sbxloop/review.json``; None when it is unusable.

    An unparseable or verdict-less review is *not* silently treated as an
    approval — returning None leaves the PR un-reviewed, which is visibly
    unfinished, where a default approval would quietly wave work through.
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict in ("approve", "approved", "accept"):
        event: ReviewEvent = "APPROVE"
    elif verdict in ("request_changes", "request-changes", "reject", "revise"):
        event = "REQUEST_CHANGES"
    else:
        return None
    summary = str(data.get("summary") or "").strip()
    raw = data.get("comments")
    parsed = (
        [c for c in (_comment(e) for e in raw) if c is not None] if isinstance(raw, list) else []
    )
    kept, dropped = parsed[:MAX_INLINE_COMMENTS], max(0, len(parsed) - MAX_INLINE_COMMENTS)
    if not summary and not kept:
        # A verdict with nothing behind it is not a review.
        return None
    return ReviewResult(event, summary, tuple(kept), dropped)


def review_body(result: ReviewResult, *, origin_run_id: str) -> str:
    """The review body: the agent's summary, plus provenance and any
    overflow the comment cap dropped."""
    parts = [result.summary or "_(no summary)_"]
    if result.dropped:
        parts.append(
            f"_{result.dropped} further inline comment(s) were not posted "
            f"(cap {MAX_INLINE_COMMENTS}); the ones above are the review's own order._"
        )
    parts.append(f"<sub>sbxloop review of run `{origin_run_id}`</sub>")
    return "\n\n".join(parts)


def collect_review(
    run: RunRecord,
    *,
    ops: GithubOps,
    repo: str,
    pr_number: int,
    origin_run_id: str,
) -> SubmittedReview | None:
    """Post the run's review to ``pr_number``; None when it wrote none.

    Reads the mounted workspace directly, like the backlog lane — and for
    the same reason: ``.sbxloop`` never travels in the delivery, so an
    unmounted run has nowhere for the file to have survived.
    """
    if not run.mounted or run.workspace is None:
        log.warning(
            "review.skipped",
            run=run.run_id,
            reason=(
                "workspace not mounted; an agent-written review needs the mounted-workspace mode"
            ),
        )
        return None
    path = run.workspace / REVIEW_FILE
    try:
        text = path.read_text()
    except OSError:
        log.warning("review.absent", run=run.run_id, path=str(path), pr=pr_number)
        return None
    result = parse_review(text)
    if result is None:
        log.warning(
            "review.unparseable",
            run=run.run_id,
            path=str(path),
            pr=pr_number,
            hint="left un-reviewed rather than defaulted to an approval",
        )
        return None
    submitted = ops.pr_review_create(
        repo,
        pr_number,
        result.event,
        review_body(result, origin_run_id=origin_run_id),
        result.comments,
    )
    log.info(
        "review.posted",
        run=run.run_id,
        pr=pr_number,
        requested=result.event,
        posted=submitted.event,
        comments=len(result.comments),
        gates_merge=submitted.gates_merge,
    )
    return submitted
