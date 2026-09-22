"""The wait between forge polls starts short and doubles while the same
thing is waited on, up to `[landing] ci_poll_interval_s`.

Field (db, 2026-09-19): a merge request whose CI went green in two seconds
spent three more minutes in 60s polls (a settle read, the undraft, the
mergeability read, the merge) before it merged. Each of those is answered
by the forge in seconds; the interval was the wait.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.engine.engine import LoopEngine, Pipeline
from sbxloop.engine.store import StateStore
from sbxloop.events import EventBus
from sbxloop.sbx.cli import SbxCLI


def engine(tmp_path: Path, **landing: Any) -> tuple[LoopEngine, Pipeline, list[float]]:
    config = Config.model_validate(
        {"home": str(tmp_path / "state"), "github": {"repo": "o/r"}, "landing": landing}
    )
    eng = LoopEngine(
        config,
        store=StateStore(tmp_path / "state" / "state.db"),
        bus=EventBus(),
        sbx=SbxCLI(binary=str(tmp_path / "no-such-sbx")),
        install_workers=False,
    )
    eng.store.create_run("rpoll0001", "ship it")
    waits: list[float] = []

    def wait(timeout: float | None = None) -> bool:
        waits.append(float(timeout or 0))
        return True

    eng._wake.wait = wait  # type: ignore[method-assign]
    eng._process_chat = lambda *args, **kwargs: None  # type: ignore[method-assign]
    pipeline = Pipeline(
        run_id="rpoll0001",
        outcome="ship it",
        pair=None,  # type: ignore[arg-type]
        phases=None,  # type: ignore[arg-type]
        granter=None,  # type: ignore[arg-type]
        deadline=float("inf"),
        ops=None,
        repo="o/r",
    )
    return eng, pipeline, waits


class TestAdaptivePoll:
    def test_the_wait_starts_short_and_doubles_to_the_interval(self, tmp_path: Path) -> None:
        eng, p, waits = engine(tmp_path, ci_poll_interval_s=60.0, ci_poll_min_s=10.0)
        try:
            for _ in range(5):
                eng._tick(p, "ci")
        finally:
            eng.store.close()
        assert waits == [10.0, 20.0, 40.0, 60.0, 60.0]

    def test_waiting_on_something_else_starts_over(self, tmp_path: Path) -> None:
        eng, p, waits = engine(tmp_path, ci_poll_interval_s=60.0, ci_poll_min_s=10.0)
        try:
            eng._tick(p, "ci")
            eng._tick(p, "ci")
            eng._tick(p, "undraft")
            eng._tick(p, "mergeability")
            eng._tick(p, "mergeability")
        finally:
            eng.store.close()
        assert waits == [10.0, 20.0, 10.0, 10.0, 20.0]

    def test_the_interval_is_still_the_ceiling(self, tmp_path: Path) -> None:
        eng, p, waits = engine(tmp_path, ci_poll_interval_s=15.0, ci_poll_min_s=10.0)
        try:
            eng._tick(p, "ci")
            eng._tick(p, "ci")
        finally:
            eng.store.close()
        assert waits == [10.0, 15.0]

    def test_the_first_wait_is_configurable(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            Config.model_validate({"home": str(tmp_path), "landing": {"ci_poll_min_s": 0}})
        eng, p, waits = engine(tmp_path, ci_poll_interval_s=60.0, ci_poll_min_s=5.0)
        try:
            eng._tick(p, "ci")
        finally:
            eng.store.close()
        assert waits == [5.0]
