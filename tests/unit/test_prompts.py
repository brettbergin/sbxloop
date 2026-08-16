"""Prompt template rendering tests."""

from importlib import resources

import pytest

from sbxloop.engine import prompts
from sbxloop.engine.prompts import bullet_list, render
from sbxloop.policy import BASELINE_REGISTRY_DOMAINS, WELL_KNOWN_REGISTRY_DOMAINS


def test_render_decompose() -> None:
    text = render("decompose", outcome="Build the thing", max_tasks="5")
    assert "Build the thing" in text
    assert "At most 5 tasks" in text
    assert "$outcome" not in text


def test_decompose_states_the_uv_project_convention() -> None:
    # #250: the decomposer writes verify commands too, and the lint holds
    # them to the uv convention when a lockfile is present — so the rule
    # has to be stated where the decomposer reads it, not only in plan.md.
    text = render("decompose", outcome="o", max_tasks="5")
    assert "uv.lock" in text
    assert "uv run pytest" in text


def test_execute_and_plan_carry_environment_notes() -> None:
    """Field regression: the agent burned its whole revision budget on
    `python3 -m venv` failing (missing ensurepip) and bare pip hitting
    PEP 668 — the prompts must state the environment facts."""
    execute = render(
        "execute",
        outcome="o",
        task_id="t1",
        task_title="tt",
        task_description="td",
        plan_steps="- s",
        expected_artifacts="- a",
        feedback="(none)",
        user_guidance="(none)",
    )
    plan = render(
        "plan",
        outcome="o",
        task_id="t1",
        task_title="tt",
        task_description="td",
        acceptance_criteria="- c",
        feedback="(none)",
        user_guidance="(none)",
    )
    for text in (execute, plan):
        assert "externally managed" in text
        assert "python3 -m venv" in text
        assert "sudo" in text
        assert "allowlist" in text
    # Plan-declared egress: the planner must know the field and its bounds,
    # and the executor must report blocked domains instead of retrying.
    assert "egress" in plan
    assert "egress" in execute
    assert "blocked domain" in execute
    # Field regression (rv4zfdb1m): the executor nested the project in a
    # subdirectory while root-relative verify commands failed every revision.
    # Both sides must be told verify runs from the workspace root.
    assert "workspace root" in plan
    assert "workspace root" in execute
    assert "cannot edit" in plan
    assert "cannot edit" in execute
    # 0.5.0 regression: environment notes buried the response-format section
    # and JSON compliance dropped. The format instructions must come LAST.
    assert plan.index("Environment facts") < plan.index("Response format")
    assert "ONLY the fenced JSON block" in plan


# Layer 3 (issue #142): the prompts must carry per-ecosystem environment
# notes at parity, so no single toolchain is the one a planner pattern-matches
# against. Each entry is (ecosystem, plan.md markers, execute.md markers);
# one row per language sub-issue.
ECOSYSTEM_NOTES: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    ("Python", ("PEP 668", "python3 -m venv", ".venv/bin/pytest"), ("PEP 668", ".venv/bin/")),
    # #250: the uv-projects note rides inside the Python block of every prompt.
    (
        "uv projects",
        ("uv.lock", "uv sync", "uv run pytest -q"),
        ("uv.lock", "uv sync", "uv run"),
    ),
    ("JavaScript/Node", ("package.json", "npm ci && npm test"), ("package.json", "npm ci")),
    (
        "TypeScript",
        ("tsconfig.json", "npx tsc --noEmit && npm test"),
        ("tsconfig.json", "tsc --noEmit"),
    ),
    ("Go", ("go.mod", "go build ./... && go test ./..."), ("go.mod", "./...")),
    ("Rust", ("Cargo.toml", "cargo test", "target/"), ("Cargo.toml", "cargo test")),
    ("Ruby", ("Gemfile", "bundle exec rspec"), ("Gemfile", "bundle exec")),
    (
        "Java/JVM",
        ("pom.xml", "mvn -q -B test", "./gradlew test"),
        ("pom.xml", "JAVA_HOME", "./gradlew"),
    ),
    ("C#/.NET", (".csproj", "dotnet test", "global.json"), (".csproj", "dotnet test")),
    (
        "PHP",
        ("composer.json", "composer install --no-interaction && ./vendor/bin/phpunit"),
        ("composer.json", "./vendor/bin/"),
    ),
    (
        "C/C++",
        ("cmake -S . -B build", "ctest --test-dir build --output-on-failure"),
        ("cmake -S . -B build", "ctest --test-dir build"),
    ),
]


@pytest.mark.parametrize(
    ("ecosystem", "plan_markers", "execute_markers"),
    ECOSYSTEM_NOTES,
    ids=[row[0] for row in ECOSYSTEM_NOTES],
)
def test_prompts_carry_ecosystem_notes(
    ecosystem: str,
    plan_markers: tuple[str, ...],
    execute_markers: tuple[str, ...],
) -> None:
    plan = render(
        "plan",
        outcome="o",
        task_id="t1",
        task_title="T",
        task_description="d",
        acceptance_criteria="- c",
        feedback="(none)",
        user_guidance="(none)",
    )
    execute = render(
        "execute",
        outcome="o",
        task_id="t1",
        task_title="T",
        task_description="d",
        plan_steps="- s",
        expected_artifacts="- a",
        feedback="(none)",
        user_guidance="(none)",
    )
    for text in (plan, execute):
        assert "Ecosystem notes" in text
        assert f"**{ecosystem}**" in text
    for marker in plan_markers:
        assert marker in plan, f"{ecosystem}: missing {marker!r} in plan.md"
    for marker in execute_markers:
        assert marker in execute, f"{ecosystem}: missing {marker!r} in execute.md"


def test_environment_facts_lead_language_neutral() -> None:
    """Layer 3 (#142): the environment opener must be toolchain-neutral —
    per-ecosystem specifics belong in the Ecosystem notes block below it, not
    in the framing every task reads."""
    plan = render(
        "plan",
        outcome="o",
        task_id="t1",
        task_title="T",
        task_description="d",
        acceptance_criteria="- c",
        feedback="(none)",
        user_guidance="(none)",
    )
    opener = plan[plan.index("## Environment facts") : plan.index("Ecosystem notes")]
    # The universal contract stays in the opener...
    assert "workspace root" in opener
    assert "cannot edit" in opener
    # ...while no ecosystem gets to frame it.
    for ecosystem_specific in ("PEP 668", ".venv", "pytest"):
        assert ecosystem_specific not in opener, (
            f"{ecosystem_specific!r} leaked into the language-neutral opener"
        )


# One full context per template. Kept in sync with the header comment at the
# top of each prompts/*.md — that header is where an editor learns which
# variables a template takes (#225).
RENDER_CONTEXTS: dict[str, dict[str, str]] = {
    "decompose": {"outcome": "o", "max_tasks": "3"},
    "plan": {
        "outcome": "o",
        "task_id": "t1",
        "task_title": "T",
        "task_description": "d",
        "acceptance_criteria": "- c",
        "feedback": "f",
        "user_guidance": "g",
    },
    "execute": {
        "outcome": "o",
        "task_id": "t1",
        "task_title": "T",
        "task_description": "d",
        "plan_steps": "- s",
        "expected_artifacts": "- a",
        "feedback": "f",
        "user_guidance": "g",
    },
    "scrutinize": {
        "task_id": "t1",
        "task_title": "T",
        "task_description": "d",
        "acceptance_criteria": "- c",
        "plan_steps": "- s",
        "prior_feedback": "f",
        "executor_report": "r",
        "evidence": "e",
        "verify_commands": "- true",
    },
    "validate": {
        "outcome": "o",
        "task_id": "t1",
        "task_title": "T",
        "task_description": "d",
        "acceptance_criteria": "- c",
        "verify_results": "v",
    },
    "steer": {
        "outcome": "o",
        "tasks_summary": "- t1 [executing] T",
        "current_task": "Task t1: T",
        "user_guidance": "(none)",
        "user_message": "how is it going?",
    },
}


def test_render_contexts_cover_every_template_on_disk() -> None:
    """RENDER_CONTEXTS is the "every template" universe for the tests below;
    if a new prompts/*.md ships without an entry, those tests silently
    skip it. Discover the resources so the omission fails loudly."""
    on_disk = {
        path.name.removesuffix(".md")
        for path in resources.files(prompts).iterdir()
        if path.name.endswith(".md")
    }
    assert on_disk == set(RENDER_CONTEXTS), (
        "add a RENDER_CONTEXTS entry for every prompts/*.md template"
    )


def test_render_all_templates_have_no_leftover_vars() -> None:
    for name, context in RENDER_CONTEXTS.items():
        text = render(name, **context)
        assert "$" not in text.replace("$?", ""), f"unsubstituted var in {name}"


def test_every_template_opens_with_contract_header() -> None:
    """#225: the rules above are enforced by this file but were discoverable
    only by breaking them. Each template must carry the contract in an
    HTML comment header naming the file's variables, so a new one cannot
    ship without one."""
    for name, context in RENDER_CONTEXTS.items():
        source = (resources.files(prompts) / f"{name}.md").read_text()
        assert source.startswith("<!--\n"), f"{name}.md lacks the contract header"
        header = source[: source.index("-->")]
        assert "string.Template" in header
        assert "$$" in header, f"{name}.md header must state the $-escaping rule"
        for var in context:
            assert f"${var}" in header, f"{name}.md header does not list ${var}"


def test_contract_header_never_reaches_the_model() -> None:
    """The header is written for editors, not the agent: it must cost no
    tokens and must not be readable as instructions."""
    for name, context in RENDER_CONTEXTS.items():
        text = render(name, **context)
        assert "<!--" not in text, f"{name}: header leaked into the rendered prompt"
        assert text.startswith("# "), f"{name}: rendered prompt must open with its title"


def test_registry_tiers_are_injected_not_hardcoded() -> None:
    """#141 moves registries between the tiers one language at a time; a
    hardcoded prompt list would drift, and a drifted list is a failed run —
    the planner either declares what needs no declaration or omits what
    does. Both tiers must reach both prompts from policy.py."""
    plan = render(
        "plan",
        outcome="o",
        task_id="t1",
        task_title="tt",
        task_description="td",
        acceptance_criteria="- c",
        feedback="(none)",
        user_guidance="(none)",
    )
    execute = render(
        "execute",
        outcome="o",
        task_id="t1",
        task_title="tt",
        task_description="td",
        plan_steps="- s",
        expected_artifacts="- a",
        feedback="(none)",
        user_guidance="(none)",
    )
    for text in (plan, execute):
        for domain in BASELINE_REGISTRY_DOMAINS:
            assert f"`{domain}`" in text
        for domain in WELL_KNOWN_REGISTRY_DOMAINS:
            assert f"`{domain}`" in text
    # The planner must be able to tell the tiers apart: the baseline is
    # named as never-declare, the well-known set as declare-if-touched.
    assert "never declare them" in plan
    assert "npm" in plan.lower()


def test_render_missing_variable_fails_loudly() -> None:
    with pytest.raises(KeyError):
        render("decompose", outcome="only outcome")


def test_retry_context_defaults_empty_and_substitutes() -> None:
    base = render("decompose", outcome="o", max_tasks="3")
    retried = render("decompose", outcome="o", max_tasks="3", retry_context="TRY AGAIN")
    assert "TRY AGAIN" not in base
    assert "TRY AGAIN" in retried


def test_steer_prompt_carries_chat_contract() -> None:
    """STEER must present the user's message, the three actions, and the
    read-only rule — direction changes flow through the engine, not edits."""
    text = render(
        "steer",
        outcome="build it",
        tasks_summary="- t1 [executing] Build",
        current_task="Task t1: Build (state: executing)",
        user_guidance="- use uv",
        user_message="switch the storage layer to postgres",
    )
    assert "switch the storage layer to postgres" in text
    for action in ("continue", "steer_task", "steer_run"):
        assert action in text
    assert "read-only" in text
    assert "Do not modify anything" in text
    assert "ONLY the fenced JSON block" in text


def test_plan_and_execute_render_standing_guidance() -> None:
    plan = render(
        "plan",
        outcome="o",
        task_id="t1",
        task_title="T",
        task_description="d",
        acceptance_criteria="- c",
        feedback="f",
        user_guidance="- always use postgres",
    )
    execute = render(
        "execute",
        outcome="o",
        task_id="t1",
        task_title="T",
        task_description="d",
        plan_steps="- s",
        expected_artifacts="- a",
        feedback="f",
        user_guidance="- always use postgres",
    )
    for text in (plan, execute):
        assert "Standing user guidance" in text
        assert "always use postgres" in text


def test_bullet_list() -> None:
    assert bullet_list([]) == "(none)"
    assert bullet_list(["a", "b"]) == "- a\n- b"
