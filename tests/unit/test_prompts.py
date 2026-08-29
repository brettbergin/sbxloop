"""Prompt template rendering tests."""

from importlib import resources

import pytest

from sbxloop.engine import prompts
from sbxloop.engine.prompts import bullet_list, render
from sbxloop.policy import BASELINE_REGISTRY_DOMAINS, WELL_KNOWN_REGISTRY_DOMAINS


def build_context(**over: str) -> dict[str, str]:
    context = {
        "outcome": "o",
        "task_id": "t1",
        "task_title": "T",
        "task_description": "d",
        "acceptance_criteria": "- c",
        "verify_commands": "- true",
        "prior_attempt": "(none)",
        "feedback": "(none)",
        "user_guidance": "(none)",
    }
    context.update(over)
    return context


def test_render_decompose() -> None:
    text = render("decompose", outcome="Build the thing", max_tasks="5", project_gate="- gate")
    assert "Build the thing" in text
    assert "At most 5 tasks" in text
    assert "$outcome" not in text


def test_decompose_states_the_uv_project_convention() -> None:
    # #250: the decomposer writes ALL the verify commands, and the lint
    # holds them to the uv convention when a lockfile is present — so the
    # rule has to be stated where the decomposer reads it.
    text = render("decompose", outcome="o", max_tasks="5", project_gate="- gate")
    assert "uv.lock" in text
    assert "uv run pytest" in text


def test_decompose_carries_verify_authoring_rules() -> None:
    """The builder cannot fix a wrong exam, so authoring time is the only
    mitigation for the wrong-check failure class (formerly verify_suspect):
    the workspace-root/sh-c/portability facts that lived in plan.md must
    now reach the decomposer."""
    text = render("decompose", outcome="o", max_tasks="5", project_gate="- gate")
    assert "workspace root" in text
    assert "cannot edit" in text
    assert "`sh -c`" in text
    assert "test runner over shell pipelines" in text


def test_decompose_warns_against_config_overriding_verify_paths() -> None:
    """#387: `uv run mypy packages` overrides the `files` pinned in
    pyproject.toml, drags in the hatchling build hook and can never pass,
    while the bare `uv run mypy` the gate runs is clean. The decomposer
    writes the verify commands, so the warning belongs in its prompt."""
    text = render("decompose", outcome="o", max_tasks="5", project_gate="- gate")
    assert "uv run mypy packages" in text
    assert "`uv run mypy`" in text
    assert "overrides the configured file" in text
    for pinned in ("mypy `files`", "ruff `include`", "pytest `testpaths`"):
        assert pinned in text, pinned
    assert "hatchling" in text


def test_build_carries_environment_notes() -> None:
    """Field regression: the agent burned its whole revision budget on
    `python3 -m venv` failing (missing ensurepip) and bare pip hitting
    PEP 668 — the prompt must state the environment facts."""
    build = render("build", **build_context())
    assert "externally managed" in build
    assert "python3 -m venv" in build
    assert "sudo" in build
    assert "allowlist" in build
    # Task-declared egress: the builder must report blocked domains instead
    # of retrying forever.
    assert "egress" in build
    assert "blocked domain" in build
    # Field regression (rv4zfdb1m): the executor nested the project in a
    # subdirectory while root-relative verify commands failed every revision.
    assert "workspace root" in build
    assert "cannot edit" in build


def test_build_shows_the_exam_and_asks_for_a_plan_first_report() -> None:
    """The builder sees the decomposer-authored verify commands verbatim
    (it no longer writes them), and its report opens with the approach —
    the chronology's plan-card replacement."""
    build = render("build", **build_context(verify_commands="- uv run pytest -q"))
    assert "Verify commands that will run when you finish" in build
    assert "- uv run pytest -q" in build
    assert "state your approach first" in build
    assert build.index("## Task") < build.index("Verify commands that will run")


# Layer 3 (issue #142): the prompt must carry per-ecosystem environment
# notes at parity, so no single toolchain is the one the builder
# pattern-matches against. One row per language sub-issue.
ECOSYSTEM_NOTES: list[tuple[str, tuple[str, ...]]] = [
    ("Python", ("PEP 668", ".venv/bin/")),
    # #250: the uv-projects note rides inside the Python block.
    ("uv projects", ("uv.lock", "uv sync", "uv run")),
    ("JavaScript/Node", ("package.json", "npm ci")),
    ("TypeScript", ("tsconfig.json", "tsc --noEmit")),
    ("Go", ("go.mod", "./...")),
    ("Rust", ("Cargo.toml", "cargo test")),
    ("Ruby", ("Gemfile", "bundle exec")),
    ("Java/JVM", ("pom.xml", "JAVA_HOME", "./gradlew")),
    ("C#/.NET", (".csproj", "dotnet test")),
    ("PHP", ("composer.json", "./vendor/bin/")),
    ("C/C++", ("cmake -S . -B build", "ctest --test-dir build")),
]


@pytest.mark.parametrize(
    ("ecosystem", "markers"),
    ECOSYSTEM_NOTES,
    ids=[row[0] for row in ECOSYSTEM_NOTES],
)
def test_prompts_carry_ecosystem_notes(ecosystem: str, markers: tuple[str, ...]) -> None:
    build = render("build", **build_context())
    assert "Ecosystem notes" in build
    assert f"**{ecosystem}**" in build
    for marker in markers:
        assert marker in build, f"{ecosystem}: missing {marker!r} in build.md"


# One full context per template. Kept in sync with the header comment at the
# top of each prompts/*.md — that header is where an editor learns which
# variables a template takes (#225).
RENDER_CONTEXTS: dict[str, dict[str, str]] = {
    "decompose": {"outcome": "o", "max_tasks": "3", "project_gate": "- gate rule"},
    "build": build_context(),
    "steer": {
        "outcome": "o",
        "tasks_summary": "- t1 [executing] T",
        "current_task": "Task t1: T",
        "user_guidance": "(none)",
        "user_message": "how is it going?",
    },
    "review": {
        "outcome": "o",
        "pr_number": "12",
        "round": "1",
        "diff": "diff --git a/x b/x",
        "tasks_summary": "- t1 [done] T",
        "prior_rounds": "(first review of this pull request)",
        "user_guidance": "(none)",
        "project_gate": "- gate rule",
    },
    "concierge": {
        "command_prefix": "!sbx",
        "repo": "owner/repo",
        "model": "auto",
        "tool_notes": "- `sbx_control` — run a verb",
        "daemon_notes": "- poll interval 60s",
        "trigger_label": "sbxloop:run",
    },
}


def test_concierge_prompt_carries_contract() -> None:
    """The concierge is trusted with the operator surface: the prompt must
    name its tools, keep steering in the run thread, and forbid claiming
    actions it did not perform (see the template header)."""
    text = render("concierge", **RENDER_CONTEXTS["concierge"])
    assert text.startswith("# You are the sbxloop concierge")
    assert "`sbx_control`" in text and "`create_issue`" in text
    assert "`enqueue_work`" not in text and "backlog" not in text and "inbox" not in text
    assert "thread" in text and "not here" in text
    assert "Never claim to have done something you did not do" in text
    assert "!sbx" in text  # the configured prefix reaches the model
    # intake is one hop: the issue is filed with the trigger label and runs
    assert "`sbxloop:run`" in text
    assert "`create_issue`, **one call, no confirmation**" in text
    assert "`label_issue_for_run`" in text and "`list_issues`" in text
    assert "queue only what\n  the person names" in text
    # triage's other half: a reply is direct, a close never is
    assert "`comment_on_issue`" in text and "`close_issue`" in text
    assert "pass **their own words** as `confirmation`" in text
    assert "The one exception is\n  `close_issue`" in text
    # drift: the concierge reports versions, a human does the upgrading
    assert "`version_status`" in text and "**You\n  cannot upgrade anything**" in text


def test_review_prompt_carries_contract() -> None:
    """REVIEW reads the PR adversarially through four named lenses, as a
    read-only session, honours refutations from earlier rounds, and answers
    in the verdict/severity vocabulary the engine validates (see the
    template header)."""
    text = render(
        "review",
        outcome="ship the feature",
        pr_number="42",
        round="2",
        diff="diff --git a/app.py b/app.py\n+print('hi')",
        tasks_summary="- t1 [done] Build",
        prior_rounds="### Round 1 — request_changes",
        user_guidance="- use uv",
        project_gate="- One task's `verify_commands` MUST run `make check`",
    )
    assert text.startswith("# Review the pull request")
    assert "pull request #42" in text and "round 2" in text
    assert "diff --git a/app.py b/app.py" in text
    assert "### Round 1 — request_changes" in text
    assert "make check" in text and "use uv" in text
    for lens in (
        "Concurrency and locking",
        "Failure ordering",
        "Input validation",
        "Cross-module interaction",
    ):
        assert lens in text, lens
    assert "read-only" in text
    assert "Do not modify" in text
    assert "refuted" in text
    assert "ONLY the fenced JSON block" in text
    for word in ("approve", "request_changes", "blocking", "major", "minor", "nit"):
        assert f"`{word}`" in text or f'"{word}"' in text, word


def test_review_prompt_describes_the_wrong_check_shape() -> None:
    """#387: the scrutinizer passed the work 6/6 and never said the check
    itself was impossible, so the run burnt its whole revision and replan
    budget. The prompt now carries the config-override worked example."""
    text = render("review", **RENDER_CONTEXTS["review"])
    assert "When the work is right and the check is wrong" in text
    assert "config-override" in text
    assert "uv run mypy packages" in text
    assert "`uv run mypy`" in text
    assert "[tool.mypy]" in text and 'files = ["packages/sbxloop/src"' in text
    assert "hatchling" in text
    assert "testpaths" in text
    assert "name the misconfigured command and its remedy in the summary" in text


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
    the decomposer either declares what needs no declaration or omits what
    does. Both tiers must reach both prompts from policy.py."""
    decompose = render("decompose", outcome="o", max_tasks="3", project_gate="- gate rule")
    build = render("build", **build_context())
    for text in (decompose, build):
        for domain in BASELINE_REGISTRY_DOMAINS:
            assert f"`{domain}`" in text
        for domain in WELL_KNOWN_REGISTRY_DOMAINS:
            assert f"`{domain}`" in text
    # The decomposer must be able to tell the tiers apart: the baseline is
    # named as never-declare, the well-known set as declare-if-touched.
    assert "never declare them" in decompose
    assert "npm" in decompose.lower()


def test_render_missing_variable_fails_loudly() -> None:
    with pytest.raises(KeyError):
        render("decompose", outcome="only outcome")


def test_retry_context_defaults_empty_and_substitutes() -> None:
    base = render("decompose", outcome="o", max_tasks="3", project_gate="- gate rule")
    retried = render(
        "decompose",
        outcome="o",
        max_tasks="3",
        project_gate="- gate rule",
        retry_context="TRY AGAIN",
    )
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


def test_build_renders_standing_guidance() -> None:
    build = render("build", **build_context(user_guidance="- always use postgres"))
    assert "Standing user guidance" in build
    assert "always use postgres" in build


def test_bullet_list() -> None:
    assert bullet_list([]) == "(none)"
    assert bullet_list(["a", "b"]) == "- a\n- b"
