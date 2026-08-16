"""Scheduled area audits: charters in the repo, issues on GitHub."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.daemon.audits import (
    Charter,
    audit_marker,
    due_charters,
    issue_body,
    load_charters,
    parse_charter,
    parse_every,
)
from tests.unit.test_daemon_loop import FakeSource, Harness, RecordingFrontend
from tests.unit.test_hostgit import make_repo


def write_charter(root: Path, name: str, every: str = "7d", enabled: str | None = None) -> Path:
    folder = root / ".github" / "sbxloop" / "audits"
    folder.mkdir(parents=True, exist_ok=True)
    meta = f"every: {every}\n" + (f"enabled: {enabled}\n" if enabled else "")
    path = folder / f"{name}.md"
    path.write_text(f"---\n{meta}---\n# Audit {name}\n\nLook at the thing.\n")
    return path


class TestParsing:
    def test_every_units(self) -> None:
        assert parse_every("7d") == 7 * 86400
        assert parse_every("12h") == 12 * 3600
        assert parse_every("30m") == 1800
        assert parse_every("0d") == 0
        with pytest.raises(ValueError, match="unrecognised interval"):
            parse_every("weekly")

    def test_charter_front_matter_and_body(self, tmp_path: Path) -> None:
        path = write_charter(tmp_path, "guardrails", "7d")
        c = parse_charter(path, ".github/sbxloop/audits/guardrails.md")
        assert c.name == "guardrails" and c.every_s == 7 * 86400 and c.enabled
        assert c.body.startswith("# Audit guardrails")
        assert c.title == "audit: guardrails" and c.rel == ".github/sbxloop/audits/guardrails.md"
        off = parse_charter(write_charter(tmp_path, "later", "1d", enabled="false"))
        assert not off.enabled

    def test_bad_charters_are_problems_not_crashes(self, tmp_path: Path) -> None:
        write_charter(tmp_path, "ok")
        folder = tmp_path / ".github" / "sbxloop" / "audits"
        (folder / "Bad Name.md").write_text("---\nevery: 1d\n---\nx\n")
        (folder / "nofm.md").write_text("# no front matter\n")
        (folder / "empty.md").write_text("---\nevery: 1d\n---\n")
        charters, problems = load_charters(tmp_path)
        assert [c.name for c in charters] == ["ok"]
        assert len(problems) == 3
        assert load_charters(tmp_path / "nowhere") == ([], [])

    def test_due_and_body(self, tmp_path: Path) -> None:
        a = parse_charter(write_charter(tmp_path, "a", "1d"))
        b = parse_charter(write_charter(tmp_path, "b", "1d"))
        off = parse_charter(write_charter(tmp_path, "c", "1d", enabled="no"))
        due = due_charters([a, b, off], {"a": 1000.0}, now=1000.0 + 3600)
        assert [c.name for c in due] == ["b"]  # a filed an hour ago, c disabled
        assert [c.name for c in due_charters([a], {"a": 1000.0}, now=1000.0 + 86400)] == ["a"]
        body = issue_body(a, audit_marker("a"))
        assert body.startswith("# Audit a") and "every 1d" in body
        assert body.endswith("<!-- sbxloop-audit a -->") and "`a.md`" in body


class SchedulingSource(FakeSource):
    name = "github"

    def __init__(self) -> None:
        super().__init__()
        self.filed: list[tuple[str, str]] = []
        self.open_titles: set[str] = set()
        self.recent_titles: set[str] = set()
        self.state_calls = 0
        self.fail = False

    def audit_issue_state(self, title: str, since_iso: str) -> tuple[bool, bool]:
        self.state_calls += 1
        if self.fail:
            raise RuntimeError("github down")
        return title in self.open_titles, title in self.recent_titles

    def file_audit(self, title: str, body: str) -> str:
        self.filed.append((title, body))
        return f"gh:{700 + len(self.filed)}"


def harness(tmp_path: Path, **daemon: Any) -> tuple[Harness, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = make_repo(tmp_path)
    cfg = Config.model_validate(
        {
            "state_dir": str(tmp_path / "state"),
            "github": {"repo": "o/r"},
            "sandbox": {"workspace": str(repo)},
            "daemon": {"audits": True, **daemon},
        }
    )
    h = Harness(tmp_path, cfg)
    h.source = SchedulingSource()
    h.loop.sources = [h.source]
    return h, repo


class TestScheduling:
    def test_due_charter_is_filed_once_then_waits_its_interval(self, tmp_path: Path) -> None:
        h, repo = harness(tmp_path)
        write_charter(repo, "guardrails", "1d")
        h.loop.tick()
        assert [t for t, _ in h.source.filed] == ["audit: guardrails"]  # type: ignore[attr-defined]
        assert "<!-- sbxloop-audit guardrails -->" in h.source.filed[0][1]  # type: ignore[attr-defined]
        h.loop.tick()  # cache says filed just now → nothing
        assert len(h.source.filed) == 1  # type: ignore[attr-defined]
        h.clock.t += 86400 + 1
        h.loop.tick()
        assert len(h.source.filed) == 2  # type: ignore[attr-defined]

    def test_github_is_the_source_of_truth(self, tmp_path: Path) -> None:
        """A wiped state dir must not double-file, and a still-open audit is
        never re-opened on top of itself."""
        h, repo = harness(tmp_path)
        write_charter(repo, "flakes", "0d")
        h.source.open_titles.add("audit: flakes")  # type: ignore[attr-defined]
        h.loop.tick()
        assert h.source.filed == []  # type: ignore[attr-defined]
        h.source.open_titles.clear()  # type: ignore[attr-defined]
        h.source.recent_titles.add("audit: flakes")  # type: ignore[attr-defined]
        h.dstore.record_audit("flakes", "gh:existing", 0.0)  # stale cache → asks GitHub
        h.clock.t += 10
        h.loop.tick()
        assert h.source.filed == []  # type: ignore[attr-defined]

    def test_gates_and_failures(self, tmp_path: Path) -> None:
        h, repo = harness(tmp_path, audits=True)
        write_charter(repo, "x", "0d")
        h.loop.pause()
        h.loop.tick()
        assert h.source.filed == []  # type: ignore[attr-defined]  # paused: no new work
        h.loop.unpause()
        h.source.fail = True  # type: ignore[attr-defined]
        h.loop.tick()  # GitHub hiccup: skipped, not raised
        assert h.source.filed == []  # type: ignore[attr-defined]
        h.source.fail = False  # type: ignore[attr-defined]
        h.loop.tick()
        assert len(h.source.filed) == 1  # type: ignore[attr-defined]
        # disabled by config → nothing, even with due charters
        h2, repo2 = harness(tmp_path / "b", audits=False)
        write_charter(repo2, "y", "0d")
        h2.loop.tick()
        assert h2.source.filed == []  # type: ignore[attr-defined]

    def test_broken_charter_is_reported_once_and_others_still_run(self, tmp_path: Path) -> None:
        h, repo = harness(tmp_path)
        front = RecordingFrontend()
        h.loop.frontend = front  # type: ignore[assignment]
        write_charter(repo, "good", "0d")
        (repo / ".github" / "sbxloop" / "audits" / "bad.md").write_text("no front matter\n")
        h.loop.tick()
        h.loop.tick()
        assert [t for t, _ in h.source.filed][:1] == ["audit: good"]  # type: ignore[attr-defined]
        assert sum("audit charter skipped" in c for c in front.seen) == 1


def test_charter_dataclass_title() -> None:
    assert Charter("n", 1.0, True, "b", "r").title == "audit: n"
