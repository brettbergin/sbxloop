"""Prompt template rendering tests."""

from typing import ClassVar

import pytest

from sbxloop.engine.prompts import bullet_list, render
from sbxloop.policy import BASELINE_REGISTRY_DOMAINS, WELL_KNOWN_REGISTRY_DOMAINS


def test_render_decompose() -> None:
    text = render("decompose", outcome="Build the thing", max_tasks="5")
    assert "Build the thing" in text
    assert "At most 5 tasks" in text
    assert "$outcome" not in text


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


def test_render_all_templates_have_no_leftover_vars() -> None:
    contexts = {
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
            "executor_report": "r",
            "evidence": "e",
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
    for name, context in contexts.items():
        text = render(name, **context)
        assert "$" not in text.replace("$?", ""), f"unsubstituted var in {name}"


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


class TestEcosystemSelection:
    """Only the selected languages' ecosystem notes reach the model (gap 3)."""

    CTX: ClassVar[dict[str, str]] = {
        "outcome": "o",
        "task_id": "t1",
        "task_title": "T",
        "task_description": "d",
        "acceptance_criteria": "a",
        "feedback": "f",
        "user_guidance": "g",
    }

    def test_unfiltered_render_keeps_every_entry(self) -> None:
        text = render("plan", **self.CTX)
        assert text.count("- **") == 10

    def test_selected_language_only(self) -> None:
        text = render("plan", languages=["rust"], **self.CTX)
        assert text.count("- **") == 1
        assert "Cargo.toml" in text
        # The nine ecosystems this sandbox has no toolchain for are gone —
        # advertising them invites a plan the sandbox cannot run.
        assert "PEP 668" not in text
        assert "node_modules" not in text

    def test_multiple_languages(self) -> None:
        text = render("plan", languages=["python", "go"], **self.CTX)
        assert text.count("- **") == 2
        assert "PEP 668" in text and "go.mod" in text

    def test_execute_template_is_filtered_too(self) -> None:
        text = render(
            "execute",
            languages=["php"],
            outcome="o",
            task_id="t1",
            task_title="T",
            task_description="d",
            plan_steps="- s",
            expected_artifacts="- a",
            feedback="f",
            user_guidance="g",
        )
        assert text.count("- **") == 1
        assert "composer" in text

    def test_no_matching_entry_drops_the_block(self) -> None:
        # Defensive: a language with no prose entry must not silently fall
        # back to advertising all ten.
        text = render("plan", languages=[], **self.CTX)
        assert text.count("- **") == 0

    def test_markers_never_reach_the_model(self) -> None:
        for languages in (None, ["rust"], []):
            text = render("plan", languages=languages, **self.CTX)
            assert "ecosystems:start" not in text
            assert "ecosystems:end" not in text
