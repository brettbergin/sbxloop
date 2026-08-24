"""Fix rounds: what one round is for, and why it is a single task.

A normal run decomposes an outcome and spends ~270 turns across ~5 phases per
task. A fix round addressing "mdformat failed" or "line 12 is off by one" is
already decomposed — the failures *are* the acceptance criteria — so asking an
agent to rediscover that structure spends a whole session for nothing.
"""

from __future__ import annotations

from pathlib import Path

from sbxloop.config import Config
from sbxloop.daemon.review import FIX_TASK_TITLE, fix_brief, fix_tasks
from sbxloop.engine.engine import LoopEngine
from sbxloop.engine.model import RunResult
from sbxloop.engine.store import StateStore
from sbxloop.events import EventBus
from sbxloop.sbx.cli import SbxCLI


class TestFixBrief:
    def test_it_names_the_failing_checks(self) -> None:
        brief = fix_brief(7, "1 of 3 check(s) failed: mdformat", ("mdformat",))
        assert "#7" in brief and "mdformat" in brief

    def test_it_says_not_to_redo_the_work(self) -> None:
        """The round runs on the PR's own branch, where the work already is.
        A brief that reads like a fresh task invites the executor to start
        over — which is how a small fix becomes a full investigation."""
        brief = fix_brief(7, "the review requested changes")
        assert "do not restructure" in brief.lower() or "not start over" in brief.lower()

    def test_it_points_at_the_review_comments(self) -> None:
        assert "review comments" in fix_brief(7, "the review requested changes")

    def test_no_failing_checks_means_no_checks_section(self) -> None:
        assert "Failing checks" not in fix_brief(7, "the review requested changes")

    def test_it_never_claims_gh_works(self) -> None:
        """The fix agent's sandbox has no GitHub credential (#437); a brief
        telling it to run `gh pr view` hands it a tool that cannot work."""
        assert "gh pr view" not in fix_brief(7, "the review requested changes")
        assert "gh pr view" not in fix_brief(7, "checks failed", ("lint",), objections="fix line 9")

    def test_objections_are_quoted_verbatim(self) -> None:
        brief = fix_brief(7, "the review requested changes", objections="- `a.py:9`: off by one")
        assert "- `a.py:9`: off by one" in brief
        assert "Address each one" in brief


class TestFixTasks:
    def test_a_round_is_exactly_one_task(self) -> None:
        tasks = fix_tasks(7, "1 check failed", ("lint",))
        assert len(tasks) == 1
        assert tasks[0].title == FIX_TASK_TITLE

    def test_the_failures_become_acceptance_criteria(self) -> None:
        tasks = fix_tasks(7, "2 checks failed", ("lint", "mdformat"))
        criteria = " ".join(tasks[0].acceptance_criteria)
        assert "lint" in criteria and "mdformat" in criteria

    def test_the_brief_rides_on_the_task_verbatim(self) -> None:
        """The dispatch site passes the persisted brief; re-wrapping it in
        fix_brief again nested one brief's boilerplate inside another's."""
        brief = fix_brief(7, "the review requested changes")
        tasks = fix_tasks(7, brief)
        assert tasks[0].description == brief


class TestSeededRunSkipsDecompose:
    """`start(tasks=...)` persists the graph before driving, which is what
    makes `_run_phases` find one already there and skip DECOMPOSE."""

    def _engine(self, tmp_path: Path) -> LoopEngine:
        config = Config.model_validate({"state_dir": str(tmp_path / "state")})
        engine = LoopEngine(
            config,
            store=StateStore(tmp_path / "state" / "state.db"),
            bus=EventBus(),
            # Never invoked: _drive is stubbed, so nothing provisions.
            sbx=SbxCLI(binary=str(tmp_path / "no-such-sbx")),
            install_workers=False,
        )
        engine._drive = lambda run_id, outcome, workspace=None: RunResult(  # type: ignore[method-assign]
            run_id=run_id, state="completed", tasks=[]
        )
        return engine

    def test_seeded_tasks_are_persisted(self, tmp_path: Path) -> None:
        engine = self._engine(tmp_path)
        engine.start("fix it", run_id="rfix00001", tasks=fix_tasks(7, "1 check failed", ("lint",)))
        stored = engine.store.get_tasks("rfix00001")
        assert [t.spec.id for t in stored] == ["fix"]
        assert stored[0].spec.title == FIX_TASK_TITLE

    def test_an_ordinary_run_seeds_nothing_and_still_decomposes(self, tmp_path: Path) -> None:
        engine = self._engine(tmp_path)
        engine.start("do the thing", run_id="rord00001")
        assert engine.store.get_tasks("rord00001") == []
