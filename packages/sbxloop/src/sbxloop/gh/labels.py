"""The repository labels sbxloop relies on, and the one place that creates
them (#630).

The daemon's six lifecycle labels (`[daemon] *_label`, per-repo overrides
on `[[github.repos]]`) and the follow-up label (`[landing] followup_label`)
are ordinary repository labels: GitHub attaches an unknown label name to an
issue without creating it, so a repository that was never set up shows the
loop's states as bare text. ``sbxloop init-repo`` creates them, idempotently
and with colors and descriptions, through :func:`ensure_label`; the engine
uses the same function for the follow-up label before filing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sbxloop.config import LABEL_KINDS, LabelSet
from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import GithubOps, raw_pages
from sbxloop.log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class LabelSpec:
    """One repository label as ``init-repo`` creates it."""

    name: str
    color: str  # six hex digits, no ``#`` — what the REST API takes
    description: str


# Colors and descriptions by lifecycle kind. The queue-in (trigger) and
# work-done (completed) ends are green/purple; the two "a human needs to
# look" states are red and amber; in-progress is yellow; gated is blue.
LIFECYCLE_DESCRIPTORS: dict[str, tuple[str, str]] = {
    "trigger": ("0e8a16", "queued for sbxloop: the daemon claims it and runs it to a merged PR"),
    "in_progress": ("fbca04", "sbxloop is working on this right now"),
    "failed": ("d73a4a", "sbxloop gave up on this; left open for a human"),
    "completed": ("6f42c1", "sbxloop landed this: its pull request merged"),
    "blocked": ("e99695", "sbxloop could not land this; a human needs to look"),
    "gated": ("1d76db", "sbxloop is ready to merge this; awaiting one approval"),
}
FOLLOWUP_DESCRIPTOR = ("c5def5", "filed by sbxloop after a merge")

EnsureResult = Literal["created", "present", "failed"]


def lifecycle_specs(labels: LabelSet, followup: str | None = None) -> list[LabelSpec]:
    """The labels ``init-repo`` creates for one repository: the six
    lifecycle labels and, when given, the follow-up label."""
    specs = [LabelSpec(getattr(labels, kind), *LIFECYCLE_DESCRIPTORS[kind]) for kind in LABEL_KINDS]
    if followup:
        specs.append(LabelSpec(followup, *FOLLOWUP_DESCRIPTOR))
    return specs


def ensure_label(ops: GithubOps, repo: str, spec: LabelSpec) -> EnsureResult:
    """Make sure ``repo`` carries ``spec``; say whether it was created,
    already there, or could not be made.

    A label that already exists is an expected condition, not an error: it
    is looked up first and left alone (its color and description are the
    operator's to change), so the run never records a failed creation call.
    The lookup goes through ``label_lookup``, which answers a 404 as data
    rather than as a failed worker job — the same treatment ``ref_lookup``
    gives an absent branch (#518), so a repository *without* the label does
    not pay a red panel for asking. Only a genuinely missing label is
    POSTed, and the 422 catch still covers the race between the two calls.
    A refusal never raises — the caller decides what "failed" costs (the
    engine files its follow-up anyway; GitHub accepts an issue whose label
    it cannot find).
    """
    try:
        existing = ops.label_lookup(repo, spec.name)
    except GithubOpsError as exc:
        # Not a 404 — no repo scope, or GitHub is unwell. One warning,
        # and no doomed POST behind it.
        log.warning("github.label_failed", repo=repo, label=spec.name, error=str(exc))
        return "failed"
    if existing:
        log.debug("github.label_present", repo=repo, label=spec.name)
        return "present"
    try:
        ops.raw(
            "POST",
            f"/repos/{repo}/labels",
            {"name": spec.name, "color": spec.color, "description": spec.description},
        )
    except GithubOpsError as exc:
        text = str(exc)
        exists = "already_exists" in text or "already exists" in text
        if exc.http_status == 422 or exists:
            log.debug("github.label_present", repo=repo, label=spec.name)
            return "present"
        log.warning("github.label_failed", repo=repo, label=spec.name, error=text)
        return "failed"
    log.info("github.label_created", repo=repo, label=spec.name)
    return "created"


def missing_labels(ops: GithubOps, repo: str, specs: list[LabelSpec]) -> list[str]:
    """The names in ``specs`` that ``repo`` does not carry (the doctor's
    drift row). Names compare case-insensitively, as GitHub does."""
    present = {
        str(label.get("name") or "").casefold()
        for label in raw_pages(ops, f"/repos/{repo}/labels")
        if isinstance(label, dict)
    }
    return [spec.name for spec in specs if spec.name.casefold() not in present]
