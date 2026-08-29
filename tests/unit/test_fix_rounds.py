"""Fix rounds: what one round is for, and why it is a single task.

A normal run decomposes an outcome and spends ~270 turns across ~5 phases per
task. A fix round addressing "mdformat failed" or "line 12 is off by one" is
already decomposed — the failures *are* the acceptance criteria — so asking an
agent to rediscover that structure costs a whole session for nothing.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from sbxloop.config import Config
from sbxloop.daemon.model import WorkItem
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


class TestFixConfigCarriesThePr:
    """A fix round knows which PR it is updating (#488): the daemon's stored
    PrState must reach delivery as config, not be rediscovered from a 422."""

    def _loop_and_item(self, tmp_path: Path) -> tuple[Any, WorkItem]:
        from sbxloop.daemon.loop import DaemonLoop
        from sbxloop.daemon.store import DaemonStore

        config = Config.model_validate({"state_dir": str(tmp_path / "state")})
        dstore = DaemonStore(config.state_dir / "state.db")
        loop = DaemonLoop(
            config,
            store=StateStore(config.state_dir / "state.db"),
            dstore=dstore,
            sources=[],
            runner=lambda item, cfg, run_id, bus, resume: RunResult(
                run_id=run_id, state="completed"
            ),
        )
        item = WorkItem(item_id="gh:1", source="github", source_key="1", title="t")
        dstore.upsert_new(item, now=1.0)
        dstore.record_delivery("gh:1", 42, "sbxloop/rabc", 1.0)
        dstore.queue_fix("gh:1", fix_brief(42, "1 check failed", ("lint",)), 2.0)
        return loop, item

    def test_fix_config_carries_branch_and_pr_number(self, tmp_path: Path) -> None:
        loop, item = self._loop_and_item(tmp_path)
        out = loop._fix_config(item, loop.config)
        assert out.sandbox.continue_branch == "sbxloop/rabc"
        assert out.sandbox.continue_pr == 42

    def test_no_pr_state_leaves_the_config_alone(self, tmp_path: Path) -> None:
        loop, _item = self._loop_and_item(tmp_path)
        other = WorkItem(item_id="gh:2", source="github", source_key="2", title="t")
        out = loop._fix_config(other, loop.config)
        assert out.sandbox.continue_pr is None and out.sandbox.continue_branch is None


class TestEngineForwardsThePrNumber:
    def test_deliver_gets_pr_number_from_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sbxloop.engine.engine as engine_mod
        from sbxloop.gh.ops import PrRef

        calls: list[dict[str, Any]] = []

        def fake_deliver(ops: Any, repo: str, **kwargs: Any) -> PrRef:
            calls.append(dict(kwargs))
            return PrRef(number=42, url="https://x/pull/42")

        monkeypatch.setattr(engine_mod, "deliver_workspace", fake_deliver)
        config = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "github": {"repo": "o/r", "deliver": True},
                "sandbox": {"continue_branch": "sbxloop/rabc", "continue_pr": 42},
            }
        )
        engine = LoopEngine(
            config,
            store=StateStore(tmp_path / "state" / "state.db"),
            bus=EventBus(),
            sbx=SbxCLI(binary=str(tmp_path / "no-such-sbx")),
            install_workers=False,
        )
        source = tmp_path / "state" / "runs" / "r1" / "artifacts"
        source.mkdir(parents=True)
        (source / "a.txt").write_text("hi")
        pair = SimpleNamespace(workspace=None, mounted=False)
        engine._deliver("r1", "ship it", cast(Any, pair), cast(Any, object()))

        assert calls and calls[0]["branch"] == "sbxloop/rabc"
        assert calls[0]["pr_number"] == 42
