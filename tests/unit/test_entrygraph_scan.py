"""The shipped sandbox script is exercised with inert GitPython/entrygraph APIs."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sbxloop.data import entrygraph_scan as scan


@dataclass
class Symbol:
    qname: str
    file: str | None
    start_line: int


@dataclass
class Edge:
    line: int
    confidence: int


@dataclass
class Finding:
    symbols: tuple[Symbol, ...]
    edges: tuple[Edge, ...]
    severity: str = "high"
    sink_category: str = "command_exec"
    taint_verified: bool | None = None


@dataclass
class Stats:
    files: int = 2
    symbols: int = 3
    edges: int = 1
    entrypoints: int = 1


@dataclass
class Detection:
    languages: tuple[dict[str, Any], ...] = ({"name": "example", "percent": 100},)
    frameworks: tuple[dict[str, Any], ...] = ()


class Findings(list[Finding]):
    mode = "widened"
    truncated = True


@pytest.fixture
def sandbox_apis(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "checkout"
    root.mkdir()
    output = tmp_path / "output"
    state = SimpleNamespace(
        root=root,
        output=output,
        sha="a" * 40,
        origin="https://example.com/team/project.git",
        clones=[],
        indexes=[],
        queries=[],
        dirty=False,
        real_git=importlib.import_module("git"),
        failure=None,
        findings=Findings(
            [
                Finding(
                    symbols=(
                        Symbol("handler", "routes/main.txt", 8),
                        Symbol("danger", None, 0),
                    ),
                    edges=(Edge(13, 1),),
                )
            ]
        ),
    )

    class Repo:
        def __init__(self, path: str | Path, **kwargs: Any) -> None:
            self.working_tree_dir = str(Path(path).resolve())
            self.bare = False
            self.head = SimpleNamespace(commit=SimpleNamespace(hexsha=state.sha))
            self.remotes = SimpleNamespace(origin=SimpleNamespace(url=state.origin))

        @classmethod
        def clone_from(cls, url: str, path: Path, **kwargs: Any) -> Repo:
            state.clones.append((url, path, kwargs))
            path.mkdir()
            return cls(path)

        def is_dirty(self, **kwargs: Any) -> bool:
            return bool(state.dirty)

        def close(self) -> None:
            pass

    class Graph:
        @classmethod
        def index(cls, root: Path, db: Path, **kwargs: Any) -> Graph:
            state.indexes.append((root, db, kwargs))
            if state.failure:
                raise state.failure
            db.write_text("inert index")
            return cls()

        def __enter__(self) -> Graph:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def stats(self) -> Stats:
            return Stats()

        def detect(self) -> Detection:
            return Detection()

        def entrypoints(self) -> list[dict[str, Any]]:
            return [
                {
                    "kind": "http_route",
                    "framework": "example",
                    "route": "/<danger>`\n",
                    "http_method": "GET",
                    "symbol": Symbol("handler", "routes/main.txt", 8),
                    "parameters": [],
                }
            ]

        def paths(self, **kwargs: Any) -> Findings:
            state.queries.append(kwargs)
            return state.findings

    monkeypatch.setitem(sys.modules, "git", SimpleNamespace(Repo=Repo))
    monkeypatch.setitem(
        sys.modules, "entrygraph", SimpleNamespace(CodeGraph=Graph, __version__="0.1.134")
    )
    return state


def test_report_preserves_evidence_and_incomplete_search(sandbox_apis: SimpleNamespace) -> None:
    s = sandbox_apis
    assert scan.main(["--repo", str(s.root), "--output", str(s.output)]) == 0
    report = json.loads((s.output / "report.json").read_text())
    assert report["source"]["revision"] == "a" * 40
    assert report["tool"]["version"] == "0.1.134"
    assert report["paths"][0]["symbols"][0]["file"] == "routes/main.txt"
    assert report["search"]["mode"] == "widened"
    assert report["search"]["truncated"] is True
    assert s.queries == [
        {"source_category": "all", "sink_category": "all", "max_paths": 100, "max_depth": 25}
    ]
    markdown = (s.output / "report.md").read_text()
    assert "routes/main.txt:13" in markdown
    assert "unknown" in markdown.lower()
    assert "incomplete" in markdown.lower()
    assert "not proof" in markdown.lower()
    assert "<danger>" not in markdown
    assert scan.main(["--check", "--output", str(s.output)]) == 0
    assert len(s.indexes) == 1
    assert not s.indexes[0][1].is_relative_to(s.output)
    assert sorted(path.name for path in s.output.iterdir()) == ["report.json", "report.md"]
    assert not s.indexes[0][1].exists()


def test_empty_result_keeps_no_safety_inference(sandbox_apis: SimpleNamespace) -> None:
    s = sandbox_apis
    s.findings = Findings()
    scan.main(["--repo", str(s.root), "--output", str(s.output)])
    text = (s.output / "report.md").read_text()
    assert "No source-to-sink paths" in text
    assert "does not establish" in text
    assert "incomplete" in text.lower()


def test_index_error_cannot_leave_a_success_report(sandbox_apis: SimpleNamespace) -> None:
    s = sandbox_apis
    s.failure = RuntimeError("parser unavailable")
    with pytest.raises(RuntimeError, match="parser unavailable"):
        scan.main(["--repo", str(s.root), "--output", str(s.output)])
    assert not (s.output / "report.json").exists()


def test_missing_checkout_fails_before_indexing(sandbox_apis: SimpleNamespace) -> None:
    s = sandbox_apis
    with pytest.raises(ValueError, match="checkout"):
        scan.main(["--repo", str(s.root / "missing"), "--output", str(s.output)])
    assert not s.indexes


@pytest.mark.parametrize(
    "url",
    [
        "file:///private/repo",
        "ssh://example.com/team/repo",
        "https://user:secret@example.com/team/repo",
        "https://example.com/team/repo?token=secret",
        "https://example.com/team/repo#fragment",
        "https://example.com/team/repo\n",
    ],
)
def test_url_rejection_precedes_clone(sandbox_apis: SimpleNamespace, url: str) -> None:
    s = sandbox_apis
    with pytest.raises(ValueError, match="HTTPS"):
        scan.main(["--repo", str(s.root / "new"), "--url", url, "--output", str(s.output)])
    assert not s.clones
    assert not s.indexes


def test_clone_uses_gitpython_without_ambient_auth(
    sandbox_apis: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = sandbox_apis
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.extraHeader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Authorization: secret")
    monkeypatch.setenv("GIT_ASKPASS", "untrusted-helper")
    root = s.root / "new"
    scan.main(["--repo", str(root), "--url", s.origin, "--output", str(s.output)])
    assert len(s.clones) == 1
    _, _, kwargs = s.clones[0]
    env = kwargs["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_GLOBAL"]
    assert env["GIT_CONFIG_SYSTEM"]
    assert "Authorization: secret" not in env.values()
    assert "untrusted-helper" not in env.values()
    assert kwargs["kill_after_timeout"] == 600
    assert kwargs["depth"] == 1


def test_existing_url_clone_is_reused_without_fetch(sandbox_apis: SimpleNamespace) -> None:
    s = sandbox_apis
    scan.main(["--repo", str(s.root), "--url", s.origin, "--output", str(s.output)])
    scan.main(["--repo", str(s.root), "--url", s.origin, "--output", str(s.output)])
    assert not s.clones


def test_wrong_origin_is_never_overwritten(sandbox_apis: SimpleNamespace) -> None:
    s = sandbox_apis
    with pytest.raises(ValueError, match="origin"):
        scan.main(
            [
                "--repo",
                str(s.root),
                "--url",
                "https://example.com/other/repo",
                "--output",
                str(s.output),
            ]
        )
    assert not s.clones
    assert not s.indexes


def test_retry_cannot_change_completed_source_revision(sandbox_apis: SimpleNamespace) -> None:
    s = sandbox_apis
    scan.main(["--repo", str(s.root), "--output", str(s.output)])
    s.sha = "b" * 40
    with pytest.raises(ValueError, match="revision"):
        scan.main(["--repo", str(s.root), "--output", str(s.output)])
    assert json.loads((s.output / "report.json").read_text())["source"]["revision"] == "a" * 40


def test_verify_rejects_changed_markdown(sandbox_apis: SimpleNamespace) -> None:
    s = sandbox_apis
    scan.main(["--repo", str(s.root), "--output", str(s.output)])
    (s.output / "report.md").write_text("Everything is safe.")
    with pytest.raises(ValueError, match="report"):
        scan.main(["--check", "--output", str(s.output)])


def test_verify_requires_complete_schema(tmp_path: Path) -> None:
    (tmp_path / "report.json").write_text('{"source": {"revision": "abc"}}')
    (tmp_path / "report.md").write_text("incomplete")
    with pytest.raises(ValueError, match="report"):
        scan.main(["--check", "--output", str(tmp_path)])


def test_configured_source_preserves_owner(sandbox_apis: SimpleNamespace) -> None:
    s = sandbox_apis
    scan.main(["--repo", str(s.root), "--source", "owner/project", "--output", str(s.output)])
    assert json.loads((s.output / "report.json").read_text())["source"]["repository"] == (
        "owner/project"
    )


def test_valid_https_port_is_supported(sandbox_apis: SimpleNamespace) -> None:
    s = sandbox_apis
    s.origin = "https://example.com:8443/team/project.git"
    scan.main(["--repo", str(s.root), "--url", s.origin, "--output", str(s.output)])


def test_false_verdict_does_not_claim_the_path_is_refuted(sandbox_apis: SimpleNamespace) -> None:
    s = sandbox_apis
    s.findings[0].taint_verified = False
    scan.main(["--repo", str(s.root), "--output", str(s.output)])
    assert "reachable, but no data flow observed" in (s.output / "report.md").read_text()


@pytest.mark.slow
def test_real_gitpython_reports_sha_and_refuses_untracked_analysis_input(
    sandbox_apis: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = sandbox_apis
    monkeypatch.setitem(sys.modules, "git", s.real_git)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    for args in [
        ["init", "--initial-branch=main"],
        [
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
    ]:
        subprocess.run(["git", *args], cwd=s.root, env=env, check=True, capture_output=True)
    scan.main(["--repo", str(s.root), "--output", str(s.output)])
    with s.real_git.Repo(s.root) as repository:
        expected = repository.head.commit.hexsha
    assert json.loads((s.output / "report.json").read_text())["source"]["revision"] == expected
    (s.root / "untracked.txt").write_text("Uncommitted analysis input")
    with pytest.raises(ValueError, match="checkout"):
        scan.main(["--repo", str(s.root), "--output", str(s.output)])
