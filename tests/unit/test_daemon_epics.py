"""An epic admits only its explicit, validated issue procedure."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from sbxloop.daemon.epics import MAX_EPIC_STEPS, EpicPlanError, resolve_epic
from sbxloop.errors import GithubOpsError
from tests.fakes.fake_github import FakeGithub

REPO = "customer/project"


def issue(number: int, body: str = "", **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "number": number,
        "title": f"Step {number}",
        "body": body,
        "state": "open",
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "labels": [],
    }
    row.update(overrides)
    return row


def github(body: str, *children: dict[str, Any]) -> FakeGithub:
    ops = FakeGithub(repo=REPO)
    ops.issue_payloads[(REPO, 40)] = issue(40, body, title="A customer outcome")
    for row in children:
        ops.issue_payloads[(REPO, row["number"])] = row
    return ops


def test_numbered_build_order_is_membership_and_preserves_snapshots() -> None:
    ops = github(
        "Earlier discussion: #99\n\n## Build order\n\n1. #8 — Prepare\n2. #3 — Deliver\n"
        "\n## Acceptance criteria\n\n- [ ] A human checks #90\n",
        issue(8, "Prepare the work"),
        issue(3, "## Depends on\n\n- #8\n"),
    )
    plan = resolve_epic(ops, REPO, 40)
    assert [step.number for step in plan.steps] == [8, 3]
    assert plan.repo == REPO and plan.epic_number == 40
    assert plan.title == "A customer outcome" and plan.order_source == "build_order"
    assert plan.steps[1].depends_on == (8,)
    assert plan.steps[0].body == "Prepare the work" and plan.steps[0].kind == "code"
    assert all(method == "GET" for method, _, _ in ops.raw_calls)
    assert len(ops.raw_calls) == 3
    ops.issue_payloads[(REPO, 8)]["body"] = "A later edit"
    assert plan.steps[0].body == "Prepare the work"
    with pytest.raises(FrozenInstanceError):
        plan.title = "mutable"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        plan.steps[0].title = "mutable"  # type: ignore[misc]


@pytest.mark.parametrize("heading", ["Tasks", "Sub-issues", "Backlog", "Child issues"])
def test_explicit_order_uses_declared_members_not_issue_numbers(heading: str) -> None:
    ops = github(f"## {heading}\n\n- [ ] #3\n- [x] #8", issue(3), issue(8))
    plan = resolve_epic(ops, REPO, 40, ordered_issue_numbers=[8, 3])
    assert [step.number for step in plan.steps] == [8, 3]
    assert plan.order_source == "explicit"


def test_markdown_links_and_repo_qualified_references_keep_one_identity() -> None:
    ops = github(
        f"## Build order\n\n1. [#8](https://github.com/{REPO}/issues/8) Prepare\n"
        f"2. {REPO}#3 Deliver",
        issue(8),
        issue(3, f"**Depends on:** [#8](https://github.com/{REPO}/issues/8)"),
    )
    plan = resolve_epic(ops, REPO, 40)
    assert [step.number for step in plan.steps] == [8, 3]
    assert plan.steps[1].depends_on == (8,)


@pytest.mark.parametrize("location", ["membership", "order", "dependency"])
def test_foreign_host_issue_references_refuse(location: str) -> None:
    foreign = f"https://other-host.invalid/{REPO}/issues/8"
    body = "## Build order\n1. #8\n2. #3"
    child = ""
    if location == "membership":
        body = f"## Tasks\n- [#8]({foreign})\n- #3\n{body}"
    elif location == "order":
        body = f"## Build order\n1. {foreign}\n2. #3"
    else:
        child = f"Depends on: [#8]({foreign})"
    ops = github(body, issue(8), issue(3, child))
    with pytest.raises(EpicPlanError, match="same host"):
        resolve_epic(ops, REPO, 40)


def test_enterprise_host_is_taken_from_the_verified_epic_url() -> None:
    origin = "https://code.customer.example"
    ops = github(
        f"## Build order\n1. {origin}/{REPO}/issues/8\n2. #3",
        issue(8, html_url=f"{origin}/{REPO}/issues/8"),
        issue(
            3,
            f"Depends on: {origin}/{REPO}/issues/8",
            html_url=f"{origin}/{REPO}/issues/3",
        ),
    )
    ops.issue_payloads[(REPO, 40)]["html_url"] = f"{origin}/{REPO}/issues/40"
    plan = resolve_epic(ops, REPO, 40)
    assert [step.number for step in plan.steps] == [8, 3]
    assert plan.steps[1].depends_on == (8,)


@pytest.mark.parametrize("reference", ["#3-8", "#3\u20138", "#3\u20148", "#3 to 8", "#3 to #8"])
@pytest.mark.parametrize("location", ["member", "dependency"])
def test_issue_ranges_must_be_spelled_out(reference: str, location: str) -> None:
    body = (
        f"## Build order\n1. {reference}"
        if location == "member"
        else ("## Build order\n1. #3\n2. #8")
    )
    ops = github(body, issue(3), issue(8, f"Depends on: {reference}"))
    with pytest.raises(EpicPlanError, match="range"):
        resolve_epic(ops, REPO, 40)


def test_code_examples_and_comments_are_not_members_or_dependencies() -> None:
    ops = github(
        "```markdown\n## Build order\n1. #99\n```\n"
        "<!--\n## Tasks\n- #90\n-->\n## Build order\n1. #8\n2. #3\n",
        issue(8, "```\nDepends on: #3\n```\n<!-- Depends on: #99 -->"),
        issue(3),
    )
    assert [step.number for step in resolve_epic(ops, REPO, 40).steps] == [8, 3]


def test_order_labels_are_a_complete_membership_only_migration_input() -> None:
    ops = github(
        "## Tasks\n- #3\n- #8",
        issue(3, labels=[{"name": "order: 6"}]),
        issue(8, labels=[{"name": "order: 3"}]),
    )
    plan = resolve_epic(ops, REPO, 40)
    assert [step.number for step in plan.steps] == [8, 3]
    assert plan.order_source == "order_labels"


def test_explicit_caller_order_overrides_the_declared_build_order() -> None:
    ops = github("## Build order\n1. #3\n2. #8", issue(3), issue(8))
    plan = resolve_epic(ops, REPO, 40, ordered_issue_numbers=[8, 3])
    assert [step.number for step in plan.steps] == [8, 3]
    assert plan.order_source == "explicit"


def test_existing_epic_checklist_and_label_annotated_build_order() -> None:
    ops = github(
        "## Backlog\n- [x] #2 Prepare\n- [x] #4 Model\n- [ ] #5 Assemble\n"
        "## Build order\nFollow this sequence:\n"
        "1. `order: 1` - #2 Prepare\n2. `order: 2` - #4 Model\n"
        "3. `order: 3` - #5 Assemble\n"
        'This order respects every issue\'s own "Depends on" line '
        "(e.g. #5 needs #2 and #4 done first; #11 needs #6-#10 done first).",
        issue(2),
        issue(4),
        issue(5, "## Depends on\n- #2 (foundation), #4 (data model, for output)"),
    )
    plan = resolve_epic(ops, REPO, 40)
    assert [step.number for step in plan.steps] == [2, 4, 5]
    assert plan.steps[2].depends_on == (2, 4)
    assert len(ops.raw_calls) == 4


@pytest.mark.parametrize(
    "first,second,reason",
    [
        ([], [], "order"),
        (["order: 1"], [], "order"),
        (["order: 1"], ["order: 1"], "duplicate"),
        (["order: 1", "order: 2"], ["order: 3"], "one order label"),
        (["order: soon"], ["order: 2"], "order label"),
        (["order: 0"], ["order: 2"], "positive"),
    ],
)
def test_incomplete_or_ambiguous_order_labels_refuse(
    first: list[str], second: list[str], reason: str
) -> None:
    ops = github(
        "## Tasks\n- #3\n- #8",
        issue(3, labels=[{"name": name} for name in first]),
        issue(8, labels=[{"name": name} for name in second]),
    )
    with pytest.raises(EpicPlanError, match=reason):
        resolve_epic(ops, REPO, 40)


@pytest.mark.parametrize(
    "body,order,reason",
    [
        ("This concerns #3 and #8", [3, 8], "membership"),
        ("## Tasks\n- #3\n- #8", [3], "membership"),
        ("## Tasks\n- #3", [3, 8], "membership"),
        ("## Tasks\n- #3\n- #8", [3, 3], "duplicate"),
        ("## Build order\n1. #3\n2. #3", None, "duplicate"),
        ("## Build order\n1. #40", None, "itself"),
        ("## Build order\n- #3\n- #8", None, "numbered"),
        ("## Build order\n1. #3\n3. #8", None, "contiguous"),
        ("## Build order\n1. #3 or #8", None, "one issue"),
        ("## Tasks\n- #3\n- #8\n## Build order\n1. #3", None, "membership"),
        ("## Tasks\n- #3\n## Sub-issues\n- #8", [3, 8], "membership"),
        ("## Build order\n1. other/repo#3", None, "same repository"),
        ("## Build order\n1. https://github.com/other/repo/issues/3", None, "same repository"),
        (f"## Build order\n1. https://github.com/{REPO}/pull/3", None, "pull request"),
        ("## Build order\n1. Create the foundation", None, "one issue"),
    ],
)
def test_invalid_membership_and_order_refuse_before_reading_children(
    body: str, order: list[int] | None, reason: str
) -> None:
    ops = github(body, issue(3), issue(8))
    with pytest.raises(EpicPlanError, match=reason):
        resolve_epic(ops, REPO, 40, ordered_issue_numbers=order)
    assert len(ops.raw_calls) == 1


@pytest.mark.parametrize("order", [[], [0], [-3], [True], ["3"]])
def test_explicit_order_requires_nonempty_positive_integer_identities(order: Any) -> None:
    ops = github("## Tasks\n- #3", issue(3))
    with pytest.raises(EpicPlanError):
        resolve_epic(ops, REPO, 40, ordered_issue_numbers=order)


def test_intake_limit_is_checked_before_fetching_children() -> None:
    body = "## Build order\n" + "\n".join(
        f"{index}. #{index + 100}" for index in range(1, MAX_EPIC_STEPS + 2)
    )
    ops = github(body)
    with pytest.raises(EpicPlanError, match="at most"):
        resolve_epic(ops, REPO, 40)
    assert len(ops.raw_calls) == 1


@pytest.mark.parametrize(
    "first,second,reason",
    [
        ("Depends on: #8", "", "itself"),
        ("Depends on: #3", "", "before"),
        ("Depends on: #3", "Depends on: #8", "cycle"),
        ("", "## Dependencies\n- #99", "missing prerequisite"),
        ("", "## Depends on\n- other/repo#8", "same repository"),
        ("", "## Dependencies\nAsk the owner which foundation", "dependency"),
        ("", "Depends on: issue to be chosen", "dependency"),
        ("", "## Dependencies\n\n", "dependency"),
        ("", "Depends on:\n- #8", "dependency"),
        ("", "This depends on #8 being delivered", "dependency"),
    ],
)
def test_declared_prerequisites_must_be_readable_and_satisfied_by_sequence(
    first: str, second: str, reason: str
) -> None:
    ops = github("## Build order\n1. #8\n2. #3", issue(8, first), issue(3, second))
    with pytest.raises(EpicPlanError, match=reason):
        resolve_epic(ops, REPO, 40)


def test_multiple_prerequisites_are_preserved_but_incidental_links_are_not() -> None:
    ops = github(
        "## Build order\n1. #8\n2. #3\n3. #7",
        issue(8, "## Dependencies\nNone."),
        issue(3, "Depends on: #8\nRelated discussion #99"),
        issue(7, "## Prerequisites\n- #8: foundation\n- #3: preparation\n## Context\n#90"),
    )
    plan = resolve_epic(ops, REPO, 40)
    assert [step.depends_on for step in plan.steps] == [(), (8,), (8, 3)]


def test_prerequisite_range_cannot_silently_mean_only_its_endpoints() -> None:
    ops = github(
        "## Build order\n1. #2\n2. #4\n3. #5",
        issue(2),
        issue(4),
        issue(5, "Depends on: #2-#4"),
    )
    with pytest.raises(EpicPlanError, match="range"):
        resolve_epic(ops, REPO, 40)


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"number": 99}, "identity"),
        ({"pull_request": {}}, "pull request"),
        ({"title": ""}, "title"),
        ({"body": {}}, "body"),
        ({"state": "unknown"}, "state"),
        ({"html_url": "https://github.com/other/repo/issues/3"}, "identity"),
        ({"html_url": f"https://other-host.invalid/{REPO}/issues/3"}, "same host"),
        ({"html_url": f"/{REPO}/issues/3"}, "identity"),
        ({"html_url": f"https://user@github.com/{REPO}/issues/3"}, "identity"),
        ({"labels": None}, "labels"),
        ({"labels": [{"no_name": "x"}]}, "labels"),
    ],
)
def test_malformed_child_responses_refuse(overrides: dict[str, Any], reason: str) -> None:
    ops = github("## Build order\n1. #3", issue(3))
    ops.issue_payloads[(REPO, 3)].update(overrides)
    with pytest.raises(EpicPlanError, match=reason):
        resolve_epic(ops, REPO, 40)


def test_missing_child_is_an_actionable_error_and_never_partial_plan() -> None:
    ops = github("## Build order\n1. #8\n2. #3", issue(8))
    with pytest.raises(EpicPlanError, match=r"cannot read.*3"):
        resolve_epic(ops, REPO, 40)
    assert all(method == "GET" for method, _, _ in ops.raw_calls)


def test_github_unavailability_is_not_empty_membership() -> None:
    ops = github("## Build order\n1. #3", issue(3))
    ops.fail_always["raw"] = GithubOpsError("HTTP 403: unavailable", http_status=403)
    with pytest.raises(EpicPlanError, match=r"cannot read.*40.*403"):
        resolve_epic(ops, REPO, 40)


def test_closed_state_is_preserved_without_claiming_completion() -> None:
    ops = github("## Build order\n1. #3", issue(3, state="closed"))
    plan = resolve_epic(ops, REPO, 40)
    assert plan.steps[0].state == "closed"
    assert plan.steps[0].labels == ()


def test_epic_level_prerequisites_require_resolution_before_admission() -> None:
    ops = github("## Dependencies\n- #9\n## Build order\n1. #3", issue(3))
    with pytest.raises(EpicPlanError, match=r"epic.*prerequisite"):
        resolve_epic(ops, REPO, 40)
    assert len(ops.raw_calls) == 1


def test_workload_labels_keep_run_kind_and_dual_labels_refuse() -> None:
    ops = github("## Build order\n1. #3", issue(3, labels=[{"name": "custom:work"}]))
    plan = resolve_epic(ops, REPO, 40, workload_label="custom:work")
    assert plan.steps[0].kind == "workload"
    assert plan.steps[0].labels == ("custom:work",)
    ops.issue_payloads[(REPO, 3)]["labels"].append({"name": "custom:code"})
    with pytest.raises(EpicPlanError, match="both"):
        resolve_epic(ops, REPO, 40, trigger_label="custom:code", workload_label="custom:work")
