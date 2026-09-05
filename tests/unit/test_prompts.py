"""Prompt template rendering tests."""

import re
from importlib import resources

import pytest

from sbxloop import toolchains
from sbxloop.engine import prompts
from sbxloop.engine.prompts import _strip_contract_header, bullet_list, render
from sbxloop.policy import BASELINE_REGISTRY_DOMAINS, WELL_KNOWN_REGISTRY_DOMAINS
from sbxloop.verifylint import config_override_example

# The engine renders the config-override example for the run's resolved
# toolchains (#634); tests that only care about the rest of a template take
# the default (Python) one.
EXAMPLE = {"config_override_example": config_override_example(None)}


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
    text = render(
        "decompose", outcome="Build the thing", max_tasks="5", project_gate="- gate", **EXAMPLE
    )
    assert "Build the thing" in text
    assert "At most 5 tasks" in text
    assert "$outcome" not in text


def test_decompose_asks_for_a_pr_title_in_the_repos_style() -> None:
    """#621: the plan names the pull request the way the repository names
    its commits, read from the workspace's own history."""
    text = render("decompose", outcome="o", max_tasks="5", project_gate="- gate", **EXAMPLE)
    assert "`pr_title`" in text
    assert "git log --oneline" in text
    assert '"pr_title"' in text, "the JSON example carries the key"


def test_decompose_carries_the_repositorys_pr_conventions_only_when_given() -> None:
    """#678: the title lint and the template are a paragraph rendered from
    the workspace (deliver.pr_conventions); the template itself teaches
    neither, so a repository without them is not told it has them."""
    from sbxloop.deliver import pr_conventions

    bare = render("decompose", outcome="o", max_tasks="5", project_gate="- gate", **EXAMPLE)
    assert "conventional commits" not in bare and "pr-body" not in bare
    told = render(
        "decompose",
        outcome="o",
        max_tasks="5",
        project_gate="- gate",
        pr_conventions="- This repository lints pull request titles as conventional commits",
        **EXAMPLE,
    )
    assert "- This repository lints pull request titles as conventional commits" in told
    assert pr_conventions(None) == ""


def test_decompose_states_the_uv_project_convention() -> None:
    # #250: the decomposer writes ALL the verify commands, and the lint
    # holds them to the uv convention when a lockfile is present — so the
    # rule has to be stated where the decomposer reads it.
    text = render("decompose", outcome="o", max_tasks="5", project_gate="- gate", **EXAMPLE)
    assert "uv.lock" in text
    assert "uv run pytest" in text


def test_decompose_carries_verify_authoring_rules() -> None:
    """The builder cannot fix a wrong exam, so authoring time is the only
    mitigation for the wrong-check failure class (formerly verify_suspect):
    the workspace-root/sh-c/portability facts that lived in plan.md must
    now reach the decomposer."""
    text = render("decompose", outcome="o", max_tasks="5", project_gate="- gate", **EXAMPLE)
    assert "workspace root" in text
    assert "cannot edit" in text
    assert "`sh -c`" in text
    assert "test runner over shell pipelines" in text


def test_decompose_demands_an_upgrade_path_task_for_persisted_state() -> None:
    """#524: a change to persisted state gets its own task, whose criteria
    enumerate the shapes a deployed instance holds and whose verify starts
    from a raw pre-change database — the plan names the path, so review is
    not where it is discovered one row state at a time."""
    text = render(
        "decompose", outcome="add a column", max_tasks="3", project_gate="- gate rule", **EXAMPLE
    )
    assert "## Risk pass: what a deployed instance already holds" in text
    assert "does it alter persisted state?" in text
    assert "**upgrade path for existing state**" in text
    assert "**enumerate** the shapes a deployed instance" in text
    for shape in ("every row state", "every id or key form", "every config\n  shape"):
        assert shape in text, shape
    flat = " ".join(text.split())
    assert "**start from a raw pre-change database**" in flat
    assert "never one produced by the new code" in text
    assert "not discovered by review" in text
    assert "If the outcome alters no persisted state, say nothing" in text


def test_decompose_treats_the_symptom_as_the_spec() -> None:
    """#535: an issue filed symptom-first is planned against the symptom;
    the requested mechanism is a hint the decomposer may overrule."""
    text = render("decompose", outcome="x", max_tasks="3", project_gate="- gate rule", **EXAMPLE)
    assert "**Symptom (as observed)** section, the symptom is\nthe specification" in text
    assert "**Requested change** section is the mechanism they asked for — a hint" in text
    assert "would not change what they saw, plan the change\nthat does" in text


def test_decompose_warns_against_config_overriding_verify_paths() -> None:
    """#387: an explicit path handed to a config-driven tool overrides the
    file set its configuration pins, drags in what the project excludes and
    can never pass, while the bare form the gate runs is clean. The
    decomposer writes the verify commands, so the warning belongs in its
    prompt — the rule text, plus the worked example rendered for the
    ecosystem (#634)."""
    text = render("decompose", outcome="o", max_tasks="5", project_gate="- gate", **EXAMPLE)
    assert "**overrides the configured file set**" in text
    for pinned in ("mypy `files`", "ruff `include`", "pytest `testpaths`", "tsc's `include`"):
        assert pinned in text, pinned
    assert "Write the bare form" in text
    assert "## The config-override, worked" in text
    assert EXAMPLE["config_override_example"] in text


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


def test_build_is_framed_as_a_branch_not_an_artifact() -> None:
    """#689: the builder edits an existing repository on a feature branch
    that a human reviews as a pull request — not a workspace it fills
    with artifacts. The greenfield phrases must not come back."""
    build = render("build", **build_context(work_dir="`/work/repo`", toolchains="python 3.13"))
    assert "feature branch of an existing repository" in build
    assert "checked out at\n`/work/repo`" in build or "checked out at `/work/repo`" in build
    assert "read the diff as a\npull request" in build or "read the diff as a pull request" in build
    assert "Resolved toolchains for this repository: python 3.13." in build
    assert "Match the conventions of the surrounding code" in build
    assert "Do not create top-level files unless the task asks for them" in build
    for greenfield in ("Write all outputs", "write all outputs", "when creating the project"):
        assert greenfield not in build, greenfield
    # the notes point at the named set, not at "this task's toolchain"
    assert "this task's toolchain" not in build


def test_build_and_review_share_the_scope_rule() -> None:
    """#689: the rule the reviewer judges by is the rule the builder is
    given, in the same words, so scope creep is named before it is built
    rather than only after."""
    rule = "beyond the outcome's scope is a defect"
    build = " ".join(render("build", **build_context()).split())
    review = " ".join(render("review", **RENDER_CONTEXTS["review"]).split())
    assert rule in build and rule in review
    assert "Change only what the task requires" in build

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
    "decompose": {"outcome": "o", "max_tasks": "3", "project_gate": "- gate rule", **EXAMPLE},
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
        **EXAMPLE,
    },
    "concierge": {
        "chat_name": "Discord",
        "command_prefix": "!sbx",
        "repo": "owner/repo",
        "model": "auto",
        "repos": "- owner/repo — enabled, base main, trigger label `sbxloop:run`",
        "tool_notes": "- `sbx_control` — run a verb",
        "daemon_notes": "- poll interval 60s",
        "trigger_label": "sbxloop:run",
        "workload_label": "sbxloop:workload",
        "workloads": "- `research`: sinks chat, issue",
    },
    # The workload's actors (#756): the operator plans and executes, the
    # judge decides.
    "operator_plan": {
        "outcome": "o",
        "max_tasks": "3",
        "work_dir": "/data",
        "bounds": "Profile `p`:\n- hosts: none",
        "user_guidance": "(none)",
    },
    "operator_execute": {
        "outcome": "o",
        "task_id": "t1",
        "task_title": "T",
        "task_description": "do the thing",
        "acceptance_criteria": "- it is done",
        "verify_commands": "(none)",
        "needs": "(none declared)",
        "work_dir": "/data",
        "prior_attempt": "(first attempt)",
        "feedback": "(none)",
        "user_guidance": "(none)",
    },
    "operator_judge": {
        "outcome": "o",
        "task_id": "t1",
        "task_title": "T",
        "task_description": "do the thing",
        "acceptance_criteria": "- it is done",
        "work_dir": "/data",
        "attempt": "1",
        "report": "## Result\n\ndone",
        "tool_digest": "1. `Bash` `ls` — ok",
        "evidence": "(no mechanical checks declared)",
    },
}


def test_concierge_prompt_carries_contract() -> None:
    """The concierge is trusted with the operator surface: the prompt must
    name its tools, keep steering in the run thread, and forbid claiming
    actions it did not perform (see the template header)."""
    text = render("concierge", **RENDER_CONTEXTS["concierge"])
    assert text.startswith("# You are the sbxloop concierge")
    assert "`sbx_control`" in text and "`create_issue`" in text
    # "backlog" is allowed only as the `list_issues` state value, never as a
    # separate queue concept the concierge could file onto
    assert "`enqueue_work`" not in text and "inbox" not in text
    assert text.count("backlog") == text.count("`backlog`") + 1
    assert "`backlog` (carrying" in text
    assert "thread" in text and "not here" in text
    assert "Never claim to have done something you did not do" in text
    assert "!sbx" in text  # the configured prefix reaches the model
    # intake is one hop: the issue is filed with the trigger label and runs
    assert "`sbxloop:run`" in text
    assert "`create_issue`, **one call, no confirmation**" in text
    assert "`label_issue_for_run`" in text and "`list_issues`" in text
    assert "queue only what\n  the person names" in text
    # `queued: false` is the exact complement, and `state` is offered
    assert "exact complement" in text and "failed or are\n  blocked" in text
    assert "`state` narrows to one exact" in text
    # triage's other half: a reply is direct, a close never is
    assert "`comment_on_issue`" in text and "`close_issue`" in text
    assert "pass **their own words** as `confirmation`" in text
    assert "The one exception is\n  `close_issue`" in text
    # the configured repositories reach the model, with their per-repo facts
    assert "owner/repo — enabled, base main" in text and "`list_repos`" in text
    # drift: the concierge reports versions, a human does the upgrading
    assert "`version_status`" in text and "**You cannot upgrade\n  anything**" in text
    # #638: no claim that the user's repository publishes sbxloop on merge,
    # and no guessed upgrade command — the report names it or nobody does
    assert "publishes a release" not in text and "pip install" not in text
    assert "operator's step" in text and "do\n  not guess a command" in text
    # #524: an ask that touches persisted state files with a migration section
    assert "**Migration of existing state** section" in text
    # #535: symptom-first filing; a fix named with no symptom gets one question
    assert "The issue\n  is **symptom-first**" in text
    assert "**A fix-shaped ask with no symptom is\n  genuinely ambiguous**" in text
    assert "What are you seeing that you want gone or changed?" in text
    assert "Worked example:" in text and "→ ask;" in text
    # #564: enumerable clarifying answers become clickable choices; open-ended
    # questions stay free text
    assert "sbx-choices" in text
    assert "**Open-ended questions stay free text: no block at all.**" in text
    assert "stays free\n  text unless you can enumerate real candidate symptoms" in text
    assert "written **against the symptom**" in text
    assert "raw pre-change database" in text
    # ask, never block: a filing-blocking question arms an sbx-pending
    # fallback; an unanswered ask files on the stated assumption, and a
    # close confirmation never proceeds on silence
    flat = " ".join(text.split())
    assert "sbx-pending" in text
    assert "State your **own best guess in the same message**" in flat
    assert "*Symptom (assumed)* section" in flat
    assert "You never wait forever and no request is ever dropped." in flat
    assert "a close never proceeds on silence" in flat
    # #797: a workload's subject is unbounded — the concierge never declares
    # an ask out of scope, and never asks whether to queue one
    assert "`start_workload`, **one call, no confirmation**" in text
    assert "Its subject is unbounded" in text
    assert "You have no scope of your own to police" in text
    assert "The topic is never yours to judge" in flat
    assert 'or "want me to queue a workload?"** — the ask *is* the yes' in flat


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
        **EXAMPLE,
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
    # #521: every blocking/major finding carries the reviewer's reproduction,
    # concrete enough to become a failing test, and names the neighbours.
    assert "## Reproduce before you file" in text
    assert "`repro` is required on every `blocking` and `major` finding" in text
    assert '"repro":' in text
    assert "name the neighbours" in text
    # #524: round 1 reviews the plan too — a persisted-state change with no
    # upgrade-path task is a blocking finding on the plan.
    assert "review the **plan** as well as the diff" in text
    # #535: the PR is judged against the symptom, not the requested mechanism.
    assert "judge the pull\nrequest against the symptom, not the mechanism" in text
    assert "`request_changes` on the plan" in text
    assert "say what would actually remove the symptom" in text
    # #517: out-of-scope notes are a first-class output, never a finding.
    assert "## Out of scope, but real: follow-ups" in text
    assert '"followups":' in text and "never\npromote one to a finding" in text
    assert "do not file it anywhere" not in text
    assert "**upgrade path for existing state**" in text
    assert "raw pre-change database" in text
    assert "`blocking` finding on the plan" in text


def test_review_prompt_describes_the_wrong_check_shape() -> None:
    """#387: the scrutinizer passed the work 6/6 and never said the check
    itself was impossible, so the run burnt its whole revision and replan
    budget. The prompt carries the wrong-check rule and the config-override
    worked example rendered for the ecosystem (#634)."""
    text = render("review", **RENDER_CONTEXTS["review"])
    assert "When the work is right and the check is wrong" in text
    assert "config-override" in text
    assert "can never go green" in text
    assert EXAMPLE["config_override_example"] in text
    assert "name the misconfigured command and its remedy in the summary" in text


def test_config_override_example_follows_the_resolved_toolchain() -> None:
    """#634: the worked example is the one for the repository in front of
    the model — a Go run reads a Go story, not a mypy one — and every
    ecosystem's story carries the same shape: what the gate runs, the
    command that overrode it, and the remedy."""
    for name in ("decompose", "review"):
        context = dict(RENDER_CONTEXTS[name])
        context["config_override_example"] = config_override_example(["go"])
        text = render(name, **context)
        assert "go test ./..." in text and "-tags integration" in text, name
        assert "[tool.mypy]" not in text, name
    go = config_override_example(["go"])
    assert "mypy" not in go
    assert "```go" in go
    for languages, marker in (
        (None, "[tool.mypy]"),
        ([], "[tool.mypy]"),
        (["python"], "[tool.mypy]"),
        (["typescript"], "tsconfig.json"),
        (["node", "typescript"], "tsconfig.json"),
        # TypeScript pulls JavaScript in as a requirement, so the resolved
        # set arrives in registry order — the tsc story still wins (#690).
        (["javascript", "typescript"], "tsconfig.json"),
        (["javascript"], ".mocharc.yml"),
        (["bun"], ".mocharc.yml"),
        (["ruby"], "--force-exclusion"),
        (["rust"], "default-members"),
        (["java"], "maven-surefire-plugin"),
        (["php"], "<testsuites>"),
        (["dotnet"], "App.slnf"),
        (["cpp"], "CMakePresets.json"),
        (["make"], "[tool.mypy]"),
        (["go", "python"], "go test ./..."),
    ):
        text = config_override_example(languages)
        assert marker in text, (languages, marker)
        assert "the remedy is re-authoring the command to the bare form" in text, languages
        assert text.startswith("```"), languages


def test_every_ecosystem_has_its_own_override_story() -> None:
    """#690: six registry ecosystems fell back to the Python story. Every
    language toolchain reads one of its own, in the same shape — the gate
    named, the command that reached past its configuration, the remedy —
    and no story is another ecosystem's."""
    task_runners = {"make", "just", "task"}
    seen: dict[str, str] = {}
    for language in toolchains.supported_languages():
        if language in task_runners:
            continue
        text = config_override_example([language])
        assert "The project gate runs `" in text, language
        assert "The task's verify command runs `" in text, language
        assert "on every attempt" in text, language
        assert "the remedy is re-authoring the command to the bare form" in text, language
        if language != "python":
            assert "[tool.mypy]" not in text, language
        seen[language] = text
    # bun is a JavaScript client and reads the JavaScript story; every
    # other ecosystem's is its own.
    assert seen.pop("bun") == seen["javascript"]
    assert len(set(seen.values())) == len(seen)


DOMAIN_ANCHORS: tuple[str, ...] = (
    # paths and build files of the loop's own repository
    "packages/sbxloop",
    "hatch_build",
    "hatchling",
    # the loop's chat bridge, and the field failure that used to be the example
    "Discord",
    "embeds",
    "unfurl",
    "preview card",
    # issue and PR numbers from the loop's own history (a two-digit
    # placeholder in a tool-call example is not one)
    r"#\d{3,}",
)
# What only the pipeline templates may not say: the concierge is the loop's
# own front desk and legitimately names its item ids and sandboxes.
PIPELINE_ANCHORS: tuple[str, ...] = (
    "sbxloop",
    "resume-pending",
    "gh:issue",
    "gh:7",
    "microVM",
    "doctor",
    "SQLite",
)


@pytest.mark.parametrize("name", sorted(RENDER_CONTEXTS))
def test_prompt_bodies_stay_domain_neutral(name: str) -> None:
    """#634: an example is a story the model pattern-matches against, and a
    story about the loop's own repository is the wrong anchor for every
    other one. No prompt body names an issue or PR number, a path or state
    name from this repository, or its chat bridge; the rules stand on their
    own phrasing, so any example can be swapped."""
    source = (resources.files(prompts) / f"{name}.md").read_text()
    body = _strip_contract_header(source)
    anchors = DOMAIN_ANCHORS + (PIPELINE_ANCHORS if name != "concierge" else ())
    for anchor in anchors:
        hit = re.search(anchor, body)
        assert hit is None, (
            f"{name}.md: {anchor!r} at {body[max(0, hit.start() - 60) : hit.end() + 60]!r}"
        )


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
    decompose = render(
        "decompose", outcome="o", max_tasks="3", project_gate="- gate rule", **EXAMPLE
    )
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
    base = render("decompose", outcome="o", max_tasks="3", project_gate="- gate rule", **EXAMPLE)
    retried = render(
        "decompose",
        outcome="o",
        max_tasks="3",
        project_gate="- gate rule",
        retry_context="TRY AGAIN",
        **EXAMPLE,
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


def test_decompose_scopes_verify_to_the_service_free_subset() -> None:
    """#682: a verify command that needs a service the sandbox lacks fails
    the same way on every attempt, so the planner is told to scope the
    exam to what runs without it and to say so."""
    text = render("decompose", outcome="o", max_tasks="3", project_gate="- gate rule", **EXAMPLE)
    assert "external services the sandbox does not have" in text
    assert "scope the\nverify command to the subset that runs without them" in text.replace(
        "  ", ""
    ) or "scope the verify command to the subset that runs without them" in " ".join(text.split())
    assert "say so in the task's description" in text


def test_review_prompt_carries_the_verification_note_when_given() -> None:
    """#682: what the sandbox's checks did not decide is a section of the
    review prompt under the gate, and nothing at all under `full`."""
    note = (
        'The operator set `verify_mode = "advisory"`: these checks failed\n'
        "- task t1: `pytest` (exit 1)"
    )
    text = render("review", **RENDER_CONTEXTS["review"], verification=note)
    assert note in text
    gate_at = text.index("## The project's own gate")
    rounds_at = text.index("## Earlier rounds")
    assert gate_at < text.index(note) < rounds_at
    plain = render("review", **RENDER_CONTEXTS["review"])
    assert "verify_mode" not in plain
    assert "$verification" not in plain


# -- the workload's prompts (#756) --------------------------------------------


def test_operator_plan_declares_needs_by_name() -> None:
    """The plan is where a task asks for the outside: hosts and credentials
    by name, never a value — the operator's box never holds a secret."""
    text = " ".join(render("operator_plan", **RENDER_CONTEXTS["operator_plan"]).split())
    assert text.startswith("# Plan a workload")
    assert "by name" in text and "never its value" in text
    assert "Declare it here" in text
    assert '"needs": {"hosts": [], "credentials": [], "sink": null, "repo": null}' in text
    assert "At most 3 tasks" in text


def test_operator_plan_shows_what_may_be_granted() -> None:
    """The profile's bounds (#758) reach the planner as their own section,
    with the rule that a need outside them ends the run before any task."""
    text = render("operator_plan", **RENDER_CONTEXTS["operator_plan"])
    assert "## What this run may ask for" in text
    assert "Profile `p`:\n- hosts: none" in text
    plain = " ".join(text.split())
    assert "a need outside it is refused and the run ends before any task runs" in plain


def test_operator_plan_makes_criteria_the_exam() -> None:
    text = render("operator_plan", **RENDER_CONTEXTS["operator_plan"])
    assert "the judge's whole exam" in text
    assert "acceptance_criteria" in text


def test_operator_execute_declares_result() -> None:
    """The executor's report is what the judge reads: it ends with a Result
    section that declares the outcome per criterion, and claims nothing."""
    text = render("operator_execute", **RENDER_CONTEXTS["operator_execute"])
    assert text.startswith("# Execute one task")
    assert "declare your result" in text.lower()
    assert "Do not claim" in text
    assert "## Result" in text
    assert "Task t1: T" in text and "- it is done" in text


def test_operator_execute_keeps_secrets_out_of_the_box() -> None:
    text = render("operator_execute", **RENDER_CONTEXTS["operator_execute"])
    assert "Credentials are never in this box" in text
    assert "never its value" in " ".join(text.split())
    # Host-tool sections ride in through the same seam as the build prompt.
    tooled = render(
        "operator_execute", **RENDER_CONTEXTS["operator_execute"], service_tools="\n\n## Tools"
    )
    assert tooled.rstrip().endswith("if\na criterion is not met, say so and why.")
    assert "## Tools" in tooled and "## Tools" not in text
    assert "$service_tools" not in text


def test_operator_judge_reads_and_never_repairs() -> None:
    text = render("operator_judge", **RENDER_CONTEXTS["operator_judge"])
    assert text.startswith("# Judge one task")
    assert "Do not modify anything" in text
    assert "a claim" in text
    assert "1. `Bash` `ls` — ok" in text and "## Result" in text
    assert "(attempt 1)" in text


def test_operator_judge_quotes_unmet_criteria() -> None:
    text = render("operator_judge", **RENDER_CONTEXTS["operator_judge"])
    assert "quote the criterion" in text
    assert '"passed": false' in text and '"unmet": [' in text
    retried = render(
        "operator_judge",
        **RENDER_CONTEXTS["operator_judge"],
        retry_context="## Previous attempt was invalid",
    )
    assert retried.rstrip().endswith("## Previous attempt was invalid")
