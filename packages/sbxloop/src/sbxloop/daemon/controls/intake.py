"""Admitting work from a surface that is not a poll (#1036).

Three forms, each through the rules its source already applies:

* an **existing issue** in a configured repository — the GitHub source
  reads it, labels it as a human would, and hands back the item a poll
  would build, so a later poll converges on the same row;
* an **inline workload** under an allowed profile — the shape the
  concierge's ``start_workload`` queues, keyed by an ``api:`` id the
  composite source routes to :class:`~sbxloop.daemon.sources.ApiSource`;
* a **registered tool recipe** with validated parameters — never a free
  command, never an agent: the recipe registry is the whole plan.

Nothing here talks HTTP. A route validates its body into one of the
request dataclasses and calls :meth:`ControlService.admit`; the prose
edge could do the same. Every refusal is a :class:`ControlError` with a
stable code; the source's own failures come back as
``source_unavailable`` (or ``unknown_target`` when it said the issue is
not there).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

from sbxloop.config import SINK_NAMES, Config
from sbxloop.daemon.controls.results import ControlError
from sbxloop.daemon.model import WorkItem
from sbxloop.engine.model import RunKind
from sbxloop.entrygraph import resolve_targets
from sbxloop.errors import ConfigError, DaemonError, GithubOpsError, SbxError, WorkerError
from sbxloop.ghids import api_item_id
from sbxloop.ids import new_run_id
from sbxloop.log import get_logger
from sbxloop.recipes import RECIPES

log = get_logger(__name__)

#: The parameters each registered recipe takes from a remote caller, by
#: name; anything else is refused before the registry is asked. A recipe
#: with no row here takes none.
RECIPE_PARAMETERS: dict[str, tuple[str, ...]] = {
    "entrygraph": ("repository", "url"),
}


@dataclass(frozen=True, slots=True)
class IssueAdmission:
    repository: str
    number: int
    run_kind: Literal["code", "workload"] = "code"


@dataclass(frozen=True, slots=True)
class WorkloadAdmission:
    ask: str
    profile: str | None = None
    sink: str | None = None
    #: The key the item id is minted from; a surface with a durable message
    #: id passes it so a retried turn queues one run. ``None`` mints one.
    key: str | None = None


@dataclass(frozen=True, slots=True)
class ToolAdmission:
    recipe: str
    parameters: dict[str, Any] = field(default_factory=dict)
    key: str | None = None


AdmitRequest = IssueAdmission | WorkloadAdmission | ToolAdmission


def _title(text: str, fallback: str) -> str:
    title = next((ln.strip() for ln in text.splitlines() if ln.strip()), fallback)
    return title if len(title) <= 120 else title[:119] + "…"


def target_key(request: AdmitRequest) -> str:
    """The item id the request would queue — what the operation is
    recorded against, so a replayed request names the same target."""
    if isinstance(request, IssueAdmission):
        # The source decides the spelling (qualified or not); the record
        # keys on what the caller named, which is unambiguous per repo.
        return f"{request.repository}#{request.number}"
    if isinstance(request, WorkloadAdmission):
        return api_item_id(request.key or new_run_id())
    # The recipe rides the id: a name that is not registered is refused
    # here, before an id is built from it — a free string is never a key.
    name = request.recipe.strip()
    if name not in RECIPES:
        known = ", ".join(sorted(RECIPES)) or "none"
        raise ControlError("unknown_target", f"unknown recipe {name!r} (registered: {known})")
    suffix = hashlib.sha256(_tool_target_text(request).encode()).hexdigest()[:12]
    return api_item_id(f"{request.key or new_run_id()}:{name}:{suffix}")


def _tool_target_text(request: ToolAdmission) -> str:
    return "|".join(f"{k}={request.parameters[k]}" for k in sorted(request.parameters))


def build_item(
    config: Config, request: AdmitRequest, *, item_id: str, requested_by: str | None
) -> WorkItem:
    """The item a workload or tool admission queues; ``ControlError`` for
    one the configuration does not allow. Issues are the source's."""
    if isinstance(request, WorkloadAdmission):
        return _workload_item(config, request, item_id=item_id, requested_by=requested_by)
    if isinstance(request, ToolAdmission):
        return _tool_item(config, request, item_id=item_id, requested_by=requested_by)
    raise ControlError("invalid_argument", "an issue admission is built by its source")


def _workload_item(
    config: Config, request: WorkloadAdmission, *, item_id: str, requested_by: str | None
) -> WorkItem:
    ask = request.ask.strip()
    if not ask:
        raise ControlError("invalid_argument", "an ask is required")
    wanted = (request.profile or "").strip() or None
    try:
        profile = config.workload_profile(wanted)
    except ConfigError as exc:
        raise ControlError("invalid_argument", str(exc)) from exc
    sink = (request.sink or "").strip() or None
    if sink is not None and sink not in SINK_NAMES:
        raise ControlError(
            "invalid_argument", f"unknown sink {sink!r}; one of {', '.join(SINK_NAMES)}"
        )
    allowed = ("chat", *(profile.sinks if profile is not None else ()))
    if sink is not None and sink not in allowed:
        who = f"profile `{profile.name}`" if profile is not None else "a run with no profile"
        raise ControlError(
            "not_eligible",
            f"sink `{sink}` is not one {who} allows ({', '.join(allowed)})",
        )
    if sink is not None and sink != "chat":
        ask = f"{ask}\n\nDeliver the result through the `{sink}` sink."
    return WorkItem(
        item_id=item_id,
        source_key=item_id.partition(":")[2],
        title=_title(ask, "workload"),
        body=ask,
        url="",
        kind="workload",
        profile=profile.name if profile is not None else None,
        requested_by=requested_by,
    )


def _tool_item(
    config: Config, request: ToolAdmission, *, item_id: str, requested_by: str | None
) -> WorkItem:
    name = request.recipe.strip()
    if name not in RECIPES:
        known = ", ".join(sorted(RECIPES)) or "none"
        raise ControlError("unknown_target", f"unknown recipe {name!r} (registered: {known})")
    allowed = RECIPE_PARAMETERS.get(name, ())
    unknown = sorted(set(request.parameters) - set(allowed))
    if unknown:
        raise ControlError(
            "invalid_argument",
            f"recipe {name!r} takes no parameter {', '.join(unknown)}"
            + (f" (it takes {', '.join(allowed)})" if allowed else ""),
            parameters=unknown,
        )
    for key, value in request.parameters.items():
        if not isinstance(value, str) or not value.strip():
            raise ControlError("invalid_argument", f"parameter {key!r} must be a non-empty string")
    # Today's one recipe. A second registers its own parameter row above
    # and its own branch here; the registry stays the source of truth for
    # what runs.
    if not config.entrygraph.enabled:
        raise ControlError(
            "not_eligible", "the entrygraph recipe is disabled ([entrygraph] enabled)"
        )
    repo = request.parameters.get("repository")
    url = request.parameters.get("url")
    if (repo is None) == (url is None):
        raise ControlError("invalid_argument", "name exactly one of `repository` or `url`")
    try:
        (target,) = resolve_targets(config, repo=repo, url=url)
    except (ConfigError, ValueError) as exc:
        raise ControlError("invalid_argument", str(exc)) from exc
    entry = config.github.find_repo(target)
    return WorkItem(
        item_id=item_id,
        source_key=item_id.partition(":")[2],
        title=_title(f"Run {name} against {target}", name),
        body=(
            f"Run {name} against {target}. Return a repository overview, entrypoints, "
            "and source-to-sink paths. Deliver the report and supporting result files "
            "through the chat sink."
        ),
        kind="tool",
        recipe=name,
        recipe_target=target,
        repo=entry.repo if entry is not None else None,
        requested_by=requested_by,
    )


def admit_issue(loop: Any, request: IssueAdmission) -> WorkItem:
    """The item the GitHub source builds for the issue, labelled for
    ``run_kind``; refusals as :class:`ControlError`."""
    config: Config = loop.config
    entry = config.github.find_repo(request.repository)
    if entry is None:
        raise ControlError("unknown_target", f"{request.repository} is not a configured repository")
    if not entry.enabled:
        raise ControlError("not_eligible", f"{entry.repo} is configured but not enabled")
    admit = getattr(loop.source, "admit", None)
    if not callable(admit):
        raise ControlError(
            "source_unavailable", "this daemon polls no repository, so it cannot admit an issue"
        )
    kind: RunKind = request.run_kind
    try:
        item: WorkItem = admit(entry.repo, str(request.number), kind)
    except KeyError as exc:
        raise ControlError("unknown_target", f"{entry.repo} is not polled by this daemon") from exc
    except ValueError as exc:
        raise ControlError("not_eligible", str(exc)) from exc
    except GithubOpsError as exc:
        if exc.http_status == 404:
            raise ControlError(
                "unknown_target", f"{entry.repo}#{request.number} was not found"
            ) from exc
        raise ControlError(
            "source_unavailable", f"the repository could not be read: {exc}"
        ) from exc
    except (WorkerError, SbxError) as exc:
        raise ControlError(
            "source_unavailable", f"the repository could not be read: {exc}"
        ) from exc
    return item


def upsert(loop: Any, item: WorkItem, *, by: str | None) -> tuple[WorkItem, bool]:
    """Queue ``item`` as discovery would; the row as the store holds it
    afterwards, and whether this call created it."""
    now = loop.clock()
    try:
        fresh = bool(loop.dstore.upsert_new(item, now))
    except DaemonError as exc:
        raise ControlError("not_eligible", f"queueing failed: {exc}") from exc
    stored = loop.dstore.get(item.item_id)
    if stored is None:
        # upsert_new may have qualified the id (a bare number held by
        # another repository's row); find the row by its identity.
        stored = next(
            (
                i
                for i in loop.dstore.items()
                if i.source_key == item.source_key and (i.repo or "") == (item.repo or "")
            ),
            item,
        )
    log.info(
        "intake.admitted",
        item=stored.item_id,
        kind=stored.kind,
        profile=stored.profile,
        recipe=stored.recipe,
        by=by,
        fresh=fresh,
        title=stored.title[:80],
    )
    return stored, fresh
