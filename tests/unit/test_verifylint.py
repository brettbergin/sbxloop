"""Verify-command lint: toolchain-convention rules keyed by language."""

from __future__ import annotations

from sbxloop.verifylint import command_heads, lint_verify_commands


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
