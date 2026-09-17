"""Follow-up issues from a landed run (#517).

The reviewer routinely sees things that are real but out of scope for the
PR under review — and, correctly, keeps them out of ``findings`` so they do
not cost a fix round. Until now they were prose in a review body that
nobody reads once the PR merges (run rfxja288b left two, both worth issues,
both filed by hand). A fix round's ``deferred:`` answers (#522) are the same
shape: acknowledged, not in this PR.

This module gathers the follow-ups a run produced across its review rounds,
merges duplicates (round 2 usually repeats round 1), renders the issue
bodies and the PR checklist, and files them (:class:`FollowupFiler`). The
engine files them when its landing clears every bar; the daemon files them
again when it completes a parked landing (a merge gate or a review wait)
with gh ops alone. Never for a failed or blocked run, so it litters
nothing, and never with the trigger label: a human promotes a follow-up to
work.

That last rule is load-bearing. The 1.0 cutover removed every path by which
the loop filed its own work, because issues used to force the loop forward
had become a spiral (#498). A follow-up is filed with its own label, capped
per run, deduplicated by title within the run and by marker across runs,
and left for a person. Creation also requires the reviewer's completed
issue lookup (``issue_lookup.py``); unchecked notes remain on the PR.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from pydantic import ValidationError

from sbxloop.engine.issue_lookup import IssueLookup, LookupUnavailable
from sbxloop.engine.review import (
    Followup,
    ReviewRound,
    ReviewVerdict,
    is_fix_task,
    prior_findings,
    reconcile_rounds,
)
from sbxloop.errors import GithubOpsError
from sbxloop.events import EventBus, HostEventTypes
from sbxloop.log import get_logger
from sbxloop.vcs.github.labels import FOLLOWUP_DESCRIPTOR, LabelSpec, ensure_label
from sbxloop.vcs.github.ops import MalformedResponse

if TYPE_CHECKING:
    from sbxloop.config import Config
    from sbxloop.engine.model import RunRecord
    from sbxloop.engine.store import StateStore
    from sbxloop.vcs.protocol import VcsOps

log = get_logger(__name__)

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
    filed_by: str | None = None,
) -> str:
    """The follow-up issue's body: the note, then where it came from.

    ``trigger_label`` is the daemon's trigger for this repository when a
    daemon dispatched the run; the "add the trigger label" instruction is
    only written then (#631) — on a repository nothing polls, it would
    point at a label that does nothing. ``filed_by`` names who the issue
    is filed on behalf of; without it the body is unchanged.
    """
    lines = [candidate.followup.body.strip() or candidate.followup.title.strip(), ""]
    if candidate.followup.decision == "regression":
        lines.extend(
            [
                f"Regression of issue #{candidate.followup.existing_issue}: "
                + candidate.followup.rationale.strip(),
                "",
                "Reproduction: " + candidate.followup.repro.strip(),
                "",
            ]
        )
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
    if filed_by:
        lines.append(f"Filed on behalf of {filed_by}.")
    lines.append("")
    lines.append(followup_marker(run_id, candidate.key))
    return "\n".join(lines)


def checklist_comment(
    candidates: Sequence[Candidate],
    *,
    run_id: str,
    filed: Sequence[tuple[str, str]] = (),
    reason: str | None = None,
    held: Sequence[tuple[Candidate, str]] = (),
) -> str:
    """The PR comment listing follow-ups — the whole record when filing is
    off (``[landing] followups = "comment"``), or a pointer to the issues
    when they were filed (``filed`` is ``(title, url)``). ``reason`` names
    why nothing was filed when it is not the configuration — Issues
    disabled on the repository (#631)."""
    lines = ["## Follow-ups", ""]
    if filed:
        lines.append("Real but out of scope here; tracked in issues (not queued by this run):")
        lines.append("")
        lines.extend(f"- [{title}]({url})" for title, url in dict.fromkeys(filed))
    elif not held:
        why = reason or '`[landing] followups = "comment"`'
        lines.append(f"Real but out of scope here. Not filed as issues ({why}); a human may:")
        lines.append("")
        lines.extend(f"- [ ] {c.followup.render()[2:]}" for c in candidates)
    if held:
        lines.extend(["", "Not filed — issue lookup needs triage:", ""])
        lines.extend(f"{c.followup.render()}\n  Reason: {why}" for c, why in held)
    lines.append("")
    lines.append(f"<!-- sbxloop-followups run={run_id} -->")
    return "\n".join(lines)


def recorded_review_rounds(store: StateStore, run_id: str) -> list[ReviewRound]:
    """A run's review rounds paired with the fix round each led to.

    Read from ``phase_attempts`` in order: a ``review`` row opens a
    round; the build report of the fix task recorded after it is that
    round's response. Chronology, not bookkeeping, so a resume sees the
    same history a live run would.
    """
    rounds: list[ReviewRound] = []
    for row in store.phase_attempts(run_id):
        if row.phase == "review":
            try:
                data = json.loads(row.output_json or "{}")
                verdict = ReviewVerdict.model_validate(data.get("verdict") or data)
            except (ValueError, ValidationError):
                continue
            rounds.append(ReviewRound(len(rounds) + 1, verdict, ""))
        elif row.phase == "build" and rounds and row.task_id and is_fix_task(str(row.task_id)):
            try:
                report = json.loads(row.output_json or "{}").get("report") or ""
            except ValueError:
                report = ""
            last = rounds[-1]
            rounds[-1] = ReviewRound(last.round, last.verdict, str(report))
    return rounds


class FollowupFiler:
    """Files a landed run's follow-ups on its repository (#517).

    Shared by the engine (its own landing) and the daemon (a parked landing
    it completes with gh ops alone). ``cfg`` supplies ``[landing]``
    (``followups``, ``followup_label``, ``max_followups_per_run``) and the
    issue the run closes (``[github] deliver_closes``); ``trigger_label`` is
    the daemon's trigger for the repository, or None when no daemon watches
    it (#631).
    """

    def __init__(
        self,
        ops: VcsOps,
        repo: str,
        store: StateStore,
        bus: EventBus,
        cfg: Config,
        trigger_label: str | None = None,
    ) -> None:
        self.ops, self.repo, self.store, self.bus = ops, repo, store, bus
        self.cfg = cfg
        self.trigger_label = trigger_label

    def file(
        self,
        run: RunRecord,
        rounds: Sequence[ReviewRound],
        *,
        issues_enabled: bool | None,
        attribution: str | None = None,
    ) -> None:
        """File the run's follow-ups after the merge.

        Best-effort and idempotent: the PR is merged, so a GitHub failure
        here is logged, never raised. Each filed issue is recorded as a
        ``followup`` phase row before the next is filed, and the body carries
        a run/key marker, so a resume between filing and recording (or a
        second pass over the same landing) finds the issue on the repository
        rather than filing it twice. Never queued for the loop: the
        follow-up label, not the trigger label.

        A repository with Issues disabled cannot take them (#631): the mode
        downgrades to ``comment`` (one checklist on the PR) and the
        ``run.followups`` event records the downgrade. Decided up front from
        ``issues_enabled`` (False), or on the spot from the 410 GitHub
        answers the first filing with. ``attribution`` names who the issues
        are filed on behalf of; None leaves the bodies unchanged.
        """
        ops, repo, run_id = self.ops, self.repo, run.run_id
        cfg = self.cfg.landing
        if cfg.followups == "off" or run.pr_number is None:
            return
        candidates = collect_followups(rounds)[: cfg.max_followups_per_run]
        if not candidates:
            return
        already = self.recorded(run_id)
        filed: list[tuple[str, str]] = []
        listed: list[str] = []
        reused: list[tuple[str, str]] = []
        # What this pass itself did (an issue filed, a row recorded, a
        # comment posted). A pass that finds everything already recorded
        # (the daemon's pass after the engine filed at the park) reports
        # nothing, rather than a second event calling the run's own issues
        # "already tracked".
        fresh: list[str] = []
        started = time.time()
        mode: str = cfg.followups
        downgraded = False
        if mode == "issues" and issues_enabled is False:
            mode, downgraded = "comment", True
        try:
            if mode == "issues":
                try:
                    self._file_issues(
                        run,
                        candidates,
                        already,
                        filed,
                        listed,
                        reused,
                        fresh,
                        started,
                        attribution,
                    )
                except GithubOpsError as exc:
                    if exc.http_status != 410:
                        raise
                    # "Issues are disabled for this repo": the probe had
                    # no `has_issues` to go on; downgrade now.
                    log.info("run.followups_issues_gone", run=run_id, repo=repo, error=str(exc))
                    mode, downgraded = "comment", True
            if mode == "comment":
                if "(comment)" not in already:
                    ops.pr_issue_comment(
                        repo,
                        run.pr_number,
                        checklist_comment(
                            candidates,
                            run_id=run_id,
                            reason=(
                                "Issues are disabled on this repository" if downgraded else None
                            ),
                        ),
                    )
                    self._record_comment(run_id, len(already) + 1, len(candidates), started)
                    fresh.append("(comment)")
                listed = [c.followup.title.strip() for c in candidates]
        except GithubOpsError:
            log.warning("run.followups_failed", run=run_id, pr=run.pr_number, exc_info=True)
        if not fresh or (not filed and not listed):
            return
        extra: dict[str, Any] = {}
        if reused:
            extra["reused"] = [{"title": t, "url": u} for t, u in reused]
        if downgraded:
            extra.update(downgraded_from="issues", reason="issues_disabled")
        log.info(
            "run.followups",
            run=run_id,
            pr=run.pr_number,
            mode=mode,
            filed=[url for _, url in filed],
            listed=len(listed),
            **extra,
        )
        self.bus.emit(
            HostEventTypes.RUN_FOLLOWUPS,
            run_id,
            pr=run.pr_number,
            mode=mode,
            filed=[{"title": t, "url": u} for t, u in filed],
            listed=listed,
            **extra,
        )

    def _file_issues(
        self,
        run: RunRecord,
        candidates: Sequence[Candidate],
        already: dict[str, str],
        filed: list[tuple[str, str]],
        listed: list[str],
        reused: list[tuple[str, str]],
        fresh: list[str],
        started: float,
        attribution: str | None,
    ) -> None:
        """The ``issues`` mode of :meth:`file`: one issue per candidate
        (recorded as filed as it goes) and one pointer comment on the PR.
        ``filed`` is appended in place so a 410 midway leaves the caller
        knowing what landed; ``fresh`` gets the key of each row this call
        records, so the caller knows whether it did anything at all."""
        ops, repo, run_id = self.ops, self.repo, run.run_id
        assert run.pr_number is not None
        cfg = self.cfg.landing
        try:
            on_repo = self.filed_on_repo(ops, repo, cfg.followup_label, run_id)
            lookup_error = ""
        except (GithubOpsError, LookupUnavailable):
            on_repo = {}
            lookup_error = "existing follow-up issues could not be read"
        lookup = IssueLookup(ops, repo, run_id, self.store)
        held: list[tuple[Candidate, str]] = []
        label_ready = False
        url: str | None
        for cand in candidates:
            title = cand.followup.title.strip()
            if cand.key in already:
                filed.append((title, already[cand.key]))
                reused.append((title, already[cand.key]))
                continue
            existing = True
            regression_of_match = cand.followup.decision == "regression" and on_repo.get(
                cand.key, ""
            ).endswith(f"/issues/{cand.followup.existing_issue}")
            if cand.key in on_repo and not regression_of_match:
                url = on_repo[cand.key]
            else:
                try:
                    if lookup_error:
                        raise LookupUnavailable(lookup_error)
                    url = lookup.check(cand.followup)
                except (GithubOpsError, LookupUnavailable) as exc:
                    held.append((cand, str(exc)))
                    listed.append(title)
                    continue
                if url is None:
                    if not label_ready:
                        self.ensure_label(ops, repo, cfg.followup_label)
                        label_ready = True
                    ref = ops.issue_create(
                        repo,
                        title,
                        issue_body(
                            cand,
                            run_id=run_id,
                            repo=repo,
                            pr_number=run.pr_number,
                            pr_url=run.pr_url or "",
                            closes=self.cfg.github.deliver_closes,
                            trigger_label=self.trigger_label,
                            filed_by=attribution,
                        ),
                        labels=[cfg.followup_label],
                    )
                    url = ref.url
                    on_repo[cand.key] = url
                    existing = False
            filed.append((title, url))
            if existing:
                reused.append((title, url))
            self.store.record_phase(
                run_id,
                "followup",
                task_id=None,
                attempt=len(already) + len(filed),
                status="reused" if existing else "filed",
                output_json=json.dumps({"key": cand.key, "title": title, "url": url}),
                started_at=started,
            )
            already[cand.key] = url
            fresh.append(cand.key)
        if (filed or held) and "(comment)" not in already:
            # One pointer on the PR, so the human sees them without opening
            # the tracker.
            ops.pr_issue_comment(
                repo,
                run.pr_number,
                checklist_comment(candidates, run_id=run_id, filed=filed, held=held),
            )
            self._record_comment(run_id, len(already) + 1, len(filed), started)
            fresh.append("(comment)")

    def _record_comment(self, run_id: str, attempt: int, count: int, started: float) -> None:
        self.store.record_phase(
            run_id,
            "followup",
            task_id=None,
            attempt=attempt,
            status="listed",
            output_json=json.dumps({"key": "(comment)", "count": count}),
            started_at=started,
        )

    def recorded(self, run_id: str) -> dict[str, str]:
        """``{key: url}`` of the follow-ups this run already filed (or
        ``"(comment)"`` when the checklist comment was posted)."""
        out: dict[str, str] = {}
        for row in self.store.phase_attempts(run_id):
            if row.phase != "followup":
                continue
            try:
                data = json.loads(row.output_json or "{}")
            except ValueError:
                continue
            key = str(data.get("key") or "")
            if key:
                out[key] = str(data.get("url") or "")
        return out

    @staticmethod
    def filed_on_repo(ops: VcsOps, repo: str, label: str, run_id: str) -> dict[str, str]:
        """Follow-ups across runs, by key; this run wins for crash recovery.
        Read from the label's issue list, which unlike search is not
        eventually consistent."""
        out: dict[str, str] = {}
        data: list[Any] = []
        for page in range(1, 101):
            try:
                chunk = ops.issues_list(repo, labels=[label], state="all", page=page)
            except MalformedResponse as exc:
                raise LookupUnavailable("follow-up listing was malformed") from exc
            data.extend(chunk)
            if len(chunk) < 100:
                break
        else:
            raise LookupUnavailable("follow-up listing was incomplete")
        for issue in data:
            # The issues endpoint lists pull requests too (#631): a labelled
            # PR carrying an old marker in its body must not read as "this
            # follow-up was filed" and suppress the issue.
            if not isinstance(issue, dict):
                raise LookupUnavailable("follow-up listing contained a malformed issue")
            if "pull_request" in issue:
                continue
            found = marker_key(str(issue.get("body") or ""))
            if found:
                url = str(issue.get("html_url") or "")
                if not url:
                    raise LookupUnavailable("existing follow-up has no issue URL")
                if found[0] == run_id:
                    out[found[1]] = url
                else:
                    out.setdefault(found[1], url)
        return out

    @staticmethod
    def ensure_label(ops: VcsOps, repo: str, label: str) -> None:
        """Make sure the repository carries the follow-up label (best-effort:
        a refusal must not stop the filing, since GitHub accepts an issue
        whose label it cannot find). See
        :func:`sbxloop.vcs.github.labels.ensure_label`; ``sbxloop init-repo``
        creates this and the lifecycle labels up front (#630)."""
        ensure_label(ops, repo, LabelSpec(label, *FOLLOWUP_DESCRIPTOR))
