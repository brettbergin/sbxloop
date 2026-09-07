"""Read an explicitly ordered GitHub epic without admitting or modifying work.

This adapter intentionally supports a small, documented Markdown vocabulary.
An issue reference elsewhere in a body is context, never campaign membership.
Native sub-issues, dependency APIs and arbitrary prose interpretation belong in
separate adapters; an unresolved plan here is an error, not an inferred order.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import SplitResult, urlsplit

from sbxloop.daemon.campaigns import CampaignPlan, CampaignStepPlan, WorkItemSnapshot
from sbxloop.engine.model import RunKind
from sbxloop.errors import SbxloopError
from sbxloop.gh.ops import GithubOps
from sbxloop.ghids import issue_item_id

# A bounded intake protects against an accidentally enormous list. It is a
# parser limit, not a dispatch budget; larger epics need explicit smaller plans.
MAX_EPIC_STEPS = 100

_MEMBERSHIP_HEADINGS = {"backlog", "sub-issues", "sub issues", "tasks", "child issues"}
_ORDER_HEADINGS = {"build order", "execution order", "implementation order"}
_DEPENDENCY_HEADINGS = {"depends on", "dependencies", "prerequisite", "prerequisites"}
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_LIST_RE = re.compile(r"^\s*(?:(\d+)[.)]|[-+*])\s+(?:\[[ xX]\]\s*)?(.*)$")
_REF_RE = re.compile(
    r"https?://[^\s<>()]+|(?<![\w/])([\w.-]+/[\w.-]+)#(\d+)\b|(?<![\w/&#])#(\d+)\b"
)
_REF_RANGE_RE = re.compile(r"#\d+\s*(?:[-\u2013\u2014]|\bto\b)\s*#?\d+\b", re.IGNORECASE)
_INLINE_DEPENDENCY_RE = re.compile(
    r"^(?:depends\s+on|blocked\s+by|prerequisites?|dependencies)\s*(?::|-)?\s*(.*)$",
    re.IGNORECASE,
)


class EpicPlanError(ValueError):
    """The available evidence does not describe one executable procedure."""


@dataclass(frozen=True)
class EpicStep:
    number: int
    title: str
    body: str
    url: str
    state: Literal["open", "closed"]
    labels: tuple[str, ...]
    kind: RunKind
    depends_on: tuple[int, ...] = ()


@dataclass(frozen=True)
class EpicPlan:
    repo: str
    epic_number: int
    title: str
    url: str
    body: str
    order_source: Literal["explicit", "build_order", "order_labels"]
    steps: tuple[EpicStep, ...]


def epic_campaign_id(repo: str, number: int) -> str:
    """One durable campaign identity for an epic, independent of its edits."""
    return f"epic:{repo.casefold()}:{number}"


def campaign_plan_from_epic(
    epic: EpicPlan,
    *,
    requested_by: str,
    requester_id: str | None = None,
    expected_base: str | None = None,
) -> CampaignPlan:
    """Pin the whole brief and intended sequence after each original child ask.

    Nothing is silently clipped: a later run must retain the shared goals and
    acceptance criteria even after the source epic or this chat has changed.
    The campaign's ordinary immutable item snapshot stores that context.
    """
    closed = [f"#{step.number}" for step in epic.steps if step.state == "closed"]
    if closed:
        raise EpicPlanError(
            "closed campaign members need delivery reconciliation before admission: "
            + ", ".join(closed)
        )
    order = "\n".join(
        f"{index}. #{step.number} — {' '.join(step.title.split())}"
        for index, step in enumerate(epic.steps, 1)
    )
    steps: list[CampaignStepPlan] = []
    for step in epic.steps:
        context = (
            f"## Campaign context (pinned at admission)\n\n"
            f"This run handles issue #{step.number} only. The other "
            "steps execute separately; use the epic brief for shared goals and acceptance "
            f"criteria.\n\nEpic: {epic.title}\nSource: {epic.url}\n\n"
            f"### Admitted order\n\n{order}\n\n### Epic brief\n\n{epic.body}"
        )
        steps.append(
            CampaignStepPlan(
                item=WorkItemSnapshot(
                    item_id=issue_item_id(step.number, repo=epic.repo),
                    source_key=str(step.number),
                    title=step.title,
                    body=f"{step.body}\n\n{context}",
                    url=step.url,
                    repo=epic.repo,
                    kind=step.kind,
                    requested_by=requester_id,
                ),
                expected_base=expected_base if step.kind == "code" else None,
                prerequisites=tuple(
                    issue_item_id(number, repo=epic.repo) for number in step.depends_on
                ),
            )
        )
    return CampaignPlan(
        campaign_id=epic_campaign_id(epic.repo, epic.epic_number),
        title=epic.title,
        requested_by=requested_by,
        source_url=epic.url,
        steps=tuple(steps),
    )


def _visible_lines(body: str) -> list[str]:
    lines: list[str] = []
    fence = ""
    for line in re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL).splitlines():
        stripped = line.lstrip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            mark = marker[1]
            if not fence:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence):
                fence = ""
            continue
        if not fence:
            lines.append(line)
    return lines


def _sections(lines: Sequence[str], headings: set[str]) -> list[list[str]]:
    sections: list[list[str]] = []
    active: list[str] | None = None
    level = 0
    for line in lines:
        heading = _HEADING_RE.match(line)
        if heading:
            if len(heading[1]) <= level:
                active = None
            name = heading[2].strip(" *_").casefold()
            if name in headings:
                active = []
                sections.append(active)
                level = len(heading[1])
        elif active is not None:
            active.append(line)
    return sections


def _issue_url(url: str) -> SplitResult:
    try:
        parsed = urlsplit(url)
        valid_port = parsed.port  # rejects malformed port identities
    except ValueError as exc:
        raise EpicPlanError("unreadable issue URL identity") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (valid_port is not None and valid_port < 1)
    ):
        raise EpicPlanError("unreadable issue URL identity")
    return parsed


def _references(text: str, repo: str, host: str) -> tuple[int, ...]:
    if _REF_RANGE_RE.search(text):
        raise EpicPlanError("issue reference range; name each issue explicitly")
    found: list[int] = []
    for match in _REF_RE.finditer(text):
        if match[0].startswith(("http://", "https://")):
            parsed = _issue_url(match[0].rstrip(".,;:"))
            path = parsed.path.rstrip("/")
            target = re.fullmatch(r"/([^/]+/[^/]+)/(issues|pull)/(\d+)", path)
            if target is None:
                continue
            if parsed.netloc.casefold() != host:
                raise EpicPlanError("campaign members and prerequisites must use the same host")
            if target[2] == "pull":
                raise EpicPlanError("a campaign member or prerequisite cannot be a pull request")
            if target[1].casefold() != repo.casefold():
                raise EpicPlanError(
                    "campaign members and prerequisites must be in the same repository"
                )
            number = int(target[3])
        elif match[1] is not None:
            if match[1].casefold() != repo.casefold():
                raise EpicPlanError(
                    "campaign members and prerequisites must be in the same repository"
                )
            number = int(match[2])
        else:
            number = int(match[3])
        if number < 1:
            raise EpicPlanError("issue numbers must be positive integers")
        if number not in found:
            found.append(number)
    return tuple(found)


def _listed_members(
    section: Sequence[str], repo: str, host: str, *, ordered: bool
) -> tuple[int, ...]:
    members: list[int] = []
    for line in section:
        entry = _LIST_RE.match(line)
        if entry is None:
            # Introductory prose does not declare a member. References outside
            # a list cannot secretly broaden the procedure.
            continue
        if ordered and entry[1] is None:
            raise EpicPlanError("Build order needs a numbered list")
        if ordered and int(entry[1]) != len(members) + 1:
            raise EpicPlanError("Build order must use contiguous positions starting at 1")
        refs = _references(entry[2], repo, host)
        if len(refs) != 1:
            raise EpicPlanError("each campaign list entry must name exactly one issue")
        if refs[0] in members:
            raise EpicPlanError(f"duplicate campaign member #{refs[0]}")
        members.append(refs[0])
    if not members:
        raise EpicPlanError("the epic membership section has no issue list")
    return tuple(members)


def _read_issue(
    ops: GithubOps,
    repo: str,
    number: int,
    trigger_label: str,
    workload_label: str,
    *,
    host: str | None = None,
) -> EpicStep:
    identity = f"{repo}#{number}"
    try:
        row = ops.raw("GET", f"/repos/{repo}/issues/{number}")
    except SbxloopError as exc:
        raise EpicPlanError(f"cannot read issue {identity}: {exc}") from exc
    if not isinstance(row, dict) or type(row.get("number")) is not int or row["number"] != number:
        raise EpicPlanError(f"issue {identity} returned an unreadable or different identity")
    if "pull_request" in row:
        raise EpicPlanError(f"{identity} is a pull request, not an issue")
    title, body, state, url = (
        row.get("title"),
        row.get("body"),
        row.get("state"),
        row.get("html_url"),
    )
    if not isinstance(title, str) or not title.strip():
        raise EpicPlanError(f"issue {identity} has no readable title")
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise EpicPlanError(f"issue {identity} has an unreadable body")
    if state not in ("open", "closed"):
        raise EpicPlanError(f"issue {identity} has an unknown state")
    if not isinstance(url, str):
        raise EpicPlanError(f"issue {identity} returned an unreadable URL identity")
    parsed = _issue_url(url)
    if parsed.path.casefold().rstrip("/") != (f"/{repo}/issues/{number}".casefold()):
        raise EpicPlanError(f"issue {identity} returned an unreadable or different URL identity")
    if host is not None and parsed.netloc.casefold() != host:
        raise EpicPlanError(f"issue {identity} must use the same host as the epic")
    labels = row.get("labels")
    if not isinstance(labels, list) or any(
        not isinstance(label, dict) or not isinstance(label.get("name"), str) for label in labels
    ):
        raise EpicPlanError(f"issue {identity} has unreadable labels")
    names = tuple(label["name"] for label in labels)
    if trigger_label in names and workload_label in names:
        raise EpicPlanError(f"issue {identity} carries both code and workload labels")
    return EpicStep(
        number,
        title,
        body,
        url,
        "open" if state == "open" else "closed",
        names,
        "workload" if workload_label in names else "code",
    )


def _dependencies(body: str, repo: str, number: int, host: str) -> tuple[int, ...]:
    lines = _visible_lines(body)
    sections = _sections(lines, _DEPENDENCY_HEADINGS)
    if any(not any(line.strip() for line in section) for section in sections):
        raise EpicPlanError(f"issue #{number} has an empty dependency declaration")
    declarations = [line for section in sections for line in section]
    section_lines = set(declarations)
    for line in lines:
        clean = line.strip().replace("**", "").replace("__", "")
        entry = _LIST_RE.match(clean)
        clean = entry[2] if entry else clean
        inline = _INLINE_DEPENDENCY_RE.match(clean)
        if inline:
            if not inline[1].strip():
                raise EpicPlanError(f"issue #{number} has an empty dependency declaration")
            declarations.append(inline[1])
        elif (
            line not in section_lines
            and re.match(
                r"^(?:this(?:\s+(?:task|issue))?|it)\s+(?:depends on|is blocked by)\b",
                clean,
                re.IGNORECASE,
            )
            and _REF_RE.search(clean)
        ):
            raise EpicPlanError(
                f"issue #{number} has an unsupported dependency declaration; "
                "use a Depends on section or 'Depends on: <issues>'"
            )
    needed: list[int] = []
    explicit_none = False
    for line in declarations:
        clean = line.strip().replace("**", "").replace("__", "")
        if not clean:
            continue
        entry = _LIST_RE.match(clean)
        clean = entry[2] if entry else clean
        inline = _INLINE_DEPENDENCY_RE.match(clean)
        clean = inline[1] if inline else clean
        if re.fullmatch(r"(?:none|no dependencies)[.!]?", clean, re.IGNORECASE):
            explicit_none = True
            continue
        refs = _references(clean, repo, host)
        if not refs:
            raise EpicPlanError(
                f"issue #{number} has an unreadable dependency declaration: {clean}"
            )
        needed.extend(ref for ref in refs if ref not in needed)
    if explicit_none and needed:
        raise EpicPlanError(f"issue #{number} declares both dependencies and no dependencies")
    return tuple(needed)


def _label_order(steps: Sequence[EpicStep]) -> tuple[int, ...]:
    positions: dict[int, int] = {}
    for step in steps:
        labels = [label for label in step.labels if re.match(r"order\s*:", label, re.IGNORECASE)]
        if len(labels) != 1:
            raise EpicPlanError(
                f"issue #{step.number} needs exactly one order label, or supply an explicit order"
            )
        match = re.fullmatch(r"order\s*:\s*(\d+)\s*", labels[0], re.IGNORECASE)
        if match is None:
            raise EpicPlanError(f"issue #{step.number} has an unreadable order label")
        position = int(match[1])
        if position < 1:
            raise EpicPlanError(f"issue #{step.number} needs a positive order label")
        if position in positions:
            raise EpicPlanError(f"duplicate order label position {position}")
        positions[position] = step.number
    return tuple(positions[position] for position in sorted(positions))


def _validate_dependencies(steps: Sequence[EpicStep]) -> None:
    positions = {step.number: position for position, step in enumerate(steps)}
    graph = {step.number: step.depends_on for step in steps}
    for step in steps:
        if step.number in step.depends_on:
            raise EpicPlanError(f"issue #{step.number} depends on itself")
        for dependency in step.depends_on:
            if dependency not in graph:
                raise EpicPlanError(f"issue #{step.number} has missing prerequisite #{dependency}")
    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(number: int) -> None:
        if number in visiting:
            raise EpicPlanError(f"dependency cycle includes issue #{number}")
        if number in visited:
            return
        visiting.add(number)
        for dependency in graph[number]:
            visit(dependency)
        visiting.remove(number)
        visited.add(number)

    for step in steps:
        visit(step.number)
    for step in steps:
        for dependency in step.depends_on:
            if positions[dependency] > positions[step.number]:
                raise EpicPlanError(
                    f"prerequisite #{dependency} must appear before issue #{step.number}"
                )


def resolve_epic(
    ops: GithubOps,
    repo: str,
    number: int,
    *,
    ordered_issue_numbers: Sequence[int] | None = None,
    trigger_label: str = "sbxloop:run",
    workload_label: str = "sbxloop:workload",
) -> EpicPlan:
    """Resolve at most 100 explicitly named children into a frozen plan.

    Membership comes from a Backlog, Sub-issues, Tasks or Child issues list,
    or a numbered Build order (also Execution order / Implementation order).
    Caller order overrides the declared order but must include the same members.
    A complete set of unique positive ``order: N`` labels is a migration fallback
    only when neither caller nor epic supplies an order. No issue-number sort,
    decomposition, label writes, admission, or completion inference happens here.

    Closed issues are preserved as closed, not declared successful: the campaign
    layer must reconcile them against delivery evidence before admitting work.
    """
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or type(number) is not int or number < 1:
        raise EpicPlanError("an epic needs a repository and a positive integer issue number")
    epic = _read_issue(ops, repo, number, trigger_label, workload_label)
    host = _issue_url(epic.url).netloc.casefold()
    if _dependencies(epic.body, repo, number, host):
        raise EpicPlanError("the epic has prerequisites that need resolution before admission")
    lines = _visible_lines(epic.body)
    member_lists = [
        _listed_members(section, repo, host, ordered=False)
        for section in _sections(lines, _MEMBERSHIP_HEADINGS)
    ]
    orders = [
        _listed_members(section, repo, host, ordered=True)
        for section in _sections(lines, _ORDER_HEADINGS)
    ]
    if not member_lists and not orders:
        raise EpicPlanError(
            "the epic needs explicit issue membership in a Tasks or Build order section"
        )
    members = (member_lists or orders)[0]
    if len(members) > MAX_EPIC_STEPS:
        raise EpicPlanError(f"an epic may contain at most {MAX_EPIC_STEPS} steps")
    if number in members:
        raise EpicPlanError("an epic cannot include itself")
    if any(set(group) != set(members) for group in [*member_lists, *orders]):
        raise EpicPlanError("the epic's declared membership lists disagree")
    if any(order != orders[0] for order in orders):
        raise EpicPlanError("the epic declares conflicting build orders")
    explicit: tuple[int, ...] | None = None
    if ordered_issue_numbers is not None:
        explicit = tuple(ordered_issue_numbers)
        if not explicit or any(type(item) is not int or item < 1 for item in explicit):
            raise EpicPlanError("explicit order needs positive integer issue numbers")
        if len(set(explicit)) != len(explicit):
            raise EpicPlanError("explicit order contains a duplicate issue")
        if set(explicit) != set(members):
            raise EpicPlanError(
                "explicit order must contain exactly the epic's declared membership"
            )
    snapshots = [
        _read_issue(ops, repo, child, trigger_label, workload_label, host=host) for child in members
    ]
    order = explicit if explicit is not None else orders[0] if orders else _label_order(snapshots)
    by_number = {step.number: step for step in snapshots}
    steps = tuple(
        EpicStep(
            step.number,
            step.title,
            step.body,
            step.url,
            step.state,
            step.labels,
            step.kind,
            _dependencies(step.body, repo, step.number, host),
        )
        for child in order
        for step in [by_number[child]]
    )
    _validate_dependencies(steps)
    return EpicPlan(
        repo,
        number,
        epic.title,
        epic.url,
        epic.body,
        "explicit" if explicit is not None else "build_order" if orders else "order_labels",
        steps,
    )
