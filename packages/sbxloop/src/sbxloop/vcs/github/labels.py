"""The repository labels sbxloop relies on, and the one place that creates
them (#630).

The daemon's seven lifecycle labels (`[daemon] *_label`, per-repo overrides
on `[[vcs.repos]]`) and the follow-up label (`[landing] followup_label`)
are ordinary repository labels: GitHub attaches an unknown label name to an
issue without creating it, so a repository that was never set up shows the
loop's states as bare text. ``sbxloop init-repo`` creates them, idempotently
and with colors and descriptions, through :func:`ensure_label`; the engine
uses the same function for the follow-up label before filing.

:func:`audit_labels` and :func:`sync_labels` are the same work for a
caller that has a whole repository in front of it rather than one label:
one listing says which of the set the repository is missing, and the sync
creates exactly those. The daemon reads the first on a cadence so a
registered repository can say whether it is set up, and runs the second
when an operator asks it to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sbxloop.config import LABEL_KINDS, LabelSet
from sbxloop.errors import GithubOpsError
from sbxloop.log import get_logger
from sbxloop.vcs.protocol import IssueOps

log = get_logger(__name__)


@dataclass(frozen=True)
class LabelSpec:
    """One repository label as ``init-repo`` creates it.

    ``kind`` is what the label is *for* — the lifecycle state it marks,
    or the standing role of a label that marks none — so a surface can
    explain a label it did not name itself. It never travels to the
    forge; the name, the color and the description do.
    """

    name: str
    color: str  # six hex digits, no ``#`` — what the REST API takes
    description: str
    kind: str = "followup"


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
    "workload": ("0052cc", "queued for sbxloop as a workload: the result comes back as a comment"),
}
FOLLOWUP_DESCRIPTOR = ("c5def5", "filed by sbxloop after a merge")

EnsureResult = Literal["created", "present", "failed"]


def lifecycle_specs(labels: LabelSet, followup: str | None = None) -> list[LabelSpec]:
    """The labels ``init-repo`` creates for one repository: the seven
    lifecycle labels and, when given, the follow-up label."""
    specs = [
        LabelSpec(getattr(labels, kind), *LIFECYCLE_DESCRIPTORS[kind], kind=kind)
        for kind in LABEL_KINDS
    ]
    if followup:
        specs.append(LabelSpec(followup, *FOLLOWUP_DESCRIPTOR))
    return specs


def ensure_label(ops: IssueOps, repo: str, spec: LabelSpec) -> EnsureResult:
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
        ops.label_create(repo, name=spec.name, color=spec.color, description=spec.description)
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


def missing_labels(ops: IssueOps, repo: str, specs: list[LabelSpec]) -> list[str]:
    """The names in ``specs`` that ``repo`` does not carry (the doctor's
    drift row). Names compare case-insensitively, as GitHub does."""
    present = {
        str(label.get("name") or "").casefold()
        for label in ops.labels_list(repo)
        if isinstance(label, dict)
    }
    return [spec.name for spec in specs if spec.name.casefold() not in present]


@dataclass(frozen=True)
class LabelReport:
    """What one repository's sbxloop labels look like after a look, and —
    for a sync — what the look changed.

    ``missing`` is what the repository still does not carry when the call
    returns: after :func:`audit_labels` the drift, after :func:`sync_labels`
    the labels it could not create. A repository whose ``missing`` is empty
    is set up: every label the loop applies exists, with its color and its
    description.
    """

    expected: tuple[str, ...]
    missing: tuple[str, ...]
    created: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    @property
    def compliant(self) -> bool:
        return not self.missing


def audit_labels(ops: IssueOps, repo: str, specs: list[LabelSpec]) -> LabelReport:
    """Which of ``specs`` ``repo`` carries, in one listing call.

    Read-only, and it fails closed: a listing the forge refused raises
    :class:`~sbxloop.errors.GithubOpsError` rather than reading as a
    repository with no labels at all — "could not tell" is not "nothing
    is there", and a caller that recorded the second would show a
    repository as needing every label it already has.
    """
    return LabelReport(
        expected=tuple(spec.name for spec in specs),
        missing=tuple(missing_labels(ops, repo, specs)),
    )


def sync_labels(ops: IssueOps, repo: str, specs: list[LabelSpec]) -> LabelReport:
    """Give ``repo`` every label in ``specs``; say what was already there,
    what this call created, and what the forge would not create.

    One listing, then one creation per missing label — a repository that
    is already set up costs a single call and writes nothing. Idempotent:
    an existing label keeps its color and description (the operator's to
    change), as ``sbxloop init-repo`` leaves them.
    """
    audit = audit_labels(ops, repo, specs)
    by_name = {spec.name: spec for spec in specs}
    created: list[str] = []
    failed: list[str] = []
    for name in audit.missing:
        result = ensure_label(ops, repo, by_name[name])
        if result == "created":
            created.append(name)
        elif result == "failed":
            failed.append(name)
    return LabelReport(
        expected=audit.expected,
        missing=tuple(failed),
        created=tuple(created),
        failed=tuple(failed),
    )
