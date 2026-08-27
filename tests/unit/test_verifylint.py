"""Verify-command lint: toolchain-convention rules keyed by language."""

from __future__ import annotations

import json
from pathlib import Path

from sbxloop.verifylint import (
    command_heads,
    gate_rule,
    lint_verify_commands,
    project_gate,
    runs_gate,
)


class TestCommandHeads:
    def test_simple_command(self) -> None:
        assert command_heads("pytest -q") == ["pytest"]

    def test_operators_start_new_positions(self) -> None:
        assert command_heads("cd app && pytest -q; echo done | grep done") == [
            "cd",
            "pytest",
            "echo",
            "grep",
        ]

    def test_env_prefix_skipped(self) -> None:
        assert command_heads("FOO=1 BAR=2 python x.py") == ["python"]

    def test_command_substitution_inspected(self) -> None:
        assert "pytest" in command_heads("echo $(pytest --version)")

    def test_pathed_invocations_keep_their_path(self) -> None:
        assert command_heads(".venv/bin/pytest -q") == [".venv/bin/pytest"]


class TestPythonRules:
    def test_bare_python_pytest_pip_flagged(self) -> None:
        problems = lint_verify_commands(
            ["python app.py", "python3 -m pytest -q", "pip install x", "pytest -q"],
            ["python"],
        )
        assert len(problems) == 4
        assert all(".venv/bin" in p for p in problems)

    def test_venv_paths_are_clean(self) -> None:
        assert (
            lint_verify_commands(
                [".venv/bin/python app.py", ".venv/bin/pytest -q", "test -f app.py"],
                ["python"],
            )
            == []
        )

    def test_bare_python_after_cd_flagged(self) -> None:
        problems = lint_verify_commands(["cd app && python -m pytest"], ["python"])
        assert len(problems) == 1

    def test_venv_bootstrap_feedback_says_move_setup_to_steps(self) -> None:
        """`python3 -m venv .venv && .venv/bin/pytest` has no compliant
        rewrite that keeps the setup half — the feedback must say the fix
        is moving setup into execution steps, or the model retries with
        the same shape and the run dies (field failure rhf9svssb)."""
        (problem,) = lint_verify_commands(
            ["python3 -m venv .venv && .venv/bin/pip install -q pytest && .venv/bin/pytest -q"],
            ["python"],
        )
        assert "execution steps" in problem
        assert ".venv/bin/pytest -q` alone" in problem

    def test_python_rules_inactive_for_other_languages(self) -> None:
        # a Go-only run may legitimately mention python in, say, a grep
        assert lint_verify_commands(["python tool.py"], ["go"]) == []

    def test_uv_run_is_clean_without_a_lockfile(self) -> None:
        # uv is provisioned with the toolchain now (#250), so `uv run` is
        # never the exec-time failure it used to be — no lockfile needed
        # to allow it.
        assert lint_verify_commands(["uv run pytest -q"], ["python"]) == []


class TestUvProjectRule:
    """#250: a `uv.lock` in the workspace flips the Python convention."""

    def test_venv_paths_flagged_with_uv_remedy(self) -> None:
        problems = lint_verify_commands(
            [".venv/bin/pytest -q", "cd app && app/.venv/bin/python check.py"],
            ["python"],
            uv_project=True,
        )
        assert len(problems) == 2
        for problem in problems:
            assert "uv.lock" in problem
            assert "uv run" in problem

    def test_bare_python_flagged_with_uv_remedy_not_venv(self) -> None:
        problems = lint_verify_commands(["pytest -q", "python3 -m x"], ["python"], uv_project=True)
        assert len(problems) == 2
        for problem in problems:
            assert "uv run pytest" in problem
            assert ".venv/bin/pytest" not in problem
        assert "uv sync` belongs in the plan's execution steps" in problems[0]

    def test_uv_run_and_non_python_commands_are_clean(self) -> None:
        assert (
            lint_verify_commands(
                [
                    "uv run pytest -q",
                    "uv run ruff check .",
                    "test -f uv.lock",
                    "git diff --exit-code",
                ],
                ["python"],
                uv_project=True,
            )
            == []
        )

    def test_lockfile_is_inert_without_the_python_toolchain(self) -> None:
        # A Go run whose workspace happens to carry a uv.lock (a polyglot
        # repo) must not start rejecting `.venv/bin/...` for a language it
        # never configured.
        assert lint_verify_commands([".venv/bin/pytest"], ["go"], uv_project=True) == []

    def test_lockfile_absent_keeps_the_venv_convention(self) -> None:
        assert lint_verify_commands([".venv/bin/pytest -q"], ["python"], uv_project=False) == []
        problems = lint_verify_commands(["pytest -q"], ["python"], uv_project=False)
        assert len(problems) == 1 and ".venv/bin/pytest" in problems[0]

    def test_other_language_rules_unaffected(self) -> None:
        problems = lint_verify_commands(["rspec spec/"], ["python", "ruby"], uv_project=True)
        assert len(problems) == 1 and "bundle exec" in problems[0]


class TestRubyAndPhpRules:
    def test_bare_rspec_flagged_bundle_exec_clean(self) -> None:
        problems = lint_verify_commands(["rspec spec/"], ["ruby"])
        assert len(problems) == 1 and "bundle exec" in problems[0]
        assert lint_verify_commands(["bundle exec rspec spec/"], ["ruby"]) == []

    def test_bare_phpunit_flagged_vendor_path_clean(self) -> None:
        problems = lint_verify_commands(["phpunit tests/"], ["php"])
        assert len(problems) == 1 and "vendor/bin" in problems[0]
        assert (
            lint_verify_commands(
                ["composer install --no-interaction && ./vendor/bin/phpunit"], ["php"]
            )
            == []
        )


class TestBareIsCorrectElsewhere:
    def test_go_rust_dotnet_node_commands_are_clean(self) -> None:
        commands = [
            "go build ./... && go test ./...",
            "cargo test",
            "dotnet test",
            "npm ci && npm test",
            "npx tsc --noEmit",
            "cmake -S . -B build && ctest --test-dir build",
        ]
        languages = ["go", "rust", "dotnet", "javascript", "typescript", "cpp"]
        assert lint_verify_commands(commands, languages) == []


class TestMutationRules:
    def test_sudo_and_apt_flagged_for_any_language(self) -> None:
        for language in ("python", "go", "rust"):
            problems = lint_verify_commands(
                ["sudo apt-get install -y thing && go test ./..."], [language]
            )
            assert problems and "must not modify the environment" in problems[0]


class TestNetworkRules:
    """#440: a verify command judges the workspace, not the network — a
    rate limit or a flake failed a review task whose local deliverable was
    present and valid."""

    def test_gh_is_always_flagged(self) -> None:
        problems = lint_verify_commands(["gh pr view 429 --json body | grep -q ."], [])
        assert problems and "not the network" in problems[0]

    def test_remote_curl_and_wget_are_flagged(self) -> None:
        for command in ("curl -fsS https://api.github.com/user", "wget https://example.com/x"):
            problems = lint_verify_commands([command], [])
            assert problems and "not the network" in problems[0], command

    def test_local_curl_is_clean(self) -> None:
        """Probing a server the command itself started is a documented
        pattern; only remote addresses are out of bounds."""
        clean = [
            "curl -fsS http://localhost:8000/status",
            "curl -fsS http://127.0.0.1:9090/healthz",
        ]
        for command in clean:
            assert lint_verify_commands([command], []) == [], command

    def test_project_local_installs_stay_legal(self) -> None:
        # npm ci / composer install are the ecosystem-prescribed verify
        # preambles; only system-level mutation is out of bounds.
        assert lint_verify_commands(["npm ci && npm test"], ["javascript"]) == []


class TestBashisms:
    """Verify commands run under `sh -c`; bash-only syntax silently means
    something else there (field failure re59gj4vq: `grep -q $'\\033[31m'`
    matched nothing under sh, and the executor could not escape the
    verify→revise loop because it may not edit the command)."""

    def test_ansi_c_quoting_flagged_with_printf_rewrite(self) -> None:
        (problem,) = lint_verify_commands(
            [".venv/bin/python app.py --color red | grep -q $'\\033[31m'"], ["python"]
        )
        assert "ANSI-C quoting" in problem and "`sh -c`" in problem
        assert "printf" in problem

    def test_other_bashisms_flagged(self) -> None:
        for cmd in (
            "[[ -f app.py ]]",
            'grep x <<< "$out"',
            "pushd app && make",
            "source .venv/bin/activate && pytest",
            "cd app && declare -a xs",
        ):
            assert lint_verify_commands([cmd], ["go"]), cmd

    def test_nested_shell_wrapper_flagged(self) -> None:
        """Field failure r7ef26eht (first sbxloop-on-sbxloop run): the plan
        wrapped a `git status | awk '{print $2}'` guard in `sh -c "..."`;
        the runner's own `sh -c` expanded `$2` first, awk printed whole
        lines, and a correct change failed every revision and the replan."""
        wrapped = (
            "sh -c \"git status --porcelain | awk '{print $2}' | grep -vE '^(README.md)$' "
            '| grep . && exit 1 || exit 0"'
        )
        (problem,) = lint_verify_commands([wrapped], ["python"])
        assert "nested `sh -c`" in problem and "write the pipeline directly" in problem
        for cmd in (
            "bash -c 'make test'",
            'cd app && /bin/sh -c "go test ./..."',
            "sh -lc 'npm test'",
        ):
            assert lint_verify_commands([cmd], ["go"]), cmd
        # the unwrapped pipeline is fine — that is the remedy
        assert (
            lint_verify_commands(
                [
                    "git status --porcelain | awk '{print $2}' "
                    "| grep -vE '^(README.md)$' | (! grep .)"
                ],
                ["python"],
            )
            == []
        )
        # `sh` as data or as a script interpreter (no -c) is not a wrapper
        assert (
            lint_verify_commands(["sh scripts/check.sh", "grep -c 'sh -c' notes.md"], ["go"]) == []
        )

    def test_an_inert_sh_c_wrapper_is_unwrapped_not_rejected(self) -> None:
        """Field failure (item gh:478, runs rv2y1a8ke and rq826h546): the
        planner wrapped two checks as `sh -c 'exit 0'` and `sh -c 'git diff
        --quiet && git diff --cached --quiet'`. Both were rejected as nested
        shells, decompose was declared invalid twice, and the item was
        abandoned — so PR #476 was never reviewed at all.

        Neither could misbehave. A single-quoted payload with no `$` and no
        backtick is handed through by the outer shell verbatim, so it runs
        exactly as it would unwrapped. The rule's own remedy ("write the
        pipeline directly") produces the same bytes.
        """
        for inert in (
            "sh -c 'exit 0'",
            "sh -c 'git diff --quiet && git diff --cached --quiet'",
            "/bin/sh -c 'test -f README.md'",
        ):
            assert lint_verify_commands([inert], ["python"]) == [], inert

    def test_the_wrapper_cannot_hide_its_payload_from_the_other_rules(self) -> None:
        """Unwrapping, not waving through. Single-quoted spans are blanked
        before the toolchain and mutating-command scans, so a wrapper
        accepted whole would smuggle past exactly the checks that catch the
        expensive mistakes — a bigger hole than the one this closes."""
        (problem,) = lint_verify_commands(["sh -c 'pytest -q'"], ["python"])
        assert "bare `pytest`" in problem
        (mutating,) = lint_verify_commands(["sh -c 'apt-get install -y jq'"], ["python"])
        assert "must not modify the environment" in mutating
        (network,) = lint_verify_commands(["sh -c 'gh pr view 1'"], ["python"])
        assert "judge the workspace, not the network" in network

    def test_only_the_provably_identical_wrapper_is_unwrapped(self) -> None:
        """Everything else can change what runs: `bash`/`dash`/`zsh` may not
        be installed, `-l` rewrites the environment, and a double-quoted or
        unquoted payload is expanded by the OUTER shell first — which is
        r7ef26eht, the failure the rule was written for."""
        for hazard in (
            "bash -c 'make test'",
            "sh -lc 'npm test'",
            'sh -c "go test ./..."',
            "sh -c 'awk \"{print $2}\" out.txt'",
            "sh -c 'echo `date`'",
        ):
            assert lint_verify_commands([hazard], ["go"]), hazard

    def test_a_gate_inside_an_inert_wrapper_still_counts_as_running_it(self) -> None:
        """The gate check reads the command text, so it has to see through
        the wrapper too — otherwise unwrapping trades one false rejection
        for another."""
        assert lint_verify_commands(["sh -c 'make check'"], ["python"], gate="make check") == []

    def test_bashism_words_as_data_are_not_flagged(self) -> None:
        """Command-like bashisms count only in command position, operator-like
        ones only outside quotes — a portable command that merely *mentions*
        them as data must not be rejected (review: grep -F 'source' file,
        test -f source/output, printf '%s' '[[literal]]')."""
        assert (
            lint_verify_commands(
                [
                    "grep -F 'source' file",
                    "test -f source/output",
                    "printf '%s' '[[literal]]'",
                    "echo '<<<'",
                    'echo "cost is $x"',
                    "grep -c local README.md",
                ],
                ["python", "go"],
            )
            == []
        )

    def test_feedback_distinguishes_silent_from_loud_failure(self) -> None:
        (ansi,) = lint_verify_commands(["grep -q $'\\033'"], ["go"])
        assert "silently reinterprets" in ansi
        (loud,) = lint_verify_commands(["[[ -f x ]]"], ["go"])
        assert "unknown command" in loud
        (syntax,) = lint_verify_commands(['grep x <<< "$y"'], ["go"])
        assert "syntax error" in syntax

    def test_portable_commands_clean(self) -> None:
        assert (
            lint_verify_commands(
                [
                    "test -f app.py",
                    "[ -f app.py ] && echo ok",
                    "printf '%s\\n' hi | grep -q hi",
                    ".venv/bin/pytest -q",
                    "cd app && go test ./...",
                ],
                ["python", "go"],
            )
            == []
        )

    def test_bashism_and_bare_python_both_reported(self) -> None:
        problems = lint_verify_commands(["python -c 'x' | grep -q $'\\033'"], ["python"])
        assert len(problems) == 2


class TestMultiLanguageRuns:
    def test_rules_union_across_configured_languages(self) -> None:
        problems = lint_verify_commands(
            ["pytest -q", "rspec spec/", "go test ./..."], ["python", "ruby", "go"]
        )
        assert len(problems) == 2  # go test is fine; the other two flagged


class TestProjectGate:
    """A delivered PR (#389) failed `mdformat` and `security` — both plain
    `make check` targets — because the plan's verify commands were a subset
    of what the repository actually enforces. The run reported success and
    the PR sat red. Rejecting that at JSON acceptance costs one retry; the
    alternative costs a PR, a review round and a human noticing.
    """

    def test_a_check_target_is_the_gate(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("check: lint typecheck test\n\t@echo ok\n")
        assert project_gate(tmp_path) == "make check"

    def test_a_makefile_without_a_check_target_declares_no_gate(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("build:\n\t@echo ok\ntest:\n\t@echo ok\n")
        assert project_gate(tmp_path) is None

    def test_no_makefile_no_gate(self, tmp_path: Path) -> None:
        """A project that declares no gate gets no requirement invented for
        it — a wrong requirement is unfixable by the executor, which cannot
        edit verify commands."""
        assert project_gate(tmp_path) is None
        assert project_gate(None) is None

    def test_the_first_makefile_make_would_read_is_the_one_read(self, tmp_path: Path) -> None:
        """GNU make reads only the first of GNUmakefile/makefile/Makefile, so
        a `check` in a later one is not the gate make would run."""
        (tmp_path / "GNUmakefile").write_text("build:\n\t@echo ok\n")
        (tmp_path / "Makefile").write_text("check:\n\t@echo ok\n")
        assert project_gate(tmp_path) is None

    def test_a_lowercase_makefile_counts(self, tmp_path: Path) -> None:
        (tmp_path / "makefile").write_text("check:\n\t@echo ok\n")
        assert project_gate(tmp_path) == "make check"

    def test_an_unreadable_makefile_is_not_fatal(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").mkdir()  # a directory, not a file
        assert project_gate(tmp_path) is None


class TestRunsGate:
    def test_the_gate_is_matched_token_exact(self) -> None:
        assert runs_gate("make check", "make check")
        assert runs_gate("make -j4 check", "make check")
        assert runs_gate("make check lint", "make check")

    def test_a_similarly_named_target_is_not_the_gate(self) -> None:
        assert not runs_gate("make check-fast", "make check")
        assert not runs_gate("make precheck", "make check")
        assert not runs_gate("make lint", "make check")

    def test_an_unparseable_command_does_not_raise(self) -> None:
        assert not runs_gate('make "unbalanced', "make check")


class TestGateRule:
    def test_skipping_the_gate_is_rejected(self) -> None:
        problems = lint_verify_commands(
            ["uv run pytest -q"], ["python"], uv_project=True, gate="make check"
        )
        assert len(problems) == 1
        assert "make check" in problems[0] and "gate" in problems[0]

    def test_running_the_gate_satisfies_it(self) -> None:
        assert lint_verify_commands(["make check"], ["python"], gate="make check") == []

    def test_narrower_commands_may_ride_along(self) -> None:
        """A fast signal alongside the gate is fine — the rule is that the
        gate is present, not that it is alone."""
        commands = ["uv run pytest -q", "make check"]
        assert lint_verify_commands(commands, ["python"], uv_project=True, gate="make check") == []

    def test_no_gate_no_requirement(self) -> None:
        assert lint_verify_commands(["uv run pytest -q"], ["python"], uv_project=True) == []

    def test_the_gate_rule_stacks_with_the_toolchain_rules(self) -> None:
        """A command can be wrong in more than one way, and the model should
        hear about both in one retry rather than one per round."""
        problems = lint_verify_commands(
            ["pytest -q"], ["python"], uv_project=True, gate="make check"
        )
        assert len(problems) == 2
        assert any("bare `pytest`" in p for p in problems)
        assert any("make check" in p for p in problems)


class TestGateDetectionAcrossEcosystems:
    """The gate is per-ecosystem knowledge, like LANGUAGE_RULES above.

    Detecting only one project's convention would silently switch the whole
    PR-validity guarantee off for every repository that does something else —
    it would look like a feature and be a no-op.
    """

    def test_a_makefile_target(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("check: lint\n\t@echo\n")
        assert project_gate(tmp_path) == "make check"

    def test_ci_counts_as_a_gate_too(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("build:\n\t@echo\nci:\n\t@echo\n")
        assert project_gate(tmp_path) == "make ci"

    def test_test_alone_is_not_a_gate(self, tmp_path: Path) -> None:
        """`check` and `ci` name the whole gate by convention; `test` is one
        part of it, and demanding it would let a lint-failing PR through
        while looking satisfied."""
        (tmp_path / "Makefile").write_text("test:\n\t@echo\n")
        assert project_gate(tmp_path) is None

    def test_a_justfile(self, tmp_path: Path) -> None:
        (tmp_path / "justfile").write_text("check:\n    echo hi\n")
        assert project_gate(tmp_path) == "just check"

    def test_a_taskfile(self, tmp_path: Path) -> None:
        (tmp_path / "Taskfile.yml").write_text(
            "version: '3'\ntasks:\n  ci:\n    cmds:\n      - echo\n"
        )
        assert project_gate(tmp_path) == "task ci"

    def test_an_npm_script(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"check": "vitest"}}))
        assert project_gate(tmp_path) == "npm run check"

    def test_tox_and_nox_are_their_own_gate(self, tmp_path: Path) -> None:
        (tmp_path / "tox.ini").write_text("[tox]\n")
        assert project_gate(tmp_path) == "tox"
        other = tmp_path / "n"
        other.mkdir()
        (other / "noxfile.py").write_text("import nox\n")
        assert project_gate(other) == "nox"

    def test_a_project_declaring_nothing_is_held_to_nothing(self, tmp_path: Path) -> None:
        """A guessed requirement is worse than none: the executor cannot edit
        verify commands, so a gate we invented is unsatisfiable."""
        assert project_gate(tmp_path) is None

    def test_malformed_declarations_do_not_raise(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{{{ not json")
        assert project_gate(tmp_path) is None

    def test_the_operator_override_wins(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("check:\n\t@echo\n")
        assert project_gate(tmp_path, "cargo make ci") == "cargo make ci"

    def test_an_empty_override_switches_the_requirement_off(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("check:\n\t@echo\n")
        assert project_gate(tmp_path, "") is None


class TestGateMatchingAcrossEcosystems:
    def test_flags_and_extra_arguments_are_fine(self) -> None:
        assert runs_gate("npm run check --silent", "npm run check")
        assert runs_gate("make -j4 check lint", "make check")
        assert runs_gate("tox -e py313", "tox")

    def test_a_similarly_named_target_is_not_the_gate(self) -> None:
        assert not runs_gate("npm run check-fast", "npm run check")
        assert not runs_gate("just precheck", "just check")

    def test_a_single_word_gate_needs_only_its_program(self) -> None:
        assert runs_gate("tox", "tox")
        assert runs_gate("nox", "nox")
        assert not runs_gate("pytest", "tox")


class TestGateRuleText:
    def test_it_names_the_projects_actual_gate(self) -> None:
        """A template that named one convention would be wrong everywhere
        else — and would teach the model to invent it."""
        assert "just ci" in gate_rule("just ci")
        assert "make check" not in gate_rule("just ci")

    def test_no_gate_says_so_rather_than_demanding_one(self) -> None:
        assert "no single gate" in gate_rule(None)
