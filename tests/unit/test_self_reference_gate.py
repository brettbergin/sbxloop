"""The gate against sbxloop self-references in user-facing surfaces (#645),
the strings #635 cleaned so it passes, and CI's push filter (#643).

`scripts/check_self_references.py` is stdlib-only and runs from `make lint`
and the CI lint job; here it is imported from its path and pointed at
synthetic trees so every rule is exercised both ways — a clean surface
passes, a planted reference fails with `path:line: rule: text`."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_self_references.py"
ALLOWLIST = ROOT / "scripts" / "self-references.allow"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_self_references", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve the module by name
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The checker aimed at an empty synthetic tree under tmp_path; each
    test plants what it needs and re-points one surface at it."""
    module = _load()
    for name in ("prompts", "src", "worker", "cli", "data"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "ALLOWLIST", tmp_path / "allow")
    monkeypatch.setattr(module, "PROMPTS", tmp_path / "prompts")
    monkeypatch.setattr(module, "TEMPLATES", [])
    monkeypatch.setattr(module, "SRC", tmp_path / "src")
    monkeypatch.setattr(module, "WORKER_SRC", tmp_path / "worker")
    monkeypatch.setattr(module, "CONSOLE_MODULES", [])
    monkeypatch.setattr(module, "GATE_FILES", set())
    monkeypatch.setattr(module, "_tracked_files", list)
    return module


def _rendered(module: ModuleType) -> list[str]:
    findings, stale = module.run()
    assert stale == []
    return [f.render() for f in findings]


class TestTheTreeIsClean:
    def test_the_repository_passes_the_gate(self) -> None:
        """#635 done, #645 holding: no finding, no stale allowlist entry."""
        module = _load()
        findings, stale = module.run()
        assert [f.render() for f in findings] == []
        assert stale == []

    def test_the_script_exits_zero_on_the_tree(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT)], capture_output=True, text=True, cwd=ROOT
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_the_allowlist_holds_only_the_concierge_placeholders(self) -> None:
        """Every exception is a reviewed line, not an inline pragma; the
        only ones today are the concierge prompt's worked-example numbers."""
        entries = [
            line.split(" ", 1)
            for line in ALLOWLIST.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        assert {path for path, _ in entries} == {
            "packages/sbxloop/src/sbxloop/engine/prompts/concierge.md"
        }
        assert all(text.startswith("#") and len(text) <= 3 for _, text in entries)

    def test_no_inline_pragmas(self) -> None:
        assert "noqa" not in SCRIPT.read_text().split('"""', 2)[2].split("ALLOWLIST")[0]


class TestPromptBodies:
    def test_the_contract_header_is_not_a_surface_but_the_body_is(
        self, gate: ModuleType, tmp_path: Path
    ) -> None:
        (tmp_path / "prompts" / "build.md").write_text(
            "<!--\nContract for humans: see #225 and packages/sbxloop/x.py.\n-->\n"
            "# Body\n\nFixed in #641 — see packages/sbxloop/src/sbxloop/engine.\n"
            "A link https://github.com/o/r/issues/641 is fine, so is `#0e8a16`.\n"
        )
        assert _rendered(gate) == [
            "prompts/build.md:6: issue-ref in prompt: #641",
            "prompts/build.md:6: sbxloop path in prompt: packages/sbxloop",
            "prompts/build.md:6: sbxloop path in prompt: src/sbxloop",
        ]

    def test_a_prompt_without_a_header_is_read_from_line_one(
        self, gate: ModuleType, tmp_path: Path
    ) -> None:
        (tmp_path / "prompts" / "plain.md").write_text("First line mentions #12.\n")
        assert _rendered(gate) == ["prompts/plain.md:1: issue-ref in prompt: #12"]


class TestExceptionMessages:
    def test_raise_strings_are_surfaces_comments_and_docstrings_are_not(
        self, gate: ModuleType, tmp_path: Path
    ) -> None:
        (tmp_path / "src" / "mod.py").write_text(
            '"""Module docstring cites #46 freely."""\n'
            "\n"
            "\n"
            "def f(repo):\n"
            '    """Docstring, #46 again."""\n'
            "    # a comment about #46\n"
            '    raise ValueError(f"no workspace for {repo} (see #46)")\n'
            "\n"
            "\n"
            "def g():\n"
            '    raise RuntimeError("plain " "concat (#57)")\n'
            "\n"
            "\n"
            "def h():\n"
            '    msg = "not raised here, so not an exception surface #99"\n'
            "    return msg\n"
        )
        (tmp_path / "worker" / "w.py").write_text('raise SystemExit("worker too #250")\n')
        assert _rendered(gate) == [
            "src/mod.py:7: issue-ref in exception: #46",
            "src/mod.py:11: issue-ref in exception: #57",
            "worker/w.py:1: issue-ref in exception: #250",
        ]

    def test_vendored_code_is_skipped(self, gate: ModuleType, tmp_path: Path) -> None:
        (tmp_path / "src" / "_vendor").mkdir()
        (tmp_path / "src" / "_vendor" / "x.py").write_text('raise ValueError("#1")\n')
        assert _rendered(gate) == []


class TestConsoleText:
    def test_every_literal_but_docstrings(
        self, gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "cli" / "doctor.py"
        path.write_text(
            '"""Doctor. Tracks #250 here, which is fine."""\n'
            "ROWS = {\n"
            '    "python": "installs the pin through uv either way (#250)",\n'
            '    "url": "https://github.com/o/r/issues/250 expanded is fine",\n'
            "}\n"
            "\n"
            "\n"
            "def render():\n"
            '    """Renders (#122)."""\n'
            '    return f"page size guard (issue #122)"\n'
        )
        monkeypatch.setattr(gate, "CONSOLE_MODULES", [path])
        assert _rendered(gate) == [
            "cli/doctor.py:3: issue-ref in console text: #250",
            "cli/doctor.py:10: issue-ref in console text: #122",
        ]


class TestInitWrittenFiles:
    def test_comments_in_templates_count(
        self, gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        template = tmp_path / "data" / "sbxloop.toml.example"
        template.write_text(
            '# [agent]\n# Which SDK runs the agent (#533):\n# backend = "copilot"\n'
        )
        monkeypatch.setattr(gate, "TEMPLATES", [template])
        assert _rendered(gate) == [
            "data/sbxloop.toml.example:2: issue-ref in init-written file: #533"
        ]


class TestPersonalIdentifiers:
    def test_tracked_files_outside_the_excluded_dirs(
        self, gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        files = {
            "README.md": "Source: https://github.com/brettbergin/sbxloop — the project's own URL\n",
            "src/host.py": 'HOST = "db.comp.bergco.net"\nHOME = "/home/bergs"\n',
            "src/repo.py": 'REPO = "brettbergin/project-mountain-dew"\n',
            "docs/deploy.md": "ssh bergs@db.comp.bergco.net\n",
            "contrib/unit.service": "User=bergs\n",
            ".github/workflows/deploy.yml": "runs-on: db\n",
            "tests/fixture.py": 'login = "brettbergin"\n',
            "pyproject.toml": 'Homepage = "https://github.com/brettbergin/sbxloop"\n',
        }
        for rel, text in files.items():
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        # an untracked file with a leak: not the repository's problem
        (tmp_path / "scratch.txt").write_text("bergs was here\n")
        tracked = [tmp_path / rel for rel in files]
        monkeypatch.setattr(gate, "_tracked_files", lambda: tracked)
        assert _rendered(gate) == [
            "src/host.py:1: personal identifier: bergco",
            "src/host.py:2: personal identifier: /home/bergs",
            "src/repo.py:1: personal identifier: brettbergin",
            "src/repo.py:1: personal identifier: project-mountain-dew",
        ]


class TestAllowlist:
    def test_an_entry_covers_every_match_of_that_text_in_that_file(
        self, gate: ModuleType, tmp_path: Path
    ) -> None:
        (tmp_path / "prompts" / "c.md").write_text("Reply on #12.\nClose #12?\nAnd #41.\n")
        (tmp_path / "allow").write_text("# reviewed\nprompts/c.md #12\n")
        findings, stale = gate.run()
        assert [f.render() for f in findings] == ["prompts/c.md:3: issue-ref in prompt: #41"]
        assert stale == []

    def test_a_stale_entry_fails_the_gate(
        self, gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "allow").write_text("prompts/gone.md #12\n")
        findings, stale = gate.run()
        assert findings == [] and stale == [("prompts/gone.md", "#12")]
        assert gate.main() == 1
        assert "stale allowlist entry: prompts/gone.md #12" in capsys.readouterr().out

    def test_main_lists_findings_as_path_line_rule_text_and_exits_one(
        self, gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "src" / "m.py").write_text('raise ValueError("see #46")\n')
        assert gate.main() == 1
        captured = capsys.readouterr()
        assert captured.out.splitlines() == ["src/m.py:1: issue-ref in exception: #46"]
        assert "1 self-reference(s)" in captured.err
        assert "add a reviewed line to allow" in captured.err

    def test_main_is_silent_and_zero_when_clean(
        self, gate: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert gate.main() == 0
        assert capsys.readouterr() == ("", "")


class TestWiring:
    def test_make_lint_and_the_ci_lint_job_run_the_gate(self) -> None:
        makefile = (ROOT / "Makefile").read_text()
        lint_target = makefile.split("lint:", 1)[1].split("\n\n", 1)[0]
        assert "python3 scripts/check_self_references.py" in lint_target
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
        steps = workflow["jobs"]["lint"]["steps"]
        assert {"run": "python3 scripts/check_self_references.py"} in steps

    def test_ci_push_filter_is_main_alone(self) -> None:
        """#643: working branches are built through their pull request; a
        push filter matching them too would run every job twice per PR."""
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
        assert workflow[True]["push"] == {"branches": ["main"]}  # yaml reads `on` as True
        assert "pull_request" in workflow[True]
